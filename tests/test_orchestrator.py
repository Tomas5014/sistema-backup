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
    RetentionPolicy,
    RetryPolicy,
    SFTPConnection,
    TaskSpec,
)
from macip_backup.orchestrator import BackupOrchestrator, DestinationStatus, ExecutionStatus
from macip_backup.restic import BackupSummary, ResticError, ResticTimeoutError


class ControlledAdapter:
    def __init__(
        self,
        failures: dict[tuple[str, str], Exception] | None = None,
        failures_once: dict[tuple[str, str], Exception] | None = None,
    ) -> None:
        self.failures = failures or {}
        self.failures_once = failures_once or {}
        self.calls: list[tuple] = []

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

    def forget(
        self,
        destination: Destination,
        _: str,
        *,
        keep_last: int,
        tag: str | None,
        timeout_seconds: int,
    ) -> None:
        self.calls.append((destination.id, "forget", keep_last, tag or ""))
        self._raise(destination, "forget")

    def _raise(self, destination: Destination, operation: str) -> None:
        error = self.failures_once.pop((destination.id, operation), None)
        if error:
            raise error
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

    def test_missing_sftp_files_fail_only_sftp_between_local_destinations(self) -> None:
        for missing_field in ("identity_file", "known_hosts_file"):
            with self.subTest(missing_field=missing_field):
                identity = self.root / f"id_ed25519-{missing_field}"
                known_hosts = self.root / f"known_hosts-{missing_field}"
                existing = known_hosts if missing_field == "identity_file" else identity
                existing.write_text("test", encoding="utf-8")
                sftp = Destination(
                    id=f"sftp-missing-{missing_field}",
                    name="SFTP sem arquivo",
                    kind=DestinationKind.SFTP,
                    repository=None,
                    password_environment="IGNORED",
                    sftp=SFTPConnection(
                        host="backup.example.test",
                        port=22,
                        user="backup",
                        remote_path="repository",
                        identity_file=str(identity),
                        known_hosts_file=str(known_hosts),
                    ),
                )
                before = Destination(
                    id=f"local-before-{missing_field}",
                    name="Local antes",
                    kind=DestinationKind.LOCAL,
                    repository=str(self.root / f"before-{missing_field}"),
                    password_environment="IGNORED",
                )
                after = Destination(
                    id=f"local-after-{missing_field}",
                    name="Local depois",
                    kind=DestinationKind.LOCAL,
                    repository=str(self.root / f"after-{missing_field}"),
                    password_environment="IGNORED",
                )
                adapter = ControlledAdapter()

                result = BackupOrchestrator(adapter, StructuredLogger(io.StringIO())).execute(
                    self._task(before, sftp, after), lambda _: "test-password"
                )

                self.assertEqual(result.status, ExecutionStatus.PARTIAL)
                self.assertEqual(
                    [item.status for item in result.destinations],
                    [DestinationStatus.SUCCESS, DestinationStatus.FAILED, DestinationStatus.SUCCESS],
                )
                self.assertIn(f"sftp.{missing_field}", result.destinations[1].error or "")
                self.assertIn((before.id, "backup", 2), adapter.calls)
                self.assertIn((after.id, "backup", 2), adapter.calls)

    def test_simulated_sftp_timeout_keeps_local_destination_running(self) -> None:
        identity = self.root / "id_ed25519"
        known_hosts = self.root / "known_hosts"
        identity.write_text("test", encoding="utf-8")
        known_hosts.write_text("test", encoding="utf-8")
        sftp = Destination(
            id="sftp-timeout",
            name="SFTP com timeout",
            kind=DestinationKind.SFTP,
            repository=None,
            password_environment="IGNORED",
            sftp=SFTPConnection(
                host="backup.example.test",
                port=22,
                user="backup",
                remote_path="repository",
                identity_file=str(identity),
                known_hosts_file=str(known_hosts),
            ),
        )
        local = Destination(
            id="local-after-timeout",
            name="Local após timeout",
            kind=DestinationKind.LOCAL,
            repository=str(self.root / "local-after-timeout"),
            password_environment="IGNORED",
        )
        adapter = ControlledAdapter({(sftp.id, "backup"): ResticTimeoutError("backup", 1)})

        result = BackupOrchestrator(adapter, StructuredLogger(io.StringIO())).execute(
            self._task(sftp, local), lambda _: "test-password"
        )

        self.assertEqual(result.status, ExecutionStatus.PARTIAL)
        self.assertEqual(result.destinations[0].status, DestinationStatus.FAILED)
        self.assertEqual(result.destinations[0].timeout_seconds, 1)
        self.assertEqual(result.destinations[1].status, DestinationStatus.SUCCESS)

    def test_retention_policy_runs_forget_scoped_to_task(self) -> None:
        destination = Destination(
            id="destination-retention",
            name="Retenção",
            kind=DestinationKind.LOCAL,
            repository=str(self.root / "repository-retention"),
            password_environment="MACIP_MISSING_TEST_PASSWORD",
            retention=RetentionPolicy(keep_last=2),
        )
        adapter = ControlledAdapter()

        result = BackupOrchestrator(adapter, StructuredLogger(io.StringIO())).execute(
            self._task(destination), lambda _: "test-password"
        )

        self.assertEqual(result.status, ExecutionStatus.SUCCESS)
        self.assertIn(
            (destination.id, "forget", 2, "task:task-orchestrator"), adapter.calls
        )

    def test_retention_failure_fails_destination_but_keeps_snapshot_id(self) -> None:
        destination = Destination(
            id="destination-retention",
            name="Retenção",
            kind=DestinationKind.LOCAL,
            repository=str(self.root / "repository-retention"),
            password_environment="MACIP_MISSING_TEST_PASSWORD",
            retention=RetentionPolicy(keep_last=2),
        )
        adapter = ControlledAdapter(
            {(destination.id, "forget"): ResticError("prune failed")}
        )

        result = BackupOrchestrator(adapter, StructuredLogger(io.StringIO())).execute(
            self._task(destination), lambda _: "test-password"
        )

        self.assertEqual(result.status, ExecutionStatus.FAILED)
        self.assertEqual(result.destinations[0].status, DestinationStatus.FAILED)
        self.assertEqual(result.destinations[0].snapshot_id, "snapshot")
        self.assertIn("prune failed", result.destinations[0].error or "")

    def test_retry_recovers_destination_and_logs_attempt(self) -> None:
        destination = self._destinations()[0]
        adapter = ControlledAdapter(
            failures_once={
                (destination.id, "backup"): ResticError("falha transitória")
            }
        )
        log_stream = io.StringIO()

        result = BackupOrchestrator(adapter, StructuredLogger(log_stream)).execute(
            self._task(destination, retry=RetryPolicy(attempts=3)),
            lambda _: "test-password",
        )

        self.assertEqual(result.status, ExecutionStatus.SUCCESS)
        backup_calls = [
            call for call in adapter.calls if call[:2] == (destination.id, "backup")
        ]
        self.assertEqual(len(backup_calls), 2)
        self.assertIn('"event": "backup.destination.retry"', log_stream.getvalue())
        self.assertIn('"attempt": 2', log_stream.getvalue())

    def test_retry_exhaustion_marks_destination_failed(self) -> None:
        destination = self._destinations()[0]
        adapter = ControlledAdapter(
            {(destination.id, "backup"): ResticError("sempre falha")}
        )

        result = BackupOrchestrator(adapter, StructuredLogger(io.StringIO())).execute(
            self._task(destination, retry=RetryPolicy(attempts=2)),
            lambda _: "test-password",
        )

        self.assertEqual(result.status, ExecutionStatus.FAILED)
        backup_calls = [
            call for call in adapter.calls if call[:2] == (destination.id, "backup")
        ]
        self.assertEqual(len(backup_calls), 2)
        self.assertIn("sempre falha", result.destinations[0].error or "")

    def test_no_retention_policy_skips_forget(self) -> None:
        first, _ = self._destinations()
        adapter = ControlledAdapter()

        result = BackupOrchestrator(adapter, StructuredLogger(io.StringIO())).execute(
            self._task(first), lambda _: "test-password"
        )

        self.assertEqual(result.status, ExecutionStatus.SUCCESS)
        self.assertFalse(any(call[1] == "forget" for call in adapter.calls))

    def _task(
        self, *destinations: Destination, retry: RetryPolicy | None = None
    ) -> TaskSpec:
        return TaskSpec(
            id="task-orchestrator",
            name="Tarefa de orquestração",
            client=Client(id="client-orchestrator", name="Cliente de teste"),
            device_id="device-orchestrator",
            sources=(self.source,),
            exclusions=(),
            compression=CompressionMode.AUTO,
            destinations=destinations,
            retry=retry or RetryPolicy(),
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
