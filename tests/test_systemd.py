"""Agendamento systemd --user: geração de units, lock, interrupção e
homologação real com systemctl --user."""

from __future__ import annotations

import fcntl
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

from macip_backup.agent import run_task
from macip_backup.models import (
    Client,
    CompressionMode,
    Destination,
    DestinationKind,
    TaskSpec,
)
from macip_backup.orchestrator import ExecutionStatus
from macip_backup.restic import ResticAdapter, ResticError
from macip_backup.store import ExecutionStore
from macip_backup.systemd import install_schedule, uninstall_schedule


def _task(task_id: str = "task-systemd") -> TaskSpec:
    return TaskSpec(
        id=task_id,
        name="Tarefa systemd",
        client=Client(id="client-systemd", name="Cliente"),
        device_id="device-systemd",
        sources=(Path("/tmp"),),
        exclusions=(),
        compression=CompressionMode.AUTO,
        destinations=(
            Destination(
                id="local",
                name="Local",
                kind=DestinationKind.LOCAL,
                repository="/tmp/repo-systemd",
                password_environment="X",
            ),
        ),
    )


class UnitGenerationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix=".test-systemd-", dir=Path.cwd())
        self.unit_dir = Path(self.temp_dir.name) / "units"
        self.store_path = Path(self.temp_dir.name) / "agent.db"
        self.config_path = Path(self.temp_dir.name) / "tarefa.json"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _install(self, enable: bool = False) -> dict[str, str]:
        with patch(
            "macip_backup.systemd.unit_directory", return_value=self.unit_dir
        ):
            return install_schedule(
                _task(), self.config_path, self.store_path, "daily", enable=enable
            )

    def test_install_writes_service_and_timer(self) -> None:
        result = self._install()

        service = Path(result["service"]).read_text()
        timer = Path(result["timer"]).read_text()
        self.assertIn("Type=oneshot", service)
        self.assertIn(
            f"-m macip_backup.cli agent {self.config_path.resolve()}",
            service,
        )
        self.assertIn(f"--store {self.store_path.resolve()}", service)
        self.assertIn("EnvironmentFile=-%h/.config/macip-backup/agent.env", service)
        self.assertIn("OnCalendar=daily", timer)
        self.assertIn("Persistent=true", timer)
        self.assertIn("WantedBy=timers.target", timer)

    def test_uninstall_removes_units(self) -> None:
        with patch(
            "macip_backup.systemd.unit_directory", return_value=self.unit_dir
        ):
            self._install()
            result = uninstall_schedule("task-systemd")

        self.assertEqual(len(result["removed"]), 2)
        self.assertFalse((self.unit_dir / "macip-backup-agent-task-systemd.service").exists())

    def test_concurrent_execution_fails_fast_on_lock(self) -> None:
        """flock impede duas execuções — CLI e service usam o mesmo lock."""
        store_path = self.store_path
        config = {
            "id": "task-lock",
            "name": "Lock",
            "client": {"id": "c", "name": "c"},
            "device_id": "d",
            "sources": ["/tmp"],
            "exclusions": [],
            "compression": "auto",
            "destinations": [
                {
                    "id": "local",
                    "name": "Local",
                    "kind": "local",
                    "repository": "/tmp/repo-lock",
                    "password_environment": "X",
                }
            ],
        }
        self.config_path.write_text(json.dumps(config))
        lock_file = Path(f"{store_path}.lock").open("a")
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(ResticError, "em andamento"):
                run_task(self.config_path, store_path)
        finally:
            lock_file.close()


class InterruptedMarkingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix=".test-interrupt-", dir=Path.cwd())
        self.db_path = Path(self.temp_dir.name) / "agent.db"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_stale_running_marked_interrupted_on_next_run(self) -> None:
        store = ExecutionStore(self.db_path)
        store.record_start(_task(), "exec-morta", "2026-10-23T10:00:00+00:00")
        store.attempt_started("exec-morta", "local", 1, "2026-10-23T10:00:01+00:00")
        marked = store.mark_stale_interrupted("2026-10-23T11:00:00+00:00")
        entries = store.history("task-systemd")
        store.close()

        self.assertEqual(marked, 1)
        self.assertEqual(entries[0]["status"], "interrupted")
        self.assertEqual(
            entries[0]["destinations"][0]["attempts"][0]["status"], "interrupted"
        )

    def test_mark_interrupted_scopes_to_one_execution(self) -> None:
        store = ExecutionStore(self.db_path)
        store.record_start(_task(), "exec-a", "2026-10-23T10:00:00+00:00")
        store.record_start(_task(), "exec-b", "2026-10-23T11:00:00+00:00")
        store.mark_interrupted("exec-a", "2026-10-23T12:00:00+00:00")
        entries = store.history("task-systemd")
        store.close()

        statuses = {entry["execution_id"]: entry["status"] for entry in entries}
        self.assertEqual(statuses, {"exec-a": "interrupted", "exec-b": "running"})


def _systemd_user_running() -> bool:
    if shutil.which("systemctl") is None:
        return False
    completed = subprocess.run(
        ["systemctl", "--user", "is-system-running"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    return completed.stdout.strip() in {"running", "degraded"}


@unittest.skipUnless(
    shutil.which("restic") and shutil.which("ssh-keygen"), "restic/ssh-keygen ausentes"
)
class SystemdUserIntegrationTests(unittest.TestCase):
    """Homologação real: service, timer, lock CLI↔serviço, SIGTERM e restore."""

    @classmethod
    def setUpClass(cls) -> None:
        if not _systemd_user_running():
            raise unittest.SkipTest("systemd --user não está ativo")

    def setUp(self) -> None:
        self.suffix = secrets.token_hex(4)
        self.task_id = f"task-sd-{self.suffix}"
        self.unit_name = f"macip-backup-agent-{self.task_id}"
        self.password = secrets.token_urlsafe(24)
        self.keyring_name = f"MACIP_SD_TEST_{self.suffix}"
        self.temp_dir = tempfile.TemporaryDirectory(prefix=".test-sd-", dir=Path.cwd())
        self.root = Path(self.temp_dir.name)
        self.source = self.root / "source"
        (self.source / "docs").mkdir(parents=True)
        (self.source / "docs" / "f.txt").write_text("dados via systemd\n")
        self.db_path = self.root / "agent.db"
        self.config_path = self.root / "tarefa.json"
        subprocess.run(
            ["keyctl", "padd", "user", f"macip.{self.keyring_name}", "@u"],
            input=self.password,
            check=True,
            capture_output=True,
            text=True,
        )
        self._write_config()

    def tearDown(self) -> None:
        uninstall_schedule(self.task_id)
        key_id = subprocess.run(
            ["keyctl", "search", "@u", "user", f"macip.{self.keyring_name}"],
            capture_output=True,
            text=True,
        ).stdout.strip()
        if key_id:
            subprocess.run(["keyctl", "revoke", key_id], check=False, capture_output=True)
        self.temp_dir.cleanup()

    def _write_config(self, destinations: list[dict[str, object]] | None = None) -> None:
        self.config_path.write_text(
            json.dumps(
                {
                    "id": self.task_id,
                    "name": "Systemd",
                    "client": {"id": "client-sd", "name": "Cliente"},
                    "device_id": "device-sd",
                    "sources": [str(self.source)],
                    "exclusions": [],
                    "compression": "auto",
                    "timeouts": {"initialize_seconds": 90},
                    "destinations": destinations
                    or [
                        {
                            "id": "local",
                            "name": "Local",
                            "kind": "local",
                            "repository": str(self.root / "repo"),
                            "password_environment": self.keyring_name,
                        }
                    ],
                }
            )
        )

    def _systemctl(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["systemctl", "--user", *args],
            capture_output=True,
            text=True,
            timeout=300,
        )

    def _history(self) -> list[dict[str, object]]:
        store = ExecutionStore(self.db_path)
        try:
            return store.history(self.task_id)
        finally:
            store.close()

    def test_service_runs_and_snapshot_restores(self) -> None:
        # credencial existe SÓ no kernel keyring @u — nada de ambiente
        install_schedule(
            TaskSpec.from_dict(json.loads(self.config_path.read_text())),
            self.config_path,
            self.db_path,
            "daily",
        )
        self._systemctl("daemon-reload")

        completed = self._systemctl("start", f"{self.unit_name}.service")
        self.assertEqual(
            completed.returncode, 0, f"{completed.stdout}{completed.stderr}"
        )

        entries = self._history()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["status"], "success")
        snapshot_id = entries[0]["destinations"][0]["snapshot_id"]

        # restauração real a partir do snapshot criado pelo serviço
        destination = Destination(
            id="local",
            name="Local",
            kind=DestinationKind.LOCAL,
            repository=str(self.root / "repo"),
            password_environment="IGNORED",
        )
        restored = self.root / "restored"
        ResticAdapter().restore(
            destination, self.password, snapshot_id or "", restored,
            source_subfolder=self.source,
        )
        restored_files = list(restored.rglob("f.txt"))
        self.assertEqual(len(restored_files), 1)
        self.assertEqual(restored_files[0].read_text(), "dados via systemd\n")

    def test_timer_fires_execution_automatically(self) -> None:
        install_schedule(
            TaskSpec.from_dict(json.loads(self.config_path.read_text())),
            self.config_path,
            self.db_path,
            "*-*-* *:*:0/10",
            enable=True,
        )
        try:
            deadline = time.monotonic() + 60
            entries: list[dict[str, object]] = []
            while time.monotonic() < deadline:
                entries = self._history()
                if entries and entries[0]["status"] not in {"running"}:
                    break
                time.sleep(1)
            self.assertTrue(entries, "timer não disparou nenhuma execução")
            self.assertEqual(entries[0]["status"], "success")
        finally:
            self._systemctl("stop", f"{self.unit_name}.timer")

    def test_sigterm_marks_execution_interrupted(self) -> None:
        # destino SFTP travado em listener mudo → tentativa fica running
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
        held: list[socket.socket] = []

        def _never_respond() -> None:
            try:
                while True:
                    held.append(listener.accept()[0])
            except OSError:
                return

        threading.Thread(target=_never_respond, daemon=True).start()
        self._write_config(
            [
                {
                    "id": "hung",
                    "name": "Travado",
                    "kind": "sftp",
                    "password_environment": self.keyring_name,
                    "sftp": {
                        "host": "127.0.0.1",
                        "port": listener.getsockname()[1],
                        "user": "backup",
                        "remote_path": "backups/teste",
                        "identity_file": str(identity),
                        "known_hosts_file": str(known_hosts),
                        "connect_timeout_seconds": 5,
                    },
                }
            ]
        )
        try:
            install_schedule(
                TaskSpec.from_dict(json.loads(self.config_path.read_text())),
                self.config_path,
                self.db_path,
                "daily",
            )
            self._systemctl("daemon-reload")
            self._systemctl("start", "--no-block", f"{self.unit_name}.service")

            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                try:
                    row = sqlite3.connect(self.db_path).execute(
                        "SELECT status FROM attempts"
                    ).fetchone()
                except sqlite3.OperationalError:
                    row = None
                if row:
                    break
                time.sleep(0.1)

            # lock compartilhado: CLI recusa execução concorrente
            with self.assertRaisesRegex(ResticError, "em andamento"):
                run_task(self.config_path, self.db_path)

            self._systemctl("kill", "-s", "SIGTERM", f"{self.unit_name}.service")
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                entries = self._history()
                if entries and entries[0]["status"] == "interrupted":
                    break
                time.sleep(0.2)

            entries = self._history()
            self.assertEqual(entries[0]["status"], "interrupted")
            self.assertEqual(
                entries[0]["destinations"][0]["attempts"][0]["status"],
                "interrupted",
            )
        finally:
            listener.close()
            for connection in held:
                connection.close()
