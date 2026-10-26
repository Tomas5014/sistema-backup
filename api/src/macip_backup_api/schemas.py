"""Schemas de entrada/saída — metadados operacionais apenas.

NUNCA receba senhas Restic, chaves SSH, credenciais S3 ou a tarefa
completa: os campos abaixo são o contrato explícito.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, Field

Name = Annotated[str, Field(min_length=1, max_length=200)]
Short = Annotated[str | None, Field(default=None, max_length=200)]
Error = Annotated[str | None, Field(default=None, max_length=4000)]

EXECUTION_STATUSES = {"success", "partial", "failed", "interrupted"}
DESTINATION_STATUSES = {"success", "failed", "interrupted"}

MAX_DESTINATIONS = 32
MAX_ATTEMPTS = 64


def _require_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("timestamp deve ser timezone-aware")
    return value


Aware = Annotated[datetime, AfterValidator(_require_aware)]


class _ResultBase(BaseModel):
    status: Literal["success", "failed", "interrupted"]
    started_at_utc: Aware
    finished_at_utc: Aware
    duration_seconds: float | None = Field(default=None, ge=0)
    snapshot_id: Annotated[str | None, Field(default=None, max_length=128)]
    snapshots_available: int | None = Field(default=None, ge=0)
    files_processed: int | None = Field(default=None, ge=0)
    bytes_processed: int | None = Field(default=None, ge=0)
    error: Error
    exit_code: int | None = None
    failed_operation: Short
    timeout_seconds: int | None = Field(default=None, ge=0)


class AttemptIn(_ResultBase):
    attempt: int = Field(ge=1, le=1000)
    retryable: bool = False


class DestinationIn(_ResultBase):
    destination_id: Name
    attempts: list[AttemptIn] = Field(default_factory=list, max_length=MAX_ATTEMPTS)


class ExecutionIn(BaseModel):
    """Metadados de uma execução concluída — sem segredos."""

    execution_id: Annotated[str, Field(min_length=1, max_length=64)]
    task_id: Name
    # tolerados apenas se idênticos ao agente autenticado — ver auth
    client_id: str | None = Field(default=None, max_length=64)
    agent_id: str | None = Field(default=None, max_length=64)
    status: Literal["success", "partial", "failed", "interrupted"]
    started_at_utc: Aware
    finished_at_utc: Aware
    destinations: list[DestinationIn] = Field(max_length=MAX_DESTINATIONS)


class ClientIn(BaseModel):
    name: Name


class AgentIn(BaseModel):
    name: Name


class CredentialIn(BaseModel):
    label: Short = None


class ClientOut(BaseModel):
    id: uuid.UUID
    name: str
    created_at: datetime


class AgentOut(BaseModel):
    id: uuid.UUID
    client_id: uuid.UUID
    name: str
    created_at: datetime
    last_seen_at: datetime | None


class CredentialOut(BaseModel):
    credential_id: uuid.UUID
    agent_id: uuid.UUID
    token: str  # entregue UMA vez — nunca armazenado em claro


class CredentialInfo(BaseModel):
    id: uuid.UUID
    agent_id: uuid.UUID
    label: str | None
    created_at: datetime
    revoked_at: datetime | None


class ExecutionSummary(BaseModel):
    id: uuid.UUID
    agent_id: uuid.UUID
    client_id: uuid.UUID
    execution_id: str
    task_id: str
    status: str
    started_at_utc: datetime
    finished_at_utc: datetime
    received_at: datetime


class ExecutionDetail(ExecutionSummary):
    destinations: list[dict]
