"""Adaptador seguro para o binário restic."""

from __future__ import annotations

import json
import os
import re
import shutil
import shlex
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from .credentials import credential_from_environment_or_os
from .models import CompressionMode, Destination, DestinationKind, OperationTimeouts


_PROCESS_STOP_GRACE_SECONDS = 5
_UNLOCK_TIMEOUT_SECONDS = 120


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


class ResticCancelledError(ResticError):
    """Execução cancelada por sinal externo — nunca repetível."""

    def __init__(self, reason: str, operation: str | None = None) -> None:
        detail = f"{reason} (em restic {operation})" if operation else reason
        super().__init__(detail)
        self.reason = reason
        self.operation = operation


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
        # Cancelamento: atributos simples (atômicos sob a GIL) — o handler
        # de sinal roda na main thread e não pode adquirir locks.
        self._cancel_reason: str | None = None
        self._current_process: subprocess.Popen[str] | None = None
        self.cancel_cleanup_error: str | None = None

    @property
    def cancelled(self) -> bool:
        return self._cancel_reason is not None

    @property
    def cancel_reason(self) -> str | None:
        return self._cancel_reason

    def cancel(self, reason: str) -> None:
        """Cancela: registra o motivo e encerra o grupo do restic em andamento.

        SEGFAULT-safe para handlers de sinal: só leituras/atribuições
        atômicas. Escalada SIGTERM → SIGKILL com prazo limitado; falha de
        limpeza é registrada em ``cancel_cleanup_error``.
        """
        self._cancel_reason = reason
        process = self._current_process
        if process is None or process.pid is None:
            return
        try:
            if not self._stop_for_cancel(process):
                self.cancel_cleanup_error = (
                    "processo ou descendentes não encerraram após escalada SIGKILL"
                )
        except OSError as error:
            self.cancel_cleanup_error = str(error)

    def initialize(
        self,
        destination: Destination,
        password: str,
        timeout_seconds: int | None = None,
    ) -> None:
        timeout = _effective_timeout(
            timeout_seconds, self._timeouts.initialize_seconds, "initialize"
        )
        try:
            self._run(
                destination,
                password,
                ["cat", "config"],
                timeout,
            )
        except ResticCommandError as error:
            # Código 10 é a resposta documentada pelo restic para repositório ausente.
            # Qualquer outro erro pode ser senha, SSH, acesso ou comunicação inválida.
            if error.return_code != 10:
                raise
        else:
            return
        self._run(
            destination,
            password,
            ["init", "--repository-version", "2"],
            timeout,
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
        timeout = _effective_timeout(timeout_seconds, self._timeouts.backup_seconds, "backup")
        arguments = ["--json", "--compression", compression.value, "backup"]
        for exclusion in exclusions:
            arguments.extend(["--exclude", exclusion])
        arguments.extend(["--tag", f"task:{task_id}", "--tag", f"client:{client_id}"])
        arguments.extend(str(source.expanduser().resolve()) for source in sources)
        output = self._run(
            destination, password, arguments, timeout
        )
        return _parse_backup_summary(output)

    def snapshots(
        self, destination: Destination, password: str, timeout_seconds: int | None = None
    ) -> list[dict[str, Any]]:
        timeout = _effective_timeout(
            timeout_seconds, self._timeouts.snapshots_seconds, "snapshots"
        )
        output = self._run(
            destination,
            password,
            ["--json", "snapshots"],
            timeout,
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
        timeout = _effective_timeout(
            timeout_seconds, self._timeouts.restore_seconds, "restore"
        )
        target.mkdir(parents=True, exist_ok=True)
        snapshot_selector = snapshot_id
        if source_subfolder is not None:
            snapshot_selector = f"{snapshot_id}:{source_subfolder.expanduser().resolve()}"
        self._run(
            destination,
            password,
            ["restore", snapshot_selector, "--target", str(target.resolve()), "--verify"],
            timeout,
        )

    def forget(
        self,
        destination: Destination,
        password: str,
        *,
        keep_last: int,
        tag: str | None = None,
        timeout_seconds: int | None = None,
    ) -> None:
        timeout = _effective_timeout(
            timeout_seconds, self._timeouts.forget_seconds, "forget"
        )
        if not isinstance(keep_last, int) or isinstance(keep_last, bool) or keep_last <= 0:
            raise ResticError("keep_last deve ser inteiro positivo")
        arguments = ["forget", "--keep-last", str(keep_last)]
        if tag is not None:
            arguments.extend(["--tag", tag])
        arguments.append("--prune")
        self._run(destination, password, arguments, timeout)

    def check(
        self,
        destination: Destination,
        password: str,
        *,
        read_data: bool = False,
        timeout_seconds: int | None = None,
    ) -> None:
        timeout = _effective_timeout(timeout_seconds, self._timeouts.check_seconds, "check")
        arguments = ["check"]
        if read_data:
            arguments.append("--read-data")
        self._run(
            destination, password, arguments, timeout
        )

    def _run(
        self,
        destination: Destination,
        password: str,
        arguments: Sequence[str],
        timeout_seconds: int,
    ) -> str:
        if self._cancel_reason is not None:
            raise ResticCancelledError(self._cancel_reason)
        if not password:
            raise ResticError("senha do repositório não foi fornecida")
        if shutil.which(self._binary) is None and not Path(self._binary).is_file():
            raise ResticError(f"binário restic não encontrado: {self._binary}")

        command = [
            self._binary,
            "--repo",
            self._repository_reference(destination),
            *self._backend_options(destination),
            *arguments,
        ]
        environment = os.environ.copy()
        environment["RESTIC_PASSWORD"] = password
        self._inject_destination_credentials(destination, environment)
        if self._cache_directory is not None:
            self._cache_directory.mkdir(parents=True, exist_ok=True)
            environment["RESTIC_CACHE_DIR"] = str(self._cache_directory.resolve())
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
            start_new_session=os.name == "posix",
        )
        self._current_process = process
        # Sinal recebido no intervalo Popen→registro: o handler viu
        # current_process=None; a limpeza é garantida aqui.
        if self._cancel_reason is not None:
            self._stop_for_cancel(process)
        operation = _operation_name(arguments)
        try:
            try:
                stdout, stderr = process.communicate(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                stdout, stderr, process_group_terminated = self._stop_timed_out_process(process)
                if self._cancel_reason is not None:
                    raise ResticCancelledError(self._cancel_reason, operation)
                if process_group_terminated:
                    unlock_detail = self._unlock_after_timeout(destination, password)
                    if unlock_detail:
                        stderr = f"{stderr}\nunlock após timeout: {unlock_detail}".strip()
                detail = _safe_error_detail(stderr or stdout)
                raise ResticTimeoutError(operation, timeout_seconds, detail)
        finally:
            self._current_process = None
            cancelled_reason = self._cancel_reason
        if cancelled_reason is not None:
            raise ResticCancelledError(cancelled_reason, operation)
        if process.returncode != 0:
            detail = _safe_error_detail(stderr or stdout)
            raise ResticCommandError(operation, process.returncode, detail)
        return stdout

    def _repository_reference(self, destination: Destination) -> str:
        if destination.kind in {DestinationKind.LOCAL, DestinationKind.SMB}:
            return str(destination.repository_path)
        if destination.kind is DestinationKind.S3:
            if destination.s3 is None:
                raise ResticError("destino S3 sem configuração")
            return destination.s3.repository_reference
        if destination.sftp is None:
            raise ResticError("destino SFTP sem configuração")
        connection = destination.sftp
        # Sintaxe nativa restic: "/" inicial preserva caminho absoluto remoto;
        # sem ela, o caminho é relativo ao diretório inicial do usuário SFTP.
        host = connection.host
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        return f"sftp:{connection.user}@{host}:{connection.remote_path}"

    def _backend_options(self, destination: Destination) -> list[str]:
        if destination.kind is DestinationKind.S3:
            if destination.s3 is None:
                raise ResticError("destino S3 sem configuração")
            connection = destination.s3
            options = []
            if connection.region:
                options.extend(["-o", f"s3.region={connection.region}"])
            if connection.bucket_lookup:
                options.extend(["-o", f"s3.bucket-lookup={connection.bucket_lookup}"])
            if connection.storage_class:
                options.extend(["-o", f"s3.storage-class={connection.storage_class}"])
            return options
        if destination.kind is not DestinationKind.SFTP:
            return []
        if destination.sftp is None:
            raise ResticError("destino SFTP sem configuração")
        connection = destination.sftp
        ssh_arguments = [
            "-oBatchMode=yes",
            "-oStrictHostKeyChecking=yes",
            f"-oUserKnownHostsFile={connection.known_hosts_file}",
            "-oIdentitiesOnly=yes",
            "-oPasswordAuthentication=no",
            "-oKbdInteractiveAuthentication=no",
            "-oPreferredAuthentications=publickey",
            "-i",
            connection.identity_file,
            "-p",
            str(connection.port),
            f"-oConnectTimeout={connection.connect_timeout_seconds}",
            f"-oServerAliveInterval={connection.server_alive_interval_seconds}",
            f"-oServerAliveCountMax={connection.server_alive_count_max}",
        ]
        # restic interpreta sftp.args como argumentos do ssh. shlex.join preserva
        # cada valor como um argumento, inclusive caminhos que contenham espaços.
        return ["-o", f"sftp.args={shlex.join(ssh_arguments)}"]

    def _stop_for_cancel(self, process: subprocess.Popen[str]) -> bool:
        """Encerra o grupo com SIGTERM→SIGKILL. Chamado por cancel() e
        pelo próprio _run quando o sinal chega logo após o Popen."""
        if os.name == "posix" and process.pid is not None:
            pgid = process.pid
            self._signal_process_group(process, signal.SIGCONT)
            self._signal_process_group(process, signal.SIGTERM)
            if self._wait_for_process_group_exit(pgid):
                return True
            self._signal_process_group(process, signal.SIGKILL)
            return self._wait_for_process_group_exit(pgid)
        process.terminate()
        try:
            process.wait(_PROCESS_STOP_GRACE_SECONDS)
            return True
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.wait(_PROCESS_STOP_GRACE_SECONDS)
                return True
            except subprocess.TimeoutExpired:
                return False

    def _stop_timed_out_process(
        self, process: subprocess.Popen[str]
    ) -> tuple[str, str, bool]:
        if os.name == "posix" and process.pid is not None:
            return self._stop_posix_process_group(process)

        self._signal_process_group(process, signal.SIGTERM)
        try:
            stdout, stderr = process.communicate(timeout=_PROCESS_STOP_GRACE_SECONDS)
            return stdout, stderr, True
        except subprocess.TimeoutExpired:
            self._signal_process_group(process, signal.SIGKILL)
            try:
                stdout, stderr = process.communicate(timeout=_PROCESS_STOP_GRACE_SECONDS)
                return stdout, stderr, True
            except subprocess.TimeoutExpired:
                self._close_pipes(process)
                return "", "processo não encerrou após SIGTERM e SIGKILL", False

    def _stop_posix_process_group(
        self, process: subprocess.Popen[str]
    ) -> tuple[str, str, bool]:
        pgid = process.pid
        assert pgid is not None
        # Um descendente parado não processa SIGTERM. Retomá-lo primeiro evita
        # que ele sobreviva ao encerramento do processo principal.
        self._signal_process_group(process, signal.SIGCONT)
        self._signal_process_group(process, signal.SIGTERM)

        stdout = ""
        stderr = ""
        parent_finished = False
        try:
            stdout, stderr = process.communicate(timeout=_PROCESS_STOP_GRACE_SECONDS)
            parent_finished = True
        except subprocess.TimeoutExpired:
            pass

        if self._process_group_exists(pgid):
            self._signal_process_group(process, signal.SIGKILL)
            if not parent_finished:
                try:
                    stdout, stderr = process.communicate(timeout=_PROCESS_STOP_GRACE_SECONDS)
                    parent_finished = True
                except subprocess.TimeoutExpired:
                    pass
            if not self._wait_for_process_group_exit(pgid):
                self._close_pipes(process)
                message = "processo ou descendentes não encerraram após SIGTERM e SIGKILL"
                return stdout, f"{stderr}\n{message}".strip(), False

        if not parent_finished:
            self._close_pipes(process)
            return stdout, f"{stderr}\nprocesso não encerrou após SIGTERM e SIGKILL".strip(), False
        return stdout, stderr, True

    def _unlock_after_timeout(self, destination: Destination, password: str) -> str | None:
        if self._cancel_reason is not None:
            return None  # cancelado — não vale iniciar um unlock
        command = [
            self._binary,
            "--repo",
            self._repository_reference(destination),
            *self._backend_options(destination),
            "unlock",
        ]
        environment = os.environ.copy()
        environment["RESTIC_PASSWORD"] = password
        self._inject_destination_credentials(destination, environment)
        if self._cache_directory is not None:
            self._cache_directory.mkdir(parents=True, exist_ok=True)
            environment["RESTIC_CACHE_DIR"] = str(self._cache_directory.resolve())
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
            start_new_session=os.name == "posix",
        )
        # O unlock também é alcançado por cancel(): fica registrado como
        # processo corrente até encerrar.
        self._current_process = process
        if self._cancel_reason is not None:
            self._stop_for_cancel(process)
        try:
            stdout, stderr = process.communicate(timeout=_UNLOCK_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            stdout, stderr, _ = self._stop_timed_out_process(process)
            return _safe_error_detail(
                f"tempo limite de {_UNLOCK_TIMEOUT_SECONDS}s excedido: {stderr or stdout}"
            )
        finally:
            self._current_process = None
        if process.returncode != 0:
            return _safe_error_detail(
                f"falhou (código {process.returncode}): {stderr or stdout}"
            )
        return None

    @staticmethod
    def _inject_destination_credentials(
        destination: Destination, environment: dict[str, str]
    ) -> None:
        if destination.kind is not DestinationKind.S3:
            return
        connection = destination.s3
        if connection is None:
            raise ResticError("destino S3 sem configuração")
        for name, aws_name in (
            (connection.access_key_environment, "AWS_ACCESS_KEY_ID"),
            (connection.secret_key_environment, "AWS_SECRET_ACCESS_KEY"),
        ):
            environment[aws_name] = credential_from_environment_or_os(name)

    @staticmethod
    def _process_group_exists(pgid: int) -> bool:
        """Grupo tem membro vivo (não-zumbi)?

        killpg(0) conta zumbis — um líder encerrado por SIGTERM mas ainda
        não recolhido pela thread principal faria o grupo parecer ativo
        para sempre. Com /proc disponível, verificamos estado real.
        """
        proc_root = Path("/proc")
        if proc_root.is_dir():
            for entry in os.listdir(proc_root):
                if not entry.isdigit():
                    continue
                try:
                    stat = (proc_root / entry / "stat").read_text()
                except OSError:
                    continue
                # stat: pid (comm) state ppid pgrp ... — comm pode ter
                # espaços/parênteses; os campos começam após o último ')'.
                fields = stat.rsplit(")", 1)[-1].split()
                if len(fields) < 3:
                    continue
                if fields[0] in ("Z", "X"):
                    continue
                if int(fields[2]) == pgid:
                    return True
            return False
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    @classmethod
    def _wait_for_process_group_exit(cls, pgid: int) -> bool:
        deadline = time.monotonic() + _PROCESS_STOP_GRACE_SECONDS
        while cls._process_group_exists(pgid):
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)
        return True

    @staticmethod
    def _signal_process_group(process: subprocess.Popen[str], signum: int) -> None:
        try:
            if os.name == "posix" and process.pid is not None:
                os.killpg(process.pid, signum)
            elif signum == signal.SIGTERM:
                process.terminate()
            else:
                process.kill()
        except ProcessLookupError:
            pass

    @staticmethod
    def _close_pipes(process: subprocess.Popen[str]) -> None:
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass


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


def _effective_timeout(value: int | None, default: int, operation: str) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ResticError(f"timeout de restic {operation} deve ser inteiro positivo")
    return value


def _safe_error_detail(detail: str) -> str:
    compact = " ".join(detail.strip().split())
    compact = re.sub(r"(?<=://)[^\s/@:]+:[^\s/@]+@", "***@", compact)
    return compact[-1_000:] or "sem detalhe"


def _operation_name(arguments: Sequence[str]) -> str:
    operations = {"init", "backup", "snapshots", "restore", "check", "cat"}
    return next((argument for argument in arguments if argument in operations), arguments[0])
