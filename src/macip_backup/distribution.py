"""Distribuição gerenciada: consulta, aplicação e confirmação.

Vínculo e layout — ``<state>/managed/``:

  - ``active.json``            — vínculo ATIVO {bond, endpoint,
    agent_id}: a troca só acontece via ``--rebind`` explícito, sob o
    lock global de gerenciamento;
  - ``<bond>/binding.json``    — dados do vínculo;
  - ``<bond>/tasks.d/<id>/``   — estado por tarefa DESTE vínculo:
      ``gen-<N>.json``    — envelope imutável da geração N;
      ``config-<N>.json`` — configuração candidata imutável;
      ``current.json``    — a configuração que as units executam;
      ``journal.jsonl``   — passos duráveis.

Jornal de uma aplicação (cada linha carrega `apply_attempt`):

    received → validated → config → prepared? ─┐
    timer_off → units → published → enabled → applied
    │── em falha de efeito: rollback restaura a geração aplicada
    │   anterior (units + current.json + estado do timer) ou remove
    │   o que a primeira tentativa criou;
    └── 'prepared' é o modo `--no-activate`: arquivos gravados, NADA
        ativado, NADA confirmado — a próxima execução com ativação
        retoma a mesma tentativa até 'applied'.

A mesma geração pode ter várias tentativas (falha → pré-requisito
corrigido → sucesso): a identidade do evento é
(task_id, generation, apply_attempt) — no SQLite local, no envelope
de confirmação e na central. `applied`/`failed` no journal bastam
para RECONSTRUIR a confirmação depois de um corte entre efeitos e
enfileiramento: conteúdo estável (agent_time = instante do journal).

Confirmações transitórias (rede/timeout/5xx) NUNCA viram bloqueio
permanente — seguem com backoff limitado; só rejeições definitivas
(401/403/404/409/422) bloqueiam, com `unblock` operado.

Ausência ≠ exclusão: páginas faltantes ou falha de rede nunca
desinstalam o que está aplicado.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .agent import task_lock_directory
from .central import CentralSpec, load_central
from .credentials import CredentialError, credential_from_environment_or_os
from .models import ConfigurationError, TaskSpec
from .restic import ResticError
from .store import ExecutionStore
from .systemd import (
    activate_managed_task,
    disable_managed_timer,
    install_pull_schedule,
    managed_timer_enabled,
    remove_managed_units,
    schedule_to_calendar,
    suspend_managed_task,
    uninstall_pull_schedule,
    write_managed_task_units,
)

_CONTRACT_VERSION = 1
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_CONFIRM_BACKOFF_BASE = 30
_CONFIRM_BACKOFF_CAP = 3600
# transitório = reenviável para sempre (backoff capado); definitivo = blocked
_TRANSIENT_HTTP = frozenset({408, 425, 429, 500, 502, 503, 504})


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """O Bearer nunca segue redirect para outro destino."""

    def redirect_request(self, *args, **kwargs):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _backoff(attempts: int) -> str:
    delay = min(_CONFIRM_BACKOFF_BASE * (2**attempts), _CONFIRM_BACKOFF_CAP)
    return (datetime.now(UTC) + timedelta(seconds=delay)).isoformat()


# ---- vínculo e layout por bond -----------------------------------------------


def managed_directory() -> Path:
    """`<state>/managed` — raiz de TODOS os vínculos gerenciados."""
    state = os.environ.get("MACIP_STATE")
    if state:
        return Path(state) / "managed"
    return Path.home() / ".local" / "state" / "macip-backup" / "managed"


def _bond_key(spec: CentralSpec, store_path: Path) -> str:
    """Identidade COMPLETA do gerenciamento: endpoint normalizado +
    UUID central + banco local — sem segredo."""
    material = (
        f"{spec.url}|{spec.agent_id}|{Path(store_path).resolve()}"
    ).encode()
    return hashlib.sha256(material).hexdigest()


def _bond_dir(bond: str) -> Path:
    return managed_directory() / bond[:16]


def _tasks_root(bond: str) -> Path:
    return _bond_dir(bond) / "tasks.d"


def _active_file() -> Path:
    return managed_directory() / "active.json"


def _read_active() -> dict | None:
    path = _active_file()
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ConfigurationError(
            f"vínculo ativo corrompido ({path}): {error}"
        ) from error


def _atomic_write(path: Path, content: str) -> None:
    """Publicação atômica: tmp no MESMO diretório + rename + fsync."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _bind(bond: str, spec: CentralSpec) -> None:
    """Adota este vínculo: active.json + binding.json atômicos."""
    _atomic_write(
        _active_file(),
        json.dumps(
            {"bond": bond, "endpoint": spec.url,
             "agent_id": spec.agent_id, "bonded_at": _now()},
            ensure_ascii=False,
        ),
    )
    _atomic_write(
        _bond_dir(bond) / "binding.json",
        json.dumps(
            {"bond": bond, "endpoint": spec.url,
             "agent_id": spec.agent_id, "bound_at": _now()},
            ensure_ascii=False,
        ),
    )


# ---- locks -------------------------------------------------------------------


def _acquire_manage_locks(store_path: Path):
    """Maintenance SH (coexiste com tarefas) + lock EX GLOBAL de
    gerenciamento — serializa reconciliações de QUALQUER vínculo: as
    units são um namespace único e a transição de vínculo não pode
    correr em paralelo com uma aplicação."""
    directory = task_lock_directory()
    directory.mkdir(parents=True, exist_ok=True)
    maintenance = (directory / "maintenance.lock").open("a")
    try:
        fcntl.flock(maintenance.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError as error:
        maintenance.close()
        raise ResticError(
            "manutenção em andamento — reconciliação recusada"
        ) from error
    lock_file = (directory / "manage.lock").open("a")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        lock_file.close()
        maintenance.close()
        raise ResticError(
            "outra reconciliação já está em andamento"
        ) from error
    return maintenance, lock_file


# ---- HTTP autenticado ---------------------------------------------------------


def _request(
    spec: CentralSpec,
    method: str,
    path: str,
    token: str,
    payload: dict | None = None,
) -> tuple[int, dict | None]:
    """(status, json|None). Erro de transporte → (0, None) — URL e
    cabeçalhos não vazam no diagnóstico."""
    body = (
        json.dumps(payload).encode("utf-8") if payload is not None else None
    )
    headers = {"Authorization": f"Bearer {token}"}
    if body is not None:
        headers["Content-Type"] = "application/json"
        headers["Content-Length"] = str(len(body))
    request = urllib.request.Request(
        f"{spec.url}{path}", data=body, headers=headers, method=method
    )
    timeout = min(spec.connect_timeout_seconds, spec.request_timeout_seconds)
    try:
        with _opener.open(request, timeout=timeout) as response:
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
            if len(raw) > _MAX_RESPONSE_BYTES:
                return 0, None
            try:
                return response.status, json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                return response.status, None
    except urllib.error.HTTPError as error:
        try:
            raw = error.read(_MAX_RESPONSE_BYTES + 1)
            try:
                return error.code, json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                return error.code, None
        except (OSError, Exception):
            return 0, None
        finally:
            error.close()
    except (urllib.error.URLError, TimeoutError, OSError):
        return 0, None


def _fetch_desired(
    spec: CentralSpec, token: str
) -> tuple[list[dict], dict | None]:
    """Todas as páginas do estado desejado. Resposta incompleta/
    malformada → erro explícito (ausência NUNCA é autorização)."""
    desired: list[dict] = []
    offset = 0
    while True:
        status, body = _request(
            spec, "GET",
            f"/api/v1/agent/tasks/desired?limit=200&offset={offset}",
            token,
        )
        if status != 200 or not isinstance(body, dict):
            return desired, {"error": "consulta_falhou", "http": status}
        if body.get("agent_id") != spec.agent_id:
            return desired, {
                "error": "agente divergente",
                "detail": "resposta para outra identidade",
            }
        total = body.get("total")
        tasks = body.get("tasks")
        if not isinstance(tasks, list) or not isinstance(total, int):
            return desired, {"error": "resposta_incompleta"}
        desired.extend(tasks)
        if len(desired) >= total or not tasks:
            break
        offset += len(tasks)
        if len(tasks) == 0 or offset > total + 200 * 10:
            return desired, {"error": "resposta_incompleta"}
    return desired, None


# ---- journal -------------------------------------------------------------------


def _journal_append(
    directory: Path, step: str, generation: int, **fields: Any
) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    line = json.dumps(
        {"step": step, "generation": generation, "at": _now(), **fields},
        ensure_ascii=False,
    )
    with (directory / "journal.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(line + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _journal_entries(directory: Path) -> list[dict]:
    journal = directory / "journal.jsonl"
    if not journal.exists():
        return []
    entries = []
    for line in journal.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def _journal_state(directory: Path) -> dict | None:
    """Último passo por geração + tentativas + gerações aplicadas."""
    entries = _journal_entries(directory)
    if not entries:
        return None
    steps: dict[int, dict] = {}
    attempts: dict[int, int] = {}
    applied: list[int] = []
    for entry in entries:
        generation = entry.get("generation")
        if generation is None:
            continue
        steps[generation] = entry
        attempts[generation] = max(
            attempts.get(generation, 0), entry.get("apply_attempt", 1)
        )
        if entry.get("step") == "applied":
            applied.append(generation)
    newest = max(steps)
    state = dict(steps[newest])
    state["steps"] = {g: e["step"] for g, e in steps.items()}
    state["attempts"] = attempts
    state["applied_generations"] = applied
    state["latest_applied"] = max(applied) if applied else None
    return state


def _envelope_file(task_dir: Path, generation: int) -> Path:
    return task_dir / f"gen-{generation}.json"


def _read_envelope(task_dir: Path, generation: int) -> dict | None:
    path = _envelope_file(task_dir, generation)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


# ---- validação do envelope -----------------------------------------------------


def _validate_envelope(envelope: dict, spec: CentralSpec) -> TaskSpec | None:
    """Identidade + integridade + contrato + pré-requisitos LOCAIS —
    ANTES de qualquer efeito. Não inicia repositório nem backup.
    Devolve o TaskSpec em `apply`; `suspend` devolve None."""
    if not isinstance(envelope, dict):
        raise ConfigurationError("envelope da tarefa não é objeto")
    if envelope.get("agent_id") != spec.agent_id:
        raise ConfigurationError("envelope para outro agente")
    task_id = envelope.get("task_id")
    if not isinstance(task_id, str):
        raise ConfigurationError("envelope sem task_id")
    try:
        import uuid as uuid_module

        uuid_module.UUID(task_id)
    except ValueError as error:
        raise ConfigurationError("task_id não é UUID") from error
    if not isinstance(envelope.get("generation"), int) \
            or isinstance(envelope.get("generation"), bool) \
            or envelope["generation"] < 1:
        raise ConfigurationError("envelope sem geração válida")
    if envelope.get("action") not in {"apply", "suspend"}:
        raise ConfigurationError("ação desconhecida no envelope")
    if envelope.get("contract_version") != _CONTRACT_VERSION:
        raise ConfigurationError(
            f"contract_version {envelope.get('contract_version')} "
            f"não suportada (máx. {_CONTRACT_VERSION})"
        )
    declared = envelope.get("hash")
    body = dict(envelope)
    body.pop("hash", None)
    expected = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if declared != expected:
        raise ConfigurationError("hash do envelope diverge do conteúdo")
    if envelope["action"] == "suspend":
        return None
    task_doc = envelope.get("task")
    if not isinstance(task_doc, dict):
        raise ConfigurationError("envelope apply sem documento")
    # identidades do documento contra o envelope e o vínculo local
    if task_doc.get("id") != task_id:
        raise ConfigurationError(
            "task.id do documento diverge do envelope"
        )
    if task_doc.get("device_id") != spec.agent_id:
        raise ConfigurationError(
            "device_id do documento diverge do agente do vínculo"
        )
    task = TaskSpec.from_dict(task_doc)  # tipos/estrutura completos
    try:
        task.validate_for_execution()  # origens, SMB, repo-fora-da-origem
        for destination in task.destinations:
            destination.validate_for_execution()  # sftp/smb do destino
    except ConfigurationError:
        raise
    except (OSError, KeyError) as error:
        raise ConfigurationError(
            f"pré-requisito local inválido: {error}"
        ) from error
    schedule = envelope.get("schedule")
    if not isinstance(schedule, dict):
        raise ConfigurationError("envelope apply sem agenda")
    try:
        schedule_to_calendar(schedule)
    except (KeyError, TypeError, ValueError) as error:
        raise ConfigurationError(
            f"agenda inválida no envelope: {error}"
        ) from error
    return task


# ---- confirmações ---------------------------------------------------------------


def _confirmation_payload(
    generation: int,
    apply_attempt: int,
    status: str,
    applied_revision: int | None,
    detail: str | None,
    agent_time: str | None = None,
) -> dict:
    return {
        "generation": generation,
        "attempt": apply_attempt,
        "status": status,
        "applied_revision": applied_revision,
        "detail": detail,
        "agent_time": agent_time or _now(),
    }


def _reconstruct_confirmations(
    store: ExecutionStore, bond: str, tasks_root: Path
) -> int:
    """Journal → fila: cada entrada 'applied'/'failed' cujo evento
    (task, gen, attempt) ainda não está na fila é regravada com o
    MESMO conteúdo — fecha a janela entre efeitos locais e
    enfileiramento, inclusive depois de reinício."""
    rebuilt = 0
    if not tasks_root.exists():
        return 0
    for task_dir in tasks_root.iterdir():
        if not task_dir.is_dir():
            continue
        for entry in _journal_entries(task_dir):
            step = entry.get("step")
            if step not in ("applied", "failed"):
                continue
            generation = entry["generation"]
            attempt = entry.get("apply_attempt", 1)
            if store.confirmation_exists(
                bond, task_dir.name, generation, attempt
            ):
                continue
            payload = _confirmation_payload(
                generation,
                attempt,
                "applied" if step == "applied" else "failed",
                entry.get("revision") if step == "applied" else None,
                entry.get("detail"),
                agent_time=entry.get("at"),
            )
            store.enqueue_confirmation(
                bond, task_dir.name, generation, attempt,
                json.dumps(payload),
            )
            rebuilt += 1
    return rebuilt


def _enqueue_confirmation(
    store: ExecutionStore,
    bond: str,
    envelope: dict,
    apply_attempt: int,
    outcome: dict,
) -> None:
    if outcome["result"] == "failed_validation":
        payload = _confirmation_payload(
            envelope["generation"], apply_attempt,
            "failed", None, outcome["detail"],
        )
    else:
        payload = _confirmation_payload(
            envelope["generation"], apply_attempt,
            "applied", outcome.get("revision"), None,
        )
    store.enqueue_confirmation(
        bond, envelope["task_id"], envelope["generation"],
        apply_attempt, json.dumps(payload),
    )


def _flush_confirmations(
    spec: CentralSpec,
    store: ExecutionStore,
    token: str,
    summary: dict,
) -> None:
    """Reenvia pendentes. Transitório (rede/timeout/5xx/429) → retry
    com backoff capado, NUNCA bloqueado; definitivo (autenticação,
    inexistente, conteúdo rejeitado) → 'blocked' até intervenção."""
    for item in store.pending_confirmations(
        _bond_key(spec, store.path), _now(), limit=50
    ):
        status, body = _request(
            spec, "POST",
            f"/api/v1/agent/tasks/{item['task_id']}/confirmations",
            token, json.loads(item["payload"]),
        )
        if (
            status == 200
            and isinstance(body, dict)
            and body.get("status") in ("recorded", "duplicate")
        ):
            store.confirmation_sent(item["id"])
            summary["confirmations_sent"].append(item["task_id"])
        elif status == 0 or status in _TRANSIENT_HTTP:
            attempts = item["attempts"] + 1
            store.confirmation_retry(
                item["id"], f"HTTP {status or 'rede/timeout'}",
                _backoff(attempts),
            )
            summary["confirmations_retry"].append(item["task_id"])
        else:
            # 401/403 autenticação · 404 tarefa · 409 conteúdo · 422
            store.confirmation_blocked(item["id"], f"HTTP {status}")
            summary["confirmations_blocked"].append(item["task_id"])


# ---- aplicação de uma geração ---------------------------------------------------


def _last_functional_generation(
    state: dict | None, exclude_generation: int
) -> int | None:
    """Última geração cujo journal declara 'applied' — estado funcional
    a restaurar quando uma nova tentativa falha nos efeitos."""
    if not state:
        return None
    candidates = [
        g for g in state["applied_generations"] if g != exclude_generation
    ]
    return max(candidates) if candidates else None


def _effects_for_generation(
    envelope: dict,
    spec: CentralSpec,
    central_path: Path,
    store_path: Path,
    task_dir: Path,
    attempt: int,
    *,
    bond: str,
    allow_bond_replacement: bool,
) -> None:
    """Sequência de efeitos ORDENADA — cada disparo encontra
    configuração completa e coerente com a unit carregada:

    1. desativa o timer atual (se houver) — agenda velha não dispara
       com configuração nova nem agenda nova com configuração velha;
    2. reescreve as units (sem systemctl — o gerenciador segue com as
       carregadas);
    3. publica `current.json` atomicamente;
    4. daemon-reload + enable --now — ÚLTIMO passo.

    Qualquer exceção → o chamador faz rollback para a última geração
    funcional."""
    generation = envelope["generation"]
    task_id = envelope["task_id"]
    timer_was_enabled = managed_timer_enabled(task_id)
    if timer_was_enabled:
        # agenda anterior desarmada ANTES de qualquer troca de arquivo
        disable_managed_timer(task_id, spec.agent_id, bond, now=True)
    _journal_append(task_dir, "timer_off", generation,
                    apply_attempt=attempt, was_enabled=timer_was_enabled)
    units = write_managed_task_units(
        task_id,
        task_dir / "current.json",
        store_path,
        central_path,
        spec.agent_id,
        bond,
        envelope["schedule"],
        allow_bond_replacement=allow_bond_replacement,
    )
    _journal_append(task_dir, "units", generation, apply_attempt=attempt,
                    service=units["service"], timer=units["timer_name"])
    _atomic_write(
        task_dir / "current.json",
        (task_dir / f"config-{generation}.json").read_text(encoding="utf-8"),
    )
    _journal_append(task_dir, "published", generation, apply_attempt=attempt)
    activate_managed_task(task_id, units["timer_name"] is not None)
    _journal_append(task_dir, "enabled", generation, apply_attempt=attempt)


def _restore_functional_state(
    envelope: dict,
    spec: CentralSpec,
    central_path: Path,
    store_path: Path,
    task_dir: Path,
    state: dict | None,
    *,
    bond: str,
    allow_bond_replacement: bool,
) -> str:
    """Rollback: reinstala a ÚLTIMA geração aplicada (envelope gen-N
    imutável → config/units/timer exatamente como estavam). Sem
    anterior funcional → remove o que a primeira tentativa criou."""
    generation = envelope["generation"]
    previous = _last_functional_generation(state, generation)
    if previous is None:
        disable_managed_timer(task_id=envelope["task_id"],
                              agent_uuid=spec.agent_id, bond=bond, now=True)
        remove_managed_units(envelope["task_id"], spec.agent_id, bond)
        for name in ("current.json",):
            (task_dir / name).unlink(missing_ok=True)
        _journal_append(task_dir, "rolled_back", generation,
                        restored="none")
        return "removed"
    previous_env = _read_envelope(task_dir, previous)
    if previous_env is None:
        raise ConfigurationError(
            "rollback impossível: envelope da geração anterior ausente"
        )
    attempt = state["attempts"].get(previous, 1)
    _effects_for_generation(
        previous_env, spec, central_path, store_path, task_dir, attempt,
        bond=bond, allow_bond_replacement=allow_bond_replacement,
    )
    _journal_append(task_dir, "rolled_back", generation,
                    apply_attempt=attempt, restored=previous)
    return f"restored gen {previous}"


def _apply_one(
    spec: CentralSpec,
    envelope: dict,
    central_path: Path,
    store_path: Path,
    tasks_root: Path,
    bond: str,
    *,
    activate: bool,
    allow_bond_replacement: bool = False,
) -> dict:
    """Aplica UMA geração de UMA tarefa — idempotente e retomável."""
    task_id = envelope["task_id"]
    generation = envelope["generation"]
    task_dir = tasks_root / task_id
    state = _journal_state(task_dir)
    steps = state["steps"] if state else {}
    step = steps.get(generation)
    attempts = state["attempts"] if state else {}

    # mesma geração já concluída — ou já existe aplicação MAIS NOVA:
    # resposta antiga não reinstala geração superada
    if step == "applied" or (
        state is not None
        and (state["latest_applied"] or 0) > generation
    ):
        return {"task_id": task_id, "generation": generation,
                "result": "already_applied"}

    # número da tentativa: 'failed' abre tentativa nova; demais
    # estados retomam a tentativa corrente
    if step == "failed":
        attempt = attempts.get(generation, 0) + 1
        step = None  # recomeça do zero com tentativa nova
    else:
        attempt = max(attempts.get(generation, 0), 1)

    # validação total ANTES de qualquer efeito
    try:
        _validate_envelope(envelope, spec)
    except ConfigurationError as error:
        _atomic_write(
            _envelope_file(task_dir, generation),
            json.dumps(envelope, ensure_ascii=False),
        )
        _journal_append(task_dir, "failed", generation,
                        apply_attempt=attempt, detail=str(error)[:900])
        return {"task_id": task_id, "generation": generation,
                "apply_attempt": attempt,
                "result": "failed_validation",
                "detail": str(error)[:900]}

    gen_file = _envelope_file(task_dir, generation)
    if not gen_file.exists():
        _atomic_write(gen_file, json.dumps(envelope, ensure_ascii=False))
    if step is None:
        _journal_append(task_dir, "received", generation,
                        apply_attempt=attempt)
    if step in (None, "received"):
        _journal_append(task_dir, "validated", generation,
                        apply_attempt=attempt)

    if envelope["action"] == "suspend":
        if not activate:
            _journal_append(task_dir, "prepared", generation,
                            apply_attempt=attempt)
            return {"task_id": task_id, "generation": generation,
                    "apply_attempt": attempt, "result": "prepared"}
        suspend_managed_task(task_id, spec.agent_id, bond)
        _journal_append(
            task_dir, "applied", generation, apply_attempt=attempt,
            revision=envelope.get("revision"), action="suspend",
        )
        return {"task_id": task_id, "generation": generation,
                "apply_attempt": attempt, "result": "suspended"}

    # apply: configuração candidata imutável antes de qualquer efeito
    if step in (None, "received", "validated"):
        _atomic_write(
            task_dir / f"config-{generation}.json",
            json.dumps(envelope["task"], ensure_ascii=False),
        )
        _journal_append(task_dir, "config", generation,
                        apply_attempt=attempt)
        step = "config"

    if not activate:
        # preparação: units escritas, NADA ativado, NADA confirmado —
        # a próxima execução com ativação retoma daqui
        write_managed_task_units(
            task_id,
            task_dir / "current.json",
            store_path,
            central_path,
            spec.agent_id,
            bond,
            envelope["schedule"],
            allow_bond_replacement=allow_bond_replacement,
        )
        _journal_append(task_dir, "prepared", generation,
                        apply_attempt=attempt)
        return {"task_id": task_id, "generation": generation,
                "apply_attempt": attempt, "result": "prepared"}

    try:
        _effects_for_generation(
            envelope, spec, central_path, store_path, task_dir, attempt,
            bond=bond, allow_bond_replacement=allow_bond_replacement,
        )
    except (ConfigurationError, ResticError, OSError) as error:
        # rollback para a última aplicação funcional — configuração,
        # agenda e estado systemd voltam juntos
        restore = _restore_functional_state(
            envelope, spec, central_path, store_path, task_dir, state,
            bond=bond, allow_bond_replacement=allow_bond_replacement,
        )
        raise ResticError(
            f"efeitos da geração {generation} falharam "
            f"({str(error)[:400]}); estado restaurado: {restore}"
        ) from error

    _journal_append(
        task_dir, "applied", generation, apply_attempt=attempt,
        revision=envelope["revision"], action="apply",
    )
    return {"task_id": task_id, "generation": generation,
            "apply_attempt": attempt,
            "result": "applied",
            "revision": envelope["revision"]}


# ---- transição de vínculo --------------------------------------------------------


def _transition_bond(
    spec: CentralSpec,
    bond: str,
    old: dict,
    tasks_root_new: Path,
) -> None:
    """Troca explícita de vínculo (depois de `--rebind`):

    1. desativa os timers das tarefas do vínculo ANTERIOR — seus
       arquivos/journals são preservados mas nada continua disparando
       sob a identidade antiga;
    2. grava `active.json` apontando o novo vínculo;
    3. confirmações pendentes do vínculo anterior NÃO migram —
       pertencem ao bond que as gerou.

    Qualquer falha → recusa com diagnóstico, sem anunciar adoção."""
    old_bond = old["bond"]
    old_root = managed_directory() / old_bond[:16] / "tasks.d"
    if old_root.exists():
        for task_dir in sorted(old_root.iterdir()):
            state = _journal_state(task_dir)
            if not state or not state["applied_generations"]:
                continue
            # só toca em units com o marcador do vínculo ANTERIOR
            disable_managed_timer(
                task_dir.name, spec.agent_id, old_bond, now=True
            )
    _bind(bond, spec)


# ---- reconciliação --------------------------------------------------------------


def _last_applied_per_task(tasks_root: Path) -> dict[str, int]:
    applied: dict[str, int] = {}
    if not tasks_root.exists():
        return applied
    for task_dir in tasks_root.iterdir():
        state = _journal_state(task_dir)
        if state and state["latest_applied"] is not None:
            applied[task_dir.name] = state["latest_applied"]
    return applied


def inspect(
    central_path: Path, store_path: Path, *, rebind: bool = False
) -> dict:
    """Inspeção SEM efeitos: o que a reconciliação aplicaria — sem
    tocar configurações, units, journal ou confirmações."""
    spec = load_central(central_path)
    try:
        token = credential_from_environment_or_os(spec.token_environment)
    except CredentialError as error:
        raise ConfigurationError(
            f"token indisponível ({spec.token_environment}) — a consulta "
            "não pode autenticar; nada foi alterado"
        ) from error
    desired, problem = _fetch_desired(spec, token)
    bond = _bond_key(spec, store_path)
    applied = _last_applied_per_task(_tasks_root(bond))
    active = _read_active()
    report = {
        "endpoint": spec.url,
        "agent_id": spec.agent_id,
        "bond": bond[:16],
        "active_bond": (active or {}).get("bond", "")[:16] or None,
        "bond_mismatch": bool(active and active["bond"] != bond),
        "desired": [],
        "problem": problem,
    }
    for envelope in desired:
        report["desired"].append(
            {
                "task_id": envelope["task_id"],
                "generation": envelope["generation"],
                "action": envelope["action"],
                "applied_generation": applied.get(envelope["task_id"]),
                "pending": applied.get(envelope["task_id"])
                != envelope["generation"],
            }
        )
    return report


def reconcile(
    central_path: Path,
    store_path: Path,
    *,
    activate: bool = True,
    rebind: bool = False,
) -> dict:
    """Consulta → vínculo → valida → aplica → confirma — uma rodada.

    `activate=False` = PREPARAÇÃO: escreve envelopes, configurações
    candidatas e units — sem systemctl, sem `applied`, sem
    confirmação. A próxima execução com ativação retoma a mesma
    tentativa até o fim.
    """
    spec = load_central(central_path)
    bond = _bond_key(spec, store_path)
    locks = _acquire_manage_locks(store_path)
    try:
        tasks_root = _tasks_root(bond)
        central_path = Path(central_path).resolve()
        summary: dict[str, Any] = {
            "endpoint": spec.url,
            "agent_id": spec.agent_id,
            "bond": bond[:16],
            "desired_total": 0,
            "applied": [],
            "suspended": [],
            "prepared": [],
            "failed": [],
            "skipped_credential": False,
            "bond_transitioned": False,
            "confirmations_rebuilt": 0,
            "confirmations_sent": [],
            "confirmations_retry": [],
            "confirmations_blocked": [],
            "communication_error": None,
        }
        try:
            token = credential_from_environment_or_os(
                spec.token_environment
            )
        except CredentialError:
            summary["skipped_credential"] = True
            return summary  # diagnóstico — nada foi alterado

        store = ExecutionStore(store_path)
        try:
            # reenvio das confirmações DESTE vínculo — as do vínculo
            # anterior NÃO migram para o novo endpoint
            _flush_confirmations(spec, store, token, summary)

            desired, problem = _fetch_desired(spec, token)
            if problem is not None:
                summary["communication_error"] = problem
                return summary

            # vínculo: adoção automática na PRIMEIRA consulta ok;
            # troca de vínculo SÓ com --rebind e transição completa
            active = _read_active()
            allow_bond_replacement = False
            if active is None:
                _bind(bond, spec)
            elif active["bond"] != bond:
                if not rebind:
                    raise ConfigurationError(
                        "o vínculo gerenciado ativo difere desta "
                        "configuração — as tarefas seguem sob o vínculo "
                        "anterior. Para adoção explícita, repita com "
                        "--rebind"
                    )
                _transition_bond(spec, bond, active, tasks_root)
                summary["bond_transitioned"] = True
                allow_bond_replacement = True

            # journal → fila: reconstrói confirmações de aplicações/
            # falhas que sofreram corte antes do enfileiramento
            summary["confirmations_rebuilt"] = _reconstruct_confirmations(
                store, bond, tasks_root
            )

            summary["desired_total"] = len(desired)
            for envelope in desired:
                try:
                    outcome = _apply_one(
                        spec, envelope, central_path, store_path,
                        tasks_root, bond, activate=activate,
                        allow_bond_replacement=allow_bond_replacement,
                    )
                except ConfigurationError as error:
                    outcome = {
                        "task_id": envelope.get("task_id", "?"),
                        "generation": envelope.get("generation", 0),
                        "apply_attempt": 0,
                        "result": "failed_validation",
                        "detail": str(error)[:900],
                    }
                except (ResticError, OSError) as error:
                    outcome = {
                        "task_id": envelope.get("task_id", "?"),
                        "generation": envelope.get("generation", 0),
                        "apply_attempt": 0,
                        "result": "failed_transient",
                        "detail": str(error)[:900],
                    }
                if outcome["result"] in ("applied", "suspended",
                                         "already_applied"):
                    key = "suspended" if outcome["result"] == "suspended" \
                        else "applied"
                    summary[key].append(outcome)
                elif outcome["result"] == "prepared":
                    summary["prepared"].append(outcome)
                else:
                    summary["failed"].append(outcome)
                # 'prepared' e transitório NÃO confirmam: não há
                # aplicação efetiva a relatar; sem apply_attempt o
                # evento não tem identidade — reconstruído pelo journal
                if outcome["result"] in ("applied", "suspended",
                                         "failed_validation") \
                        and outcome["apply_attempt"] >= 1:
                    _enqueue_confirmation(
                        store, bond, envelope,
                        outcome["apply_attempt"], outcome,
                    )
            _flush_confirmations(spec, store, token, summary)
        finally:
            store.close()
    finally:
        for lock in locks:
            lock.close()
    return summary


def status(central_path: Path, store_path: Path) -> dict:
    """Estado LOCAL gerenciado: vínculo ativo, aplicações do journal e
    fila de confirmações — offline, sem rede."""
    spec = load_central(central_path)
    bond = _bond_key(spec, store_path)
    tasks_root = _tasks_root(bond)
    applied = _last_applied_per_task(tasks_root)
    tasks = []
    if tasks_root.exists():
        for task_dir in sorted(tasks_root.iterdir()):
            state = _journal_state(task_dir)
            tasks.append(
                {
                    "task_id": task_dir.name,
                    "last_step": state["step"] if state else None,
                    "applied_generation": applied.get(task_dir.name),
                    "current": (task_dir / "current.json").exists(),
                }
            )
    store = ExecutionStore(store_path)
    try:
        confirmations = store.confirmation_state()
    finally:
        store.close()
    active = _read_active()
    bonds = [
        item.name
        for item in managed_directory().iterdir()
        if item.is_dir()
    ] if managed_directory().exists() else []
    return {
        "endpoint": spec.url,
        "agent_id": spec.agent_id,
        "bond": bond[:16],
        "active": active,
        "other_bonds": [b for b in bonds if b != bond[:16]],
        "tasks": tasks,
        "confirmations": confirmations,
    }


def schedule_pull(
    store_path: Path, central_path: Path, calendar: str,
    enable: bool = False,
) -> dict:
    return install_pull_schedule(store_path, central_path, calendar, enable)


def unschedule_pull(store_path: Path, central_path: Path) -> dict:
    return uninstall_pull_schedule(store_path, central_path)
