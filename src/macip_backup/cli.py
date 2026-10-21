"""CLI da prova de conceito."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import unittest
from pathlib import Path

from .models import ConfigurationError, TaskSpec
from .orchestrator import BackupOrchestrator, ExecutionStatus
from .restic import ResticAdapter, ResticError


def main() -> int:
    parser = argparse.ArgumentParser(description="POC de backup MAC IP")
    subcommands = parser.add_subparsers(dest="command", required=True)
    for command in ("validate", "run"):
        command_parser = subcommands.add_parser(command)
        command_parser.add_argument("config", type=Path)
    subcommands.add_parser(
        "verify-integration",
        help="executa testes reais e falha se restic não estiver disponível",
    )
    subcommands.add_parser(
        "verify-sftp-integration",
        help="executa testes SFTP reais e falha sem pré-requisitos",
    )
    arguments = parser.parse_args()

    try:
        if arguments.command == "verify-integration":
            return verify_integration()
        if arguments.command == "verify-sftp-integration":
            return verify_sftp_integration()
        task = _load_task(arguments.config)
        if arguments.command == "validate":
            task.validate_for_execution()
            print(json.dumps({"status": "valid", "task_id": task.id}))
            return 0
        result = BackupOrchestrator(ResticAdapter()).execute(task)
        print(json.dumps(result.as_dict(), ensure_ascii=False))
        return 0 if result.status is ExecutionStatus.SUCCESS else 2
    except (ConfigurationError, ResticError, OSError, json.JSONDecodeError) as error:
        parser.error(str(error))
        return 2


def _load_task(path: Path) -> TaskSpec:
    with path.open(encoding="utf-8") as config_file:
        return TaskSpec.from_dict(json.load(config_file))


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


if __name__ == "__main__":
    raise SystemExit(main())
