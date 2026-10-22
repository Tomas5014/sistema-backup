from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import call, patch

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from macip_backup.models import (
    CompressionMode,
    Destination,
    DestinationKind,
    SFTPConnection,
)
from macip_backup.restic import ResticAdapter, ResticCommandError, ResticError, ResticTimeoutError


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
        self.timeout: int | None = None

    def communicate(self, timeout: int | None = None) -> tuple[str, str]:
        self.timeout = timeout
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
            with patch("macip_backup.restic.os.killpg") as killpg, patch(
                "macip_backup.restic.ResticAdapter._process_group_exists",
                return_value=False,
            ):
                with self.assertRaisesRegex(ResticTimeoutError, "tempo limite de 1s") as context:
                    adapter.initialize(self.destination, "secret-not-for-command", timeout_seconds=1)

        killpg.assert_has_calls(
            [call(process.pid, signal.SIGCONT), call(process.pid, signal.SIGTERM)]
        )
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

    def test_explicit_timeout_must_be_positive_and_none_uses_default(self) -> None:
        adapter = ResticAdapter()
        for value in (0, -1, True, 1.5):
            with self.subTest(value=value), patch(
                "macip_backup.restic.subprocess.Popen"
            ) as popen, self.assertRaisesRegex(
                ResticError, "timeout de restic backup deve ser inteiro positivo"
            ):
                adapter.backup(
                    "task",
                    "client",
                    [Path("/tmp/source")],
                    (),
                    CompressionMode.AUTO,
                    self.destination,
                    "test-password",
                    timeout_seconds=value,  # type: ignore[arg-type]
                )
            popen.assert_not_called()

        process = SuccessfulProcess(
            '{"message_type":"summary","snapshot_id":"snapshot",'
            '"total_files_processed":1,"total_bytes_processed":2}\n'
        )
        with patch("macip_backup.restic.shutil.which", return_value="/usr/bin/restic"), patch(
            "macip_backup.restic.subprocess.Popen", return_value=process
        ):
            adapter.backup(
                "task",
                "client",
                [Path("/tmp/source")],
                (),
                CompressionMode.AUTO,
                self.destination,
                "test-password",
                timeout_seconds=None,
            )
        self.assertEqual(process.timeout, 14_400)

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
                "    starttime = open(f'/proc/{child.pid}/stat').read().rsplit(')', 1)[1].split()[19]\n"
                "    open(os.environ['MACIP_CHILD_PID_FILE'], 'w').write(f'{child.pid}:{starttime}')\n"
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

            child_pid, child_starttime = _read_process_identity(pid_file)
            deadline = time.monotonic() + 2
            while _process_is_running(child_pid, child_starttime) and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertFalse(_process_is_running(child_pid, child_starttime))

    @unittest.skipUnless(os.name == "posix", "exige grupo de processos POSIX")
    def test_timeout_resumes_and_kills_stopped_descendant(self) -> None:
        with tempfile.TemporaryDirectory(prefix="macip-stopped-child-") as directory:
            root = Path(directory)
            pid_file = root / "child.pid"
            executable = root / "fake-restic.py"
            executable.write_text(
                "#!/usr/bin/env python3\n"
                "import os, signal, subprocess, sys, time\n"
                "if 'cat' in sys.argv: sys.exit(10)\n"
                "if 'init' in sys.argv: sys.exit(0)\n"
                "if 'backup' in sys.argv:\n"
                "    child = subprocess.Popen([sys.executable, '-c', "
                "'import os, signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "os.kill(os.getpid(), signal.SIGSTOP); time.sleep(30)'])\n"
                "    while open(f'/proc/{child.pid}/stat').read().split()[2] != 'T': time.sleep(0.01)\n"
                "    starttime = open(f'/proc/{child.pid}/stat').read().rsplit(')', 1)[1].split()[19]\n"
                "    open(os.environ['MACIP_CHILD_PID_FILE'], 'w').write(f'{child.pid}:{starttime}')\n"
                "    time.sleep(30)\n",
                encoding="utf-8",
            )
            executable.chmod(0o700)
            adapter = ResticAdapter(binary=str(executable))
            errors: list[Exception] = []

            def run_backup() -> None:
                try:
                    adapter.backup(
                        "task",
                        "client",
                        [root],
                        (),
                        CompressionMode.AUTO,
                        self.destination,
                        "test-password",
                        timeout_seconds=3,
                    )
                except Exception as error:
                    errors.append(error)

            with patch.dict(os.environ, {"MACIP_CHILD_PID_FILE": str(pid_file)}):
                adapter.initialize(self.destination, "test-password", timeout_seconds=1)
                thread = threading.Thread(target=run_backup)
                thread.start()
                deadline = time.monotonic() + 2
                while not pid_file.is_file() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(pid_file.is_file())
                child_pid, child_starttime = _read_process_identity(pid_file)
                self.assertEqual(_process_state(child_pid), "T")
                self.assertTrue(thread.is_alive())
                thread.join(timeout=8)

            self.assertFalse(thread.is_alive())
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], ResticTimeoutError)
            deadline = time.monotonic() + 2
            while _process_is_running(child_pid, child_starttime) and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertFalse(_process_is_running(child_pid, child_starttime))

    def test_normal_child_completion_is_not_reported_as_interruption(self) -> None:
        with tempfile.TemporaryDirectory(prefix="macip-normal-child-") as directory:
            root = Path(directory)
            completed = root / "completed"
            executable = root / "fake-restic.py"
            executable.write_text(
                "#!/usr/bin/env python3\n"
                "import os, subprocess, sys\n"
                "if 'cat' in sys.argv: sys.exit(10)\n"
                "if 'init' in sys.argv: sys.exit(0)\n"
                "if 'backup' in sys.argv:\n"
                "    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(0.05)'])\n"
                "    child.wait()\n"
                "    open(os.environ['MACIP_NORMAL_CHILD_COMPLETED'], 'w').write('done')\n"
                "    print('{\"message_type\":\"summary\",\"snapshot_id\":\"normal\",\"total_files_processed\":1,\"total_bytes_processed\":2}')\n",
                encoding="utf-8",
            )
            executable.chmod(0o700)
            adapter = ResticAdapter(binary=str(executable))
            with patch.dict(os.environ, {"MACIP_NORMAL_CHILD_COMPLETED": str(completed)}):
                adapter.initialize(self.destination, "test-password", timeout_seconds=1)
                summary = adapter.backup(
                    "task",
                    "client",
                    [root],
                    (),
                    CompressionMode.AUTO,
                    self.destination,
                    "test-password",
                    timeout_seconds=1,
                )

            self.assertEqual(summary.snapshot_id, "normal")
            self.assertTrue(completed.is_file())

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


def _process_is_running(pid: int, starttime: str | None) -> bool:
    current_starttime = _process_starttime(pid)
    if current_starttime is None or current_starttime != starttime:
        return False
    stat_file = Path(f"/proc/{pid}/stat")
    return stat_file.read_text().split()[2] != "Z"


def _process_starttime(pid: int) -> str | None:
    stat_file = Path(f"/proc/{pid}/stat")
    try:
        fields = stat_file.read_text().rsplit(")", 1)[1].split()
    except (FileNotFoundError, IndexError):
        return None
    return fields[19]


def _read_process_identity(path: Path) -> tuple[int, str]:
    pid, starttime = path.read_text(encoding="utf-8").split(":", 1)
    return int(pid), starttime


def _process_state(pid: int) -> str | None:
    stat_file = Path(f"/proc/{pid}/stat")
    try:
        return stat_file.read_text().rsplit(")", 1)[1].split()[0]
    except (FileNotFoundError, IndexError):
        return None
