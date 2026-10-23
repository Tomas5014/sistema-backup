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
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .models import ConfigurationError

if TYPE_CHECKING:
    from .models import TaskSpec
    from .orchestrator import AttemptResult, DestinationResult, ExecutionResult

_SCHEMA_VERSION = 3

_SCHEMA = """
CREATE TABLE IF NOT EXISTS executions (
    execution_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    client_id TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at_utc TEXT NOT NULL,
    finished_at_utc TEXT,
    interrupt_detail TEXT
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
"""


class ExecutionStore:
    def __init__(self, path: Path, check_same_thread: bool = True) -> None:
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

    # -- ciclo de vida da execução -------------------------------------------

    def record_start(
        self, task: "TaskSpec", execution_id: str, started_at_utc: str
    ) -> None:
        """Grava a execução como `running` antes de qualquer tentativa."""
        with self._connection:
            self._connection.execute(
                "INSERT OR REPLACE INTO executions "
                "(execution_id, task_id, client_id, status, started_at_utc, "
                "finished_at_utc) VALUES (?, ?, ?, ?, ?, NULL)",
                (execution_id, task.id, task.client.id, "running", started_at_utc),
            )

    def record_finish(self, result: "ExecutionResult") -> None:
        """Conclui a execução e garante resultados persistidos.

        Com o recorder incremental as linhas já existem — os INSERT OR
        REPLACE abaixo são idempotentes e também cobrem gravações feitas
        sem hooks incrementais.
        """
        with self._connection:
            self._connection.execute(
                "UPDATE executions SET status = ?, finished_at_utc = ? "
                "WHERE execution_id = ?",
                (result.status.value, result.finished_at_utc, result.execution_id),
            )
        for destination in result.destinations:
            self.destination_finished(result.execution_id, destination)
            for attempt in destination.attempts:
                self._backfill_attempt(
                    result.execution_id, destination.destination_id, attempt
                )

    def _backfill_attempt(
        self,
        execution_id: str,
        destination_id: str,
        attempt: "AttemptResult",
    ) -> None:
        with self._connection:
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
            self._connection.execute(
                "UPDATE executions SET status = 'interrupted', "
                "finished_at_utc = ?, interrupt_detail = ? "
                "WHERE execution_id = ?",
                (finished_at_utc, detail, execution_id),
            )
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
