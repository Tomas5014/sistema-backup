"""Agente local: persistência SQLite, histórico e credenciais do SO."""

from __future__ import annotations

import io
import json
import os
import secrets
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from macip_backup.agent import history, run_task
from macip_backup.cli import main as cli_main
from macip_backup.logging import StructuredLogger
from macip_backup.models import (
    Client,
    CompressionMode,
    Destination,
    DestinationKind,
    TaskSpec,
)
from macip_backup.orchestrator import (
    BackupOrchestrator,
    DestinationResult,
    DestinationStatus,
    ExecutionResult,
    ExecutionStatus,
)
from macip_backup.store import ExecutionStore


def _fake_result(
    task_id: str = "task-store",
    status: str = "success",
    execution_id: str = "exec-1",
) -> ExecutionResult:
    return ExecutionResult(
        execution_id=execution_id,
        task_id=task_id,
        client_id="client-store",
        status=ExecutionStatus(status),
        started_at_utc="2026-10-22T10:00:00+00:00",
        finished_at_utc="2026-10-22T10:01:00+00:00",
        destinations=(
            DestinationResult(
                destination_id="local",
                status=DestinationStatus.SUCCESS,
                started_at_utc="2026-10-22T10:00:00+00:00",
                finished_at_utc="2026-10-22T10:01:00+00:00",
                duration_seconds=1.5,
                snapshot_id="snap-1",
                snapshots_available=3,
                files_processed=10,
                bytes_processed=2048,
            ),
            DestinationResult(
                destination_id="remoto",
                status=DestinationStatus.FAILED,
                started_at_utc="2026-10-22T10:00:00+00:00",
                finished_at_utc="2026-10-22T10:01:00+00:00",
                duration_seconds=0.5,
                error="conexão recusada",
                exit_code=1,
                failed_operation="backup",
            ),
        ),
    )


class ExecutionStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix=".test-store-", dir=Path.cwd())
        self.db_path = Path(self.temp_dir.name) / "agent.db"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_record_and_history_roundtrip(self) -> None:
        store = ExecutionStore(self.db_path)
        task = TaskSpec(
            id="task-store",
            name="Tarefa",
            client=Client(id="client-store", name="Cliente"),
            device_id="device",
            sources=(Path("/tmp"),),
            exclusions=(),
            compression=CompressionMode.AUTO,
            destinations=(
                Destination(
                    id="local",
                    name="Local",
                    kind=DestinationKind.LOCAL,
                    repository="/tmp/repo-store",
                    password_environment="X",
                ),
            ),
        )
        store.record(task, _fake_result())
        store.record(task, _fake_result(execution_id="exec-2"))

        entries = store.history("task-store")
        store.close()

        self.assertEqual(len(entries), 2)
        entry = entries[0]
        self.assertEqual(entry["status"], "success")
        self.assertEqual(len(entry["destinations"]), 2)
        self.assertEqual(entry["destinations"][0]["snapshot_id"], "snap-1")
        self.assertEqual(entry["destinations"][1]["error"], "conexão recusada")

    def test_history_filters_by_task_and_empty_database(self) -> None:
        store = ExecutionStore(self.db_path)
        self.assertEqual(store.history("nada"), [])
        store.close()


@unittest.skipUnless(shutil.which("restic"), "restic não está instalado")
class AgentIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.password = secrets.token_urlsafe(32)
        self.temp_dir = tempfile.TemporaryDirectory(prefix=".test-agent-", dir=Path.cwd())
        self.root = Path(self.temp_dir.name)
        self.source = self.root / "source"
        (self.source / "docs").mkdir(parents=True)
        (self.source / "docs" / "f.txt").write_text("dados do agente\n")
        self.db_path = self.root / "agent.db"
        self.config_path = self.root / "tarefa.json"
        config = {
            "id": "task-agent",
            "name": "Agente",
            "client": {"id": "client-agent", "name": "Cliente"},
            "device_id": "device-agent",
            "sources": [str(self.source)],
            "exclusions": [],
            "compression": "auto",
            "destinations": [
                {
                    "id": "local",
                    "name": "Local",
                    "kind": "local",
                    "repository": str(self.root / "repo"),
                    "password_environment": "MACIP_AGENT_TEST_PASSWORD",
                }
            ],
        }
        self.config_path.write_text(json.dumps(config))

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_agent_runs_task_and_records_history(self) -> None:
        with patch.dict(
            os.environ, {"MACIP_AGENT_TEST_PASSWORD": self.password}
        ):
            result = run_task(self.config_path, self.db_path)

        self.assertEqual(result.status, ExecutionStatus.SUCCESS)

        entries = history(self.db_path, "task-agent")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["status"], "success")
        self.assertEqual(
            entries[0]["destinations"][0]["snapshot_id"],
            result.destinations[0].snapshot_id,
        )

    def test_history_cli_prints_stored_executions(self) -> None:
        with patch.dict(
            os.environ, {"MACIP_AGENT_TEST_PASSWORD": self.password}
        ):
            run_task(self.config_path, self.db_path)

        stdout = io.StringIO()
        with patch(
            "sys.argv",
            [
                "macip-backup-poc",
                "history",
                "--store",
                str(self.db_path),
                "--task",
                "task-agent",
            ],
        ), patch("sys.stdout", stdout):
            exit_code = cli_main()

        self.assertEqual(exit_code, 0)
        entries = json.loads(stdout.getvalue())
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["task_id"], "task-agent")
