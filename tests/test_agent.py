"""Agente local: persistência SQLite, histórico e credenciais do SO."""

from __future__ import annotations

import io
import json
import os
import secrets
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
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


def _store_task() -> TaskSpec:
    return TaskSpec(
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

    def test_incremental_writes_visible_while_running(self) -> None:
        store = ExecutionStore(self.db_path)
        task = self._task()
        store.record_start(task, "exec-parcial", "2026-10-22T12:00:00+00:00")
        store.attempt_started(
            "exec-parcial", "local", 1, "2026-10-22T12:00:01+00:00"
        )
        store.attempt_snapshot("exec-parcial", "local", 1, "snap-parcial")

        entries = store.history("task-store")
        self.assertEqual(entries[0]["status"], "running")
        destination = entries[0]["destinations"][0]
        self.assertEqual(destination["status"], "running")
        attempt = destination["attempts"][0]
        self.assertEqual(attempt["status"], "running")
        self.assertEqual(attempt["snapshot_id"], "snap-parcial")
        self.assertIsNone(attempt["finished_at_utc"])

        store.attempt_finished(
            "exec-parcial",
            "local",
            AttemptResult(
                attempt=1,
                status=DestinationStatus.SUCCESS,
                started_at_utc="2026-10-22T12:00:01+00:00",
                finished_at_utc="2026-10-22T12:00:30+00:00",
                duration_seconds=29.0,
                retryable=False,
                snapshot_id="snap-parcial",
            ),
        )
        attempt = store.history("task-store")[0]["destinations"][0]["attempts"][0]
        store.close()
        self.assertEqual(attempt["status"], "success")
        self.assertEqual(attempt["snapshot_id"], "snap-parcial")

    def _task(self) -> TaskSpec:
        return _store_task()


class ExecutionStoreMigrationTests(unittest.TestCase):
    """Banco criado no esquema legado (v0, finished_at_utc NOT NULL)."""

    LEGACY_SCHEMA = """
    CREATE TABLE executions (
        execution_id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL,
        client_id TEXT NOT NULL,
        status TEXT NOT NULL,
        started_at_utc TEXT NOT NULL,
        finished_at_utc TEXT NOT NULL
    );
    CREATE TABLE destination_results (
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
    CREATE INDEX idx_executions_task ON executions(task_id, started_at_utc);
    """

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix=".test-migration-", dir=Path.cwd())
        self.db_path = Path(self.temp_dir.name) / "agent.db"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _legacy_database(self) -> None:
        connection = sqlite3.connect(self.db_path)
        try:
            connection.executescript(self.LEGACY_SCHEMA)
            connection.execute(
                "INSERT INTO executions VALUES (?, ?, ?, ?, ?, ?)",
                ("exec-antiga", "task-store", "client-store", "success",
                 "2026-10-20T09:00:00+00:00", "2026-10-20T09:01:00+00:00"),
            )
            connection.execute(
                "INSERT INTO destination_results VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("exec-antiga", "local", "success", "snap-antigo", 5, 3,
                 1000, None, None, None, None, 12.5),
            )
            connection.commit()
        finally:
            connection.close()

    def test_migrates_legacy_schema_preserving_history(self) -> None:
        self._legacy_database()

        store = ExecutionStore(self.db_path)
        version = store._connection.execute("PRAGMA user_version").fetchone()[0]
        self.assertEqual(version, 4)
        entries = store.history("task-store")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["execution_id"], "exec-antiga")
        self.assertEqual(
            entries[0]["destinations"][0]["snapshot_id"], "snap-antigo"
        )
        # índice e chaves estrangeiras sobrevivem aos rebuilds
        indexes = store._connection.execute(
            "PRAGMA index_list(executions)"
        ).fetchall()
        self.assertTrue(any("idx_executions_task" in row for row in indexes))
        foreign_keys = store._connection.execute(
            "PRAGMA foreign_key_list(destination_results)"
        ).fetchall()
        self.assertTrue(any(row[2] == "executions" for row in foreign_keys))

        # execução running + conclusão funcionam no esquema migrado
        store.record_start(
            _store_task(), "exec-nova", "2026-10-22T12:00:00+00:00"
        )
        self.assertEqual(
            store.history("task-store")[0]["status"], "running"
        )
        store.record_finish(
            _fake_result(execution_id="exec-nova", status="partial")
        )
        store.close()

        # reabrir não repete nem danifica a migração
        store = ExecutionStore(self.db_path)
        version = store._connection.execute("PRAGMA user_version").fetchone()[0]
        self.assertEqual(version, 4)
        entries = store.history("task-store")
        store.close()
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["status"], "partial")
        self.assertEqual(entries[1]["status"], "success")

    def test_failed_migration_rolls_back_and_later_retry_succeeds(self) -> None:
        self._legacy_database()
        created_connections: list[_FlakyConnection] = []
        real_connect = sqlite3.connect

        class _FlakyConnection:
            """Aborta no ALTER...RENAME — depois do DROP TABLE."""

            def __init__(self, path: str) -> None:
                self._inner = real_connect(path)
                self.closed = False
                created_connections.append(self)

            def __getattr__(self, name: str) -> object:
                return getattr(self._inner, name)

            def execute(self, sql: str, *args: object) -> object:
                if "RENAME" in str(sql).upper():
                    raise sqlite3.OperationalError("falha injetada após DROP")
                return self._inner.execute(sql, *args)

            def close(self) -> None:
                self.closed = True
                self._inner.close()

        with patch(
            "macip_backup.store.sqlite3.connect",
            side_effect=lambda path, **kw: _FlakyConnection(path),
        ):
            with self.assertRaisesRegex(
                sqlite3.OperationalError, "falha injetada"
            ):
                ExecutionStore(self.db_path)

        # store fecha a conexão quando a inicialização falha
        self.assertTrue(created_connections[0].closed)

        # Reabertura com conexão nova: esquema, versão e histórico intactos.
        connection = sqlite3.connect(self.db_path)
        try:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            self.assertEqual(version, 0)
            rows = connection.execute(
                "SELECT execution_id, status FROM executions"
            ).fetchall()
            self.assertEqual(rows, [("exec-antiga", "success")])
            orphan = connection.execute(
                "SELECT name FROM sqlite_master WHERE name = 'executions_v1'"
            ).fetchall()
            self.assertEqual(orphan, [])
        finally:
            connection.close()

        # Após a falha, uma nova migração conclui normalmente.
        store = ExecutionStore(self.db_path)
        version = store._connection.execute("PRAGMA user_version").fetchone()[0]
        self.assertEqual(version, 4)
        entries = store.history("task-store")
        store.close()
        self.assertEqual(entries[0]["execution_id"], "exec-antiga")
        self.assertEqual(
            entries[0]["destinations"][0]["snapshot_id"], "snap-antigo"
        )

    def test_future_schema_version_is_rejected_untouched(self) -> None:
        connection = sqlite3.connect(self.db_path)
        connection.executescript(
            "CREATE TABLE executions (execution_id TEXT PRIMARY KEY);"
        )
        connection.execute("INSERT INTO executions VALUES ('x')")
        connection.execute("PRAGMA user_version = 99")
        connection.commit()
        connection.close()

        from macip_backup.models import ConfigurationError

        with self.assertRaisesRegex(ConfigurationError, "não suportado"):
            ExecutionStore(self.db_path)

        connection = sqlite3.connect(self.db_path)
        try:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            self.assertEqual(version, 99)
            rows = connection.execute("SELECT * FROM executions").fetchall()
            self.assertEqual(rows, [("x",)])
        finally:
            connection.close()


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

    @unittest.skipUnless(
        shutil.which("restic") and shutil.which("ssh") and shutil.which("ssh-keygen"),
        "restic/ssh/ssh-keygen ausentes",
    )
    def test_killed_agent_keeps_persisted_attempts(self) -> None:
        """Morte abrupta no meio de uma tentativa: histórico sobrevive.

        Destino 1 (local) conclui com snapshot; destino 2 (SFTP) aponta
        para um listener TCP que aceita e nunca responde — a tentativa
        trava no handshake e é morta via SIGKILL no process group.
        """
        identity = self.root / "id_teste"
        subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-f", str(identity), "-N", "", "-q"],
            check=True,
        )
        public_key = identity.with_suffix(".pub").read_text().split()[1]
        known_hosts = self.root / "known_hosts"
        known_hosts.write_text(f"127.0.0.1 ssh-ed25519 {public_key}\n")

        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        held_connections: list[socket.socket] = []

        def _never_respond() -> None:
            try:
                while True:
                    connection, _ = listener.accept()
                    held_connections.append(connection)
            except OSError:
                return

        threading.Thread(target=_never_respond, daemon=True).start()

        config = {
            "id": "task-kill",
            "name": "Interrupção",
            "client": {"id": "client-agent", "name": "Cliente"},
            "device_id": "device-agent",
            "sources": [str(self.source)],
            "exclusions": [],
            "compression": "auto",
            "timeouts": {"initialize_seconds": 120},
            "destinations": [
                {
                    "id": "local",
                    "name": "Local",
                    "kind": "local",
                    "repository": str(self.root / "repo-local"),
                    "password_environment": "MACIP_KILL_TEST_PASSWORD",
                },
                {
                    "id": "hung-sftp",
                    "name": "Travado",
                    "kind": "sftp",
                    "password_environment": "MACIP_KILL_TEST_PASSWORD",
                    "sftp": {
                        "host": "127.0.0.1",
                        "port": port,
                        "user": "backup",
                        "remote_path": "backups/teste",
                        "identity_file": str(identity),
                        "known_hosts_file": str(known_hosts),
                        "connect_timeout_seconds": 5,
                    },
                },
            ],
        }
        self.config_path.write_text(json.dumps(config))
        environment = {
            "PYTHONPATH": str(Path(__file__).parents[1] / "src"),
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
            "MACIP_KILL_TEST_PASSWORD": self.password,
        }
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import sys; from pathlib import Path; "
                "from macip_backup.agent import run_task; "
                "run_task(Path(sys.argv[1]), Path(sys.argv[2]))",
                str(self.config_path),
                str(self.db_path),
            ],
            env=environment,
            start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 90
            hung_attempt_running = False
            while time.monotonic() < deadline:
                try:
                    connection = sqlite3.connect(self.db_path)
                    row = connection.execute(
                        "SELECT status FROM attempts "
                        "WHERE destination_id = 'hung-sftp'"
                    ).fetchone()
                    connection.close()
                except sqlite3.OperationalError:
                    row = None
                if row:
                    hung_attempt_running = row[0] == "running"
                    break
                if process.poll() is not None:
                    break
                time.sleep(0.05)
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=30)
            listener.close()
            for connection in held_connections:
                connection.close()

        self.assertTrue(hung_attempt_running)

        # Banco reaberto após morte abrupta: o que foi gravado sobrevive.
        entries = history(self.db_path, "task-kill")
        self.assertEqual(len(entries), 1)
        execution = entries[0]
        self.assertEqual(execution["status"], "running")
        self.assertIsNone(execution["finished_at_utc"])

        destinations = {item["destination_id"]: item for item in execution["destinations"]}
        local = destinations["local"]
        self.assertEqual(local["status"], "success")
        self.assertTrue(local["snapshot_id"])
        self.assertEqual(local["attempts"][0]["status"], "success")

        hung = destinations["hung-sftp"]
        self.assertEqual(hung["status"], "running")
        self.assertEqual(hung["attempts"][0]["status"], "running")
        self.assertIsNone(hung["attempts"][0]["finished_at_utc"])

        log_files = list((self.db_path.parent / "logs").glob("*.jsonl"))
        self.assertEqual(len(log_files), 1)
        self.assertIn("backup.destination.started", log_files[0].read_text())
