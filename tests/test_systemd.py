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
from macip_backup.models import ConfigurationError
from macip_backup.systemd import install_schedule, uninstall_schedule


def _lock_config(task_id: str, root: Path | None = None) -> dict[str, object]:
    root = root or Path("/tmp")
    return {
        "id": task_id,
        "name": "Lock",
        "client": {"id": "c", "name": "c"},
        "device_id": "d",
        "sources": [str(root / "docs")],
        "exclusions": [],
        "compression": "auto",
        "destinations": [
            {
                "id": "local",
                "name": "Local",
                "kind": "local",
                "repository": str(root / "repo-lock"),
                "password_environment": "X",
            }
        ],
    }


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
        self.lock_dir = Path(self.temp_dir.name) / "locks"
        self._previous_lock_env = os.environ.get("MACIP_LOCK_DIR")
        os.environ["MACIP_LOCK_DIR"] = str(self.lock_dir)

    def tearDown(self) -> None:
        if self._previous_lock_env is None:
            os.environ.pop("MACIP_LOCK_DIR", None)
        else:
            os.environ["MACIP_LOCK_DIR"] = self._previous_lock_env
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
            f'agent "{self.config_path.resolve()}"',
            service,
        )
        self.assertIn(f'--store "{self.store_path.resolve()}"', service)
        self.assertIn("EnvironmentFile=-%h/.config/macip-backup/agent.env", service)
        self.assertIn("OnCalendar=daily", timer)
        self.assertIn("Persistent=true", timer)
        self.assertIn("WantedBy=timers.target", timer)

    def test_environment_file_with_spaced_config_dir_is_unquoted(self) -> None:
        """EnvironmentFile trata a linha inteira como filename: o caminho
        vai literal, SEM aspas — `-"<path>"` era ignorado pelo systemd
        (bug homologado na VM do piloto)."""
        config_dir = Path(self.temp_dir.name) / "cfg com espaços"
        with patch.dict(os.environ, {"MACIP_CONFIG": str(config_dir)}):
            result = self._install()
        service = Path(result["service"]).read_text()
        line = next(
            l for l in service.splitlines() if l.startswith("EnvironmentFile")
        )
        self.assertEqual(line, f"EnvironmentFile=-{config_dir}/agent.env")
        self.assertNotIn('"', line)

        # systemd-analyze verify não pode acusar o caminho.
        verify = subprocess.run(
            ["systemd-analyze", "verify", result["service"]],
            capture_output=True,
            text=True,
        )
        self.assertNotIn("path is not absolute", verify.stderr + verify.stdout)

    def test_paths_with_spaces_are_escaped(self) -> None:
        spaced = Path(self.temp_dir.name) / "dir com espaços"
        spaced.mkdir()
        config = spaced / "tarefa backup.json"
        config.write_text("{}")
        store = spaced / "agent backup.db"
        with patch(
            "macip_backup.systemd.unit_directory", return_value=self.unit_dir
        ):
            result = install_schedule(_task(), config, store, "daily")
        service = Path(result["service"]).read_text()
        self.assertIn(f'agent "{config.resolve()}"', service)
        self.assertIn(f'--store "{store.resolve()}"', service)

    def test_invalid_calendar_does_not_touch_existing_units(self) -> None:
        result = self._install()
        original = Path(result["timer"]).read_text()
        with patch(
            "macip_backup.systemd.unit_directory", return_value=self.unit_dir
        ), self.assertRaisesRegex(ConfigurationError, "OnCalendar"):
            install_schedule(
                _task(), self.config_path, self.store_path, "isto nao eh calendario"
            )
        self.assertEqual(Path(result["timer"]).read_text(), original)

    def test_uninstall_removes_units(self) -> None:
        with patch(
            "macip_backup.systemd.unit_directory", return_value=self.unit_dir
        ):
            self._install()
            result = uninstall_schedule("task-systemd")

        self.assertEqual(len(result["removed"]), 2)
        self.assertFalse((self.unit_dir / "macip-backup-agent-task-systemd.service").exists())

    def test_first_run_creates_state_directories(self) -> None:
        """Store em diretório inexistente: lock e DB criados na ordem."""
        deep_store = Path(self.temp_dir.name) / "a" / "b" / "c" / "agent.db"
        (Path(self.temp_dir.name) / "docs").mkdir()
        self.config_path.write_text(
            json.dumps(_lock_config("task-lock", Path(self.temp_dir.name)))
        )
        result = run_task(self.config_path, deep_store)
        self.assertTrue(deep_store.exists())
        self.assertTrue((self.lock_dir / "task-lock.lock").exists())

    def test_concurrent_execution_fails_fast_on_lock(self) -> None:
        """flock por tarefa — compartilhado por agent, run e service."""
        self.config_path.write_text(
            json.dumps(_lock_config("task-lock", Path(self.temp_dir.name)))
        )
        self.lock_dir.mkdir(parents=True, exist_ok=True)
        lock_file = (self.lock_dir / "task-lock.lock").open("a")
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(ResticError, "em andamento"):
                run_task(self.config_path, self.store_path)
        finally:
            lock_file.close()


class InterruptedMarkingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix=".test-interrupt-", dir=Path.cwd())
        self.db_path = Path(self.temp_dir.name) / "agent.db"
        self.lock_dir = Path(self.temp_dir.name) / "locks"
        self._previous_lock_env = os.environ.get("MACIP_LOCK_DIR")
        os.environ["MACIP_LOCK_DIR"] = str(self.lock_dir)

    def tearDown(self) -> None:
        if self._previous_lock_env is None:
            os.environ.pop("MACIP_LOCK_DIR", None)
        else:
            os.environ["MACIP_LOCK_DIR"] = self._previous_lock_env
        self.temp_dir.cleanup()

    def test_sweep_preserves_running_of_locked_task_and_marks_orphan(self) -> None:
        """A e B no mesmo SQLite: iniciar B preserva A (lock ocupado);
        execução realmente órfã (lock livre) é identificada."""
        from macip_backup.agent import _sweep_stale_executions, acquire_task_lock

        store = ExecutionStore(self.db_path)
        task_a = _task("task-A")
        task_b = _task("task-B")
        task_c = _task("task-C")
        store.record_start(task_a, "exec-A", "2026-10-23T10:00:00+00:00")
        store.record_start(task_b, "exec-B-velha", "2026-10-23T09:00:00+00:00")
        store.record_start(task_c, "exec-C-orfa", "2026-10-23T09:30:00+00:00")

        # task A está realmente em execução — seu lock está ocupado.
        locks_a = acquire_task_lock("task-A")
        try:
            _sweep_stale_executions(store, "task-B")
        finally:
            for lock_fd in locks_a:
                lock_fd.close()

        statuses = {
            entry["execution_id"]: entry["status"]
            for entry in store.history()
            for _ in [entry]
        }
        store.close()
        self.assertEqual(statuses["exec-A"], "running")          # lock ocupado → intacta
        self.assertEqual(statuses["exec-B-velha"], "interrupted")  # lock próprio → órfã
        self.assertEqual(statuses["exec-C-orfa"], "interrupted")   # lock livre → órfã

    def test_mark_interrupted_scopes_to_one_execution(self) -> None:
        store = ExecutionStore(self.db_path)
        store.record_start(_task(), "exec-a", "2026-10-23T10:00:00+00:00")
        store.record_start(_task(), "exec-b", "2026-10-23T11:00:00+00:00")
        store.mark_interrupted("exec-a", "2026-10-23T12:00:00+00:00", "sinal 15")
        entries = store.history("task-systemd")
        store.close()

        statuses = {entry["execution_id"]: entry["status"] for entry in entries}
        self.assertEqual(statuses, {"exec-a": "interrupted", "exec-b": "running"})
        details = {entry["execution_id"]: entry["interrupt_detail"] for entry in entries}
        self.assertEqual(details["exec-a"], "sinal 15")


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

    def test_service_loads_credential_from_environment_file(self) -> None:
        """Credencial SOMENTE em agent.env (dir com espaços) — sem
        variável herdada, sem keyring. Homologa a correção do
        EnvironmentFile: backup deve ter sucesso e o journal não pode
        acusar 'path is not absolute'."""
        env_name = f"MACIP_ENVFILE_PW_{self.suffix}"
        config_dir = self.root / "cfg com espaços"
        config_dir.mkdir()
        env_file = config_dir / "agent.env"
        env_file.write_text(f"{env_name}={self.password}\n")
        env_file.chmod(0o600)

        task_dict = json.loads(self.config_path.read_text())
        task_dict["destinations"][0]["password_environment"] = env_name
        self.config_path.write_text(json.dumps(task_dict))

        # garante ausência no keyring — a única fonte é o arquivo
        self.assertNotIn(env_name, os.environ)
        search = subprocess.run(
            ["keyctl", "search", "@u", "user", f"macip.{env_name}"],
            capture_output=True, text=True,
        )
        self.assertNotEqual(search.returncode, 0)

        with patch.dict(os.environ, {"MACIP_CONFIG": str(config_dir)}):
            install_schedule(
                TaskSpec.from_dict(json.loads(self.config_path.read_text())),
                self.config_path,
                self.db_path,
                "daily",
            )
        service_text = (
            Path.home() / ".config" / "systemd" / "user"
            / f"{self.unit_name}.service"
        ).read_text()
        envfile_line = next(
            l for l in service_text.splitlines() if l.startswith("EnvironmentFile")
        )
        self.assertEqual(envfile_line, f"EnvironmentFile=-{env_file}")

        self._systemctl("daemon-reload")
        completed = self._systemctl("start", f"{self.unit_name}.service")
        self.assertEqual(
            completed.returncode, 0, f"{completed.stdout}{completed.stderr}"
        )
        entries = self._history()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["status"], "success")

        journal = subprocess.run(
            ["journalctl", "--user", "--user-unit", f"{self.unit_name}.service",
             "--no-pager", "-q"],
            capture_output=True, text=True, timeout=60,
        )
        self.assertNotIn("path is not absolute", journal.stdout + journal.stderr)

    def test_service_with_spaced_paths_receives_correct_arguments(self) -> None:
        """ExecStart escapado: o service recebe exatamente os caminhos gerados."""
        spaced = self.root / "dir com espaços"
        spaced.mkdir()
        config = spaced / "tarefa backup.json"
        config.write_text(self.config_path.read_text())
        store = spaced / "agent backup.db"
        install_schedule(
            TaskSpec.from_dict(json.loads(config.read_text())),
            config,
            store,
            "daily",
        )
        self._systemctl("daemon-reload")
        completed = self._systemctl("start", f"{self.unit_name}.service")
        self.assertEqual(
            completed.returncode, 0, f"{completed.stdout}{completed.stderr}"
        )
        store_obj = ExecutionStore(store)
        try:
            entries = store_obj.history(self.task_id)
        finally:
            store_obj.close()
        self.assertEqual(entries[0]["status"], "success")

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
                    connection = sqlite3.connect(self.db_path)
                    row = connection.execute("SELECT status FROM attempts").fetchone()
                    connection.close()
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
            attempt = entries[0]["destinations"][0]["attempts"][0]
            self.assertEqual(attempt["status"], "failed")
            self.assertIn("sinal 15", attempt["error"] or "")
        finally:
            listener.close()
            for connection in held:
                connection.close()


@unittest.skipUnless(
    shutil.which("restic") and shutil.which("ssh-keygen") and shutil.which("ssh"),
    "restic/ssh-keygen/ssh ausentes",
)
class AgentProcessTests(unittest.TestCase):
    """Colisão e cancelamento no processo agente real — sem cgroup do systemd."""

    def setUp(self) -> None:
        self.suffix = secrets.token_hex(4)
        self.task_id = f"task-cx-{self.suffix}"
        self.temp_dir = tempfile.TemporaryDirectory(prefix=".test-cx-", dir=Path.cwd())
        self.root = Path(self.temp_dir.name)
        self.source = self.root / "source"
        self.source.mkdir(parents=True)
        (self.source / "f.txt").write_text("dados\n")
        self.db_path = self.root / "agent.db"
        self.config_path = self.root / "tarefa.json"
        self.lock_dir = self.root / "locks"
        self.password = secrets.token_urlsafe(24)

        identity = self.root / "id_teste"
        subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-f", str(identity), "-N", "", "-q"],
            check=True,
        )
        public_key = identity.with_suffix(".pub").read_text().split()[1]
        known_hosts = self.root / "known_hosts"
        known_hosts.write_text(f"127.0.0.1 ssh-ed25519 {public_key}\n")
        self.identity = identity
        self.known_hosts = known_hosts

        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen()
        self.held: list[socket.socket] = []
        threading.Thread(target=self._never_respond, daemon=True).start()

    def tearDown(self) -> None:
        self.listener.close()
        for connection in self.held:
            connection.close()
        self.temp_dir.cleanup()

    def _never_respond(self) -> None:
        try:
            while True:
                self.held.append(self.listener.accept()[0])
        except OSError:
            return

    def _write_hung_config(self) -> None:
        self.config_path.write_text(
            json.dumps(
                {
                    "id": self.task_id,
                    "name": "Concorrência",
                    "client": {"id": "client-cx", "name": "Cliente"},
                    "device_id": "device-cx",
                    "sources": [str(self.source)],
                    "exclusions": [],
                    "compression": "auto",
                    "timeouts": {"initialize_seconds": 120},
                    "destinations": [
                        {
                            "id": "hung",
                            "name": "Travado",
                            "kind": "sftp",
                            "password_environment": "MACIP_CX_PASSWORD",
                            "sftp": {
                                "host": "127.0.0.1",
                                "port": self.listener.getsockname()[1],
                                "user": "backup",
                                "remote_path": f"backups/{self.task_id}",
                                "identity_file": str(self.identity),
                                "known_hosts_file": str(self.known_hosts),
                                "connect_timeout_seconds": 5,
                            },
                        }
                    ],
                }
            )
        )

    def _env(self) -> dict[str, str]:
        return {
            "PYTHONPATH": str(Path(__file__).parents[1] / "src"),
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
            "MACIP_LOCK_DIR": str(self.lock_dir),
            "MACIP_CX_PASSWORD": self.password,
        }

    def _start_agent(self) -> subprocess.Popen:
        return subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import sys; from pathlib import Path; "
                "from macip_backup.agent import run_task; "
                "run_task(Path(sys.argv[1]), Path(sys.argv[2]))",
                str(self.config_path),
                str(self.db_path),
            ],
            env=self._env(),
        )

    def _wait_attempt_running(
        self, process: subprocess.Popen, destination_id: str | None = None
    ) -> bool:
        deadline = time.monotonic() + 60
        query = "SELECT status FROM attempts"
        params: tuple[str, ...] = ()
        if destination_id is not None:
            query += " WHERE destination_id = ?"
            params = (destination_id,)
        while time.monotonic() < deadline:
            try:
                connection = sqlite3.connect(self.db_path)
                row = connection.execute(query, params).fetchone()
                connection.close()
            except sqlite3.OperationalError:
                row = None
            if row:
                return True
            if process.poll() is not None:
                return False
            time.sleep(0.05)
        return False

    def test_cli_run_collides_with_agent_on_same_task(self) -> None:
        """Colisão real: agent rodando → `run` da mesma tarefa recusa."""
        self._write_hung_config()
        agent = self._start_agent()
        try:
            self.assertTrue(self._wait_attempt_running(agent))

            run = subprocess.run(
                [sys.executable, "-m", "macip_backup.cli", "run", str(self.config_path)],
                env=self._env(),
                capture_output=True,
                text=True,
                timeout=60,
            )
            self.assertNotEqual(run.returncode, 0)
            self.assertIn("em andamento", run.stderr + run.stdout)
        finally:
            if agent.poll() is None:
                agent.kill()
            agent.wait(timeout=30)

    def test_sigterm_only_to_agent_pid_cleans_restic_group(self) -> None:
        """SIGTERM só no PID do agente: restic+ssh encerrados, lock liberado
        somente após a limpeza, execução marcada interrupted."""
        self._write_hung_config()
        agent = self._start_agent()
        self.assertTrue(self._wait_attempt_running(agent))

        os.kill(agent.pid, signal.SIGTERM)  # só o PID — não o grupo
        agent.wait(timeout=60)
        self.assertIsNotNone(agent.returncode)

        # Nenhum restic/ssh deste destino ficou vivo
        leftovers = subprocess.run(
            ["pgrep", "-f", f"backups/{self.task_id}"],
            capture_output=True,
            text=True,
        ).stdout.strip()
        self.assertEqual(leftovers, "", "descendentes restic/ssh sobreviveram")

        # Histórico: execução interrupted com motivo
        store = ExecutionStore(self.db_path)
        try:
            entries = store.history(self.task_id)
        finally:
            store.close()
        self.assertEqual(entries[0]["status"], "interrupted")
        self.assertIn("sinal 15", entries[0]["interrupt_detail"] or "")

        # Lock liberado só depois da limpeza — nova execução inicia
        store_path = self.root / "agent2.db"
        self._write_local_success_config()
        with patch.dict(os.environ, {"MACIP_CX_PASSWORD": self.password}):
            result = run_task(self.config_path, store_path)
        self.assertEqual(result.status, ExecutionStatus.SUCCESS)

    def _write_local_success_config(self) -> None:
        config = json.loads(self.config_path.read_text())
        config["destinations"] = [
            {
                "id": "local",
                "name": "Local",
                "kind": "local",
                "repository": str(self.root / "repo-ok"),
                "password_environment": "MACIP_CX_PASSWORD",
            }
        ]
        config["id"] = f"{self.task_id}-novo"
        self.config_path.write_text(json.dumps(config))

    def test_sigterm_mid_second_destination_keeps_first_snapshot(self) -> None:
        """Multi-destino: A conclui → SIGTERM só no PID durante B →
        interrupted com motivo, snapshot de A preservado, destinos
        posteriores nem iniciados."""
        self._write_multi_config()
        agent = self._start_agent()
        try:
            self.assertTrue(self._wait_attempt_running(agent, "hung"))
            os.kill(agent.pid, signal.SIGTERM)  # só o PID
            agent.wait(timeout=60)
        finally:
            if agent.poll() is None:
                agent.kill()
                agent.wait(timeout=30)

        leftovers = subprocess.run(
            ["pgrep", "-f", f"backups/{self.task_id}"],
            capture_output=True, text=True,
        ).stdout.strip()
        self.assertEqual(leftovers, "")

        store = ExecutionStore(self.db_path)
        try:
            entries = store.history(self.task_id)
        finally:
            store.close()
        execution = entries[0]
        self.assertEqual(execution["status"], "interrupted")
        self.assertIn("sinal 15", execution["interrupt_detail"] or "")

        destinations = {
            item["destination_id"]: item for item in execution["destinations"]
        }
        self.assertEqual(destinations["primeiro"]["status"], "success")
        self.assertTrue(destinations["primeiro"]["snapshot_id"])
        self.assertEqual(destinations["hung"]["status"], "interrupted")
        hung_attempts = destinations["hung"]["attempts"]
        self.assertEqual(hung_attempts[0]["status"], "failed")
        self.assertIn("sinal 15", hung_attempts[0]["error"] or "")
        # destino posterior nem iniciado — interrupted sem tentativas
        self.assertEqual(destinations["terceiro"]["status"], "interrupted")
        self.assertEqual(destinations["terceiro"]["attempts"], [])

    def _write_multi_config(self) -> None:
        self.config_path.write_text(
            json.dumps(
                {
                    "id": self.task_id,
                    "name": "Multi-destino",
                    "client": {"id": "client-cx", "name": "Cliente"},
                    "device_id": "device-cx",
                    "sources": [str(self.source)],
                    "exclusions": [],
                    "compression": "auto",
                    "timeouts": {"initialize_seconds": 120},
                    "destinations": [
                        {
                            "id": "primeiro",
                            "name": "Local",
                            "kind": "local",
                            "repository": str(self.root / "repo-um"),
                            "password_environment": "MACIP_CX_PASSWORD",
                        },
                        {
                            "id": "hung",
                            "name": "Travado",
                            "kind": "sftp",
                            "password_environment": "MACIP_CX_PASSWORD",
                            "sftp": {
                                "host": "127.0.0.1",
                                "port": self.listener.getsockname()[1],
                                "user": "backup",
                                "remote_path": f"backups/{self.task_id}",
                                "identity_file": str(self.identity),
                                "known_hosts_file": str(self.known_hosts),
                                "connect_timeout_seconds": 5,
                            },
                        },
                        {
                            "id": "terceiro",
                            "name": "Local 2",
                            "kind": "local",
                            "repository": str(self.root / "repo-dois"),
                            "password_environment": "MACIP_CX_PASSWORD",
                        },
                    ],
                }
            )
        )


class AdapterCancellationTests(unittest.TestCase):
    """cancel(): confirma encerramento do grupo sem espera cheia e sem
    cancel_cleanup_error em encerramento limpo — incluindo descendente
    que ignora SIGTERM."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix=".test-cancel-", dir=Path.cwd())
        self.root = Path(self.temp_dir.name)
        self.destination = Destination(
            id="local",
            name="Local",
            kind=DestinationKind.LOCAL,
            repository=str(self.root / "repo"),
            password_environment="X",
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _fake_restic(self, body: str) -> Path:
        script = self.root / "fake-restic"
        script.write_text(f"#!/usr/bin/env python3\n{body}")
        script.chmod(0o755)
        return script

    def _spawn(self, script: Path) -> tuple["ResticAdapter", threading.Thread, list[BaseException]]:
        from macip_backup.restic import ResticAdapter

        adapter = ResticAdapter(binary=str(script))
        errors: list[BaseException] = []
        thread = threading.Thread(
            target=lambda: self._capture(
                errors, adapter._run, self.destination, "pw", ["backup"], 60
            ),
            daemon=True,
        )
        thread.start()
        deadline = time.monotonic() + 10
        while adapter._current_process is None and time.monotonic() < deadline:
            time.sleep(0.01)
        return adapter, thread, errors

    @staticmethod
    def _capture(errors: list[BaseException], fn: object, *args: object) -> None:
        try:
            fn(*args)  # type: ignore[operator]
        except BaseException as error:  # noqa: BLE001
            errors.append(error)

    def test_clean_sigterm_exit_no_cleanup_error_no_full_wait(self) -> None:
        script = self._fake_restic(
            "import time\ntime.sleep(60)\n"
        )
        adapter, thread, errors = self._spawn(script)
        start = time.monotonic()
        adapter.cancel("teste-sigterm")
        thread.join(timeout=30)
        elapsed = time.monotonic() - start

        self.assertLess(elapsed, 10, "limpeza limpa não pode consumir a espera de SIGKILL")
        self.assertIsNone(adapter.cancel_cleanup_error)
        self.assertTrue(errors, "_run deveria ter levantado cancelamento")
        self.assertIsInstance(errors[0], ResticError)

    def test_descendant_ignoring_sigterm_escalates_to_sigkill(self) -> None:
        marker = f"cx-{secrets.token_hex(4)}"
        script = self._fake_restic(
            "import signal, subprocess, sys, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            f"subprocess.Popen([sys.executable, '-c', '#{marker}\\n"
            "import signal,time\\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\\n"
            "time.sleep(60)'])\n"
            "time.sleep(60)\n"
        )
        adapter, thread, errors = self._spawn(script)
        time.sleep(0.3)  # descendente iniciado
        adapter.cancel("teste-sigkill")
        thread.join(timeout=60)

        self.assertIsNone(adapter.cancel_cleanup_error)
        leftovers = subprocess.run(
            ["pgrep", "-f", marker], capture_output=True, text=True
        ).stdout.strip()
        self.assertEqual(leftovers, "", "descendente que ignora SIGTERM sobreviveu")
        self.assertTrue(errors)

    def test_cancel_without_process_is_noop(self) -> None:
        from macip_backup.restic import ResticAdapter

        adapter = ResticAdapter()
        adapter.cancel("motivo")
        self.assertTrue(adapter.cancelled)
        self.assertEqual(adapter.cancel_reason, "motivo")
        self.assertIsNone(adapter.cancel_cleanup_error)
