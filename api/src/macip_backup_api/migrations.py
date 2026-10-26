"""Migrações versionadas e transacionais — nunca apagam histórico.

Cada migração é uma lista de DDL executada DENTRO de uma transação;
`schema_migrations` registra versões aplicadas. Um advisory lock de
sessão impede corrida entre instâncias.
"""

from __future__ import annotations

from sqlalchemy import Connection, Engine, text

MIGRATIONS: list[tuple[int, str, str]] = [
    (
        1,
        "init",
        """
        CREATE TABLE IF NOT EXISTS clients (
            id          uuid PRIMARY KEY,
            name        varchar(200) NOT NULL UNIQUE,
            created_at  timestamptz NOT NULL DEFAULT now()
        );
        CREATE TABLE IF NOT EXISTS agents (
            id           uuid PRIMARY KEY,
            client_id    uuid NOT NULL REFERENCES clients(id),
            name         varchar(200) NOT NULL,
            created_at   timestamptz NOT NULL DEFAULT now(),
            last_seen_at timestamptz
        );
        CREATE INDEX IF NOT EXISTS ix_agents_client ON agents(client_id);
        CREATE TABLE IF NOT EXISTS agent_credentials (
            id         uuid PRIMARY KEY,
            agent_id   uuid NOT NULL REFERENCES agents(id),
            token_hash varchar(64) NOT NULL UNIQUE,
            label      varchar(200),
            created_at timestamptz NOT NULL DEFAULT now(),
            revoked_at timestamptz
        );
        CREATE INDEX IF NOT EXISTS ix_credentials_agent ON agent_credentials(agent_id);
        CREATE TABLE IF NOT EXISTS executions (
            id            uuid PRIMARY KEY,
            agent_id      uuid NOT NULL REFERENCES agents(id),
            execution_id  varchar(64) NOT NULL,
            task_id       varchar(200) NOT NULL,
            status        varchar(20) NOT NULL,
            started_at_utc  timestamptz NOT NULL,
            finished_at_utc timestamptz NOT NULL,
            received_at   timestamptz NOT NULL DEFAULT now(),
            payload_hash  varchar(64) NOT NULL,
            CONSTRAINT uq_execution_agent_exec UNIQUE (agent_id, execution_id)
        );
        CREATE INDEX IF NOT EXISTS ix_executions_agent   ON executions(agent_id);
        CREATE INDEX IF NOT EXISTS ix_executions_task    ON executions(task_id);
        CREATE INDEX IF NOT EXISTS ix_executions_status  ON executions(status);
        CREATE INDEX IF NOT EXISTS ix_executions_started ON executions(started_at_utc);
        CREATE TABLE IF NOT EXISTS destination_results (
            id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            execution_row_id   uuid NOT NULL REFERENCES executions(id),
            destination_id     varchar(200) NOT NULL,
            status             varchar(20) NOT NULL,
            started_at_utc     timestamptz NOT NULL,
            finished_at_utc    timestamptz NOT NULL,
            duration_seconds   numeric,
            snapshot_id        varchar(128),
            snapshots_available integer,
            files_processed    bigint,
            bytes_processed    bigint,
            error              text,
            exit_code          integer,
            failed_operation   varchar(200),
            timeout_seconds    integer
        );
        CREATE INDEX IF NOT EXISTS ix_dest_exec ON destination_results(execution_row_id);
        CREATE TABLE IF NOT EXISTS attempt_results (
            id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            destination_row_id bigint NOT NULL REFERENCES destination_results(id),
            attempt            integer NOT NULL,
            status             varchar(20) NOT NULL,
            started_at_utc     timestamptz NOT NULL,
            finished_at_utc    timestamptz NOT NULL,
            duration_seconds   numeric,
            retryable          boolean NOT NULL DEFAULT false,
            snapshot_id        varchar(128),
            snapshots_available integer,
            files_processed    bigint,
            bytes_processed    bigint,
            error              text,
            exit_code          integer,
            failed_operation   varchar(200),
            timeout_seconds    integer
        );
        CREATE INDEX IF NOT EXISTS ix_attempt_dest ON attempt_results(destination_row_id);
        """,
    ),
]

_ADVISORY_KEY = 727_203_001


def run_migrations(connection: Connection) -> list[int]:
    """Aplica migrações pendentes; devolve versões aplicadas agora."""
    applied: list[int] = []
    connection.execute(
        text(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version integer PRIMARY KEY, name varchar(200) NOT NULL, "
            "applied_at timestamptz NOT NULL DEFAULT now())"
        )
    )
    done = {
        row[0]
        for row in connection.execute(text("SELECT version FROM schema_migrations"))
    }
    for version, name, ddl in MIGRATIONS:
        if version in done:
            continue
        connection.execute(text(ddl))
        connection.execute(
            text("INSERT INTO schema_migrations (version, name) VALUES (:v, :n)"),
            {"v": version, "n": name},
        )
        applied.append(version)
    return applied


def migrate(engine: Engine) -> list[int]:
    """Advisory lock de sessão + transação — seguro contra concorrência."""
    with engine.connect() as connection:
        connection.execute(text("SELECT pg_advisory_lock(:k)"), {"k": _ADVISORY_KEY})
        connection.commit()  # lock é de sessão — persiste fora da txn
        try:
            with connection.begin():
                return run_migrations(connection)
        finally:
            connection.execute(
                text("SELECT pg_advisory_unlock(:k)"), {"k": _ADVISORY_KEY}
            )
            connection.commit()
