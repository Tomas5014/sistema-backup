from __future__ import annotations

import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from macip_backup.logging import StructuredLogger
from macip_backup.models import (
    Client,
    CompressionMode,
    Destination,
    DestinationKind,
    OperationTimeouts,
    TaskSpec,
)
from macip_backup.orchestrator import BackupOrchestrator, DestinationStatus, ExecutionStatus
from macip_backup.restic import BackupSummary, ResticError, ResticTimeoutError


class ControlledAdapter:
    def __init__(self, failures: dict[tuple[str, str], Exception] | None = None) -> None:
        self.failures = failures or {}
        self.calls: list[tuple[str, str, int]] = []

    def initialize(self, destination: Destination, _: str, timeout_seconds: int) -> None:
        self.calls.append((destination.id, "init", timeout_seconds))
        self._raise(destination, "init")

    def backup(self, *args: object) -> BackupSummary:
        destination = args[5]
        timeout_seconds = args[7]
        assert isinstance(destination, Destination)
        assert isinstance(timeout_seconds, int)
        self.calls.append((destination.id, "backup", timeout_seconds))
        self._raise(destination, "backup")
        return BackupSummary("snapshot", 1, 2)

    def check(
        self, destination: Destination, _: str, *, timeout_seconds: int, read_data: bool = False
    ) -> None:
        self.calls.append((destination.id, "check", timeout_seconds))
        self._raise(destination, "check")

    def snapshots(self, destination: Destination, _: str, timeout_seconds: int) -> list[dict[str, str]]:
        self.calls.append((destination.id, "snapshots", timeout_seconds))
        self._raise(destination, "snapshots")
        return [{"id": "snapshot"}]

    def _raise(self, destination: Destination, operation: str) -> None:
        error = self.failures.get((destination.id, operation))
        if error:
            raise error


class BackupOrchestratorUnitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix=".test-orchestrator-", dir=Path.cwd())
        self.root = Path(self.temp_dir.name)
        self.source = self.root / "source"
        self.source.mkdir()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_timeout_in_one_destination_keeps_next_destination_running(self) -> None:
        first, second = self._destinations()
        adapter = ControlledAdapter({(first.id, "backup"): ResticTimeoutError("backup", 1)})
        log_stream = io.StringIO()

        result = BackupOrchestrator(adapter, StructuredLogger(log_stream)).execute(
            self._task(first, second), lambda _: "test-password"
        )

        self.assertEqual(result.status, ExecutionStatus.PARTIAL)
        self.assertEqual(result.destinations[0].status, DestinationStatus.FAILED)
        self.assertEqual(result.destinations[0].failed_operation, "backup")
        self.assertEqual(result.destinations[0].timeout_seconds, 1)
        self.assertEqual(result.destinations[1].status, DestinationStatus.SUCCESS)
        self.assertIn((second.id, "backup", 2), adapter.calls)
        self.assertIn((second.id, "check", 4), adapter.calls)
        self.assertIn((second.id, "snapshots", 3), adapter.calls)
        self.assertIn('"failed_operation": "backup"', log_stream.getvalue())

    def test_all_failed_destinations_return_failed(self) -> None:
        first, second = self._destinations()
        adapter = ControlledAdapter(
            {
                (first.id, "backup"): ResticError("first failed"),
                (second.id, "backup"): ResticError("second failed"),
            }
        )

        result = BackupOrchestrator(adapter, StructuredLogger(io.StringIO())).execute(
            self._task(first, second), lambda _: "test-password"
        )

        self.assertEqual(result.status, ExecutionStatus.FAILED)
        self.assertTrue(all(item.status is DestinationStatus.FAILED for item in result.destinations))

    def test_missing_credential_returns_failure_without_secret(self) -> None:
        first, _ = self._destinations()
        log_stream = io.StringIO()
        with patch.dict(os.environ, {}, clear=True):
            result = BackupOrchestrator(ControlledAdapter(), StructuredLogger(log_stream)).execute(
                self._task(first)
            )

        self.assertEqual(result.status, ExecutionStatus.FAILED)
        self.assertIn("MACIP_MISSING_TEST_PASSWORD", result.destinations[0].error or "")
        self.assertNotIn("test-password", log_stream.getvalue())

    def _task(self, *destinations: Destination) -> TaskSpec:
        return TaskSpec(
            id="task-orchestrator",
            name="Tarefa de orquestração",
            client=Client(id="client-orchestrator", name="Cliente de teste"),
            device_id="device-orchestrator",
            sources=(self.source,),
            exclusions=(),
            compression=CompressionMode.AUTO,
            destinations=destinations,
            timeouts=OperationTimeouts(
                initialize_seconds=1,
                backup_seconds=2,
                snapshots_seconds=3,
                check_seconds=4,
                restore_seconds=5,
            ),
        )

    def _destinations(self) -> tuple[Destination, Destination]:
        return (
            Destination(
                id="destination-timeout",
                name="Timeout",
                kind=DestinationKind.LOCAL,
                repository=str(self.root / "repository-timeout"),
                password_environment="MACIP_MISSING_TEST_PASSWORD",
            ),
            Destination(
                id="destination-success",
                name="Sucesso",
                kind=DestinationKind.LOCAL,
                repository=str(self.root / "repository-success"),
                password_environment="MACIP_MISSING_TEST_PASSWORD",
            ),
        )
