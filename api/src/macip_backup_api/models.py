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
from sqlalchemy.dialects.postgresql import UUID
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


class SchemaMigration(Base):
    __tablename__ = "schema_migrations"

    version = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    applied_at = mapped_column(DateTime(timezone=True), server_default=func.now())
