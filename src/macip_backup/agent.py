"""Agente local: executa tarefas offline e registra histórico em SQLite.

O agente não depende de portal/API: tarefa, segredos (ambiente ou kernel
keyring) e histórico são locais. Quando a API central existir, o mesmo
registro poderá ser sincronizado — o fluxo de execução não muda.

Execução exclusiva POR TAREFA: `flock` em
``<lock_dir>/<task_id>.lock`` — o mesmo arquivo para `run`, `agent` e o
service systemd (mesmo caminho de código). O lock não depende de
``--store``: outro banco não contorna a exclusão. O kernel libera o
lock mesmo em morte abrupta.

Cancelamento: SIGTERM/SIGINT pedem ao adaptador o encerramento do grupo
de processos do restic (SIGTERM→SIGKILL com prazo limitado). A
execução só é marcada `interrupted` depois que o encerramento é
confirmado; falha de limpeza é registrada explicitamente. Cancelamento
nunca vira retry (ResticCancelledError não é repetível).

Execuções são gravadas como `running` **antes** do backup e atualizadas
na conclusão. Execuções ainda `running` de mortes anteriores (kill -9,
reboot) viram `interrupted` na próxima execução.

Os eventos estruturados da execução são persistidos em
``<store-dir>/logs/<execution_id>.jsonl`` — nunca descartados.
"""

from __future__ import annotations

import fcntl
import json
import os
import signal
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import IO

from .logging import StructuredLogger
from .models import TaskSpec
from .orchestrator import (
    BackupOrchestrator,
    ExecutionResult,
    ExecutionStatus,
    password_from_environment,
)
from .restic import ResticAdapter, ResticError
from .store import ExecutionStore


def _load_task(path: Path) -> TaskSpec:
    with path.open(encoding="utf-8") as config_file:
        return TaskSpec.from_dict(json.load(config_file))


def _now() -> str:
    return datetime.now(UTC).isoformat()


def task_lock_directory() -> Path:
    """Locks por tarefa — mesma regra do instalador: MACIP_STATE/locks
    (propagado pelo launcher) ou o default do usuário."""
    override = os.environ.get("MACIP_LOCK_DIR")
    if override:
        return Path(override)
    state = os.environ.get("MACIP_STATE")
    if state:
        return Path(state) / "locks"
    return Path.home() / ".local" / "state" / "macip-backup" / "locks"


def cache_directory() -> Path:
    """Cache restic por usuário — MACIP_CACHE (mesmo do instalador)."""
    override = os.environ.get("MACIP_CACHE")
    if override:
        return Path(override)
    return Path.home() / ".cache" / "macip-backup"


def acquire_task_lock(task_id: str) -> tuple[IO[str], IO[str]]:
    """Lock de manutenção (SH) + lock de tarefa (EX), ambos NB.

    SH em ``maintenance.lock``: muitas tarefas correm juntas fora de
    manutenção, mas install/update/uninstall (EX) são recusados enquanto
    houver execução — e execução recusa iniciar durante manutenção.
    Devolve (manutenção, tarefa) — ambos precisam ser fechados.
    """
    directory = task_lock_directory()
    directory.mkdir(parents=True, exist_ok=True)
    maintenance = (directory / "maintenance.lock").open("a")
    try:
        fcntl.flock(maintenance.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError as error:
        maintenance.close()
        raise ResticError("manutenção em andamento — tente mais tarde") from error
    lock_file = (directory / f"{task_id}.lock").open("a")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        lock_file.close()
        maintenance.close()
        raise ResticError(
            f"já existe execução da tarefa '{task_id}' em andamento"
        ) from error
    return maintenance, lock_file


def install_cancel_handlers(
    adapter: ResticAdapter, on_signal: object = None
) -> dict[int, object]:
    """SIGTERM/SIGINT → adapter.cancel(motivo). Devolve os handlers
    anteriores para restauração com ``restore_signal_handlers``."""
    previous: dict[int, object] = {}
    for signum in (signal.SIGTERM, signal.SIGINT):
        def _handler(
            received: int, _frame: object, signum: int = signum
        ) -> None:
            if callable(on_signal):
                on_signal(signum)
            adapter.cancel(f"interrompido por sinal {signum}")

        previous[signum] = signal.signal(signum, _handler)
    return previous


def restore_signal_handlers(previous: dict[int, object]) -> None:
    for signum, handler in previous.items():
        signal.signal(signum, handler)


def _sweep_stale_executions(store: ExecutionStore, own_task_id: str) -> None:
    """Marca `running` órfãs como `interrupted`, respeitando locks.

    A própria tarefa já está sob lock — suas execuções `running` são
    necessariamente órfãs. Para outras tarefas, o lock correspondente é
    provado por tentativa de aquisição: ocupado → execução legítima,
    preservada; livre → órfã, marcada. Uma execução de outra tarefa com
    lock ocupado NUNCA é alterada.
    """
    for task_id in store.running_task_ids():
        if task_id == own_task_id:
            store.mark_stale_interrupted(task_id, _now())
            continue
        try:
            probe = acquire_task_lock(task_id)
        except ResticError:
            continue
        try:
            store.mark_stale_interrupted(task_id, _now())
        finally:
            for lock_fd in probe:
                lock_fd.close()


def run_task(
    config_path: Path, store_path: Path, central_path: Path | None = None
) -> ExecutionResult:
    """Executa uma tarefa como agente local e persiste o resultado.

    Credenciais resolvem pelo mesmo mecanismo dos demais comandos:
    variável de ambiente → kernel keyring (`macip.<nome>`).

    ``central_path`` é OPCIONAL — com a configuração central presente,
    o resultado é enfileirado na outbox na mesma transação da
    conclusão; sem ela o agente trabalha exatamente como antes.
    """
    task = _load_task(config_path)
    outbox = None
    if central_path is not None:
        from .central import load_central

        spec = load_central(central_path)
        outbox = (spec.url, spec.agent_id)
    store_path = Path(store_path)
    # O diretório de estado existe antes de qualquer toque no lock/DB.
    store_path.parent.mkdir(parents=True, exist_ok=True)
    execution_id = str(uuid.uuid4())
    maintenance_file, lock_file = acquire_task_lock(task.id)
    adapter = ResticAdapter(
        timeouts=task.timeouts, cache_directory=cache_directory()
    )
    try:
        store = ExecutionStore(store_path, outbox=outbox)
    except Exception:
        lock_file.close()
        maintenance_file.close()
        raise
    interrupted_by: list[int] = []
    previous_handlers: dict[int, object] = {}
    try:
        _sweep_stale_executions(store, task.id)
        store.record_start(task, execution_id, _now())
        previous_handlers = install_cancel_handlers(
            adapter, on_signal=interrupted_by.append
        )
        log_directory = store_path.parent / "logs"
        log_directory.mkdir(parents=True, exist_ok=True)
        log_path = log_directory / f"{execution_id}.jsonl"
        with log_path.open("a", encoding="utf-8") as log_stream:
            logger = StructuredLogger(log_stream)
            result = BackupOrchestrator(
                adapter, logger
            ).execute(
                task,
                password_from_environment,
                execution_id,
                recorder=store,
            )
            if interrupted_by:
                # execute() retornou → cancel() confirmou o encerramento do
                # grupo (ou registrou a falha). O sinal observado antes da
                # conclusão vence o resultado normal: histórico e payload são
                # congelados uma única vez como interrupted.
                signum = interrupted_by[0]
                detail = f"sinal {signum}"
                if adapter.cancel_cleanup_error:
                    detail += (
                        f"; falha na limpeza: {adapter.cancel_cleanup_error}"
                    )
                logger.emit(
                    "warning",
                    "backup.execution.interrupted",
                    task_id=task.id,
                    execution_id=execution_id,
                    signum=signum,
                    cleanup_error=adapter.cancel_cleanup_error,
                )
                result = replace(
                    result,
                    status=ExecutionStatus.INTERRUPTED,
                    finished_at_utc=_now(),
                )
                store.record_finish(
                    result,
                    interruption_detail=detail,
                )
            else:
                store.record_finish(result)
        return result
    finally:
        restore_signal_handlers(previous_handlers)
        store.close()
        lock_file.close()
        maintenance_file.close()


def history(store_path: Path, task_id: str | None = None) -> list[dict[str, object]]:
    store = ExecutionStore(store_path)
    try:
        return store.history(task_id)
    finally:
        store.close()


def history_json(store_path: Path, task_id: str | None = None) -> str:
    return json.dumps(history(store_path, task_id), ensure_ascii=False, indent=2)
