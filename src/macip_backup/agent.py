"""Agente local: executa tarefas offline e registra histórico em SQLite.

O agente não depende de portal/API: tarefa, segredos (ambiente ou kernel
keyring) e histórico são locais. Quando a API central existir, o mesmo
registro poderá ser sincronizado — o fluxo de execução não muda.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

from .credentials import credential_from_environment_or_os
from .logging import StructuredLogger
from .models import Destination, TaskSpec
from .orchestrator import BackupOrchestrator, ExecutionResult
from .restic import ResticAdapter
from .store import ExecutionStore


def _load_task(path: Path) -> TaskSpec:
    with path.open(encoding="utf-8") as config_file:
        return TaskSpec.from_dict(json.load(config_file))


def _os_aware_password_resolver(destination: Destination) -> str:
    return credential_from_environment_or_os(destination.password_environment)


def run_task(config_path: Path, store_path: Path) -> ExecutionResult:
    """Executa uma tarefa como agente local e persiste o resultado."""
    task = _load_task(config_path)
    store = ExecutionStore(store_path)
    try:
        result = BackupOrchestrator(
            ResticAdapter(timeouts=task.timeouts), StructuredLogger(io.StringIO())
        ).execute(task, _os_aware_password_resolver)
        store.record(task, result)
    finally:
        store.close()
    return result


def history(store_path: Path, task_id: str | None = None) -> list[dict[str, object]]:
    store = ExecutionStore(store_path)
    try:
        return store.history(task_id)
    finally:
        store.close()


def history_json(store_path: Path, task_id: str | None = None) -> str:
    return json.dumps(history(store_path, task_id), ensure_ascii=False, indent=2)
