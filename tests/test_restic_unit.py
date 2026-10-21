from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from macip_backup.models import (
    CompressionMode,
    Destination,
    DestinationKind,
    SFTPConnection,
)
from macip_backup.restic import ResticAdapter, ResticCommandError, ResticTimeoutError


class TimedOutProcess:
    def __init__(self) -> None:
        self.communicate_calls = 0
        self.returncode: int | None = None
        self.pid = 12345
        self.stdout = None
        self.stderr = None

    def communicate(self, timeout: int | None = None) -> tuple[str, str]:
        self.communicate_calls += 1
        if self.communicate_calls == 1:
            raise subprocess.TimeoutExpired(["restic", "backup"], timeout)
        self.returncode = -9
        return "", "simulated timeout"

class SuccessfulProcess:
    def __init__(self, stdout: str) -> None:
        self.returncode = 0
        self.stdout = stdout

    def communicate(self, timeout: int | None = None) -> tuple[str, str]:
        return self.stdout, ""


class MissingRepositoryProcess:
    def __init__(self) -> None:
        self.returncode = 10
        self.pid = 12344
        self.stdout = None
        self.stderr = None

    def communicate(self, timeout: int | None = None) -> tuple[str, str]:
        return "", "repository does not exist"


class FailedRepositoryProcess(MissingRepositoryProcess):
    def __init__(self, returncode: int) -> None:
        super().__init__()
        self.returncode = returncode


class ResticAdapterUnitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.destination = Destination(
            id="destination-unit",
            name="Destino unitário",
            kind=DestinationKind.LOCAL,
            repository="/tmp/macip-unit-repository",
            password_environment="MACIP_TEST_PASSWORD",
        )

    def test_timeout_kills_and_reaps_process_without_password_in_command(self) -> None:
        process = TimedOutProcess()
        adapter = ResticAdapter()
        with patch("macip_backup.restic.shutil.which", return_value="/usr/bin/restic"), patch(
            "macip_backup.restic.subprocess.Popen",
            side_effect=[MissingRepositoryProcess(), process],
        ) as popen:
            with patch("macip_backup.restic.os.killpg") as killpg:
                with self.assertRaisesRegex(ResticTimeoutError, "tempo limite de 1s") as context:
                    adapter.initialize(self.destination, "secret-not-for-command", timeout_seconds=1)

        killpg.assert_called_once()
        self.assertEqual(process.communicate_calls, 2)
        self.assertEqual(context.exception.operation, "init")
        command = popen.call_args_list[1].args[0]
        self.assertNotIn("secret-not-for-command", command)
        self.assertNotIn("secret-not-for-command", str(context.exception))

    def test_sftp_uses_native_backend_and_generated_ssh_arguments(self) -> None:
        destination = Destination(
            id="destination-sftp",
            name="SFTP",
            kind=DestinationKind.SFTP,
            repository=None,
            password_environment="MACIP_TEST_PASSWORD",
            sftp=SFTPConnection(
                host="backup.example.test",
                port=2222,
                user="backup",
                remote_path="repositories/client",
                identity_file="/run/macip/id ed25519",
                known_hosts_file="/run/macip/known hosts",
            ),
        )
        adapter = ResticAdapter()
        with patch("macip_backup.restic.shutil.which", return_value="/usr/bin/restic"), patch(
            "macip_backup.restic.subprocess.Popen",
            return_value=SuccessfulProcess("[]"),
        ) as popen:
            adapter.snapshots(destination, "secret-not-for-command")

        command = popen.call_args.args[0]
        self.assertIn("sftp:backup@backup.example.test:repositories/client", command)
        sftp_args = command[command.index("-o") + 1]
        self.assertIn("BatchMode=yes", sftp_args)
        self.assertIn("StrictHostKeyChecking=yes", sftp_args)
        self.assertIn("UserKnownHostsFile=", sftp_args)
        self.assertIn("IdentitiesOnly=yes", sftp_args)
        self.assertIn("ConnectTimeout=15", sftp_args)
        self.assertNotIn("secret-not-for-command", command)

    def test_initialize_does_not_mask_access_or_password_error_with_init(self) -> None:
        for returncode in (1, 12):
            with self.subTest(returncode=returncode), patch(
                "macip_backup.restic.shutil.which", return_value="/usr/bin/restic"
            ), patch(
                "macip_backup.restic.subprocess.Popen",
                return_value=FailedRepositoryProcess(returncode),
            ) as popen:
                with self.assertRaises(ResticCommandError) as context:
                    ResticAdapter().initialize(self.destination, "test-password")

            self.assertEqual(context.exception.return_code, returncode)
            self.assertEqual(popen.call_count, 1)
            self.assertIn("cat", popen.call_args.args[0])

    def test_timeout_kills_real_child_process_group_and_next_destination_continues(self) -> None:
        with tempfile.TemporaryDirectory(prefix="macip-timeout-") as directory:
            root = Path(directory)
            pid_file = root / "child.pid"
            executable = root / "fake-restic.py"
            executable.write_text(
                "#!/usr/bin/env python3\n"
                "import os, subprocess, sys, time\n"
                "if 'cat' in sys.argv: sys.exit(10)\n"
                "if 'init' in sys.argv: sys.exit(0)\n"
                "if 'backup' in sys.argv:\n"
                "    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
                "    open(os.environ['MACIP_CHILD_PID_FILE'], 'w').write(str(child.pid))\n"
                "    time.sleep(30)\n"
            )
            executable.chmod(0o700)
            adapter = ResticAdapter(binary=str(executable))
            first = self.destination
            second = Destination(
                id="destination-next",
                name="Próximo",
                kind=DestinationKind.LOCAL,
                repository=str(root / "next-repository"),
                password_environment="MACIP_TEST_PASSWORD",
            )
            with patch.dict(os.environ, {"MACIP_CHILD_PID_FILE": str(pid_file)}):
                adapter.initialize(first, "test-password", timeout_seconds=1)
                with self.assertRaises(ResticTimeoutError):
                    adapter.backup(
                        "task",
                        "client",
                        [root],
                        (),
                        CompressionMode.AUTO,
                        first,
                        "test-password",
                        timeout_seconds=1,
                    )
                adapter.initialize(second, "test-password", timeout_seconds=1)

            child_pid = int(pid_file.read_text())
            deadline = time.monotonic() + 2
            while _process_exists(child_pid) and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertFalse(_process_exists(child_pid))

    def test_backup_passes_each_compression_mode_as_structured_argument(self) -> None:
        processes: list[SuccessfulProcess] = []

        def make_process(*_: object, **__: object) -> SuccessfulProcess:
            process = SuccessfulProcess(
                '{"message_type":"summary","snapshot_id":"snapshot",'
                '"total_files_processed":1,"total_bytes_processed":2}\n'
            )
            processes.append(process)
            return process

        adapter = ResticAdapter()
        with patch("macip_backup.restic.shutil.which", return_value="/usr/bin/restic"), patch(
            "macip_backup.restic.subprocess.Popen", side_effect=make_process
        ) as popen:
            for compression in CompressionMode:
                adapter.backup(
                    "task-unit",
                    "client-unit",
                    [Path("/tmp/source")],
                    ["*.tmp"],
                    compression,
                    self.destination,
                    "secret-not-for-command",
                )

        commands = [call.args[0] for call in popen.call_args_list]
        for compression, command in zip(CompressionMode, commands, strict=True):
            self.assertEqual(command[command.index("--compression") + 1], compression.value)
            self.assertNotIn("secret-not-for-command", command)


def _process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    stat_file = Path(f"/proc/{pid}/stat")
    return not stat_file.is_file() or stat_file.read_text().split()[2] != "Z"
