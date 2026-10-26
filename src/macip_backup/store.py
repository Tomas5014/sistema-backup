"""Estado local do agente em SQLite: histórico/auditoria de execuções.

Banco pequeno e autocontido — funciona offline e pode ser sincronizado
com a API central quando ela existir. Nenhum segredo é persistido.

Ciclo de vida incremental:

- ``record_start`` grava a execução como ``running`` antes de qualquer
  tentativa — uma execução interrompida permanece identificável.
- ``attempt_started``/``attempt_finished`` gravam cada tentativa quando
  ela começa e termina; ``attempt_snapshot`` grava ``snapshot_id`` assim
  que o backup retorna.
- ``destination_finished`` grava o resultado final do destino.
- ``record_finish`` apenas conclui a execução — as tentativas já estão
  persistidas; uma morte abrupta entre gravações não perde histórico.

Migração versionada por ``PRAGMA user_version``: bancos do esquema
inicial (v0, ``finished_at_utc NOT NULL``, sem ``attempts``) e do esquema
de consolidação (v1, ``attempts`` com campos ``NOT NULL``) são migrados
transacionalmente para v2 preservando todo o histórico.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .models import ConfigurationError


def _utcnow() -> str:
    return datetime.now(UTC).isoformat()

if TYPE_CHECKING:
    from .models import TaskSpec
    from .orchestrator import AttemptResult, DestinationResult, ExecutionResult

_SCHEMA_VERSION = 5

_SCHEMA = """
CREATE TABLE IF NOT EXISTS executions (
    execution_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    client_id TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at_utc TEXT NOT NULL,
    finished_at_utc TEXT,
    interrupt_detail TEXT,
    outbox_enabled INTEGER NOT NULL DEFAULT 0,
    outbox_endpoint TEXT,
    outbox_agent_id TEXT
);
CREATE TABLE IF NOT EXISTS destination_results (
    execution_id TEXT NOT NULL,
    destination_id TEXT NOT NULL,
    status TEXT NOT NULL,
    snapshot_id TEXT,
    snapshots_available INTEGER,
    files_processed INTEGER,
    bytes_processed INTEGER,
    error TEXT,
    exit_code INTEGER,
    failed_operation TEXT,
    timeout_seconds INTEGER,
    duration_seconds REAL,
    PRIMARY KEY (execution_id, destination_id),
    FOREIGN KEY (execution_id) REFERENCES executions(execution_id)
);
CREATE TABLE IF NOT EXISTS attempts (
    execution_id TEXT NOT NULL,
    destination_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    status TEXT NOT NULL,
    retryable INTEGER NOT NULL DEFAULT 0,
    started_at_utc TEXT NOT NULL,
    finished_at_utc TEXT,
    duration_seconds REAL,
    failed_operation TEXT,
    snapshot_id TEXT,
    error TEXT,
    exit_code INTEGER,
    timeout_seconds INTEGER,
    PRIMARY KEY (execution_id, destination_id, attempt),
    FOREIGN KEY (execution_id) REFERENCES executions(execution_id)
);
CREATE INDEX IF NOT EXISTS idx_executions_task ON executions(task_id, started_at_utc);
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    execution_id TEXT NOT NULL UNIQUE,
    endpoint TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


class ExecutionStore:
    def __init__(
        self,
        path: Path,
        check_same_thread: bool = True,
        outbox: tuple[str, str] | None = None,
    ) -> None:
        """outbox=(endpoint, agent_id_central) habilita enfileiramento
        de resultados na mesma transação da conclusão da execução.
        Cada item fica vinculado ao endpoint/agente do enfileiramento —
        mudança de configuração não redireciona itens antigos."""
        self._outbox = outbox
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(
            str(self._path), check_same_thread=check_same_thread
        )
        try:
            self._migrate()
            self._connection.executescript(_SCHEMA)
        except Exception:
            self._connection.close()
            raise

    def close(self) -> None:
        self._connection.close()

    # -- migração versionada -------------------------------------------------
    #
    # Toda a migração roda numa ÚNICA transação explícita: BEGIN, DDL por
    # execute() (executescript comitaria no meio e deixaria o banco
    # quebrado), atualização de user_version e COMMIT. Qualquer erro faz
    # ROLLBACK e o banco permanece exatamente como estava.

    def _migrate(self) -> None:
        version = self._connection.execute("PRAGMA user_version").fetchone()[0]
        if version > _SCHEMA_VERSION:
            raise ConfigurationError(
                f"banco do agente tem esquema v{version} — não suportado "
                f"por esta versão (máx. v{_SCHEMA_VERSION}); o banco não "
                "foi modificado"
            )
        if version == _SCHEMA_VERSION:
            return
        # foreign_keys deve ficar off durante rebuilds; o pragma só vale
        # fora de transação, por isso vem antes do BEGIN.
        self._connection.execute("PRAGMA foreign_keys = OFF")
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            if version < 1:
                self._migrate_v0()
            if version < 2:
                self._migrate_v1()
            if version < 3:
                self._migrate_v2()
            if version < 4:
                self._migrate_v3()
            if version < 5:
                self._migrate_v4()
            self._connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
            self._connection.execute("COMMIT")
        except Exception:
            self._connection.execute("ROLLBACK")
            raise

    def _table_exists(self, name: str) -> bool:
        return bool(
            self._connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (name,),
            ).fetchone()
        )

    def _migrate_v0(self) -> None:
        """v0 → v1: executions ganha finished_at_utc anulável.

        Resíduos de uma migração antiga não atômica (executions_v1 sem
        executions, ou as duas) são saneados antes do rebuild — nunca
        assumem o lugar de executions se ela existir.
        """
        if not self._table_exists("executions"):
            if self._table_exists("executions_v1"):
                # Único vestígio: a tabela nova já estava completa.
                self._connection.execute(
                    "ALTER TABLE executions_v1 RENAME TO executions"
                )
            return
        if self._table_exists("executions_v1"):
            # Resíduo de crash entre CREATE e DROP da migração antiga —
            # a tabela original é a fonte de verdade.
            self._connection.execute("DROP TABLE executions_v1")
        for statement in (
            """
            CREATE TABLE executions_v1 (
                execution_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                client_id TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at_utc TEXT NOT NULL,
                finished_at_utc TEXT
            )
            """,
            "INSERT INTO executions_v1 SELECT * FROM executions",
            "DROP TABLE executions",
            "ALTER TABLE executions_v1 RENAME TO executions",
            # O índice morre junto com a tabela — recriado após o rename.
            "CREATE INDEX IF NOT EXISTS idx_executions_task "
            "ON executions(task_id, started_at_utc)",
        ):
            self._connection.execute(statement)

    def _migrate_v1(self) -> None:
        """v1 → v2: attempts/destination_results permitem linhas running.

        Tentativa em andamento não tem conclusão nem duração ainda;
        destino em andamento não tem duração. Linhas e chaves
        estrangeiras são preservadas pelo rebuild transacional.
        """
        if self._table_exists("attempts_v2") and not self._table_exists("attempts"):
            self._connection.execute("ALTER TABLE attempts_v2 RENAME TO attempts")
        elif self._table_exists("attempts"):
            if self._table_exists("attempts_v2"):
                self._connection.execute("DROP TABLE attempts_v2")
            for statement in (
                """
                CREATE TABLE attempts_v2 (
                    execution_id TEXT NOT NULL,
                    destination_id TEXT NOT NULL,
                    attempt INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    retryable INTEGER NOT NULL DEFAULT 0,
                    started_at_utc TEXT NOT NULL,
                    finished_at_utc TEXT,
                    duration_seconds REAL,
                    failed_operation TEXT,
                    snapshot_id TEXT,
                    error TEXT,
                    exit_code INTEGER,
                    timeout_seconds INTEGER,
                    PRIMARY KEY (execution_id, destination_id, attempt),
                    FOREIGN KEY (execution_id) REFERENCES executions(execution_id)
                )
                """,
                "INSERT INTO attempts_v2 SELECT * FROM attempts",
                "DROP TABLE attempts",
                "ALTER TABLE attempts_v2 RENAME TO attempts",
            ):
                self._connection.execute(statement)
        if self._table_exists("destination_results_v2") and not self._table_exists(
            "destination_results"
        ):
            self._connection.execute(
                "ALTER TABLE destination_results_v2 RENAME TO destination_results"
            )
        elif self._table_exists("destination_results"):
            if self._table_exists("destination_results_v2"):
                self._connection.execute("DROP TABLE destination_results_v2")
            for statement in (
                """
                CREATE TABLE destination_results_v2 (
                    execution_id TEXT NOT NULL,
                    destination_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    snapshot_id TEXT,
                    snapshots_available INTEGER,
                    files_processed INTEGER,
                    bytes_processed INTEGER,
                    error TEXT,
                    exit_code INTEGER,
                    failed_operation TEXT,
                    timeout_seconds INTEGER,
                    duration_seconds REAL,
                    PRIMARY KEY (execution_id, destination_id),
                    FOREIGN KEY (execution_id) REFERENCES executions(execution_id)
                )
                """,
                "INSERT INTO destination_results_v2 "
                "SELECT * FROM destination_results",
                "DROP TABLE destination_results",
                "ALTER TABLE destination_results_v2 RENAME TO destination_results",
            ):
                self._connection.execute(statement)

    def _migrate_v2(self) -> None:
        """v2 → v3: executions ganha interrupt_detail (motivo do
        cancelamento, incluindo falha de limpeza)."""
        columns = {
            row[1]
            for row in self._connection.execute(
                "PRAGMA table_info(executions)"
            ).fetchall()
        }
        if self._table_exists("executions") and "interrupt_detail" not in columns:
            self._connection.execute(
                "ALTER TABLE executions ADD COLUMN interrupt_detail TEXT"
            )

    def _migrate_v3(self) -> None:
        """v3 → v4: outbox persistente para envio dos resultados."""
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                execution_id TEXT NOT NULL UNIQUE,
                endpoint TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                payload TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at TEXT,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )

    def _migrate_v4(self) -> None:
        """v4 → v5: congela a intenção de entrega no início da execução.

        Linhas antigas não têm como provar para qual central foram iniciadas.
        Elas ficam com ``outbox_enabled=0``: não são enfileiradas por uma
        configuração posterior. Itens já existentes em ``outbox`` preservam
        endpoint, agente e payload originais.
        """
        if not self._table_exists("executions"):
            return
        columns = {
            row[1]
            for row in self._connection.execute(
                "PRAGMA table_info(executions)"
            ).fetchall()
        }
        for name, definition in (
            ("outbox_enabled", "INTEGER NOT NULL DEFAULT 0"),
            ("outbox_endpoint", "TEXT"),
            ("outbox_agent_id", "TEXT"),
        ):
            if name not in columns:
                self._connection.execute(
                    f"ALTER TABLE executions ADD COLUMN {name} {definition}"
                )

    # -- outbox: vinculado à identidade/endpoint do enfileiramento -----------

    def _enqueue_locked(self, execution_id: str) -> None:
        """INSERT dentro da transação da conclusão — nunca órfão."""
        binding = self._connection.execute(
            "SELECT outbox_enabled, outbox_endpoint, outbox_agent_id "
            "FROM executions WHERE execution_id = ?",
            (execution_id,),
        ).fetchone()
        if (
            binding is None
            or not binding[0]
            or not binding[1]
            or not binding[2]
        ):
            return
        endpoint, agent_id = binding[1], binding[2]
        payload = self._payload_from_rows(execution_id, agent_id)
        now = _utcnow()
        self._connection.execute(
            "INSERT OR IGNORE INTO outbox "
            "(execution_id, endpoint, agent_id, payload, status, "
            "attempts, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'pending', 0, ?, ?)",
            (
                execution_id,
                endpoint,
                agent_id,
                payload,
                now,
                now,
            ),
        )

    def _payload_from_rows(self, execution_id: str, agent_id: str) -> str:
        """Metadados no contrato da API — client_id LOCAL não é enviado
        (não é a identidade central); agent_id é o UUID central da
        configuração. Nenhum segredo nem tarefa completa."""
        import json as _json

        execution = self._connection.execute(
            "SELECT status, task_id, started_at_utc, finished_at_utc "
            "FROM executions WHERE execution_id = ?",
            (execution_id,),
        ).fetchone()
        if execution is None:
            return _json.dumps({"execution_id": execution_id, "error": "missing"})
        destinations = []
        for row in self._connection.execute(
            "SELECT destination_id, status, duration_seconds, snapshot_id, "
            "snapshots_available, files_processed, bytes_processed, error, "
            "exit_code, failed_operation, timeout_seconds "
            "FROM destination_results WHERE execution_id = ? "
            "ORDER BY destination_id",
            (execution_id,),
        ):
            attempts = [
                {
                    "attempt": a[0],
                    "status": a[1],
                    "started_at_utc": a[2],
                    "finished_at_utc": a[3],
                    "duration_seconds": a[4],
                    "retryable": bool(a[5]),
                    "snapshot_id": a[6],
                    "error": a[7],
                    "exit_code": a[8],
                    "failed_operation": a[9],
                    "timeout_seconds": a[10],
                }
                for a in self._connection.execute(
                    "SELECT attempt, status, started_at_utc, finished_at_utc, "
                    "duration_seconds, retryable, snapshot_id, error, "
                    "exit_code, failed_operation, timeout_seconds "
                    "FROM attempts WHERE execution_id = ? AND destination_id = ? "
                    "ORDER BY attempt",
                    (execution_id, row[0]),
                )
            ]
            # o destino não grava timestamps próprios — derivam das
            # tentativas (fallback para o intervalo da execução)
            started = attempts[0]["started_at_utc"] if attempts else execution[2]
            finished = (
                attempts[-1]["finished_at_utc"] if attempts else execution[3]
            )
            destinations.append(
                {
                    "destination_id": row[0],
                    "status": row[1],
                    "started_at_utc": started,
                    "finished_at_utc": finished,
                    "duration_seconds": row[2],
                    "snapshot_id": row[3],
                    "snapshots_available": row[4],
                    "files_processed": row[5],
                    "bytes_processed": row[6],
                    "error": row[7],
                    "exit_code": row[8],
                    "failed_operation": row[9],
                    "timeout_seconds": row[10],
                    "attempts": attempts,
                }
            )
        return _json.dumps(
            {
                "execution_id": execution_id,
                "task_id": execution[1],
                "agent_id": agent_id,
                "status": execution[0],
                "started_at_utc": execution[2],
                "finished_at_utc": execution[3],
                "destinations": destinations,
            },
            ensure_ascii=False,
        )

    def pending_outbox(
        self, endpoint: str, agent_id: str, now: str, limit: int
    ) -> list[dict]:
        """Itens pendentes DESTE endpoint/agente — a fila fica vinculada
        à identidade do enfileiramento; outra configuração não os toca."""
        rows = self._connection.execute(
            "SELECT id, execution_id, payload, attempts FROM outbox "
            "WHERE status = 'pending' AND endpoint = ? AND agent_id = ? "
            "AND (next_attempt_at IS NULL OR next_attempt_at <= ?) "
            "ORDER BY id LIMIT ?",
            (endpoint, agent_id, now, limit),
        ).fetchall()
        return [
            {"id": r[0], "execution_id": r[1], "payload": r[2], "attempts": r[3]}
            for r in rows
        ]

    def outbox_state(self) -> list[dict]:
        """Fila consultável: pendentes, bloqueados, permanentes, enviados."""
        rows = self._connection.execute(
            "SELECT id, execution_id, endpoint, agent_id, payload, status, "
            "attempts, next_attempt_at, last_error, created_at, updated_at "
            "FROM outbox ORDER BY id"
        ).fetchall()
        return [
            {
                "id": r[0],
                "execution_id": r[1],
                "endpoint": r[2],
                "agent_id": r[3],
                "payload": r[4],
                "status": r[5],
                "attempts": r[6],
                "next_attempt_at": r[7],
                "last_error": r[8],
                "created_at": r[9],
                "updated_at": r[10],
            }
            for r in rows
        ]

    def _outbox_update(
        self,
        row_id: int,
        status: str,
        attempts: int | None = None,
        next_attempt_at: str | None = None,
        last_error: str | None = None,
    ) -> None:
        with self._connection:
            self._connection.execute(
                "UPDATE outbox SET status = ?, attempts = COALESCE(?, attempts), "
                "next_attempt_at = ?, last_error = ?, updated_at = ? "
                "WHERE id = ?",
                (status, attempts, next_attempt_at, last_error, _utcnow(), row_id),
            )

    def outbox_sent(self, row_id: int) -> None:
        self._outbox_update(row_id, "sent")

    def outbox_retry(self, row_id: int, error: str, next_attempt_at: str) -> None:
        with self._connection:
            self._connection.execute(
                "UPDATE outbox SET attempts = attempts + 1, next_attempt_at = ?, "
                "last_error = ?, updated_at = ? WHERE id = ?",
                (next_attempt_at, error[:500], _utcnow(), row_id),
            )

    def outbox_blocked(self, row_id: int, kind: str, error: str) -> None:
        """auth_blocked | failed_permanent — consultável, sem reenvio."""
        self._outbox_update(row_id, kind, last_error=error[:500])

    def rearm_outbox(
        self,
        endpoint: str,
        agent_id: str,
        include_permanent: bool = False,
    ) -> list[str]:
        """Rearma bloqueios explicitamente vinculados à central informada.

        Não toca itens enviados e preserva tentativas, payload e execution_id.
        """
        statuses = ("auth_blocked", "failed_permanent") if include_permanent else (
            "auth_blocked",
        )
        placeholders = ", ".join("?" for _ in statuses)
        with self._connection:
            rows = self._connection.execute(
                "SELECT execution_id FROM outbox WHERE endpoint = ? AND agent_id = ? "
                f"AND status IN ({placeholders}) ORDER BY id",
                (endpoint, agent_id, *statuses),
            ).fetchall()
            self._connection.execute(
                "UPDATE outbox SET status = 'pending', next_attempt_at = NULL, "
                "updated_at = ? WHERE endpoint = ? AND agent_id = ? "
                f"AND status IN ({placeholders})",
                (_utcnow(), endpoint, agent_id, *statuses),
            )
        return [row[0] for row in rows]

    # -- ciclo de vida da execução -------------------------------------------

    def record_start(
        self, task: "TaskSpec", execution_id: str, started_at_utc: str
    ) -> None:
        """Grava a execução como `running` antes de qualquer tentativa."""
        with self._connection:
            binding = self._outbox or (None, None)
            self._connection.execute(
                "INSERT OR REPLACE INTO executions "
                "(execution_id, task_id, client_id, status, started_at_utc, "
                "finished_at_utc, outbox_enabled, outbox_endpoint, outbox_agent_id) "
                "VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?)",
                (
                    execution_id,
                    task.id,
                    task.client.id,
                    "running",
                    started_at_utc,
                    1 if self._outbox else 0,
                    binding[0],
                    binding[1],
                ),
            )

    def record_finish(
        self,
        result: "ExecutionResult",
        interruption_detail: str | None = None,
        finished_at_utc: str | None = None,
    ) -> None:
        """Conclui a execução e garante resultados persistidos.

        Com o recorder incremental as linhas já existem — os INSERT OR
        REPLACE abaixo são idempotentes e também cobrem gravações feitas
        sem hooks incrementais. A conclusão da execução e o item da
        outbox acontecem NA MESMA transação — nunca órfãos.
        """
        with self._connection:
            status = "interrupted" if interruption_detail is not None else result.status.value
            final_finished_at = finished_at_utc or result.finished_at_utc
            cursor = self._connection.execute(
                "UPDATE executions SET status = ?, finished_at_utc = ?, "
                "interrupt_detail = ? WHERE execution_id = ? "
                "AND status = 'running' AND finished_at_utc IS NULL",
                (status, final_finished_at, interruption_detail, result.execution_id),
            )
            # Uma conclusão já congelada nunca muda histórico nem payload.
            if cursor.rowcount != 1:
                return
            for destination in result.destinations:
                self._destination_upsert(result.execution_id, destination)
                for attempt in destination.attempts:
                    self._attempt_upsert(
                        result.execution_id, destination.destination_id, attempt
                    )
            self._enqueue_locked(result.execution_id)

    def _attempt_upsert(
        self,
        execution_id: str,
        destination_id: str,
        attempt: "AttemptResult",
    ) -> None:
        self._connection.execute(
                "INSERT OR REPLACE INTO attempts "
                "(execution_id, destination_id, attempt, status, retryable, "
                "started_at_utc, finished_at_utc, duration_seconds, "
                "failed_operation, snapshot_id, error, exit_code, "
                "timeout_seconds) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    execution_id,
                    destination_id,
                    attempt.attempt,
                    attempt.status.value,
                    1 if attempt.retryable else 0,
                    attempt.started_at_utc,
                    attempt.finished_at_utc,
                    attempt.duration_seconds,
                    attempt.failed_operation,
                    attempt.snapshot_id,
                    attempt.error,
                    attempt.exit_code,
                    attempt.timeout_seconds,
                ),
            )

    def running_task_ids(self) -> list[str]:
        rows = self._connection.execute(
            "SELECT DISTINCT task_id FROM executions WHERE status = 'running'"
        ).fetchall()
        return [row[0] for row in rows]

    def mark_stale_interrupted(self, task_id: str, finished_at_utc: str) -> int:
        """Marca execuções `running` DESTA tarefa como `interrupted`.

        Chamado apenas quando a posse do lock da tarefa está confirmada
        (próprio lock, ou lock livre provado por tentativa de aquisição)
        — nunca toca execução de tarefa com lock ocupado.
        """
        with self._connection:
            stale_ids = [
                row[0]
                for row in self._connection.execute(
                    "SELECT execution_id FROM executions "
                    "WHERE status = 'running' AND task_id = ?",
                    (task_id,),
                ).fetchall()
            ]
            cursor = self._connection.execute(
                "UPDATE executions SET status = 'interrupted', finished_at_utc = ? "
                "WHERE status = 'running' AND task_id = ?",
                (finished_at_utc, task_id),
            )
            self._connection.execute(
                "UPDATE attempts SET status = 'interrupted', "
                "finished_at_utc = COALESCE(finished_at_utc, ?) "
                "WHERE status = 'running' AND execution_id IN "
                "(SELECT execution_id FROM executions WHERE task_id = ?)",
                (finished_at_utc, task_id),
            )
            self._connection.execute(
                "UPDATE destination_results SET status = 'interrupted' "
                "WHERE status = 'running' AND execution_id IN "
                "(SELECT execution_id FROM executions WHERE task_id = ?)",
                (task_id,),
            )
            for execution_id in stale_ids:
                self._enqueue_locked(execution_id)
        return cursor.rowcount

    def mark_interrupted(
        self,
        execution_id: str,
        finished_at_utc: str,
        detail: str | None = None,
    ) -> None:
        """Marca UMA execução como interrompida (sinal recebido).

        ``detail`` registra o motivo — sinal recebido e eventual falha
        na limpeza do grupo de processos.
        """
        with self._connection:
            cursor = self._connection.execute(
                "UPDATE executions SET status = 'interrupted', "
                "finished_at_utc = ?, interrupt_detail = ? "
                "WHERE execution_id = ? AND status = 'running' "
                "AND finished_at_utc IS NULL",
                (finished_at_utc, detail, execution_id),
            )
            if cursor.rowcount != 1:
                return
            self._connection.execute(
                "UPDATE attempts SET status = 'interrupted', "
                "finished_at_utc = COALESCE(finished_at_utc, ?) "
                "WHERE execution_id = ? AND status = 'running'",
                (finished_at_utc, execution_id),
            )
            self._connection.execute(
                "UPDATE destination_results SET status = 'interrupted' "
                "WHERE execution_id = ? AND status = 'running'",
                (execution_id,),
            )
            self._enqueue_locked(execution_id)

    # -- gravação incremental por tentativa ----------------------------------

    def attempt_started(
        self,
        execution_id: str,
        destination_id: str,
        attempt: int,
        started_at_utc: str,
    ) -> None:
        with self._connection:
            self._connection.execute(
                "INSERT OR IGNORE INTO destination_results "
                "(execution_id, destination_id, status) VALUES (?, ?, 'running')",
                (execution_id, destination_id),
            )
            self._connection.execute(
                "UPDATE destination_results SET status = 'running' "
                "WHERE execution_id = ? AND destination_id = ?",
                (execution_id, destination_id),
            )
            self._connection.execute(
                "INSERT OR REPLACE INTO attempts "
                "(execution_id, destination_id, attempt, status, started_at_utc) "
                "VALUES (?, ?, ?, 'running', ?)",
                (execution_id, destination_id, attempt, started_at_utc),
            )

    def attempt_snapshot(
        self,
        execution_id: str,
        destination_id: str,
        attempt: int,
        snapshot_id: str,
    ) -> None:
        """Persiste o snapshot assim que o backup retorna sucesso."""
        with self._connection:
            self._connection.execute(
                "UPDATE attempts SET snapshot_id = ? WHERE execution_id = ? "
                "AND destination_id = ? AND attempt = ?",
                (snapshot_id, execution_id, destination_id, attempt),
            )
            self._connection.execute(
                "UPDATE destination_results SET snapshot_id = ? "
                "WHERE execution_id = ? AND destination_id = ?",
                (snapshot_id, execution_id, destination_id),
            )

    def attempt_finished(
        self,
        execution_id: str,
        destination_id: str,
        attempt: "AttemptResult",
    ) -> None:
        with self._connection:
            self._connection.execute(
                "UPDATE attempts SET status = ?, retryable = ?, "
                "finished_at_utc = ?, duration_seconds = ?, "
                "failed_operation = ?, snapshot_id = COALESCE(?, snapshot_id), "
                "error = ?, exit_code = ?, timeout_seconds = ? "
                "WHERE execution_id = ? AND destination_id = ? AND attempt = ?",
                (
                    attempt.status.value,
                    1 if attempt.retryable else 0,
                    attempt.finished_at_utc,
                    attempt.duration_seconds,
                    attempt.failed_operation,
                    attempt.snapshot_id,
                    attempt.error,
                    attempt.exit_code,
                    attempt.timeout_seconds,
                    execution_id,
                    destination_id,
                    attempt.attempt,
                ),
            )

    def destination_finished(
        self, execution_id: str, result: "DestinationResult"
    ) -> None:
        with self._connection:
            self._destination_upsert(execution_id, result)

    def _destination_upsert(
        self, execution_id: str, result: "DestinationResult"
    ) -> None:
        self._connection.execute(
            "INSERT OR REPLACE INTO destination_results "
            "(execution_id, destination_id, status, snapshot_id, "
            "snapshots_available, files_processed, bytes_processed, "
            "error, exit_code, failed_operation, timeout_seconds, "
            "duration_seconds) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                execution_id,
                result.destination_id,
                result.status.value,
                result.snapshot_id,
                result.snapshots_available,
                result.files_processed,
                result.bytes_processed,
                result.error,
                result.exit_code,
                result.failed_operation,
                result.timeout_seconds,
                result.duration_seconds,
            ),
        )

    # -- consulta -------------------------------------------------------------

    def history(self, task_id: str | None = None) -> list[dict[str, Any]]:
        if task_id is not None:
            rows = self._connection.execute(
                "SELECT * FROM executions WHERE task_id = ? "
                "ORDER BY started_at_utc DESC",
                (task_id,),
            ).fetchall()
        else:
            rows = self._connection.execute(
                "SELECT * FROM executions ORDER BY started_at_utc DESC"
            ).fetchall()
        return [
            {
                "execution_id": row[0],
                "task_id": row[1],
                "client_id": row[2],
                "status": row[3],
                "started_at_utc": row[4],
                "finished_at_utc": row[5],
                "interrupt_detail": row[6],
                "destinations": self._destinations_of(row[0]),
            }
            for row in rows
        ]

    def _destinations_of(self, execution_id: str) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            "SELECT * FROM destination_results WHERE execution_id = ?",
            (execution_id,),
        ).fetchall()
        return [
            {
                "destination_id": row[1],
                "status": row[2],
                "snapshot_id": row[3],
                "snapshots_available": row[4],
                "files_processed": row[5],
                "bytes_processed": row[6],
                "error": row[7],
                "exit_code": row[8],
                "failed_operation": row[9],
                "timeout_seconds": row[10],
                "duration_seconds": row[11],
                "attempts": self._attempts_of(execution_id, row[1]),
            }
            for row in rows
        ]

    def _attempts_of(
        self, execution_id: str, destination_id: str
    ) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            "SELECT * FROM attempts WHERE execution_id = ? AND destination_id = ? "
            "ORDER BY attempt",
            (execution_id, destination_id),
        ).fetchall()
        return [
            {
                "attempt": row[2],
                "status": row[3],
                "retryable": bool(row[4]),
                "started_at_utc": row[5],
                "finished_at_utc": row[6],
                "duration_seconds": row[7],
                "failed_operation": row[8],
                "snapshot_id": row[9],
                "error": row[10],
                "exit_code": row[11],
                "timeout_seconds": row[12],
            }
            for row in rows
        ]
