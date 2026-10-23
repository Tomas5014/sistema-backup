"""Orquestra destinos independentes de uma tarefa."""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from time import monotonic, sleep
from typing import Callable, Protocol

from .credentials import credential_from_environment_or_os
from .logging import StructuredLogger
from .models import ConfigurationError, Destination, TaskSpec
from .restic import (
    BackupSummary,
    ResticAdapter,
    ResticCommandError,
    ResticError,
    ResticTimeoutError,
)


class DestinationStatus(StrEnum):
    SUCCESS = "success"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class ExecutionStatus(StrEnum):
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


@dataclass(frozen=True)
class AttemptResult:
    """Registro consultável de cada tentativa de um destino."""

    attempt: int
    status: DestinationStatus
    started_at_utc: str
    finished_at_utc: str
    duration_seconds: float
    retryable: bool
    snapshot_id: str | None = None
    snapshots_available: int | None = None
    files_processed: int | None = None
    bytes_processed: int | None = None
    error: str | None = None
    exit_code: int | None = None
    failed_operation: str | None = None
    timeout_seconds: int | None = None


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
    failed_operation: str | None = None
    timeout_seconds: int | None = None
    attempts: tuple[AttemptResult, ...] = ()


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
    """Mesma resolução de segredos de todos os comandos: env → kernel keyring."""
    return credential_from_environment_or_os(destination.password_environment)


class ExecutionRecorder(Protocol):
    """Gravação incremental de tentativas/resultados durante a execução.

    Implementado por ExecutionStore (agente). Chamadores que não
    persistem (ex.: `run` sem --store) passam ``None``.
    """

    def attempt_started(
        self,
        execution_id: str,
        destination_id: str,
        attempt: int,
        started_at_utc: str,
    ) -> None: ...

    def attempt_snapshot(
        self,
        execution_id: str,
        destination_id: str,
        attempt: int,
        snapshot_id: str,
    ) -> None: ...

    def attempt_finished(
        self,
        execution_id: str,
        destination_id: str,
        attempt: "AttemptResult",
    ) -> None: ...

    def destination_finished(
        self, execution_id: str, result: "DestinationResult"
    ) -> None: ...


# Classificação de retry por conteúdo do erro do backend. Restic usa
# códigos de saída genéricos (quase sempre 1); a classificação usa o
# texto de erro. Limitação documentada: padrões não reconhecidos são
# tratados como NÃO repetíveis (conservador — prefere perder uma
# retentativa a martelar um erro determinístico).
_NON_RETRYABLE_COMMAND_PATTERNS = (
    "wrong password",
    "no key found",
    "permission denied",
    "access denied",
    "accessdenied",
    "host key",
    "authentication failed",
    "invalidaccesskeyid",
    "signaturedoesnotmatch",
    "forbidden",
)
_TRANSIENT_COMMAND_PATTERNS = (
    "connection refused",
    "connection reset",
    "timed out",
    "timeout",
    "no route to host",
    "network is unreachable",
    "temporary failure",
    "service unavailable",
    "i/o timeout",
    "unexpected eof",
    "broken pipe",
)


def _is_retryable_command_error(error: ResticCommandError) -> bool:
    detail = error.detail.lower()
    if any(pattern in detail for pattern in _NON_RETRYABLE_COMMAND_PATTERNS):
        return False
    return any(pattern in detail for pattern in _TRANSIENT_COMMAND_PATTERNS)


# Latência máxima documentada da resposta a cancelamento durante o
# backoff entre tentativas.
_CANCEL_POLL_SECONDS = 0.1


class BackupOrchestrator:
    def __init__(self, adapter: ResticAdapter, logger: StructuredLogger | None = None) -> None:
        self._adapter = adapter
        self._logger = logger or StructuredLogger()

    def execute(
        self,
        task: TaskSpec,
        password_resolver: PasswordResolver = password_from_environment,
        execution_id: str | None = None,
        recorder: ExecutionRecorder | None = None,
    ) -> ExecutionResult:
        task.validate_for_execution()
        execution_id = execution_id or str(uuid.uuid4())
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
            self._cancelled_destination(execution_id, destination, recorder)
            if self._is_cancelled()
            else self._execute_destination(
                task, execution_id, destination, password_resolver, recorder
            )
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

    def _is_cancelled(self) -> bool:
        return bool(getattr(self._adapter, "cancelled", False))

    def _sleep_until_cancelled(self, seconds: int) -> bool:
        """Backoff sensível a cancelamento — resposta em ≤0.1s.

        Não usa threading.Event: o handler de sinal roda na main thread e
        `Event.set()` adquire um cond-lock que pode congelar o handler.
        Poll em fatias é a alternativa sem locks.
        """
        deadline = monotonic() + seconds
        while True:
            if self._is_cancelled():
                return True
            remaining = deadline - monotonic()
            if remaining <= 0:
                return False
            sleep(min(remaining, _CANCEL_POLL_SECONDS))

    def _cancelled_destination(
        self,
        execution_id: str,
        destination: Destination,
        recorder: ExecutionRecorder | None,
    ) -> DestinationResult:
        """Destino nem iniciado após cancelamento — registrado como
        interrupted, sem tentativas, consistente com SQLite e eventos."""
        reason = getattr(self._adapter, "cancel_reason", None) or "cancelado"
        now = _utc_now()
        self._logger.emit(
            "warning",
            "backup.destination.interrupted",
            execution_id=execution_id,
            destination_id=destination.id,
            reason=reason,
        )
        result = DestinationResult(
            destination_id=destination.id,
            status=DestinationStatus.INTERRUPTED,
            started_at_utc=now,
            finished_at_utc=now,
            duration_seconds=0.0,
            error=reason,
            attempts=(),
        )
        if recorder is not None:
            recorder.destination_finished(execution_id, result)
        return result

    def _execute_destination(
        self,
        task: TaskSpec,
        execution_id: str,
        destination: Destination,
        password_resolver: PasswordResolver,
        recorder: ExecutionRecorder | None,
    ) -> DestinationResult:
        attempts: list[AttemptResult] = []
        summary: BackupSummary | None = None
        for attempt in range(1, task.retry.attempts + 1):
            # Cancelamento é conferido ANTES de qualquer nova tentativa —
            # inclusive logo após o backoff (evita um initialize espúrio).
            if self._is_cancelled():
                break
            attempt_result, summary = self._attempt_destination(
                task,
                execution_id,
                destination,
                password_resolver,
                attempt,
                resume_summary=summary,
                recorder=recorder,
            )
            attempts.append(attempt_result)
            if attempt_result.status is DestinationStatus.SUCCESS:
                break
            if (
                not attempt_result.retryable
                or attempt == task.retry.attempts
                or self._is_cancelled()
            ):
                break
            self._logger.emit(
                "warning",
                "backup.destination.retry",
                client_id=task.client.id,
                device_id=task.device_id,
                task_id=task.id,
                execution_id=execution_id,
                destination_id=destination.id,
                attempt=attempt + 1,
                backoff_seconds=task.retry.backoff_seconds,
                previous_error=attempt_result.error,
            )
            if self._sleep_until_cancelled(task.retry.backoff_seconds):
                break
        final_result = self._finalize_destination(destination, attempts)
        if recorder is not None:
            recorder.destination_finished(execution_id, final_result)
        return final_result

    def _finalize_destination(
        self, destination: Destination, attempts: list[AttemptResult]
    ) -> DestinationResult:
        last = attempts[-1]
        status = last.status
        if status is not DestinationStatus.SUCCESS and self._is_cancelled():
            status = DestinationStatus.INTERRUPTED
        return DestinationResult(
            destination_id=destination.id,
            status=status,
            started_at_utc=last.started_at_utc,
            finished_at_utc=last.finished_at_utc,
            duration_seconds=last.duration_seconds,
            snapshot_id=last.snapshot_id,
            snapshots_available=last.snapshots_available,
            files_processed=last.files_processed,
            bytes_processed=last.bytes_processed,
            error=last.error,
            exit_code=last.exit_code,
            failed_operation=last.failed_operation,
            timeout_seconds=last.timeout_seconds,
            attempts=tuple(attempts),
        )

    def _attempt_destination(
        self,
        task: TaskSpec,
        execution_id: str,
        destination: Destination,
        password_resolver: PasswordResolver,
        attempt: int,
        resume_summary: BackupSummary | None,
        recorder: ExecutionRecorder | None,
    ) -> tuple[AttemptResult, BackupSummary | None]:
        started_at = _utc_now()
        started_monotonic = monotonic()
        common = {
            "client_id": task.client.id,
            "device_id": task.device_id,
            "task_id": task.id,
            "execution_id": execution_id,
            "destination_id": destination.id,
            "attempt": attempt,
        }
        if recorder is not None:
            recorder.attempt_started(
                execution_id, destination.id, attempt, started_at
            )
        self._logger.emit("info", "backup.destination.started", phase="initialize", **common)
        summary = resume_summary
        try:
            destination.validate_for_execution()
            password = password_resolver(destination)
            self._adapter.initialize(
                destination, password, task.timeouts.initialize_seconds
            )
            if summary is None:
                summary = self._adapter.backup(
                    task.id,
                    task.client.id,
                    task.source_paths,
                    task.exclusions,
                    task.compression,
                    destination,
                    password,
                    task.timeouts.backup_seconds,
                )
                if recorder is not None:
                    recorder.attempt_snapshot(
                        execution_id,
                        destination.id,
                        attempt,
                        summary.snapshot_id,
                    )
            else:
                self._logger.emit(
                    "info",
                    "backup.destination.resume",
                    phase="resume_pending",
                    snapshot_id=summary.snapshot_id,
                    **common,
                )
            self._adapter.check(
                destination, password, timeout_seconds=task.timeouts.check_seconds
            )
            if destination.retention.keep_last is not None:
                self._adapter.forget(
                    destination,
                    password,
                    keep_last=destination.retention.keep_last,
                    tag=f"task:{task.id}",
                    timeout_seconds=task.timeouts.forget_seconds,
                )
            snapshots = self._adapter.snapshots(
                destination, password, task.timeouts.snapshots_seconds
            )
            attempt_result = AttemptResult(
                attempt=attempt,
                status=DestinationStatus.SUCCESS,
                started_at_utc=started_at,
                finished_at_utc=_utc_now(),
                duration_seconds=round(monotonic() - started_monotonic, 3),
                retryable=False,
                snapshot_id=summary.snapshot_id,
                snapshots_available=len(snapshots),
                files_processed=summary.files_processed,
                bytes_processed=summary.bytes_processed,
            )
            self._logger.emit(
                "info",
                "backup.destination.finished",
                phase="verify_structure",
                status=attempt_result.status,
                duration_seconds=attempt_result.duration_seconds,
                snapshot_id=attempt_result.snapshot_id,
                snapshots_available=attempt_result.snapshots_available,
                files_processed=attempt_result.files_processed,
                bytes_processed=attempt_result.bytes_processed,
                **common,
            )
            if recorder is not None:
                recorder.attempt_finished(
                    execution_id, destination.id, attempt_result
                )
            return attempt_result, summary
        except ResticTimeoutError as error:
            exit_code = None
            detail = str(error)
            failed_operation = error.operation
            timeout_seconds = error.timeout_seconds
            retryable = True
        except ResticCommandError as error:
            exit_code = error.return_code
            detail = error.detail
            failed_operation = error.command_name
            timeout_seconds = None
            retryable = _is_retryable_command_error(error)
        except (ConfigurationError, ValueError) as error:
            # Configuração inválida ou credencial ausente não se resolvem
            # repetindo — exigem correção antes de nova tentativa.
            exit_code = None
            detail = str(error)
            failed_operation = None
            timeout_seconds = None
            retryable = False
        except ResticError as error:
            # Pré-condições internas do adaptador (binário ausente, destino
            # sem configuração, resposta inválida) também são determinísticas.
            exit_code = None
            detail = str(error)
            failed_operation = None
            timeout_seconds = None
            retryable = False
        except OSError as error:
            # Falhas de transporte/disco podem ser transitórias.
            exit_code = None
            detail = str(error)
            failed_operation = None
            timeout_seconds = None
            retryable = True

        attempt_result = AttemptResult(
            attempt=attempt,
            status=DestinationStatus.FAILED,
            started_at_utc=started_at,
            finished_at_utc=_utc_now(),
            duration_seconds=round(monotonic() - started_monotonic, 3),
            retryable=retryable,
            snapshot_id=summary.snapshot_id if summary is not None else None,
            error=detail,
            exit_code=exit_code,
            failed_operation=failed_operation,
            timeout_seconds=timeout_seconds,
        )
        self._logger.emit(
            "error",
            "backup.destination.finished",
            phase="failed",
            status=attempt_result.status,
            duration_seconds=attempt_result.duration_seconds,
            error=attempt_result.error,
            exit_code=attempt_result.exit_code,
            failed_operation=attempt_result.failed_operation,
            timeout_seconds=attempt_result.timeout_seconds,
            **common,
        )
        if recorder is not None:
            recorder.attempt_finished(
                execution_id, destination.id, attempt_result
            )
        return attempt_result, summary


def _execution_status(results: tuple[DestinationResult, ...]) -> ExecutionStatus:
    if any(result.status is DestinationStatus.INTERRUPTED for result in results):
        return ExecutionStatus.INTERRUPTED
    successes = sum(result.status is DestinationStatus.SUCCESS for result in results)
    if successes == len(results):
        return ExecutionStatus.SUCCESS
    if successes:
        return ExecutionStatus.PARTIAL
    return ExecutionStatus.FAILED


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()
