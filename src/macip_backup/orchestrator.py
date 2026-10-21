"""Orquestra destinos independentes de uma tarefa."""

from __future__ import annotations

import os
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from time import monotonic
from typing import Callable

from .logging import StructuredLogger
from .models import Destination, TaskSpec
from .restic import ResticAdapter, ResticCommandError, ResticError


class DestinationStatus(StrEnum):
    SUCCESS = "success"
    FAILED = "failed"


class ExecutionStatus(StrEnum):
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"


@dataclass(frozen=True)
class DestinationResult:
    destination_id: str
    status: DestinationStatus
    started_at_utc: str
    finished_at_utc: str
    duration_seconds: float
    snapshot_id: str | None = None
    snapshots_available: int | None = None
    files_processed: int | None = None
    bytes_processed: int | None = None
    error: str | None = None
    exit_code: int | None = None


@dataclass(frozen=True)
class ExecutionResult:
    execution_id: str
    task_id: str
    client_id: str
    status: ExecutionStatus
    started_at_utc: str
    finished_at_utc: str
    destinations: tuple[DestinationResult, ...]

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


PasswordResolver = Callable[[Destination], str]


def password_from_environment(destination: Destination) -> str:
    password = os.environ.get(destination.password_environment)
    if not password:
        raise ResticError(
            "credencial ausente na variável de ambiente "
            f"{destination.password_environment}"
        )
    return password


class BackupOrchestrator:
    def __init__(self, adapter: ResticAdapter, logger: StructuredLogger | None = None) -> None:
        self._adapter = adapter
        self._logger = logger or StructuredLogger()

    def execute(
        self,
        task: TaskSpec,
        password_resolver: PasswordResolver = password_from_environment,
    ) -> ExecutionResult:
        task.validate_for_execution()
        execution_id = str(uuid.uuid4())
        started_at = _utc_now()
        self._logger.emit(
            "info",
            "backup.execution.started",
            client_id=task.client.id,
            device_id=task.device_id,
            task_id=task.id,
            execution_id=execution_id,
            destinations=len(task.destinations),
        )

        results = tuple(
            self._execute_destination(task, execution_id, destination, password_resolver)
            for destination in task.destinations
        )
        status = _execution_status(results)
        finished_at = _utc_now()
        self._logger.emit(
            "info" if status is ExecutionStatus.SUCCESS else "error",
            "backup.execution.finished",
            client_id=task.client.id,
            device_id=task.device_id,
            task_id=task.id,
            execution_id=execution_id,
            status=status,
            destinations_successful=sum(item.status is DestinationStatus.SUCCESS for item in results),
            destinations_total=len(results),
        )
        return ExecutionResult(
            execution_id=execution_id,
            task_id=task.id,
            client_id=task.client.id,
            status=status,
            started_at_utc=started_at,
            finished_at_utc=finished_at,
            destinations=results,
        )

    def _execute_destination(
        self,
        task: TaskSpec,
        execution_id: str,
        destination: Destination,
        password_resolver: PasswordResolver,
    ) -> DestinationResult:
        started_at = _utc_now()
        started_monotonic = monotonic()
        common = {
            "client_id": task.client.id,
            "device_id": task.device_id,
            "task_id": task.id,
            "execution_id": execution_id,
            "destination_id": destination.id,
            "attempt": 1,
        }
        self._logger.emit("info", "backup.destination.started", phase="initialize", **common)
        try:
            password = password_resolver(destination)
            self._adapter.initialize(destination, password)
            summary = self._adapter.backup(
                task.id,
                task.client.id,
                task.sources,
                task.exclusions,
                task.compression,
                destination,
                password,
            )
            self._adapter.check(destination, password)
            snapshots = self._adapter.snapshots(destination, password)
            result = DestinationResult(
                destination_id=destination.id,
                status=DestinationStatus.SUCCESS,
                started_at_utc=started_at,
                finished_at_utc=_utc_now(),
                duration_seconds=round(monotonic() - started_monotonic, 3),
                snapshot_id=summary.snapshot_id,
                snapshots_available=len(snapshots),
                files_processed=summary.files_processed,
                bytes_processed=summary.bytes_processed,
            )
            self._logger.emit(
                "info",
                "backup.destination.finished",
                phase="verify_structure",
                status=result.status,
                duration_seconds=result.duration_seconds,
                snapshot_id=result.snapshot_id,
                snapshots_available=result.snapshots_available,
                files_processed=result.files_processed,
                bytes_processed=result.bytes_processed,
                **common,
            )
            return result
        except ResticCommandError as error:
            exit_code = error.return_code
            detail = error.detail
        except (ResticError, OSError, ValueError) as error:
            exit_code = None
            detail = str(error)

        result = DestinationResult(
            destination_id=destination.id,
            status=DestinationStatus.FAILED,
            started_at_utc=started_at,
            finished_at_utc=_utc_now(),
            duration_seconds=round(monotonic() - started_monotonic, 3),
            error=detail,
            exit_code=exit_code,
        )
        self._logger.emit(
            "error",
            "backup.destination.finished",
            phase="failed",
            status=result.status,
            duration_seconds=result.duration_seconds,
            error=result.error,
            exit_code=result.exit_code,
            **common,
        )
        return result


def _execution_status(results: tuple[DestinationResult, ...]) -> ExecutionStatus:
    successes = sum(result.status is DestinationStatus.SUCCESS for result in results)
    if successes == len(results):
        return ExecutionStatus.SUCCESS
    if successes:
        return ExecutionStatus.PARTIAL
    return ExecutionStatus.FAILED


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()
