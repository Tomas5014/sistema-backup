"""Agente local: persistência SQLite, histórico e credenciais do SO."""

from __future__ import annotations

import io
import json
import os
import secrets
import shutil
import subprocess
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
    AttemptResult,
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
    attempts = (
        AttemptResult(
            attempt=1,
            status=DestinationStatus.FAILED,
            started_at_utc="2026-10-22T10:00:00+00:00",
            finished_at_utc="2026-10-22T10:00:30+00:00",
            duration_seconds=0.5,
            retryable=True,
            error="conexão recusada",
            failed_operation="backup",
        ),
        AttemptResult(
            attempt=2,
            status=DestinationStatus.SUCCESS,
            started_at_utc="2026-10-22T10:00:30+00:00",
            finished_at_utc="2026-10-22T10:01:00+00:00",
            duration_seconds=1.0,
            retryable=False,
            snapshot_id="snap-1",
        ),
    )
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
                attempts=attempts,
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
                attempts=(attempts[0],),
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
        for result in (_fake_result(), _fake_result(execution_id="exec-2")):
            store.record_start(
                task, result.execution_id, result.started_at_utc
            )
            store.record_finish(result)

        entries = store.history("task-store")
        store.close()

        self.assertEqual(len(entries), 2)
        entry = entries[0]
        self.assertEqual(entry["status"], "success")
        self.assertEqual(len(entry["destinations"]), 2)
        self.assertEqual(entry["destinations"][0]["snapshot_id"], "snap-1")
        self.assertEqual(entry["destinations"][1]["error"], "conexão recusada")
        attempts = entry["destinations"][0]["attempts"]
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0]["status"], "failed")
        self.assertEqual(attempts[1]["status"], "success")
        self.assertEqual(attempts[1]["snapshot_id"], "snap-1")

    def test_interrupted_execution_stays_identifiable(self) -> None:
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
        store.record_start(
            task, "exec-interrompida", "2026-10-22T12:00:00+00:00"
        )
        # execução morta antes de concluir: nenhum record_finish.

        entries = store.history("task-store")
        store.close()

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["status"], "running")
        self.assertEqual(entries[0]["execution_id"], "exec-interrompida")
        self.assertIsNone(entries[0]["finished_at_utc"])

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
        attempts = entries[0]["destinations"][0]["attempts"]
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["status"], "success")

    def test_agent_persists_log_events_and_never_secrets(self) -> None:
        secret = f"senha-secreta-{secrets.token_hex(8)}"
        with patch.dict(os.environ, {"MACIP_AGENT_TEST_PASSWORD": secret}):
            result = run_task(self.config_path, self.db_path)

        self.assertEqual(result.status, ExecutionStatus.SUCCESS)
        log_path = (
            self.db_path.parent / "logs" / f"{result.execution_id}.jsonl"
        )
        self.assertTrue(log_path.exists())
        log_content = log_path.read_text()
        self.assertIn("backup.execution.started", log_content)
        self.assertIn("backup.execution.finished", log_content)
        self.assertNotIn(secret, log_content)
        self.assertNotIn(secret, self.db_path.read_bytes().decode("utf-8", "ignore"))

    @unittest.skipUnless(shutil.which("keyctl"), "keyctl não está instalado")
    def test_recovery_works_with_credential_only_in_keyring(self) -> None:
        name = f"MACIP_KEYRING_RECOVERY_{secrets.token_hex(6)}"
        key_name = f"macip.{name}"
        subprocess.run(
            ["keyctl", "padd", "user", key_name, "@s"],
            input=self.password,
            check=True,
            capture_output=True,
            text=True,
        )
        minimal_env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
        }
        try:
            config = json.loads(self.config_path.read_text())
            config["destinations"][0]["password_environment"] = name
            self.config_path.write_text(json.dumps(config))

            with patch.dict(os.environ, minimal_env, clear=True):
                result = run_task(self.config_path, self.db_path)
            self.assertEqual(result.status, ExecutionStatus.SUCCESS)

            stdout = io.StringIO()
            with patch.dict(os.environ, minimal_env, clear=True), patch(
                "sys.argv",
                ["macip-backup-poc", "snapshots", str(self.config_path), "local"],
            ), patch("sys.stdout", stdout):
                exit_code = cli_main()
            self.assertEqual(exit_code, 0)
            self.assertIn(result.destinations[0].snapshot_id, stdout.getvalue())
        finally:
            key_id = subprocess.run(
                ["keyctl", "search", "@s", "user", key_name],
                capture_output=True,
                text=True,
            ).stdout.strip()
            if key_id:
                subprocess.run(
                    ["keyctl", "revoke", key_id], check=False, capture_output=True
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
