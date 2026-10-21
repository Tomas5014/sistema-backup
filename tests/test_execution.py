from __future__ import annotations

import io
import secrets
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

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

        first_result = result.destinations[0]
        self.assertIsNotNone(first_result.snapshot_id)
        self.adapter.check(first, self.password, read_data=True)
        restored = self.root / "restored"
        self.adapter.restore(
            first,
            self.password,
            first_result.snapshot_id or "",
            restored,
            source_subfolder=self.source,
        )
        restored_files = list(restored.rglob("report.txt"))
        self.assertEqual(len(restored_files), 1)
        self.assertEqual(restored_files[0].read_text(), "dados importantes\n")
        self.assertFalse(list(restored.rglob("ignored.tmp")))

    def test_failed_destination_does_not_stop_valid_destination(self) -> None:
        failed_repository = self.root / "not-a-directory"
        failed_repository.write_text("bloqueia init")
        failed = self._destination("invalid", failed_repository)
        valid = self._destination("local", self.root / "repository-local")

        result = BackupOrchestrator(self.adapter, StructuredLogger(io.StringIO())).execute(
            self._task(failed, valid), self._password
        )

        self.assertEqual(result.status, ExecutionStatus.PARTIAL)
        self.assertEqual(result.destinations[0].status, DestinationStatus.FAILED)
        self.assertEqual(result.destinations[1].status, DestinationStatus.SUCCESS)
        self.assertTrue(self.adapter.snapshots(valid, self.password))

    def _task(self, *destinations: Destination) -> TaskSpec:
        return TaskSpec(
            id="task-integration",
            name="Integração temporária",
            client=Client(id="client-integration", name="Cliente de teste"),
            device_id="device-integration",
            sources=(self.source,),
            exclusions=("*.tmp",),
            compression=CompressionMode.AUTO,
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
