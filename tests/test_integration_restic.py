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
    DestinationStatus,
    ExecutionStatus,
)
from macip_backup.restic import ResticAdapter


@unittest.skipUnless(shutil.which("restic"), "restic não está instalado")
class BackupExecutionIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.password = secrets.token_urlsafe(32)
        self.temp_dir = tempfile.TemporaryDirectory(prefix=".test-backup-", dir=Path.cwd())
        self.root = Path(self.temp_dir.name)
        self.source = self.root / "source"
        (self.source / "documents").mkdir(parents=True)
        (self.source / "documents" / "report.txt").write_text("dados importantes\n")
        (self.source / "ignored.tmp").write_text("não deve entrar\n")
        self.adapter = ResticAdapter(cache_directory=self.root / "cache")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_backup_restore_and_data_check_for_two_destinations(self) -> None:
        first = self._destination("local", self.root / "repository-local")
        second = self._destination("macip", self.root / "repository-macip")
        task = self._task(first, second)
        log_stream = io.StringIO()

        result = BackupOrchestrator(
            self.adapter, StructuredLogger(log_stream)
        ).execute(task, self._password)

        self.assertEqual(result.status, ExecutionStatus.SUCCESS)
        self.assertEqual(
            [item.status for item in result.destinations],
            [DestinationStatus.SUCCESS, DestinationStatus.SUCCESS],
        )
        self.assertIn('"event": "backup.destination.finished"', log_stream.getvalue())

        for destination, destination_result in zip(
            (first, second), result.destinations, strict=True
        ):
            self._assert_restored_content(destination, destination_result.snapshot_id or "")

    def test_all_compression_modes_are_accepted_by_restic(self) -> None:
        destination = self._destination("compression", self.root / "repository-compression")
        self.adapter.initialize(destination, self.password)
        for compression in CompressionMode:
            with self.subTest(compression=compression.value):
                summary = self.adapter.backup(
                    "task-compression",
                    "client-integration",
                    (self.source,),
                    ("*.tmp",),
                    compression,
                    destination,
                    self.password,
                )
                self.assertTrue(summary.snapshot_id)
        self.adapter.check(destination, self.password, read_data=True)

    def test_cli_recovery_commands_restore_after_source_removal(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="macip-cli-source-"
        ) as source_directory, tempfile.TemporaryDirectory(
            prefix="macip-cli-restore-"
        ) as restore_directory:
            source = Path(source_directory) / "source"
            source.mkdir()
            (source / "report.txt").write_text("dados de recuperação\n")
            (source / "ignored.tmp").write_text("não deve entrar\n")
            repository = self.root / "repository-cli-recovery"
            password_environment = "MACIP_CLI_RECOVERY_PASSWORD"
            config = {
                "id": "task-cli-recovery",
                "name": "Recuperação CLI",
                "client": {"id": "client-cli", "name": "Cliente CLI"},
                "device_id": "device-cli",
                "sources": [str(source)],
                "exclusions": ["*.tmp"],
                "destinations": [
                    {
                        "id": "local-cli",
                        "name": "Local CLI",
                        "kind": "local",
                        "repository": str(repository),
                        "password_environment": password_environment,
                    }
                ],
            }
            config_path = self.root / "task-cli-recovery.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")

            output = io.StringIO()
            environment = {
                password_environment: self.password,
                "RESTIC_CACHE_DIR": str(self.root / "cache-cli"),
            }
            with patch.dict(os.environ, environment), patch("sys.stdout", output):
                with patch.object(
                    sys, "argv", ["macip-backup-poc", "run", str(config_path)]
                ):
                    self.assertEqual(cli_main(), 0)
                run_output = json.loads(output.getvalue())
                self.assertEqual(run_output["status"], "success")
                snapshot_id = run_output["destinations"][0]["snapshot_id"]
                self.assertTrue(snapshot_id)

                output.seek(0)
                output.truncate(0)
                with patch.object(
                    sys, "argv", ["macip-backup-poc", "snapshots", str(config_path), "local-cli"]
                ):
                    self.assertEqual(cli_main(), 0)
                snapshots = json.loads(output.getvalue())["snapshots"]
                self.assertIn(snapshot_id, [snapshot["id"] for snapshot in snapshots])

                output.seek(0)
                output.truncate(0)
                with patch.object(
                    sys,
                    "argv",
                    ["macip-backup-poc", "check", str(config_path), "local-cli", "--read-data"],
                ):
                    self.assertEqual(cli_main(), 0)
                self.assertTrue(json.loads(output.getvalue())["read_data"])

                shutil.rmtree(source)
                target = Path(restore_directory) / "restored-cli"
                output.seek(0)
                output.truncate(0)
                with patch.object(
                    sys,
                    "argv",
                    [
                        "macip-backup-poc",
                        "restore",
                        str(config_path),
                        "local-cli",
                        snapshot_id,
                        str(target),
                    ],
                ):
                    self.assertEqual(cli_main(), 0)

            self.assertEqual(json.loads(output.getvalue())["target"], str(target.resolve()))
            restored_reports = list(target.rglob("report.txt"))
            self.assertEqual(len(restored_reports), 1)
            self.assertEqual(restored_reports[0].read_text(), "dados de recuperação\n")
            self.assertFalse(list(target.rglob("ignored.tmp")))

    def test_failed_destination_does_not_stop_valid_destination(self) -> None:
        failed_repository = self.root / "not-a-directory"
        failed_repository.mkdir()
        (failed_repository / "config").write_text("configuração inválida")
        failed = self._destination("invalid", failed_repository)
        valid = self._destination("local", self.root / "repository-local")

        result = BackupOrchestrator(self.adapter, StructuredLogger(io.StringIO())).execute(
            self._task(failed, valid), self._password
        )

        self.assertEqual(result.status, ExecutionStatus.PARTIAL)
        self.assertEqual(result.destinations[0].status, DestinationStatus.FAILED)
        self.assertEqual(result.destinations[1].status, DestinationStatus.SUCCESS)
        self.assertTrue(self.adapter.snapshots(valid, self.password))

    def _task(
        self, *destinations: Destination, compression: CompressionMode = CompressionMode.AUTO
    ) -> TaskSpec:
        return TaskSpec(
            id="task-integration",
            name="Integração temporária",
            client=Client(id="client-integration", name="Cliente de teste"),
            device_id="device-integration",
            sources=(self.source,),
            exclusions=("*.tmp",),
            compression=compression,
            destinations=destinations,
        )

    def _destination(self, identifier: str, repository: Path) -> Destination:
        return Destination(
            id=f"destination-{identifier}",
            name=identifier,
            kind=DestinationKind.LOCAL,
            repository=str(repository),
            password_environment="IGNORED_BY_TEST",
        )

    def _password(self, _: Destination) -> str:
        return self.password

    def _assert_restored_content(self, destination: Destination, snapshot_id: str) -> None:
        self.assertTrue(snapshot_id)
        self.adapter.check(destination, self.password, read_data=True)
        restored = self.root / f"restored-{destination.id}"
        self.adapter.restore(
            destination,
            self.password,
            snapshot_id,
            restored,
            source_subfolder=self.source,
        )
        restored_files = list(restored.rglob("report.txt"))
        self.assertEqual(len(restored_files), 1)
        self.assertEqual(restored_files[0].read_text(), "dados importantes\n")
        self.assertFalse(list(restored.rglob("ignored.tmp")))
