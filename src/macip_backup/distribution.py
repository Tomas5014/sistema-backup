"""Distribuição gerenciada: consulta, aplicação e confirmação.

Fluxo por tarefa (em ``<state>/managed/tasks.d/<task_id>/``):

  - ``gen-<N>.json``   — envelope imutável da geração N (recebido);
  - ``journal.jsonl``  — passos duráveis: received → validated →
    units → applied → confirmed — uma operação interrompida retoma
    exatamente onde parou;
  - ``current.json``   — o `tarefa.json` REFERENCIADO pelas units —
    só avança (rename atômico) depois de TODOS os efeitos locais, ou
    seja, configuração inaplicável preserva a última boa.

Confirmações ficam na tabela ``confirmations`` do SQLite (fila
separada da outbox de execuções — o payload daquela é imutável).
Resposta HTTP perdida → a linha permanece pendente e é reenviada na
próxima rodada; a central deduplica por (task_id, generation).

Exclusão: maintenance SH + lock EX POR VÍNCULO (banco+endpoint+UUID)
— a mesma regra da sincronização; reconciliações do mesmo vínculo são
mutuamente exclusivas sem bloquear tarefas em execução.

Ausência ≠ exclusão: a consulta paginada só aplica o que VEIO na
resposta; uma página faltante ou falha de rede nunca desinstala o que
já está configurado.
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
    install_managed_task,
    install_pull_schedule,
    suspend_managed_task,
    uninstall_pull_schedule,
)

_CONTRACT_VERSION = 1
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_CONFIRM_MAX_ATTEMPTS = 10
_CONFIRM_BACKOFF_BASE = 30
_CONFIRM_BACKOFF_CAP = 3600


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


# ---- diretório gerenciado e vínculo -----------------------------------------


def managed_directory() -> Path:
    """`<state>/managed` — MACIP_STATE como os demais componentes."""
    state = os.environ.get("MACIP_STATE")
    if state:
        return Path(state) / "managed"
    return Path.home() / ".local" / "state" / "macip-backup" / "managed"


def _bond_key(spec: CentralSpec) -> str:
    """Identidade do vínculo SEM segredo — endpoint normalizado + UUID."""
    return f"{spec.url}|{spec.agent_id}"


def _binding_file() -> Path:
    return managed_directory() / "binding.json"


def _check_binding(spec: CentralSpec, rebind: bool) -> dict | None:
    """O vínculo gravado deve ser o mesmo da consulta. Trocar o
    arquivo de configuração ou apontar outra central NÃO pode adotar
    silenciosamente tarefas já gerenciadas — exige `--rebind` explícito."""
    path = _binding_file()
    if not path.exists():
        return None
    try:
        binding = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ConfigurationError(
            f"vínculo local corrompido ({path}): {error}"
        ) from error
    if binding.get("bond") != _bond_key(spec) and not rebind:
        raise ConfigurationError(
            "o vínculo gerenciado existente difere da configuração "
            "informada — as tarefas locais permanecem gerenciadas pelo "
            "vínculo anterior. Para adotar um NOVO vínculo de forma "
            "explícita (o antigo deixa de administrá-las), repita com "
            "--rebind"
        )
    return binding


def _write_binding(spec: CentralSpec) -> None:
    path = _binding_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(
        path,
        json.dumps(
            {"bond": _bond_key(spec), "bound_at": _now()},
            ensure_ascii=False,
        ),
    )


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


# ---- lock de reconciliação por vínculo ---------------------------------------


def _acquire_manage_locks(bond: str, store_path: Path):
    """Maintenance SH (coexiste com tarefas) + lock EX do vínculo."""
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
    material = f"{Path(store_path).resolve()}\0{bond}".encode()
    name = f"manage-{hashlib.sha256(material).hexdigest()[:16]}.lock"
    lock_file = (directory / name).open("a")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        lock_file.close()
        maintenance.close()
        raise ResticError(
            "outra reconciliação deste vínculo já está em andamento"
        ) from error
    return maintenance, lock_file


# ---- HTTP autenticado (TLS verificado, sem redirects, limite de corpo) --------


def _request(
    spec: CentralSpec,
    method: str,
    path: str,
    token: str,
    payload: dict | None = None,
) -> tuple[int, dict | None]:
    """Requisição autenticada — devolve (status, json|None). Erros de
    transporte devolvem (0, None) sem texto (URL/cabeçalhos não vazam)."""
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
            return desired, {
                "error": "consulta_falhou",
                "http": status,
            }
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


# ---- journal durável por tarefa ----------------------------------------------


def _journal_append(directory: Path, step: str, generation: int,
                    **fields: Any) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    line = json.dumps(
        {"step": step, "generation": generation, "at": _now(), **fields},
        ensure_ascii=False,
    )
    with (directory / "journal.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(line + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _journal_state(directory: Path) -> dict | None:
    """Último passo registrado de cada geração — a retomada resume."""
    journal = directory / "journal.jsonl"
    if not journal.exists():
        return None
    last: dict = {}
    for line in journal.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        generation = entry.get("generation")
        if generation is not None:
            last[generation] = entry
    if not last:
        return None
    newest = max(last)
    entry = dict(last[newest])
    entry["steps"] = {
        generation: item["step"] for generation, item in last.items()
    }
    return entry


def _validate_envelope(envelope: dict, spec: CentralSpec) -> None:
    """Identidade + integridade + versão do contrato — antes de
    qualquer efeito local. Diagnóstico sanitizado (sem valores)."""
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
            or envelope["generation"] < 1:
        raise ConfigurationError("envelope sem geração válida")
    if envelope.get("action") not in {"apply", "suspend"}:
        raise ConfigurationError("ação desconhecida no envelope")
    if envelope.get("contract_version") != _CONTRACT_VERSION:
        raise ConfigurationError(
            f"contract_version {envelope.get('contract_version')} "
            f"não suportada (máx. {_CONTRACT_VERSION})"
        )
    # hash canônico do conteúdo — recalculado localmente
    declared = envelope.get("hash")
    body = dict(envelope)
    body.pop("hash", None)
    expected = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if declared != expected:
        raise ConfigurationError("hash do envelope diverge do conteúdo")
    if envelope["action"] == "apply":
        # carrega pelo TaskSpec REAL — mesmas validações locais
        task_doc = envelope.get("task")
        if not isinstance(task_doc, dict):
            raise ConfigurationError("envelope apply sem documento")
        TaskSpec.from_dict(task_doc)


def _confirmation_payload(
    envelope: dict, status: str, applied_revision: int | None,
    detail: str | None,
) -> dict:
    return {
        "generation": envelope["generation"],
        "status": status,
        "applied_revision": applied_revision,
        "detail": detail,
        "agent_time": _now(),
    }


def _apply_one(
    spec: CentralSpec,
    envelope: dict,
    central_path: Path,
    store_path: Path,
    tasks_root: Path,
    *,
    activate: bool,
) -> dict:
    """Aplica UMA geração de UMA tarefa — idempotente e retomável.

    Ordem: identidade/hash/contrato validados → gen-N gravado → units
    → current.json atômico → journal 'applied'. Interrupção antes de
    'applied' → retoma no passo que falta; depois → só re-confirma.
    A mesma geração já aplicada é idempotente e UMA resposta antiga
    nunca reinstala geração superada (o journal marca o aplicado).
    """
    task_id = envelope["task_id"]
    generation = envelope["generation"]
    task_dir = tasks_root / task_id
    state = _journal_state(task_dir)
    steps = state["steps"] if state else {}
    step = steps.get(generation)

    # mesma geração já concluída — e se o journal tem aplicação MAIS
    # NOVA (gen > envelope), a resposta antiga é inerte
    if step == "applied" or (
        state is not None
        and any(
            g > generation and s == "applied"
            for g, s in steps.items()
        )
    ):
        return {"task_id": task_id, "generation": generation,
                "result": "already_applied"}

    # validação total ANTES de qualquer efeito — identidade, hash,
    # contrato e TaskSpec real (idempotente na retomada)
    _validate_envelope(envelope, spec)

    gen_file = task_dir / f"gen-{generation}.json"
    if not gen_file.exists():
        _atomic_write(
            gen_file, json.dumps(envelope, ensure_ascii=False)
        )
    if step is None:
        _journal_append(task_dir, "received", generation)
    if step in (None, "received"):
        _journal_append(task_dir, "validated", generation)

    if envelope["action"] == "suspend":
        suspend_managed_task(
            task_id, spec.agent_id, activate=activate
        )
        _journal_append(
            task_dir, "applied", generation,
            revision=envelope.get("revision"), action="suspend",
        )
        return {"task_id": task_id, "generation": generation,
                "result": "suspended"}

    current = task_dir / "current.json"
    if step not in ("units", "applied"):
        units = install_managed_task(
            task_id,
            current,
            store_path,
            central_path,
            spec.agent_id,
            envelope["schedule"],
            activate=activate,
        )
        _journal_append(
            task_dir, "units", generation,
            service=units["service"], timer=units["timer_name"],
        )
    # último efeito local: o documento que a unit referencia só
    # avança DEPOIS de tudo — falha anterior preserva a última boa
    _atomic_write(
        current, json.dumps(envelope["task"], ensure_ascii=False)
    )
    _journal_append(
        task_dir, "applied", generation,
        revision=envelope["revision"], action="apply",
    )
    return {"task_id": task_id, "generation": generation,
            "result": "applied",
            "revision": envelope["revision"]}


# ---- reconciliação -----------------------------------------------------------


def _flush_confirmations(
    spec: CentralSpec,
    store: ExecutionStore,
    token: str,
    summary: dict,
) -> None:
    """Reenvia confirmações pendentes — resposta HTTP perdida nunca
    perde o relato."""
    for item in store.pending_confirmations(
        _bond_key(spec), _now(), limit=50
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
        elif status in (401, 403):
            store.confirmation_blocked(item["id"], f"HTTP {status}")
            summary["confirmations_blocked"].append(item["task_id"])
        elif status in (409, 422):
            store.confirmation_blocked(item["id"], f"HTTP {status}")
            summary["confirmations_blocked"].append(item["task_id"])
        else:
            attempts = item["attempts"] + 1
            if attempts >= _CONFIRM_MAX_ATTEMPTS:
                store.confirmation_blocked(
                    item["id"], f"tentativas esgotadas ({status or 'rede'})"
                )
                summary["confirmations_blocked"].append(item["task_id"])
            else:
                store.confirmation_retry(
                    item["id"], f"HTTP {status or 'rede/timeout'}",
                    _backoff(attempts),
                )
                summary["confirmations_retry"].append(item["task_id"])


def _enqueue_confirmation(
    store: ExecutionStore, bond: str, envelope: dict, outcome: dict
) -> None:
    if outcome["result"] == "failed_validation":
        payload = _confirmation_payload(
            envelope, "failed", None, outcome["detail"]
        )
    else:
        payload = _confirmation_payload(
            envelope, "applied", outcome.get("revision"), None
        )
    store.enqueue_confirmation(
        bond, envelope["task_id"], envelope["generation"],
        json.dumps(payload),
    )


def inspect(
    central_path: Path, store_path: Path, *, rebind: bool = False
) -> dict:
    """Inspeção SEM efeitos: o que a reconciliação aplicaria — sem
    tocar em configurações, units, journal ou confirmações."""
    spec = load_central(central_path)
    try:
        token = credential_from_environment_or_os(spec.token_environment)
    except CredentialError as error:
        raise ConfigurationError(
            f"token indisponível ({spec.token_environment}) — a consulta "
            "não pode autenticar; nada foi alterado"
        ) from error
    desired, problem = _fetch_desired(spec, token)
    applied = _applied_generations()
    report = {
        "endpoint": spec.url,
        "agent_id": spec.agent_id,
        "desired": [],
        "problem": problem,
    }
    for envelope in desired:
        state = _journal_state(
            managed_directory() / "tasks.d" / envelope["task_id"]
        )
        applied_gen = (
            max(g for g, s in state["steps"].items() if s == "applied")
            if state and any(
                s == "applied" for s in state["steps"].values()
            )
            else None
        )
        report["desired"].append(
            {
                "task_id": envelope["task_id"],
                "generation": envelope["generation"],
                "action": envelope["action"],
                "applied_generation": applied_gen,
                "pending": applied_gen != envelope["generation"],
            }
        )
    return report


def _applied_generations() -> dict[str, int]:
    """task_id → última geração aplicada — derivado dos journals."""
    applied: dict[str, int] = {}
    tasks_root = managed_directory() / "tasks.d"
    if not tasks_root.exists():
        return applied
    for task_dir in tasks_root.iterdir():
        state = _journal_state(task_dir)
        if not state:
            continue
        generations = [
            g for g, s in state["steps"].items() if s == "applied"
        ]
        if generations:
            applied[task_dir.name] = max(generations)
    return applied


def reconcile(
    central_path: Path,
    store_path: Path,
    *,
    activate: bool = True,
    rebind: bool = False,
) -> dict:
    """Consulta → valida → aplica → confirma — uma rodada completa.

    `activate=False` escreve e valida units sem daemon-reload/enable
    (prova integrada sem sessão systemd de usuário).
    """
    spec = load_central(central_path)
    _check_binding(spec, rebind)
    locks = _acquire_manage_locks(_bond_key(spec), store_path)
    try:
        tasks_root = managed_directory() / "tasks.d"
        central_path = Path(central_path).resolve()
        summary: dict[str, Any] = {
            "endpoint": spec.url,
            "agent_id": spec.agent_id,
            "desired_total": 0,
            "applied": [],
            "suspended": [],
            "failed": [],
            "skipped_credential": False,
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
            # reenvio de confirmações pendentes ANTES de novas ações —
            # perda de resposta não atrasa o relato da aplicação
            _flush_confirmations(spec, store, token, summary)

            desired, problem = _fetch_desired(spec, token)
            if problem is not None:
                # central indisponível/resposta ruim → diagnóstico e
                # nova tentativa depois; backups locais NÃO são tocados
                summary["communication_error"] = problem
                return summary
            _write_binding(spec)  # vínculo confirmado só após 1ª consulta
            summary["desired_total"] = len(desired)

            for envelope in desired:
                try:
                    outcome = _apply_one(
                        spec, envelope, central_path, store_path,
                        tasks_root, activate=activate,
                    )
                except ConfigurationError as error:
                    outcome = {
                        "task_id": envelope.get("task_id", "?"),
                        "generation": envelope.get("generation", 0),
                        "result": "failed_validation",
                        "detail": str(error)[:900],
                    }
                except (ResticError, OSError) as error:
                    outcome = {
                        "task_id": envelope.get("task_id", "?"),
                        "generation": envelope.get("generation", 0),
                        "result": "failed_transient",
                        "detail": str(error)[:900],
                    }
                if outcome["result"] in ("applied", "suspended",
                                         "already_applied"):
                    key = "suspended" if outcome["result"] == "suspended" \
                        else "applied"
                    summary[key].append(outcome)
                else:
                    summary["failed"].append(outcome)
                if outcome["result"] != "failed_transient" \
                        and outcome["result"] != "already_applied":
                    # transiente: não confirma — retenta na próxima rodada
                    _enqueue_confirmation(
                        store, _bond_key(spec), envelope, outcome
                    )
            # confirmar os desta rodada imediatamente (melhor esforço)
            _flush_confirmations(spec, store, token, summary)
        finally:
            store.close()
    finally:
        for lock in locks:
            lock.close()
    return summary


def status(central_path: Path, store_path: Path) -> dict:
    """Estado LOCAL gerenciado: vínculo, aplicações do journal e fila
    de confirmações — consulta offline, sem chamada de rede."""
    spec = load_central(central_path)
    applied = _applied_generations()
    tasks = []
    tasks_root = managed_directory() / "tasks.d"
    if tasks_root.exists():
        for task_dir in sorted(tasks_root.iterdir()):
            state = _journal_state(task_dir)
            tasks.append(
                {
                    "task_id": task_dir.name,
                    "last_step": state["step"] if state else None,
                    "applied_generation": applied.get(task_dir.name),
                    "current": (
                        task_dir / "current.json"
                    ).exists(),
                }
            )
    store = ExecutionStore(store_path)
    try:
        confirmations = store.confirmation_state()
    finally:
        store.close()
    binding = None
    if _binding_file().exists():
        binding = json.loads(
            _binding_file().read_text(encoding="utf-8")
        )
    return {
        "endpoint": spec.url,
        "agent_id": spec.agent_id,
        "binding": binding,
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
