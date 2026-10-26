"""Schemas de entrada/saída — metadados operacionais apenas.

NUNCA receba senhas Restic, chaves SSH, credenciais S3 ou a tarefa
completa: os campos abaixo são o contrato explícito.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, Field, model_validator

Name = Annotated[str, Field(min_length=1, max_length=200)]
Short = Annotated[str | None, Field(default=None, max_length=200)]
Error = Annotated[str | None, Field(default=None, max_length=4000)]

# limites compatíveis com os tipos das colunas PostgreSQL
_BIGINT_MAX = 9_223_372_036_854_775_807
_INT_MAX = 2_147_483_647

BigCount = Annotated[int | None, Field(default=None, ge=0, le=_BIGINT_MAX)]
IntCount = Annotated[int | None, Field(default=None, ge=0, le=_INT_MAX)]

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
    duration_seconds: float | None = Field(
        default=None, ge=0, le=_BIGINT_MAX, allow_inf_nan=False
    )
    snapshot_id: Annotated[str | None, Field(default=None, max_length=128)]
    snapshots_available: IntCount
    files_processed: BigCount
    bytes_processed: BigCount
    error: Error
    exit_code: int | None = Field(default=None, ge=-_INT_MAX - 1, le=_INT_MAX)
    failed_operation: Short
    timeout_seconds: IntCount

    @model_validator(mode="after")
    def _ordered(self):
        if self.finished_at_utc < self.started_at_utc:
            raise ValueError("finished_at_utc anterior a started_at_utc")
        return self


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

    @model_validator(mode="after")
    def _ordered(self):
        if self.finished_at_utc < self.started_at_utc:
            raise ValueError("finished_at_utc anterior a started_at_utc")
        return self


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
