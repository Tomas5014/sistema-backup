"""CLI da prova de conceito."""

from __future__ import annotations

import argparse
import json
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
    arguments = parser.parse_args()

    try:
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


if __name__ == "__main__":
    raise SystemExit(main())
