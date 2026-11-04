"""Operações tipadas de recuperação: HTTP registra, agente executa."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from typing import Literal

from fastapi import Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from datetime import datetime

from .auth import csrf_guard, require_agent, require_viewer
from .db import session
from .models import Agent, Task, TaskRevision
from .task_contract import export_taskspec


class OperationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: uuid.UUID
    kind: Literal["snapshots", "restore"]
    destination_id: str = Field(min_length=1, max_length=200)
    revision: StrictInt | None = Field(default=None, gt=0)
    query_id: uuid.UUID | None = None
    snapshot_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def typed_intent(self):
        if self.kind == "snapshots":
            if self.revision is None or self.query_id is not None or self.snapshot_id is not None:
                raise ValueError("consulta exige revisão, sem ponto ou consulta anterior")
        elif self.query_id is None or self.snapshot_id is None or self.revision is not None:
            raise ValueError("restauração exige consulta e ponto, sem revisão livre")
        return self


class ClaimIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    claim_id: uuid.UUID


SafeError = Literal[
    "invalid_operation", "credential_missing", "repository_error", "snapshot_missing",
    "invalid_target", "maintenance", "timeout", "interrupted", "abandoned",
]


class ResultIn(ClaimIn):
    state: Literal["succeeded", "failed", "interrupted"]
    result: dict | None = None
    error: SafeError | None = None

    @field_validator("error", mode="before")
    @classmethod
    def safe_error_category(cls, value):
        if value not in (None, "invalid_operation", "credential_missing", "repository_error",
                         "snapshot_missing", "invalid_target", "maintenance", "timeout",
                         "interrupted", "abandoned"):
            raise HTTPException(422, "categoria de erro de recuperação inválida")
        return value

    @model_validator(mode="after")
    def terminal_payload(self):
        if self.state == "succeeded":
            if self.result is None or self.error is not None:
                raise ValueError("sucesso exige resultado sem erro")
        elif self.error is None:
            raise ValueError("falha/interrupção exige categoria de erro")
        return self


class SnapshotOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-f0-9]{64}$")
    time: str | None = Field(default=None, max_length=100)
    hostname: str | None = Field(default=None, max_length=200)
    tags: list[str] = Field(max_length=100)


class SnapshotResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    snapshots: list[SnapshotOut] = Field(max_length=1000)


class RestoreResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target: str = Field(min_length=1, max_length=4096)


TERMINAL = {"succeeded", "failed", "interrupted"}


def _out(row, *, agent=False):
    fields = (
        "id", "client_id", "agent_id", "task_id", "revision", "kind", "destination_id",
        "snapshot_id", "query_id", "requester_kind", "requester_ref", "state", "result",
        "error", "created_at", "started_at", "finished_at",
    )
    result = {key: str(row[key]) if isinstance(row[key], uuid.UUID) else row[key] for key in fields}
    if agent:
        result.update(config=row["config"], config_hash=row["config_hash"],
                      claim_id=str(row["claim_id"]) if row["claim_id"] else None,
                      contract_version=1)
    return result


def _same_intent(row, task_id, payload):
    return (row["task_id"] == task_id and row["kind"] == payload.kind
            and row["destination_id"] == payload.destination_id
            and row["query_id"] == payload.query_id and row["snapshot_id"] == payload.snapshot_id
            and (payload.kind == "restore" or row["revision"] == payload.revision))


def _result_document(row, payload):
    """Valida somente metadados tipados; nunca recebe erro Restic livre."""
    if payload.result is None:
        return None
    try:
        if row["kind"] == "snapshots":
            if payload.state != "succeeded":
                raise ValueError("consulta falha não deve conter pontos")
            result = SnapshotResult.model_validate(payload.result)
            ids = set()
            for snapshot in result.snapshots:
                if (f"task:{row['task_id']}" not in snapshot.tags
                        or any(len(tag) > 200 or re.search(r"[\x00-\x1f]", tag) for tag in snapshot.tags)
                        or any(re.search(r"[\x00-\x1f]", item) for item in (snapshot.time, snapshot.hostname) if item)
                        or snapshot.id in ids):
                    raise ValueError("ponto sem identidade autorizada ou duplicado")
                ids.add(snapshot.id)
        else:
            result = RestoreResult.model_validate(payload.result)
            if not result.target.startswith("/") or re.search(r"[\x00-\x1f]", result.target):
                raise ValueError("pasta efetiva inválida")
        return result.model_dump(exclude_none=True)
    except ValueError:
        # Não reproduzir valores recebidos do agente no erro da API.
        raise HTTPException(422, "resultado de recuperação incompatível") from None


def register_recovery(app, require_admin_identity):
    @app.post("/api/v1/tasks/{task_id}/recovery", status_code=202,
              dependencies=[Depends(csrf_guard)])
    def request_recovery(task_id: uuid.UUID, payload: OperationIn,
                         identity=Depends(require_admin_identity), db: Session = Depends(session)):
        task = db.execute(select(Task).where(Task.id == task_id).with_for_update()).scalar_one_or_none()
        if task is None:
            raise HTTPException(404, "tarefa inexistente")
        previous = db.execute(text("SELECT * FROM recovery_operations WHERE id=:id"),
                              {"id": payload.id}).mappings().first()
        if previous:
            if not _same_intent(previous, task_id, payload):
                raise HTTPException(409, "identificador já usado por outra solicitação")
            return _out(previous)
        if payload.kind == "restore":
            query = db.execute(text("SELECT * FROM recovery_operations WHERE id=:id"),
                               {"id": payload.query_id}).mappings().first()
            if (query is None or query["task_id"] != task_id or query["kind"] != "snapshots"
                    or query["destination_id"] != payload.destination_id or query["state"] != "succeeded"):
                raise HTTPException(422, "consulta de pontos não corresponde à solicitação")
            if payload.snapshot_id not in {item["id"] for item in query["result"]["snapshots"]}:
                raise HTTPException(422, "ponto não pertence à consulta autorizada")
            existing = db.execute(text(
                "SELECT * FROM recovery_operations WHERE query_id=:query AND snapshot_id=:snapshot"
            ), {"query": payload.query_id, "snapshot": payload.snapshot_id}).mappings().first()
            if existing:
                return _out(existing)
            revision, config, digest = query["revision"], query["config"], query["config_hash"]
        else:
            revision = payload.revision
            row = db.execute(select(TaskRevision).where(
                TaskRevision.task_id == task_id, TaskRevision.revision == revision
            )).scalar_one_or_none()
            if row is None:
                raise HTTPException(404, "revisão inexistente")
            config = export_taskspec(task_id=task_id, name=row.config["name"],
                client_id=task.agent.client_id, client_name=row.client_name or task.agent.client.name,
                agent_id=task.agent_id, spec=row.config["spec"])
            if payload.destination_id not in {item["id"] for item in config["destinations"]}:
                raise HTTPException(422, "destino não pertence à revisão")
            digest = hashlib.sha256(json.dumps(config, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        params = {"id": payload.id, "client": task.agent.client_id, "agent": task.agent_id,
                  "task": task_id, "revision": revision, "kind": payload.kind,
                  "destination": payload.destination_id, "snapshot": payload.snapshot_id,
                  "query": payload.query_id, "config": json.dumps(config), "hash": digest,
                  "requester_kind": identity.kind, "requester_ref": identity.ref}
        inserted = db.execute(text(
            "INSERT INTO recovery_operations (id,client_id,agent_id,task_id,revision,kind,"
            "destination_id,snapshot_id,query_id,config,config_hash,requester_kind,requester_ref,state) "
            "VALUES (:id,:client,:agent,:task,:revision,:kind,:destination,:snapshot,:query,"
            "CAST(:config AS jsonb),:hash,:requester_kind,:requester_ref,'pending') "
            "ON CONFLICT (id) DO NOTHING RETURNING *"
        ), params).mappings().first()
        if inserted is None:
            raise HTTPException(409, "identificador já usado por outra solicitação")
        result = _out(inserted)
        db.commit()
        return result

    @app.get("/api/v1/tasks/{task_id}/recovery", dependencies=[Depends(require_viewer)])
    def task_operations(task_id: uuid.UUID, db: Session = Depends(session)):
        if db.get(Task, task_id) is None:
            raise HTTPException(404, "tarefa inexistente")
        rows = db.execute(text(
            "SELECT * FROM recovery_operations WHERE task_id=:task ORDER BY created_at DESC,id DESC LIMIT 100"
        ), {"task": task_id}).mappings()
        return [_out(row) for row in rows]

    @app.get("/api/v1/agent/recovery")
    def agent_operations(agent: Agent = Depends(require_agent), db: Session = Depends(session),
                         after: uuid.UUID | None = Query(default=None),
                         limit: int = Query(default=100, ge=1, le=100)):
        # Paginação por cursor estável: ordenação, cursor e predicado de
        # continuação representam a MESMA ordem total imutável —
        # (created_at, id). Campos imutáveis garantem estabilidade quando o
        # estado muda entre páginas (pending→running não move a operação
        # na ordem, então não há omissão nem duplicação). O cursor é o
        # último id da página; a âncora é resolvida para (created_at, id).
        # A descoberta além de 100 operações é garantida pela paginação,
        # não pela prioridade de estado: mesmo que running ocupem as
        # primeiras páginas, as pendentes são alcançadas nas seguintes.
        # Isolamento por agente autenticado. Cursor de outro agente ou
        # inexistente = 404.
        params = {"agent": agent.id, "limit": limit}
        if after is not None:
            params["after"] = str(after)
            anchor = db.execute(text(
                "SELECT created_at, id FROM recovery_operations WHERE id=:after AND agent_id=:agent"
            ), {"after": str(after), "agent": agent.id}).mappings().first()
            if anchor is None:
                raise HTTPException(404, "cursor inexistente")
            params["anchor_created"] = anchor["created_at"]
            params["anchor_id"] = anchor["id"]
        rows = db.execute(text(
            "SELECT * FROM recovery_operations WHERE agent_id=:agent AND state IN ('pending','running') "
            + ("AND (created_at, id) > (:anchor_created, :anchor_id) " if after is not None else "")
            + "ORDER BY created_at, id LIMIT :limit"
        ), params).mappings()
        result = [_out(row, agent=True) for row in rows]
        next_cursor = result[-1]["id"] if len(result) == limit else None
        return {"operations": result, "next_cursor": next_cursor}

    def locked_operation(db, operation_id, agent_id):
        row = db.execute(text(
            "SELECT * FROM recovery_operations WHERE id=:id AND agent_id=:agent FOR UPDATE"
        ), {"id": operation_id, "agent": agent_id}).mappings().first()
        if row is None:
            raise HTTPException(404, "operação inexistente")
        return row

    @app.post("/api/v1/agent/recovery/{operation_id}/claim")
    def claim(operation_id: uuid.UUID, payload: ClaimIn,
              agent: Agent = Depends(require_agent), db: Session = Depends(session)):
        row = locked_operation(db, operation_id, agent.id)
        if row["state"] == "pending":
            db.execute(text("UPDATE recovery_operations SET state='running',claim_id=:claim,"
                            "started_at=now() WHERE id=:id"),
                       {"claim": payload.claim_id, "id": operation_id})
        elif row["claim_id"] != payload.claim_id:
            raise HTTPException(409, "operação já reivindicada")
        state = "running" if row["state"] == "pending" else row["state"]
        db.commit()
        return {"id": str(operation_id), "state": state, "claim_id": str(payload.claim_id)}

    @app.post("/api/v1/agent/recovery/{operation_id}/result")
    def operation_result(operation_id: uuid.UUID, payload: ResultIn,
                         agent: Agent = Depends(require_agent), db: Session = Depends(session)):
        row = locked_operation(db, operation_id, agent.id)
        if row["claim_id"] != payload.claim_id:
            raise HTTPException(409, "reivindicação não corresponde à operação")
        result = _result_document(row, payload)
        if row["state"] in TERMINAL:
            if (row["state"] != payload.state or row["result"] != result or row["error"] != payload.error):
                raise HTTPException(409, "resultado terminal já registrado")
        elif row["state"] != "running":
            raise HTTPException(409, "operação não iniciada")
        else:
            db.execute(text("UPDATE recovery_operations SET state=:state,result=CAST(:result AS jsonb),"
                            "error=:error,finished_at=now() WHERE id=:id"),
                       {"state": payload.state, "result": json.dumps(result) if result is not None else None,
                        "error": payload.error, "id": operation_id})
        db.commit()
        return {"id": str(operation_id), "state": payload.state}
