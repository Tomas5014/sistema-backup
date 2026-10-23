"""Agente local: executa tarefas offline e registra histórico em SQLite.

O agente não depende de portal/API: tarefa, segredos (ambiente ou kernel
keyring) e histórico são locais. Quando a API central existir, o mesmo
registro poderá ser sincronizado — o fluxo de execução não muda.

Execução exclusiva: um `flock` no arquivo `<store>.lock` impede duas
execuções simultâneas — vale igualmente para a CLI e para o service
systemd (mesmo caminho de código). O lock é liberado pelo kernel mesmo
em morte abrupta.

Execuções são gravadas como `running` **antes** do backup e atualizadas
na conclusão. No início, execuções ainda `running` (morte anterior sem
sinal) são marcadas `interrupted`; SIGTERM/SIGINT também marcam a
execução corrente como `interrupted` antes de sair — o histórico nunca
confunde "parou" com "está rodando".

Os eventos estruturados da execução são persistidos em
``<store>.logs/<execution_id>.jsonl`` — nunca descartados.
"""

from __future__ import annotations

import fcntl
import json
import signal
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
from .restic import ResticAdapter, ResticError
from .store import ExecutionStore


def _load_task(path: Path) -> TaskSpec:
    with path.open(encoding="utf-8") as config_file:
        return TaskSpec.from_dict(json.load(config_file))


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _acquire_execution_lock(store_path: Path) -> object:
    """flock exclusivo não bloqueante — impede execuções simultâneas."""
    lock_path = Path(f"{store_path}.lock")
    lock_file = lock_path.open("a")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        lock_file.close()
        raise ResticError(
            f"já existe uma execução do agente em andamento ({lock_path})"
        ) from error
    return lock_file


def run_task(config_path: Path, store_path: Path) -> ExecutionResult:
    """Executa uma tarefa como agente local e persiste o resultado.

    Credenciais resolvem pelo mesmo mecanismo dos demais comandos:
    variável de ambiente → kernel keyring (`macip.<nome>`).
    """
    task = _load_task(config_path)
    store_path = Path(store_path)
    execution_id = str(uuid.uuid4())
    lock_file = _acquire_execution_lock(store_path)
    store = ExecutionStore(store_path)
    previous_handlers: dict[int, object] = {}
    try:
        store.mark_stale_interrupted(_now())
        store.record_start(task, execution_id, _now())

        def _interrupted(signum: int, _frame: object) -> None:
            # Best-effort: se o sinal cair no meio de outra escrita
            # sqlite, a marcação pode falhar — a varredura do próximo
            # run_task ainda marca a execução como interrupted.
            store.mark_interrupted(execution_id, _now())
            raise SystemExit(128 + signum)

        for signum in (signal.SIGTERM, signal.SIGINT):
            previous_handlers[signum] = signal.signal(signum, _interrupted)

        log_directory = store_path.parent / "logs"
        log_directory.mkdir(parents=True, exist_ok=True)
        log_path = log_directory / f"{execution_id}.jsonl"
        with log_path.open("a", encoding="utf-8") as log_stream:
            result = BackupOrchestrator(
                ResticAdapter(timeouts=task.timeouts),
                StructuredLogger(log_stream),
            ).execute(
                task,
                password_from_environment,
                execution_id,
                recorder=store,
            )
        store.record_finish(result)
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
        store.close()
        lock_file.close()
    return result


def history(store_path: Path, task_id: str | None = None) -> list[dict[str, object]]:
    store = ExecutionStore(store_path)
    try:
        return store.history(task_id)
    finally:
        store.close()


def history_json(store_path: Path, task_id: str | None = None) -> str:
    return json.dumps(history(store_path, task_id), ensure_ascii=False, indent=2)
