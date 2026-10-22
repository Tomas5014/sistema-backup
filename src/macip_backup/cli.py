"""CLI da prova de conceito."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import unittest
from pathlib import Path

from .models import ConfigurationError, DestinationKind, TaskSpec
from .orchestrator import BackupOrchestrator, ExecutionStatus, password_from_environment
from .restic import ResticAdapter, ResticError


def main() -> int:
    parser = argparse.ArgumentParser(description="POC de backup MAC IP")
    subcommands = parser.add_subparsers(dest="command", required=True)
    for command in ("validate", "run"):
        command_parser = subcommands.add_parser(command)
        command_parser.add_argument("config", type=Path)
    snapshots_parser = subcommands.add_parser("snapshots", help="lista snapshots de um destino")
    snapshots_parser.add_argument("config", type=Path)
    snapshots_parser.add_argument("destination_id")
    check_parser = subcommands.add_parser("check", help="verifica a integridade de um destino")
    check_parser.add_argument("config", type=Path)
    check_parser.add_argument("destination_id")
    check_parser.add_argument("--read-data", action="store_true")
    restore_parser = subcommands.add_parser("restore", help="restaura um snapshot em diretório novo")
    restore_parser.add_argument("config", type=Path)
    restore_parser.add_argument("destination_id")
    restore_parser.add_argument("snapshot_id")
    restore_parser.add_argument("target", type=Path)
    forget_parser = subcommands.add_parser(
        "forget", help="aplica a política de retenção declarada no destino"
    )
    forget_parser.add_argument("config", type=Path)
    forget_parser.add_argument("destination_id")
    subcommands.add_parser(
        "verify-integration",
        help="executa testes reais e falha se restic não estiver disponível",
    )
    subcommands.add_parser(
        "verify-sftp-integration",
        help="executa testes SFTP reais e falha sem pré-requisitos",
    )
    subcommands.add_parser(
        "verify-s3-integration",
        help="executa testes S3 reais e falha sem pré-requisitos",
    )
    arguments = parser.parse_args()

    try:
        if arguments.command == "verify-integration":
            return verify_integration()
        if arguments.command == "verify-sftp-integration":
            return verify_sftp_integration()
        if arguments.command == "verify-s3-integration":
            return verify_s3_integration()
        task = _load_task(arguments.config)
        if arguments.command == "validate":
            task.validate_for_execution()
            print(json.dumps({"status": "valid", "task_id": task.id}))
            return 0
        if arguments.command in {"snapshots", "check", "restore", "forget"}:
            return _run_recovery_operation(task, arguments)
        result = BackupOrchestrator(ResticAdapter()).execute(task)
        print(json.dumps(result.as_dict(), ensure_ascii=False))
        return 0 if result.status is ExecutionStatus.SUCCESS else 2
    except (ConfigurationError, ResticError, OSError, json.JSONDecodeError) as error:
        parser.error(str(error))
        return 2


def _load_task(path: Path) -> TaskSpec:
    with path.open(encoding="utf-8") as config_file:
        return TaskSpec.from_dict(json.load(config_file))


def _run_recovery_operation(task: TaskSpec, arguments: argparse.Namespace) -> int:
    destination = _destination_by_id(task, arguments.destination_id)
    task.validate_recovery_for_execution()
    target = (
        _safe_restore_target(task, destination, arguments.target)
        if arguments.command == "restore"
        else None
    )
    destination.validate_for_execution()
    password = password_from_environment(destination)
    adapter = ResticAdapter(timeouts=task.timeouts)

    if arguments.command == "snapshots":
        snapshots = adapter.snapshots(
            destination, password, timeout_seconds=task.timeouts.snapshots_seconds
        )
        print(
            json.dumps(
                {
                    "status": "success",
                    "task_id": task.id,
                    "destination_id": destination.id,
                    "snapshots": snapshots,
                },
                ensure_ascii=False,
            )
        )
        return 0

    if arguments.command == "check":
        adapter.check(
            destination,
            password,
            read_data=arguments.read_data,
            timeout_seconds=task.timeouts.check_seconds,
        )
        print(
            json.dumps(
                {
                    "status": "success",
                    "task_id": task.id,
                    "destination_id": destination.id,
                    "read_data": arguments.read_data,
                },
                ensure_ascii=False,
            )
        )
        return 0

    if arguments.command == "forget":
        keep_last = destination.retention.keep_last
        if keep_last is None:
            raise ConfigurationError(
                f"destino {destination.id} não declara retention.keep_last"
            )
        adapter.forget(
            destination,
            password,
            keep_last=keep_last,
            tag=f"task:{task.id}",
            timeout_seconds=task.timeouts.forget_seconds,
        )
        snapshots = adapter.snapshots(
            destination, password, timeout_seconds=task.timeouts.snapshots_seconds
        )
        print(
            json.dumps(
                {
                    "status": "success",
                    "task_id": task.id,
                    "destination_id": destination.id,
                    "keep_last": keep_last,
                    "snapshots_available": len(snapshots),
                },
                ensure_ascii=False,
            )
        )
        return 0

    assert target is not None
    adapter.restore(
        destination,
        password,
        arguments.snapshot_id,
        target,
        timeout_seconds=task.timeouts.restore_seconds,
    )
    print(
        json.dumps(
            {
                "status": "success",
                "task_id": task.id,
                "destination_id": destination.id,
                "snapshot_id": arguments.snapshot_id,
                "target": str(target),
            },
            ensure_ascii=False,
        )
    )
    return 0


def _destination_by_id(task: TaskSpec, destination_id: str):
    for destination in task.destinations:
        if destination.id == destination_id:
            return destination
    raise ConfigurationError(f"destination_id não encontrado na tarefa: {destination_id}")


def _safe_restore_target(task: TaskSpec, destination, target: Path) -> Path:
    resolved_target = target.expanduser().resolve(strict=False)
    for source in task.sources:
        resolved_source = source.path.expanduser().resolve(strict=False)
        if _paths_overlap(resolved_target, resolved_source):
            raise ConfigurationError(
                f"diretório de restauração não pode sobrepor uma origem: {resolved_target}"
            )
    if destination.kind in {DestinationKind.LOCAL, DestinationKind.SMB}:
        repository = destination.repository_path
        if _paths_overlap(resolved_target, repository):
            raise ConfigurationError(
                f"diretório de restauração não pode sobrepor o repositório: {resolved_target}"
            )
    if resolved_target.exists():
        raise ConfigurationError(f"diretório de restauração já existe: {resolved_target}")
    return resolved_target


def _paths_overlap(first: Path, second: Path) -> bool:
    return (
        first == second
        or first.is_relative_to(second)
        or second.is_relative_to(first)
    )


def verify_integration() -> int:
    if shutil.which("restic") is None:
        print("integração não validada: binário restic não encontrado")
        return 1

    repository_root = Path(__file__).resolve().parents[2]
    tests_directory = repository_root / "tests"
    suite = unittest.defaultTestLoader.discover(
        str(tests_directory), pattern="test_integration_restic.py", top_level_dir=str(repository_root)
    )
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if result.testsRun == 0 or len(result.skipped) == result.testsRun:
        print("integração não validada: nenhum teste real executado")
        return 1
    return 0 if result.wasSuccessful() else 1


def verify_sftp_integration() -> int:
    required_environment = (
        "MACIP_SFTP_INTEGRATION",
        "MACIP_SFTP_HOST",
        "MACIP_SFTP_PORT",
        "MACIP_SFTP_USER",
        "MACIP_SFTP_REMOTE_PATH",
        "MACIP_SFTP_IDENTITY_FILE",
        "MACIP_SFTP_KNOWN_HOSTS_FILE",
    )
    if shutil.which("restic") is None or shutil.which("ssh") is None:
        print("integração SFTP não validada: restic ou ssh não encontrado")
        return 1
    if any(not os.environ.get(name) for name in required_environment):
        print("integração SFTP não validada: ambiente isolado não foi preparado")
        return 1

    repository_root = Path(__file__).resolve().parents[2]
    tests_directory = repository_root / "tests"
    suite = unittest.defaultTestLoader.discover(
        str(tests_directory),
        pattern="test_integration_sftp.py",
        top_level_dir=str(repository_root),
    )
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if result.testsRun == 0 or len(result.skipped) == result.testsRun:
        print("integração SFTP não validada: nenhum teste real executado")
        return 1
    return 0 if result.wasSuccessful() else 1


def verify_s3_integration() -> int:
    required_environment = (
        "MACIP_S3_INTEGRATION",
        "MACIP_S3_ENDPOINT",
        "MACIP_S3_BUCKET",
        "MACIP_S3_ACCESS_KEY_ENV",
        "MACIP_S3_SECRET_KEY_ENV",
    )
    if shutil.which("restic") is None:
        print("integração S3 não validada: binário restic não encontrado")
        return 1
    if any(not os.environ.get(name) for name in required_environment):
        print("integração S3 não validada: ambiente não foi preparado")
        return 1

    repository_root = Path(__file__).resolve().parents[2]
    tests_directory = repository_root / "tests"
    suite = unittest.defaultTestLoader.discover(
        str(tests_directory),
        pattern="test_integration_s3.py",
        top_level_dir=str(repository_root),
    )
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if result.testsRun == 0 or len(result.skipped) == result.testsRun:
        print("integração S3 não validada: nenhum teste real executado")
        return 1
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
