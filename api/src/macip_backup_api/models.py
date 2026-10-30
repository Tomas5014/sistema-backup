"""Modelo relacional da API central — espelha o contrato do agente."""

from __future__ import annotations

import uuid

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    BigInteger,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Client(Base):
    __tablename__ = "clients"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    created_at = mapped_column(DateTime(timezone=True), server_default=func.now())

    agents: Mapped[list["Agent"]] = relationship(back_populates="client")


class Agent(Base):
    __tablename__ = "agents"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    client_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("clients.id"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at = mapped_column(DateTime(timezone=True), nullable=True)

    client: Mapped[Client] = relationship(back_populates="agents")
    credentials: Mapped[list["AgentCredential"]] = relationship(back_populates="agent")


class AgentCredential(Base):
    __tablename__ = "agent_credentials"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    label: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at = mapped_column(DateTime(timezone=True), server_default=func.now())
    revoked_at = mapped_column(DateTime(timezone=True), nullable=True)

    agent: Mapped[Agent] = relationship(back_populates="credentials")


class Execution(Base):
    __tablename__ = "executions"
    __table_args__ = (
        UniqueConstraint("agent_id", "execution_id", name="uq_execution_agent_exec"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False, index=True
    )
    execution_id: Mapped[str] = mapped_column(String(64), nullable=False)
    task_id: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    started_at_utc = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at_utc = mapped_column(DateTime(timezone=True), nullable=False)
    received_at = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    agent: Mapped[Agent] = relationship()
    destinations: Mapped[list["DestinationResultRow"]] = relationship(
        back_populates="execution", cascade="all, delete-orphan"
    )


class DestinationResultRow(Base):
    __tablename__ = "destination_results"

    id = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    execution_row_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("executions.id"), nullable=False, index=True
    )
    destination_id: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    started_at_utc = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at_utc = mapped_column(DateTime(timezone=True), nullable=False)
    duration_seconds = mapped_column(Numeric, nullable=True)
    snapshot_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    snapshots_available = mapped_column(Integer, nullable=True)
    files_processed = mapped_column(BigInteger, nullable=True)
    bytes_processed = mapped_column(BigInteger, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    exit_code = mapped_column(Integer, nullable=True)
    failed_operation: Mapped[str | None] = mapped_column(String(200), nullable=True)
    timeout_seconds = mapped_column(Integer, nullable=True)

    execution: Mapped[Execution] = relationship(back_populates="destinations")
    attempts: Mapped[list["AttemptResultRow"]] = relationship(
        back_populates="destination", cascade="all, delete-orphan"
    )


class AttemptResultRow(Base):
    __tablename__ = "attempt_results"

    id = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    destination_row_id = mapped_column(
        BigInteger, ForeignKey("destination_results.id"), nullable=False, index=True
    )
    attempt = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    started_at_utc = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at_utc = mapped_column(DateTime(timezone=True), nullable=False)
    duration_seconds = mapped_column(Numeric, nullable=True)
    retryable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    snapshot_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    snapshots_available = mapped_column(Integer, nullable=True)
    files_processed = mapped_column(BigInteger, nullable=True)
    bytes_processed = mapped_column(BigInteger, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    exit_code = mapped_column(Integer, nullable=True)
    failed_operation: Mapped[str | None] = mapped_column(String(200), nullable=True)
    timeout_seconds = mapped_column(Integer, nullable=True)

    destination: Mapped[DestinationResultRow] = relationship(back_populates="attempts")


class Operator(Base):
    """Operador interno MAC IP — login normalizado único + Argon2id."""

    __tablename__ = "operators"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    login: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(256), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_login_at = mapped_column(DateTime(timezone=True), nullable=True)

    sessions: Mapped[list["OperatorSession"]] = relationship(
        back_populates="operator", cascade="all, delete-orphan"
    )


class OperatorSession(Base):
    """Sessão opaca — persistimos SÓ o hash do identificador."""

    __tablename__ = "operator_sessions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    operator_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("operators.id"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    created_at = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at = mapped_column(DateTime(timezone=True), nullable=True)

    operator: Mapped[Operator] = relationship(back_populates="sessions")


class LoginAttempt(Base):
    """Tentativas de login — limitação válida entre processos da API."""

    __tablename__ = "login_attempts"

    id = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    login: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    ip: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    attempted_at = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Task(Base):
    """Cadastro central de uma tarefa — identidade estável por UUID.

    O nome atual é desnormalizado da revisão vigente só para listagem;
    a verdade de cada revisão está em task_revisions.config.
    """

    __tablename__ = "tasks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    current_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at = mapped_column(DateTime(timezone=True), server_default=func.now())
    archived_at = mapped_column(DateTime(timezone=True), nullable=True)

    agent: Mapped[Agent] = relationship()
    revisions: Mapped[list["TaskRevision"]] = relationship(
        back_populates="task", order_by="TaskRevision.revision"
    )


class TaskRevision(Base):
    """Revisão imutável da configuração — snapshot completo por número."""

    __tablename__ = "task_revisions"
    __table_args__ = (
        UniqueConstraint("task_id", "revision", name="uq_task_revision"),
    )

    id = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id"), nullable=False, index=True
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    contract_version: Mapped[int] = mapped_column(Integer, nullable=False)
    config = mapped_column(JSONB, nullable=False)
    # snapshot do nome do cliente NA revisão — exportação histórica
    # não pode depender de um nome editável no presente; revisões
    # anteriores à migração 4 receberam o nome vigente à época
    client_name: Mapped[str | None] = mapped_column(
        String(200), nullable=True
    )
    author_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    author_ref: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    task: Mapped[Task] = relationship(back_populates="revisions")


class TaskDistribution(Base):
    """Estado DESEJADO de distribuição — separado das revisões
    imutáveis. `generation` é monotônica e distingue publicação,
    atualização e suspensão mesmo com a mesma revisão. As colunas
    `confirmed_*` guardam a última confirmação CUJA geração é a
    vigente — confirmações antigas ficam só no log."""

    __tablename__ = "task_distributions"

    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id"), primary_key=True
    )
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    published_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # none = nunca publicada; apply = agenda instalada; suspend = timer desligado
    desired_action: Mapped[str] = mapped_column(
        String(20), nullable=False, default="none"
    )
    author_kind: Mapped[str | None] = mapped_column(String(20), nullable=True)
    author_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    updated_at = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    confirmed_generation: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    confirmed_status: Mapped[str | None] = mapped_column(
        String(20), nullable=True
    )
    confirmed_revision: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    confirmed_detail: Mapped[str | None] = mapped_column(
        String(1000), nullable=True
    )
    confirmed_agent_time: Mapped[str | None] = mapped_column(
        String(100), nullable=True
    )
    confirmed_at = mapped_column(DateTime(timezone=True), nullable=True)

    task: Mapped[Task] = relationship()


class TaskConfirmation(Base):
    """Log durável de confirmações — (task_id, generation) único com
    hash do conteúdo, como a deduplicação das execuções."""

    __tablename__ = "task_confirmations"
    __table_args__ = (
        UniqueConstraint("task_id", "generation", name="uq_task_confirmation"),
    )

    id = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id"), nullable=False, index=True
    )
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    applied_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    detail: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    agent_time: Mapped[str | None] = mapped_column(String(100), nullable=True)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    received_at = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class SchemaMigration(Base):
    __tablename__ = "schema_migrations"

    version = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    applied_at = mapped_column(DateTime(timezone=True), server_default=func.now())
