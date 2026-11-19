"""Preparação assíncrona do servidor primário A.

O navegador registra intenção tipada. Um executor dedicado reconcilia A.
Nenhuma rota aceita comando, caminho administrativo ou segredo.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import Depends, HTTPException, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.orm import Session

from . import config
from .auth import csrf_guard, require_admin, require_viewer
from .db import session
from .models import Agent, Client, InstallationPreparation, ProvisioningOperation
from .protection_routes import public_protection, register_protection, source_fingerprint
from .provisioning_sftp import matches_endpoint

_bearer = HTTPBearer(auto_error=False)
_IDENTIFIER = r"^[a-z0-9][a-z0-9._-]{2,79}$"
_TIME = r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$"
_REPOSITORY_ID = r"^[a-f0-9]{64}$"
_ACCOUNT = r"^macip[a-f0-9]{20}$"
_CLAIM_DURATION = timedelta(minutes=5)
_SAFE_ERRORS = {
    "access_denied",
    "secret_missing",
    "repository_mismatch",
    "key_mismatch",
    "invalid_state",
    "repository_error",
}


class PrepareIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    client_name: str = Field(min_length=1, max_length=200)
    installation_identifier: str = Field(pattern=_IDENTIFIER)
    machine_name: str = Field(min_length=1, max_length=200)
    suggested_time: str = Field(pattern=_TIME)

    @field_validator("client_name", "machine_name")
    @classmethod
    def clean_text(cls, value: str) -> str:
        value = value.strip()
        if not value or re.search(r"[\x00-\x1f]", value):
            raise ValueError("texto vazio ou incompatível")
        return value


class RetryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: uuid.UUID


class ClaimIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    executor_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]{2,99}$")


class WindowsProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nome: str = Field(min_length=1, max_length=200)
    repositorio: str = Field(min_length=1, max_length=500)
    repositorio_id: str = Field(pattern=_REPOSITORY_ID)
    known_hosts: str = Field(min_length=1, max_length=1000)
    horario_sugerido: str = Field(pattern=_TIME)

    @model_validator(mode="after")
    def safe_public_profile(self):
        lowered = json.dumps(self.model_dump(), ensure_ascii=False).lower()
        if any(word in lowered for word in ("private_key", "password", "senha", "token")):
            raise ValueError("perfil contém campo incompatível")
        if "\n" in self.known_hosts or "\r" in self.known_hosts:
            raise ValueError("known_hosts deve conter uma linha")
        return self


class LabSeed(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repository_id: str = Field(pattern=_REPOSITORY_ID)
    snapshot_a: str = Field(pattern=_REPOSITORY_ID)
    manifest_sha256: str = Field(pattern=_REPOSITORY_ID)


class SuccessResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lab_seed: LabSeed | None = None
    account_name: str = Field(pattern=_ACCOUNT)
    repository_id: str = Field(pattern=_REPOSITORY_ID)
    public_key_fingerprint: str = Field(
        pattern=r"^SHA256:[A-Za-z0-9+/]{20,100}$"
    )
    profile: WindowsProfile


class ResultIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim_id: uuid.UUID
    state: Literal["succeeded", "failed"]
    result: SuccessResult | None = None
    error_code: str | None = None

    @field_validator("error_code", mode="before")
    @classmethod
    def safe_error(cls, value):
        if value not in {None, *_SAFE_ERRORS}:
            raise HTTPException(422, "categoria de erro de preparação inválida")
        return value

    @model_validator(mode="after")
    def typed_result(self):
        if self.state == "succeeded" and (self.result is None or self.error_code):
            raise ValueError("sucesso exige resultado sem erro")
        if self.state == "failed" and (self.result is not None or not self.error_code):
            raise ValueError("falha exige categoria sem resultado")
        return self


def account_name(client_id: uuid.UUID, installation_identifier: str) -> str:
    material = f"{client_id}:{installation_identifier}".encode()
    return "macip" + hashlib.sha256(material).hexdigest()[:20]


def require_provisioner(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> None:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(401, "credencial do executor ausente")
    if not hmac.compare_digest(
        credentials.credentials.encode(), config.provisioner_token().encode()
    ):
        raise HTTPException(403, "credencial do executor inválida")


def _operation(row: ProvisioningOperation) -> dict:
    return {
        "id": str(row.id),
        "kind": row.kind,
        "attempt": row.attempt,
        "state": row.state,
        "step": row.step,
        "error_code": row.error_code,
        "created_at": row.created_at.isoformat(),
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
    }


def _profile_is_current(row: InstallationPreparation) -> bool:
    if not isinstance(row.profile, dict) or not row.account_name:
        return False
    repository = row.profile.get("repositorio")
    known_hosts = row.profile.get("known_hosts")
    return (
        isinstance(repository, str)
        and isinstance(known_hosts, str)
        and row.profile.get("repositorio_id") == row.repository_id
        and matches_endpoint(row.account_name, repository, known_hosts)
    )


def _preparation(db: Session, row: InstallationPreparation) -> dict:
    agent = db.get(Agent, row.agent_id)
    operation = db.get(ProvisioningOperation, row.current_operation_id)
    protection_state, last_copy, profile_available = public_protection(row)
    return {
        "client_id": str(row.client_id),
        "installation": {
            "id": str(agent.id),
            "identifier": agent.installation_identifier,
            "machine_name": agent.name,
            "platform": agent.platform,
            "suggested_time": agent.suggested_time,
        },
        "state": row.state,
        "step": row.step,
        "protection_state": protection_state,
        "last_protected_copy": last_copy,
        "repository_id": row.repository_id,
        "error_code": row.error_code,
        "profile_available": profile_available and _profile_is_current(row),
        "activation_available": profile_available and _profile_is_current(row)
            and os.environ.get("MACIP_API_LAB_ACTIVATION") == "1",
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
        "operation": _operation(operation),
        **_automation_summary(db, row),
    }


def _automation_summary(db, row):
    from .lab_automation_routes import enabled, summary
    return {"automation": summary(db, row)} if enabled() else {}


def _get_preparation(db: Session, agent_id: uuid.UUID, *, lock=False):
    query = select(InstallationPreparation).where(
        InstallationPreparation.agent_id == agent_id
    )
    if lock:
        query = query.with_for_update()
    row = db.execute(query).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "preparação inexistente")
    return row


def _new_operation(
    db: Session,
    *,
    operation_id: uuid.UUID,
    agent: Agent,
    kind: str,
    attempt: int,
    identity,
) -> ProvisioningOperation:
    operation = ProvisioningOperation(
        id=operation_id,
        client_id=agent.client_id,
        agent_id=agent.id,
        kind=kind,
        attempt=attempt,
        state="pending",
        step="queued",
        requester_kind=identity.kind,
        requester_ref=identity.ref,
    )
    db.add(operation)
    db.flush()
    return operation


def _validate_success(db: Session, operation: ProvisioningOperation, result: SuccessResult):
    agent = db.get(Agent, operation.agent_id)
    client = db.get(Client, operation.client_id)
    expected_account = account_name(client.id, agent.installation_identifier)
    profile = result.profile
    if result.account_name != expected_account:
        raise HTTPException(422, "resultado de preparação incompatível")
    if result.repository_id != profile.repositorio_id:
        raise HTTPException(422, "resultado de preparação incompatível")
    if profile.nome != client.name or profile.horario_sugerido != agent.suggested_time:
        raise HTTPException(422, "resultado de preparação incompatível")
    if not matches_endpoint(expected_account, profile.repositorio, profile.known_hosts):
        raise HTTPException(422, "resultado de preparação incompatível")


def register_provisioning(app, require_admin_identity):
    from .activation_routes import register_activation
    register_activation(app, require_admin_identity, require_provisioner)
    register_protection(app, _get_preparation, _profile_is_current)
    from .lab_automation_routes import register_automation, enabled
    register_automation(app, require_provisioner)
    from .storage_routes import register_storage
    register_storage(app, require_admin_identity, require_provisioner)

    @app.post(
        "/api/v1/clients/prepare",
        status_code=202,
        dependencies=[Depends(csrf_guard)],
    )
    def prepare_client(
        payload: PrepareIn,
        identity=Depends(require_admin_identity),
        db: Session = Depends(session),
    ):
        existing_operation = db.get(ProvisioningOperation, payload.id)
        if existing_operation is not None:
            agent = db.get(Agent, existing_operation.agent_id)
            client = db.get(Client, existing_operation.client_id)
            if (
                client.name != payload.client_name
                or agent.installation_identifier != payload.installation_identifier
                or agent.name != payload.machine_name
                or agent.suggested_time != payload.suggested_time
            ):
                raise HTTPException(409, "identificador já usado em outra preparação")
            return _preparation(db, _get_preparation(db, agent.id))

        client_id = db.execute(
            text(
                "INSERT INTO clients(id,name) VALUES (:id,:name) "
                "ON CONFLICT(name) DO UPDATE SET name=EXCLUDED.name RETURNING id"
            ),
            {"id": uuid.uuid4(), "name": payload.client_name},
        ).scalar_one()
        client = db.execute(
            select(Client).where(Client.id == client_id).with_for_update()
        ).scalar_one()
        agent_id = db.execute(
            text(
                "INSERT INTO agents(id,client_id,name,installation_identifier,platform,suggested_time) "
                "VALUES (:id,:client,:name,:identifier,'windows',:time) "
                "ON CONFLICT(client_id,installation_identifier) "
                "WHERE installation_identifier IS NOT NULL DO NOTHING RETURNING id"
            ),
            {
                "id": uuid.uuid4(),
                "client": client.id,
                "name": payload.machine_name,
                "identifier": payload.installation_identifier,
                "time": payload.suggested_time,
            },
        ).scalar_one_or_none()
        if agent_id is None:
            agent = db.execute(
                select(Agent)
                .where(
                    Agent.client_id == client.id,
                    Agent.installation_identifier == payload.installation_identifier,
                )
                .with_for_update()
            ).scalar_one()
            if agent.name != payload.machine_name or agent.suggested_time != payload.suggested_time:
                raise HTTPException(409, "instalação já existe com outros dados")
        else:
            agent = db.get(Agent, agent_id)

        preparation = db.get(InstallationPreparation, agent.id)
        if preparation is not None:
            db.commit()
            return _preparation(db, preparation)

        operation = _new_operation(
            db,
            operation_id=payload.id,
            agent=agent,
            kind="prepare_primary",
            attempt=1,
            identity=identity,
        )
        preparation = InstallationPreparation(
            agent_id=agent.id,
            client_id=client.id,
            current_operation_id=operation.id,
            state="pending",
            step="queued",
            protection_state="pending",
        )
        db.add(preparation)
        db.commit()
        db.refresh(preparation)
        return _preparation(db, preparation)

    @app.get(
        "/api/v1/clients/{client_id}/preparations",
        dependencies=[Depends(require_viewer)],
    )
    def list_preparations(client_id: uuid.UUID, db: Session = Depends(session)):
        if db.get(Client, client_id) is None:
            raise HTTPException(404, "cliente inexistente")
        rows = db.execute(
            select(InstallationPreparation)
            .where(InstallationPreparation.client_id == client_id)
            .order_by(InstallationPreparation.created_at)
        ).scalars()
        return [_preparation(db, row) for row in rows]

    @app.get(
        "/api/v1/installations/{agent_id}/preparation",
        dependencies=[Depends(require_viewer)],
    )
    def get_preparation(agent_id: uuid.UUID, db: Session = Depends(session)):
        return _preparation(db, _get_preparation(db, agent_id))

    @app.post(
        "/api/v1/installations/{agent_id}/preparation/retry",
        status_code=202,
        dependencies=[Depends(csrf_guard)],
    )
    def retry_preparation(
        agent_id: uuid.UUID,
        payload: RetryIn,
        identity=Depends(require_admin_identity),
        db: Session = Depends(session),
    ):
        old = db.get(ProvisioningOperation, payload.id)
        if old is not None:
            if old.agent_id != agent_id:
                raise HTTPException(409, "identificador já usado em outra preparação")
            return _preparation(db, _get_preparation(db, agent_id))
        preparation = _get_preparation(db, agent_id, lock=True)
        if preparation.state in {"pending", "running", "primary_ready"}:
            return _preparation(db, preparation)
        attempt = db.execute(
            select(func.max(ProvisioningOperation.attempt)).where(
                ProvisioningOperation.agent_id == agent_id
            )
        ).scalar_one() or 0
        operation = _new_operation(
            db,
            operation_id=payload.id,
            agent=db.get(Agent, agent_id),
            kind="retry_primary",
            attempt=attempt + 1,
            identity=identity,
        )
        preparation.current_operation_id = operation.id
        preparation.state = "pending"
        preparation.step = "queued"
        preparation.error_code = None
        preparation.updated_at = datetime.now(UTC)
        db.commit()
        return _preparation(db, preparation)

    @app.get(
        "/api/v1/installations/{agent_id}/profile",
        dependencies=[Depends(require_admin)],
    )
    def download_profile(agent_id: uuid.UUID, response: Response, db: Session = Depends(session)):
        preparation = _get_preparation(db, agent_id)
        if not public_protection(preparation)[2]:
            raise HTTPException(409, "perfil bloqueado: prova de cópia protegida pendente")
        if not _profile_is_current(preparation):
            raise HTTPException(409, "perfil legado requer nova verificação")
        response.headers["Cache-Control"] = "no-store"
        return preparation.profile

    @app.post(
        "/api/v1/provisioner/operations/claim",
        dependencies=[Depends(require_provisioner)],
    )
    def claim_operation(payload: ClaimIn, db: Session = Depends(session)):
        operation = db.execute(
            select(ProvisioningOperation)
            .where(ProvisioningOperation.kind.in_(("prepare_primary", "retry_primary")))
            .where(or_(
                ProvisioningOperation.state == "pending",
                and_(
                    ProvisioningOperation.state == "running",
                    or_(
                        # Reservas criadas antes da migração 9 não têm duração.
                        ProvisioningOperation.claim_expires_at.is_(None),
                        ProvisioningOperation.claim_expires_at <= func.clock_timestamp(),
                    ),
                ),
            ))
            .order_by(ProvisioningOperation.created_at, ProvisioningOperation.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        ).scalar_one_or_none()
        if operation is None:
            return {"operation": None}
        preparation = _get_preparation(db, operation.agent_id, lock=True)
        if enabled() and not preparation.reader_public_key:
            return {"operation": None}
        claim_id = uuid.uuid4()
        now = db.scalar(select(func.clock_timestamp()))
        expires = now + _CLAIM_DURATION
        operation.state = "running"
        operation.step = "reconciling"
        operation.claim_id = claim_id
        operation.claim_expires_at = expires
        operation.claimed_by = payload.executor_id
        if operation.started_at is None:
            operation.started_at = now
        preparation.state = "running"
        preparation.step = "reconciling"
        preparation.updated_at = now
        agent = db.get(Agent, operation.agent_id)
        client = db.get(Client, operation.client_id)
        db.commit()
        return {
            "operation": {
                "id": str(operation.id),
                "claim_id": str(claim_id),
                "claim_expires_at": expires.isoformat(),
                "attempt": operation.attempt,
                "client_id": str(client.id),
                "client_name": client.name,
                "installation_id": str(agent.id),
                "installation_identifier": agent.installation_identifier,
                "machine_name": agent.name,
                "suggested_time": agent.suggested_time,
                "account_name": account_name(client.id, agent.installation_identifier),
                "reader_public_key": preparation.reader_public_key,
            }
        }

    @app.post(
        "/api/v1/provisioner/operations/{operation_id}/result",
        dependencies=[Depends(require_provisioner)],
    )
    def operation_result(
        operation_id: uuid.UUID,
        payload: ResultIn,
        db: Session = Depends(session),
    ):
        operation = db.execute(
            select(ProvisioningOperation)
            .where(ProvisioningOperation.id == operation_id)
            .with_for_update()
        ).scalar_one_or_none()
        if operation is None or operation.kind not in {"prepare_primary", "retry_primary"}:
            raise HTTPException(404, "operação inexistente")
        normalized = payload.model_dump(mode="json")
        # Campo aditivo ausente não muda o hash de resultados terminais pré-migração 13.
        if normalized["result"] is not None and normalized["result"].get("lab_seed") is None:
            normalized["result"].pop("lab_seed", None)
        digest = hashlib.sha256(
            json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        preparation = _get_preparation(db, operation.agent_id, lock=True)
        if operation.state in {"succeeded", "failed"}:
            if operation.payload_hash != digest:
                raise HTTPException(409, "resultado terminal divergente")
            return _preparation(db, preparation)
        if operation.state != "running" or operation.claim_id != payload.claim_id:
            raise HTTPException(409, "claim ausente ou divergente")
        now = db.scalar(select(func.clock_timestamp()))
        if operation.claim_expires_at is None or operation.claim_expires_at <= now:
            raise HTTPException(409, "claim expirado")
        if preparation.current_operation_id != operation.id:
            raise HTTPException(409, "operação não é mais vigente")

        operation.state = payload.state
        operation.payload_hash = digest
        operation.finished_at = now
        preparation.updated_at = now
        if payload.state == "failed":
            operation.step = "failed"
            operation.error_code = payload.error_code
            preparation.state = "failed"
            preparation.step = "failed"
            preparation.error_code = payload.error_code
        else:
            _validate_success(db, operation, payload.result)
            result = payload.result
            if enabled() and (not result.lab_seed or result.lab_seed.repository_id != result.repository_id):
                raise HTTPException(422, "automação exige referência de ensaio vinculada ao primário")
            if result.lab_seed:
                preparation.lab_seed = result.lab_seed.model_dump()
            operation.step = "verified"
            preparation.state = "primary_ready"
            preparation.step = "verified"
            preparation.account_name = result.account_name
            preparation.repository_id = result.repository_id
            preparation.public_key_fingerprint = result.public_key_fingerprint
            preparation.vault_reference = f"installation/{operation.agent_id}/bundle"
            preparation.profile = result.profile.model_dump()
            preparation.error_code = None
            binding = preparation.protected_copy
            if not binding or binding["source"] != source_fingerprint(preparation):
                preparation.protected_copy = None
                preparation.protection_state = "pending"
        if enabled() and preparation.state == "primary_ready":
            from .lab_automation_routes import enqueue
            enqueue(db, preparation)
        db.commit()
        return _preparation(db, preparation)

    @app.get(
        "/api/v1/lab/installations/{agent_id}/profile",
        dependencies=[Depends(require_provisioner)],
    )
    def lab_profile(agent_id: uuid.UUID, db: Session = Depends(session)):
        if not config.lab_profile_export_enabled():
            raise HTTPException(404, "exportação de laboratório desabilitada")
        preparation = _get_preparation(db, agent_id)
        if preparation.state != "primary_ready" or preparation.profile is None:
            raise HTTPException(409, "estrutura primária ainda não verificada")
        if not _profile_is_current(preparation):
            raise HTTPException(409, "perfil legado requer nova verificação")
        return preparation.profile
