"""Adaptador seguro para o binário restic."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from .models import CompressionMode, Destination, OperationTimeouts


class ResticError(RuntimeError):
    pass


class ResticCommandError(ResticError):
    def __init__(self, command_name: str, return_code: int, detail: str) -> None:
        super().__init__(f"restic {command_name} falhou (código {return_code}): {detail}")
        self.command_name = command_name
        self.return_code = return_code
        self.detail = detail


class ResticTimeoutError(ResticError):
    def __init__(self, operation: str, timeout_seconds: int, detail: str = "") -> None:
        reason = f"tempo limite de {timeout_seconds}s excedido em restic {operation}"
        if detail:
            reason = f"{reason}: {detail}"
        super().__init__(reason)
        self.operation = operation
        self.timeout_seconds = timeout_seconds
        self.detail = detail


@dataclass(frozen=True)
class BackupSummary:
    snapshot_id: str
    files_processed: int | None
    bytes_processed: int | None


class ResticAdapter:
    """Executa comandos estruturados. Senhas trafegam somente pelo ambiente."""

    def __init__(
        self,
        binary: str = "restic",
        cache_directory: Path | None = None,
        timeouts: OperationTimeouts | None = None,
    ) -> None:
        self._binary = binary
        self._cache_directory = cache_directory
        self._timeouts = timeouts or OperationTimeouts()

    def initialize(
        self,
        destination: Destination,
        password: str,
        timeout_seconds: int | None = None,
    ) -> None:
        repository = destination.repository_path
        if (repository / "config").is_file():
            return
        self._run(
            destination,
            password,
            ["init", "--repository-version", "2"],
            timeout_seconds or self._timeouts.initialize_seconds,
        )

    def backup(
        self,
        task_id: str,
        client_id: str,
        sources: Sequence[Path],
        exclusions: Sequence[str],
        compression: CompressionMode,
        destination: Destination,
        password: str,
        timeout_seconds: int | None = None,
    ) -> BackupSummary:
        arguments = ["--json", "--compression", compression.value, "backup"]
        for exclusion in exclusions:
            arguments.extend(["--exclude", exclusion])
        arguments.extend(["--tag", f"task:{task_id}", "--tag", f"client:{client_id}"])
        arguments.extend(str(source.expanduser().resolve()) for source in sources)
        output = self._run(
            destination, password, arguments, timeout_seconds or self._timeouts.backup_seconds
        )
        return _parse_backup_summary(output)

    def snapshots(
        self, destination: Destination, password: str, timeout_seconds: int | None = None
    ) -> list[dict[str, Any]]:
        output = self._run(
            destination,
            password,
            ["--json", "snapshots"],
            timeout_seconds or self._timeouts.snapshots_seconds,
        )
        try:
            snapshots = json.loads(output)
        except json.JSONDecodeError as error:
            raise ResticError("restic snapshots não retornou JSON válido") from error
        if not isinstance(snapshots, list):
            raise ResticError("restic snapshots retornou formato inesperado")
        return snapshots

    def restore(
        self,
        destination: Destination,
        password: str,
        snapshot_id: str,
        target: Path,
        source_subfolder: Path | None = None,
        timeout_seconds: int | None = None,
    ) -> None:
        target.mkdir(parents=True, exist_ok=True)
        snapshot_selector = snapshot_id
        if source_subfolder is not None:
            snapshot_selector = f"{snapshot_id}:{source_subfolder.expanduser().resolve()}"
        self._run(
            destination,
            password,
            ["restore", snapshot_selector, "--target", str(target.resolve()), "--verify"],
            timeout_seconds or self._timeouts.restore_seconds,
        )

    def check(
        self,
        destination: Destination,
        password: str,
        *,
        read_data: bool = False,
        timeout_seconds: int | None = None,
    ) -> None:
        arguments = ["check"]
        if read_data:
            arguments.append("--read-data")
        self._run(
            destination, password, arguments, timeout_seconds or self._timeouts.check_seconds
        )

    def _run(
        self,
        destination: Destination,
        password: str,
        arguments: Sequence[str],
        timeout_seconds: int,
    ) -> str:
        if not password:
            raise ResticError("senha do repositório não foi fornecida")
        if shutil.which(self._binary) is None and not Path(self._binary).is_file():
            raise ResticError(f"binário restic não encontrado: {self._binary}")

        command = [self._binary, "--repo", str(destination.repository_path), *arguments]
        environment = os.environ.copy()
        environment["RESTIC_PASSWORD"] = password
        if self._cache_directory is not None:
            self._cache_directory.mkdir(parents=True, exist_ok=True)
            environment["RESTIC_CACHE_DIR"] = str(self._cache_directory.resolve())
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
        )
        operation = _operation_name(arguments)
        try:
            stdout, stderr = process.communicate(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            stdout, stderr = process.communicate()
            detail = _safe_error_detail(stderr or stdout)
            raise ResticTimeoutError(operation, timeout_seconds, detail)
        if process.returncode != 0:
            detail = _safe_error_detail(stderr or stdout)
            raise ResticCommandError(operation, process.returncode, detail)
        return stdout


def _parse_backup_summary(output: str) -> BackupSummary:
    for line in reversed(output.splitlines()):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("message_type") == "summary" and event.get("snapshot_id"):
            return BackupSummary(
                snapshot_id=event["snapshot_id"],
                files_processed=event.get("total_files_processed"),
                bytes_processed=event.get("total_bytes_processed"),
            )
    raise ResticError("restic backup não retornou resumo com snapshot_id")


def _safe_error_detail(detail: str) -> str:
    compact = " ".join(detail.strip().split())
    compact = re.sub(r"(?<=://)[^\s/@:]+:[^\s/@]+@", "***@", compact)
    return compact[-1_000:] or "sem detalhe"


def _operation_name(arguments: Sequence[str]) -> str:
    operations = {"init", "backup", "snapshots", "restore", "check"}
    return next((argument for argument in arguments if argument in operations), arguments[0])
