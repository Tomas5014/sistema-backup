from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from macip_backup.models import CompressionMode, Destination, DestinationKind
from macip_backup.restic import ResticAdapter, ResticTimeoutError


class TimedOutProcess:
    def __init__(self) -> None:
        self.communicate_calls = 0
        self.killed = False
        self.returncode: int | None = None

    def communicate(self, timeout: int | None = None) -> tuple[str, str]:
        self.communicate_calls += 1
        if self.communicate_calls == 1:
            raise subprocess.TimeoutExpired(["restic", "backup"], timeout)
        self.returncode = -9
        return "", "simulated timeout"

    def kill(self) -> None:
        self.killed = True


class SuccessfulProcess:
    def __init__(self, stdout: str) -> None:
        self.returncode = 0
        self.stdout = stdout

    def communicate(self, timeout: int | None = None) -> tuple[str, str]:
        return self.stdout, ""


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
            "macip_backup.restic.subprocess.Popen", return_value=process
        ) as popen:
            with self.assertRaisesRegex(ResticTimeoutError, "tempo limite de 1s") as context:
                adapter.initialize(self.destination, "secret-not-for-command", timeout_seconds=1)

        self.assertTrue(process.killed)
        self.assertEqual(process.communicate_calls, 2)
        self.assertEqual(context.exception.operation, "init")
        command = popen.call_args.args[0]
        self.assertNotIn("secret-not-for-command", command)
        self.assertNotIn("secret-not-for-command", str(context.exception))

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
