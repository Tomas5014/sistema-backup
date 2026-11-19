"""Telemetria mínima Windows e ocorrências duráveis. Uso exclusivo de laboratório."""
from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
import hashlib
import hmac
import json
import os
import smtplib
import uuid
from email.message import EmailMessage
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import Depends, HTTPException, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, model_validator, field_validator
from sqlalchemy import text, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from .auth import csrf_guard, require_admin, require_viewer, token_hash
from .db import session, engine
from .models import Agent, AgentCredential, InstallationPreparation, Execution, DestinationResultRow
from .monitoring import next_occurrence, previous_occurrence
from .protection_routes import current_protection, public_protection, source_fingerprint


def lab():
    if os.environ.get("MACIP_API_LAB_ALERTS") != "1":
        raise HTTPException(404, "alertas de laboratório desabilitados")


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Enrollment(Strict):
    activation_id: uuid.UUID
    resume_secret: str = Field(min_length=43, max_length=128)


class Schedule(Strict):
    kind: Literal["daily"] = "daily"
    timezone: str = Field(max_length=100)
    hour: int = Field(ge=0, le=23, strict=True)
    minute: int = Field(ge=0, le=59, strict=True)
    tolerance_seconds: int = Field(default=1800, ge=60, le=86400, strict=True)
    copy_seconds: int = Field(default=86400, ge=60, le=604800, strict=True)

    @field_validator("timezone")
    @classmethod
    def zone(cls, value):
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("fuso IANA não reconhecido") from None
        return value


class ScheduleIn(Strict):
    previous_schedule_id: uuid.UUID | None = None
    schedule_id: uuid.UUID
    schedule: Schedule
    paused: bool = Field(strict=True)


class ResultIn(Strict):
    execution_id: uuid.UUID
    schedule_id: uuid.UUID | None = None
    trigger: Literal["scheduled", "manual"]
    occurrence: datetime | None = None
    started_at: datetime
    finished_at: datetime
    status: Literal["success", "partial", "failed", "interrupted"]
    result_code: Literal["ok", "restic_failed", "incomplete", "interrupted", "validation_failed"]
    repository_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    snapshot_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def coherent(self):
        for value in (self.started_at, self.finished_at, self.occurrence):
            if value is not None and value.tzinfo is None:
                raise ValueError("horário exige fuso explícito")
        if self.finished_at < self.started_at:
            raise ValueError("fim anterior ao início")
        if self.trigger == "scheduled" and (not self.schedule_id or self.occurrence is None):
            raise ValueError("execução agendada exige agenda e ocorrência")
        if self.trigger == "manual" and self.occurrence is not None:
            raise ValueError("execução manual não confirma ocorrência")
        if self.status == "success" and (not self.snapshot_id or self.result_code != "ok"):
            raise ValueError("sucesso exige snapshot e código ok")
        if self.status != "success" and self.result_code == "ok":
            raise ValueError("falha exige motivo")
        return self


_bearer = HTTPBearer(auto_error=False)


def result_agent(request: Request, db: Session = Depends(session),
                 credential: HTTPAuthorizationCredentials | None = Depends(_bearer)):
    if request.url.scheme != "https":
        raise HTTPException(400, "resultados exigem HTTPS")
    if credential is None:
        raise HTTPException(401, "credencial da instalação ausente")
    row = db.scalar(select(AgentCredential).where(AgentCredential.token_hash == token_hash(credential.credentials)))
    if not row or row.revoked_at or row.scope != "windows-results":
        raise HTTPException(403, "credencial da instalação inválida")
    agent = db.get(Agent, row.agent_id)
    agent.last_seen_at = datetime.now(UTC)
    db.commit()
    return agent


def register_alerts(app):
    gate = [Depends(lab)]

    @app.post("/api/v1/activations/monitoring", dependencies=gate)
    def enroll(payload: Enrollment, request: Request, response: Response, db: Session = Depends(session)):
        if request.url.scheme != "https":
            raise HTTPException(400, "monitoramento exige HTTPS")
        row = db.execute(text("SELECT * FROM activation_codes WHERE activation_id=:id FOR UPDATE"),
                         {"id": payload.activation_id}).mappings().one_or_none()
        if (not row or row["revoked_at"] or row["state"] != "delivered" or
            not hmac.compare_digest(row["resume_hash"], token_hash(payload.resume_secret)) or
            row["used_at"] + timedelta(hours=24) <= datetime.now(UTC)):
            raise HTTPException(403, "ativação não autorizada")
        preparation = db.get(InstallationPreparation, row["agent_id"])
        if not public_protection(preparation)[2]:
            raise HTTPException(409, "prova vigente de B pendente")
        # A retomada recebe exatamente a mesma credencial. Somente seu hash é persistido.
        token = hmac.new(payload.resume_secret.encode(),
                         ("windows-results-v1:" + str(payload.activation_id)).encode(), hashlib.sha256).hexdigest()
        credential_id = uuid.uuid5(payload.activation_id, "windows-results-v1")
        db.execute(insert(AgentCredential).values(id=credential_id, agent_id=row["agent_id"],
                   token_hash=token_hash(token), label="Windows laboratório · resultados", scope="windows-results")
                   .on_conflict_do_nothing(index_elements=["id"]))
        credential = db.get(AgentCredential, credential_id)
        if credential.revoked_at:
            raise HTTPException(403, "credencial de monitoramento revogada")
        db.commit()
        response.headers["Cache-Control"] = "no-store"
        return {"contract_version": 1, "installation_id": str(row["agent_id"]), "token": token}

    @app.post("/api/v1/windows-results/schedule", dependencies=gate)
    def schedule(payload: ScheduleIn, agent=Depends(result_agent), db: Session = Depends(session)):
        # Identidade é sempre a do Bearer; não há agent_id livre no contrato.
        db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:id))"), {"id": str(agent.id)})
        old = db.execute(text("SELECT * FROM windows_monitors WHERE agent_id=:id FOR UPDATE"),
                         {"id": agent.id}).mappings().one_or_none()
        values = payload.model_dump(mode="json")
        if old and str(old["schedule_id"]) == values["schedule_id"]:
            if old["schedule"] != values["schedule"] or old["paused"] != payload.paused:
                raise HTTPException(409, "identidade de agenda já usada com outro conteúdo")
        else:
            if (str(old["schedule_id"]) if old else None) != (str(payload.previous_schedule_id) if payload.previous_schedule_id else None):
                raise HTTPException(409, "agenda mudou; atualização atrasada recusada")
            db.execute(text("INSERT INTO windows_monitors(agent_id,schedule_id,schedule,paused) "
                "VALUES(:id,:sid,CAST(:schedule AS jsonb),:paused) ON CONFLICT(agent_id) DO UPDATE SET "
                "schedule_id=excluded.schedule_id,schedule=excluded.schedule,paused=excluded.paused,effective_at=now()"),
                {"id": agent.id, "sid": payload.schedule_id, "schedule": json.dumps(values["schedule"]), "paused": payload.paused})
        db.commit()
        return {"accepted": True}

    @app.post("/api/v1/windows-results/executions", dependencies=gate)
    def receive(payload: ResultIn, agent=Depends(result_agent), db: Session = Depends(session)):
        if payload.finished_at > datetime.now(UTC) + timedelta(seconds=120):
            raise HTTPException(422, "relógio da execução no futuro")
        preparation = db.get(InstallationPreparation, agent.id)
        if not preparation or preparation.repository_id != payload.repository_id:
            raise HTTPException(409, "repositório divergente")
        data = payload.model_dump(mode="json")
        encoded = json.dumps(data, sort_keys=True, separators=(",", ":"))
        digest = token_hash(encoded)
        db.execute(text("INSERT INTO windows_results(agent_id,execution_id,started_at,finished_at,payload,payload_hash) "
            "VALUES(:id,:exec,:start,:end,CAST(:payload AS jsonb),:hash) ON CONFLICT DO NOTHING"),
            dict(id=agent.id, exec=payload.execution_id, start=payload.started_at, end=payload.finished_at, payload=encoded, hash=digest))
        stored = db.scalar(text("SELECT payload_hash FROM windows_results WHERE agent_id=:id AND execution_id=:exec"),
                           {"id": agent.id, "exec": payload.execution_id})
        if stored != digest:
            raise HTTPException(409, "execução já registrada com outro conteúdo")
        # Projeta no histórico existente, na MESMA transação e com a mesma identidade.
        execution_row = uuid.uuid5(agent.id, "windows:" + str(payload.execution_id))
        created = db.execute(insert(Execution).values(
            id=execution_row, agent_id=agent.id, execution_id=str(payload.execution_id),
            task_id="windows:" + str(agent.id), status=payload.status,
            started_at_utc=payload.started_at, finished_at_utc=payload.finished_at,
            payload_hash=digest,
        ).on_conflict_do_nothing(index_elements=["agent_id", "execution_id"]).returning(Execution.id)).scalar()
        existing_hash = db.scalar(select(Execution.payload_hash).where(
            Execution.agent_id == agent.id, Execution.execution_id == str(payload.execution_id)))
        if existing_hash != digest:
            raise HTTPException(409, "execução já pertence a outro contrato")
        if created:
            db.add(DestinationResultRow(execution_row_id=created, destination_id="primary-a",
                status=payload.status, started_at_utc=payload.started_at, finished_at_utc=payload.finished_at,
                snapshot_id=payload.snapshot_id,
                error=payload.result_code if payload.status != "success" else None))
        from .lab_automation_routes import enabled, enqueue
        if enabled():
            # O INSERT do resultado e a intenção de copiar são uma transação.
            enqueue(db, preparation)
        db.commit()
        return {"accepted": True, "execution_id": str(payload.execution_id)}

    @app.get("/api/v1/installations/{agent_id}/alerts", dependencies=[*gate, Depends(require_viewer)])
    def overview(agent_id: uuid.UUID, db: Session = Depends(session)):
        if not db.get(Agent, agent_id):
            raise HTTPException(404, "instalação inexistente")
        return status(db, agent_id, datetime.now(UTC))

    @app.post("/api/v1/alerts/{alert_id}/ack", dependencies=[*gate, Depends(require_admin), Depends(csrf_guard)])
    def acknowledge(alert_id: uuid.UUID, request: Request, db: Session = Depends(session)):
        from .auth import current_operator
        operator = current_operator(request, db)
        row = db.execute(text("UPDATE central_alerts SET acknowledged_at=coalesce(acknowledged_at,now()), "
            "acknowledged_by=coalesce(acknowledged_by,:who) WHERE id=:id RETURNING id"),
            {"id": alert_id, "who": str(operator.id) if operator else "admin-automation"}).first()
        if not row:
            raise HTTPException(404, "alerta inexistente")
        db.commit()
        return {"acknowledged": True}


def instant(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def records(db, agent_id):
    return list(db.execute(text("SELECT * FROM windows_results WHERE agent_id=:id ORDER BY started_at DESC,execution_id"),
                           {"id": agent_id}).mappings())


def monitor(db, agent_id):
    return db.execute(text("SELECT * FROM windows_monitors WHERE agent_id=:id"), {"id": agent_id}).mappings().one_or_none()


def status(db, agent_id, now):
    setting = monitor(db, agent_id)
    events = records(db, agent_id)
    prep = db.get(InstallationPreparation, agent_id)
    binding = current_protection(prep) if prep else None
    points = list(db.execute(text("SELECT * FROM protected_points WHERE agent_id=:id AND binding_id=:binding ORDER BY COALESCE((SELECT max(w.started_at) FROM windows_results w WHERE w.agent_id=protected_points.agent_id AND w.payload->>'snapshot_id'=protected_points.snapshot_a), confirmed_at) DESC"),
                            {"id": agent_id, "binding": (binding or {}).get("binding_id")}).mappings())
    deadline = None
    if setting and not setting["paused"]:
        due = previous_occurrence(setting["schedule"], now)
        following = next_occurrence(setting["schedule"], due) if due else None
        covered = {e["payload"].get("occurrence") for e in events if e["payload"]["trigger"] == "scheduled"
                   and e["payload"].get("schedule_id") == str(setting["schedule_id"])
                   and due and due <= e["started_at"] < following}
        if due and due > setting["effective_at"] and not any(instant(c) == due for c in covered if c):
            deadline = due + timedelta(seconds=setting["schedule"]["tolerance_seconds"])
        else:
            deadline = next_occurrence(setting["schedule"], now) + timedelta(seconds=setting["schedule"]["tolerance_seconds"])
    alerts = [dict(r) for r in db.execute(text("SELECT * FROM central_alerts WHERE agent_id=:id ORDER BY created_at DESC,id"), {"id": agent_id}).mappings()]
    return {"configured": bool(setting), "paused": bool(setting and setting["paused"]),
            "last_backup": events[0]["payload"] if events else None,
            "last_protected_copy": dict(points[0]) if points else None,
            "next_deadline": deadline, "alerts": alerts}


def evaluate(db: Session, now: datetime):
    """Relógio central. Materializa cada ocorrência uma vez, inclusive sem agente ligado."""
    db.execute(text("SELECT pg_advisory_xact_lock(727203013)"))
    for prep in db.query(InstallationPreparation).all():
        events = records(db, prep.agent_id)
        setting = monitor(db, prep.agent_id)
        wanted = {}
        newest_success = next((e for e in events if e["payload"]["status"] == "success"), None)
        for event in events:
            p = event["payload"]
            if p["status"] != "success":
                # Registra também falhas antigas recebidas após um sucesso, já resolvidas.
                detail = {"status": p["status"], "result_code": p["result_code"], "execution_id": p["execution_id"]}
                db.execute(text("INSERT INTO central_alerts(id,agent_id,kind,occurrence_key,detail,created_at,resolved_at) VALUES(:uuid,:agent,'backup_failed',:key,CAST(:detail AS jsonb),:now,:resolved) ON CONFLICT DO NOTHING"),
                    dict(uuid=uuid.uuid4(), agent=prep.agent_id, key=str(event["execution_id"]), detail=json.dumps(detail), now=now,
                         resolved=now if newest_success and event["started_at"] < newest_success["started_at"] else None))
            if p["status"] != "success" and (not newest_success or event["started_at"] >= newest_success["started_at"]):
                wanted[("backup_failed", str(event["execution_id"]))] = {"status": p["status"], "result_code": p["result_code"], "execution_id": p["execution_id"]}
        if setting and not setting["paused"]:
            schedule = setting["schedule"]
            due = next_occurrence(schedule, setting["effective_at"])
            # Limite explícito: agendas anteriores a 400 dias exigem intervenção.
            if now - setting["effective_at"] > timedelta(days=400):
                raise RuntimeError("agenda de laboratório supera 400 dias; revisar antes de avaliar")
            while due and due + timedelta(seconds=schedule["tolerance_seconds"]) < now:
                following = next_occurrence(schedule, due)
                found = any(e["payload"]["trigger"] == "scheduled"
                    and e["payload"].get("schedule_id") == str(setting["schedule_id"])
                    and e["payload"].get("occurrence") and instant(e["payload"]["occurrence"]) == due
                    and due <= e["started_at"] < following for e in events)
                if not found:
                    wanted[("backup_missing", str(setting["schedule_id"]) + ":" + due.isoformat())] = {"occurrence": due.isoformat(), "deadline": (due + timedelta(seconds=schedule["tolerance_seconds"])).isoformat()}
                due = following
        binding = current_protection(prep)
        binding_ref = (binding or {}).get("binding_id")
        copied = set()
        if binding_ref:
            copied = {r[0] for r in db.execute(text("SELECT snapshot_a FROM protected_points WHERE agent_id=:id AND binding_id=:binding"),
                      {"id": prep.agent_id, "binding": binding_ref})}
        elif prep.profile and prep.repository_id:
            binding_ref = "unbound:" + source_fingerprint(prep)
        if binding_ref:
            for event in events:
                p = event["payload"]
                delay = setting["schedule"]["copy_seconds"] if setting else 86400
                if (p.get("snapshot_id") and p["repository_id"] == prep.repository_id
                    and event["received_at"] + timedelta(seconds=delay) < now and p["snapshot_id"] not in copied):
                    wanted[("copy_late", binding_ref + ":" + p["snapshot_id"])] = {"snapshot_a": p["snapshot_id"], "binding_id": binding_ref, "deadline": (event["received_at"] + timedelta(seconds=delay)).isoformat()}
        for (kind, key), detail in wanted.items():
            db.execute(text("INSERT INTO central_alerts(id,agent_id,kind,occurrence_key,detail,created_at) VALUES(:uuid,:agent,:kind,:key,CAST(:detail AS jsonb),:now) ON CONFLICT DO NOTHING"),
                       dict(uuid=uuid.uuid4(), agent=prep.agent_id, kind=kind, key=key, detail=json.dumps(detail), now=now))
        for row in db.execute(text("SELECT id,kind,occurrence_key FROM central_alerts WHERE agent_id=:id AND resolved_at IS NULL"), {"id": prep.agent_id}).mappings():
            if (row["kind"], row["occurrence_key"]) not in wanted:
                db.execute(text("UPDATE central_alerts SET resolved_at=:now WHERE id=:id"), {"id": row["id"], "now": now})
    db.commit()


LABELS = {"backup_failed": "Backup falhou", "backup_missing": "Backup não confirmado no prazo", "copy_late": "Cópia protegida atrasada"}


def deliver(db, port: int):
    """SMTP exclusivamente loopback para capturador descartável; sem destinatário configurável."""
    for candidate in db.execute(text("SELECT id FROM central_alerts WHERE delivery_state='pending' AND resolved_at IS NULL ORDER BY created_at")).all():
        row = db.execute(text("SELECT * FROM central_alerts WHERE id=:id FOR UPDATE SKIP LOCKED"), {"id": candidate.id}).mappings().one_or_none()
        if not row or row["delivery_state"] != "pending":
            continue
        db.execute(text("UPDATE central_alerts SET delivery_state='sending' WHERE id=:id"), {"id": row["id"]})
        db.commit()  # após crash, 'sending' é ambíguo e NÃO é reenviado automaticamente
        message = EmailMessage()
        message["From"] = "macip-lab@example.invalid"
        message["To"] = "operator-lab@example.invalid"
        message["Subject"] = LABELS[row["kind"]]
        message["Message-ID"] = f'<{row["id"]}@macip-lab.invalid>'
        message.set_content(f'Laboratório MACIP. Instalação {row["agent_id"]}.\nOcorrência {row["id"]}.\n' + json.dumps(row["detail"], ensure_ascii=False))
        try:
            with smtplib.SMTP("127.0.0.1", port, timeout=5) as smtp:
                smtp.send_message(message)
        except (ConnectionRefusedError, smtplib.SMTPConnectError):
            state = "pending"
        except (OSError, smtplib.SMTPException):
            state = "uncertain"
        else:
            state = "sent"
        db.execute(text("UPDATE central_alerts SET delivery_state=:state,delivered_at=CASE WHEN :sent THEN now() ELSE NULL END WHERE id=:id"), {"state": state, "sent": state == "sent", "id": row["id"]})
        db.commit()


def main():
    parser = argparse.ArgumentParser(description="Uma avaliação de alertas + envio ao capturador SMTP local de laboratório")
    parser.add_argument("--smtp-port", type=int, required=True)
    args = parser.parse_args()
    lab()
    if not 1024 <= args.smtp_port <= 65535:
        parser.error("porta descartável deve ser 1024..65535")
    from .db import migrate_now
    migrate_now()
    with Session(engine()) as db:
        evaluate(db, datetime.now(UTC))
        deliver(db, args.smtp_port)


if __name__ == "__main__":
    main()
