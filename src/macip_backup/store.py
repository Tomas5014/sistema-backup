"""Estado local do agente em SQLite: histórico/auditoria de execuções.

Banco pequeno e autocontido — funciona offline e pode ser sincronizado
com a API central quando ela existir. Nenhum segredo é persistido.

Ciclo de vida: ``record_start`` grava a execução com status ``running``
antes de qualquer tentativa — uma execução interrompida permanece
identificável no histórico. ``record_finish`` atualiza a conclusão e
grava o resultado final de cada destino e **todas as tentativas**.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from .models import TaskSpec
from .orchestrator import ExecutionResult

_SCHEMA = """
CREATE TABLE IF NOT EXISTS executions (
    execution_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    client_id TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at_utc TEXT NOT NULL,
    finished_at_utc TEXT
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
    duration_seconds REAL NOT NULL,
    PRIMARY KEY (execution_id, destination_id),
    FOREIGN KEY (execution_id) REFERENCES executions(execution_id)
);
CREATE TABLE IF NOT EXISTS attempts (
    execution_id TEXT NOT NULL,
    destination_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    status TEXT NOT NULL,
    retryable INTEGER NOT NULL,
    started_at_utc TEXT NOT NULL,
    finished_at_utc TEXT NOT NULL,
    duration_seconds REAL NOT NULL,
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
    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(self._path))
        self._connection.executescript(_SCHEMA)

    def close(self) -> None:
        self._connection.close()

    def record_start(
        self, task: TaskSpec, execution_id: str, started_at_utc: str
    ) -> None:
        """Grava a execução como `running` antes de qualquer tentativa."""
        with self._connection:
            self._connection.execute(
                "INSERT OR REPLACE INTO executions VALUES (?, ?, ?, ?, ?, NULL)",
                (execution_id, task.id, task.client.id, "running", started_at_utc),
            )

    def record_finish(self, result: ExecutionResult) -> None:
        with self._connection:
            self._connection.execute(
                "UPDATE executions SET status = ?, finished_at_utc = ? "
                "WHERE execution_id = ?",
                (result.status.value, result.finished_at_utc, result.execution_id),
            )
            for destination in result.destinations:
                self._connection.execute(
                    "INSERT OR REPLACE INTO destination_results "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        result.execution_id,
                        destination.destination_id,
                        destination.status.value,
                        destination.snapshot_id,
                        destination.snapshots_available,
                        destination.files_processed,
                        destination.bytes_processed,
                        destination.error,
                        destination.exit_code,
                        destination.failed_operation,
                        destination.timeout_seconds,
                        destination.duration_seconds,
                    ),
                )
                for attempt in destination.attempts:
                    self._connection.execute(
                        "INSERT OR REPLACE INTO attempts "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            result.execution_id,
                            destination.destination_id,
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
