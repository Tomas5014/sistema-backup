"""Integrações reais SFTP; exigem ambiente temporário explícito."""

from __future__ import annotations

import io
import os
import secrets
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from macip_backup.logging import StructuredLogger
from macip_backup.models import (
    Client,
    CompressionMode,
    Destination,
    DestinationKind,
    SFTPConnection,
    TaskSpec,
)
from macip_backup.orchestrator import BackupOrchestrator, ExecutionStatus
from macip_backup.restic import ResticAdapter, ResticCommandError, ResticTimeoutError


def _environment_ready() -> bool:
    required = (
        "MACIP_SFTP_INTEGRATION",
        "MACIP_SFTP_HOST",
        "MACIP_SFTP_PORT",
        "MACIP_SFTP_USER",
        "MACIP_SFTP_REMOTE_PATH",
        "MACIP_SFTP_IDENTITY_FILE",
        "MACIP_SFTP_KNOWN_HOSTS_FILE",
    )
    return (
        shutil.which("restic") is not None
        and os.environ.get("MACIP_SFTP_INTEGRATION") == "1"
        and all(os.environ.get(name) for name in required)
    )


@unittest.skipUnless(_environment_ready(), "ambiente SFTP isolado não preparado")
class SFTPIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.password = secrets.token_urlsafe(32)
        self.temp_dir = tempfile.TemporaryDirectory(prefix=".test-sftp-", dir=Path.cwd())
        self.root = Path(self.temp_dir.name)
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "report.txt").write_text("dados SFTP\n", encoding="utf-8")
        (self.source / "ignored.tmp").write_text("ignorar\n", encoding="utf-8")
        remote_root = os.environ["MACIP_SFTP_REMOTE_PATH"].rstrip("/")
        repository_id = f"macip-test-{self.id().rsplit('.', 1)[-1]}-{secrets.token_hex(6)}"
        self.remote_path = f"{remote_root}/{repository_id}" if remote_root else f"/{repository_id}"
        self.connection = SFTPConnection(
            host=os.environ["MACIP_SFTP_HOST"],
            port=int(os.environ["MACIP_SFTP_PORT"]),
            user=os.environ["MACIP_SFTP_USER"],
            remote_path=self.remote_path,
            identity_file=os.environ["MACIP_SFTP_IDENTITY_FILE"],
            known_hosts_file=os.environ["MACIP_SFTP_KNOWN_HOSTS_FILE"],
        )
        with socket.create_connection((self.connection.host, self.connection.port), 3):
            pass
        self.adapter = ResticAdapter(cache_directory=self.root / "cache-one")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_sftp_backup_check_restore_with_fresh_cache_and_reuse(self) -> None:
        destination = self._sftp_destination()
        task = self._task(destination)

        first = BackupOrchestrator(self.adapter, StructuredLogger(io.StringIO())).execute(
            task, lambda _: self.password
        )
        self.assertEqual(first.status, ExecutionStatus.SUCCESS)
        snapshot_id = first.destinations[0].snapshot_id
        self.assertTrue(snapshot_id)

        fresh = ResticAdapter(cache_directory=self.root / "cache-two")
        fresh.check(destination, self.password, read_data=True)
        self.assertTrue(fresh.snapshots(destination, self.password))
        restored = self.root / "restored"
        fresh.restore(destination, self.password, snapshot_id or "", restored, self.source)
        self.assertEqual(next(restored.rglob("report.txt")).read_text(encoding="utf-8"), "dados SFTP\n")
        self.assertFalse(list(restored.rglob("ignored.tmp")))

        second = BackupOrchestrator(fresh, StructuredLogger(io.StringIO())).execute(
            task, lambda _: self.password
        )
        self.assertEqual(second.status, ExecutionStatus.SUCCESS)

    def test_local_and_sftp_destinations_are_independent(self) -> None:
        local = Destination(
            id="local",
            name="Local temporário",
            kind=DestinationKind.LOCAL,
            repository=str(self.root / "local-repository"),
            password_environment="IGNORED",
        )
        result = BackupOrchestrator(self.adapter, StructuredLogger(io.StringIO())).execute(
            self._task(local, self._sftp_destination()), lambda _: self.password
        )
        self.assertEqual(result.status, ExecutionStatus.SUCCESS)

    def test_rejects_invalid_ssh_key(self) -> None:
        invalid_key = self.root / "invalid-key"
        invalid_key.write_text("não é uma chave privada", encoding="utf-8")
        invalid_key.chmod(0o600)

        result = self._execute_failure(replace(self.connection, identity_file=str(invalid_key)))

        self.assertEqual(result.status, ExecutionStatus.FAILED)

    def test_rejects_unknown_host_key(self) -> None:
        unknown_hosts = self.root / "unknown_hosts"
        unknown_hosts.write_text("", encoding="utf-8")

        result = self._execute_failure(replace(self.connection, known_hosts_file=str(unknown_hosts)))

        self.assertEqual(result.status, ExecutionStatus.FAILED)

    def test_rejects_access_denied_and_server_unavailable(self) -> None:
        denied = replace(self.connection, user="denied")
        unavailable = replace(self.connection, port=1, connect_timeout_seconds=1)

        self.assertEqual(self._execute_failure(denied).status, ExecutionStatus.FAILED)
        self.assertEqual(self._execute_failure(unavailable).status, ExecutionStatus.FAILED)

    def test_rejects_incorrect_restic_password(self) -> None:
        destination = self._sftp_destination()
        self.adapter.initialize(destination, self.password)

        with self.assertRaises(ResticCommandError):
            self.adapter.snapshots(destination, "senha-restic-incorreta")

    def test_interrupted_transfer_can_retry(self) -> None:
        destination = self._sftp_destination()
        (self.source / "transfer.bin").write_bytes(os.urandom(2 * 1024 * 1024))
        marker = self.root / "transfer-started"
        child_pid_file = self.root / "restic-child.pid"
        completion_file = self.root / "restic-completed"
        wrapper = self._blocking_restic_wrapper()
        interrupting = ResticAdapter(binary=str(wrapper), cache_directory=self.root / "cache-interrupt")
        interrupting.initialize(destination, self.password)
        errors: list[Exception] = []

        def run_backup() -> None:
            try:
                interrupting.backup(
                    "sftp-interrupted",
                    "client-sftp",
                    (self.source,),
                    (),
                    CompressionMode.OFF,
                    destination,
                    self.password,
                    timeout_seconds=5,
                )
            except Exception as error:
                errors.append(error)

        with patch.dict(
            os.environ,
            {
                "MACIP_INTERRUPT_MARKER": str(marker),
                "MACIP_INTERRUPT_CHILD_PID": str(child_pid_file),
                "MACIP_INTERRUPT_COMPLETION": str(completion_file),
            },
        ):
            thread = threading.Thread(target=run_backup)
            thread.start()
            deadline = time.monotonic() + 4
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(marker.exists(), "transferência não confirmou bytes enviados")
            self.assertTrue(child_pid_file.is_file())
            self.assertEqual(_process_state(int(child_pid_file.read_text())), "T")
            self.assertTrue(thread.is_alive())
            thread.join(timeout=12)

        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], ResticTimeoutError)
        self.assertTrue(child_pid_file.is_file())
        self.assertFalse(_process_is_running(int(child_pid_file.read_text())))
        self.assertFalse(completion_file.exists(), "backup concluiu antes da interrupção")

        fresh = ResticAdapter(cache_directory=self.root / "cache-retry")
        summary = fresh.backup(
            "sftp-interrupted",
            "client-sftp",
            (self.source,),
            (),
            CompressionMode.OFF,
            destination,
            self.password,
        )
        fresh.check(destination, self.password, read_data=True)
        restored = self.root / "restored-after-interruption"
        fresh.restore(destination, self.password, summary.snapshot_id, restored, self.source)
        self.assertEqual(
            next(restored.rglob("report.txt")).read_text(encoding="utf-8"), "dados SFTP\n"
        )
        self.assertEqual(
            next(restored.rglob("transfer.bin")).read_bytes(),
            (self.source / "transfer.bin").read_bytes(),
        )

    def _blocking_restic_wrapper(self) -> Path:
        real_restic = shutil.which("restic")
        assert real_restic is not None
        wrapper = self.root / "restic-transfer-wrapper.py"
        wrapper.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, signal, subprocess, sys, time\n"
            "real_restic = " + repr(real_restic) + "\n"
            "arguments = sys.argv[1:]\n"
            "if 'backup' not in arguments:\n"
            "    os.execv(real_restic, [real_restic, *arguments])\n"
            "backup_index = arguments.index('backup')\n"
            "arguments.insert(backup_index, '--limit-upload=512')\n"
            "child = subprocess.Popen([real_restic, *arguments], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)\n"
            "open(os.environ['MACIP_INTERRUPT_CHILD_PID'], 'w').write(str(child.pid))\n"
            "for line in child.stdout:\n"
            "    print(line, end='', flush=True)\n"
            "    try:\n"
            "        event = json.loads(line)\n"
            "    except json.JSONDecodeError:\n"
            "        continue\n"
            "    if event.get('message_type') == 'status' and event.get('bytes_done', 0) > 0:\n"
            "        if child.poll() is not None:\n"
            "            sys.exit('restic concluiu antes do ponto de interrupção')\n"
            "        os.kill(child.pid, signal.SIGSTOP)\n"
            "        open(os.environ['MACIP_INTERRUPT_MARKER'], 'w').write('bytes sent')\n"
            "        time.sleep(30)\n"
            "stdout, stderr = child.communicate()\n"
            "open(os.environ['MACIP_INTERRUPT_COMPLETION'], 'w').write(str(child.returncode))\n"
            "sys.stdout.write(stdout)\n"
            "sys.stderr.write(stderr)\n"
            "sys.exit(child.returncode)\n",
            encoding="utf-8",
        )
        wrapper.chmod(0o700)
        return wrapper

    def _sftp_destination(self) -> Destination:
        return Destination(
            id="sftp",
            name="SFTP temporário",
            kind=DestinationKind.SFTP,
            repository=None,
            password_environment="IGNORED",
            sftp=self.connection,
        )

    def _execute_failure(self, connection: SFTPConnection):
        destination = replace(self._sftp_destination(), sftp=connection)
        return BackupOrchestrator(self.adapter, StructuredLogger(io.StringIO())).execute(
            self._task(destination), lambda _: self.password
        )

    def _task(self, *destinations: Destination) -> TaskSpec:
        return TaskSpec(
            id="sftp-integration",
            name="Integração SFTP isolada",
            client=Client(id="client-sftp", name="Cliente SFTP"),
            device_id="device-sftp",
            sources=(self.source,),
            exclusions=("*.tmp",),
            compression=CompressionMode.AUTO,
            destinations=destinations,
        )


def _process_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    stat_file = Path(f"/proc/{pid}/stat")
    return not stat_file.is_file() or stat_file.read_text().split()[2] != "Z"


def _process_state(pid: int) -> str | None:
    stat_file = Path(f"/proc/{pid}/stat")
    if not stat_file.is_file():
        return None
    return stat_file.read_text().split()[2]
