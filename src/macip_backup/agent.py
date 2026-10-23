"""Agente local: executa tarefas offline e registra histórico em SQLite.

O agente não depende de portal/API: tarefa, segredos (ambiente ou kernel
keyring) e histórico são locais. Quando a API central existir, o mesmo
registro poderá ser sincronizado — o fluxo de execução não muda.

Cada execução é gravada como `running` **antes** do backup e atualizada
na conclusão — uma execução interrompida permanece identificável. Os
eventos estruturados da execução são persistidos em
``<store>.logs/<execution_id>.jsonl`` — nunca descartados.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

from .logging import StructuredLogger
from .models import TaskSpec
from .orchestrator import (
    BackupOrchestrator,
    ExecutionResult,
    password_from_environment,
)
from .restic import ResticAdapter
from .store import ExecutionStore


def _load_task(path: Path) -> TaskSpec:
    with path.open(encoding="utf-8") as config_file:
        return TaskSpec.from_dict(json.load(config_file))


def run_task(config_path: Path, store_path: Path) -> ExecutionResult:
    """Executa uma tarefa como agente local e persiste o resultado.

    Credenciais resolvem pelo mesmo mecanismo dos demais comandos:
    variável de ambiente → kernel keyring (`macip.<nome>`).
    """
    task = _load_task(config_path)
    store_path = Path(store_path)
    execution_id = str(uuid.uuid4())
    store = ExecutionStore(store_path)
    try:
        store.record_start(
            task, execution_id, datetime.now(UTC).isoformat()
        )
        log_directory = store_path.parent / "logs"
        log_directory.mkdir(parents=True, exist_ok=True)
        log_path = log_directory / f"{execution_id}.jsonl"
        with log_path.open("a", encoding="utf-8") as log_stream:
            result = BackupOrchestrator(
                ResticAdapter(timeouts=task.timeouts),
                StructuredLogger(log_stream),
            ).execute(task, password_from_environment, execution_id)
        store.record_finish(result)
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
