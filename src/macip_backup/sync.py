"""Sincronização da outbox com a API central — rodada limitada.

O envio é independente do backup: indisponibilidade da API, token
ausente ou erro de autenticação não alteram um resultado já concluído
— o item permanece na fila e é reenviado depois.

Segurança:
- token resolvido por ambiente → keyring SOMENTE durante o envio;
- HTTPS obrigatório fora de localhost (CentralSpec valida);
- verificação TLS padrão do Python — nunca desligada;
- redirecionamentos NÃO seguidos — o Bearer não pode vazar para outro
  destino;
- token/Authorization jamais são registrados em erros, SQLite ou logs.

Nenhuma transação SQLite fica aberta durante a chamada HTTP: a fila é
lida, o POST acontece e o resultado é gravado numa transação curta.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .agent import task_lock_directory
from .central import CentralSpec, load_central
from .credentials import CredentialError, credential_from_environment_or_os
from .models import ConfigurationError
from .restic import ResticError
from .store import ExecutionStore

_MAX_ATTEMPTS = 10
_BACKOFF_BASE_SECONDS = 30
_BACKOFF_CAP_SECONDS = 3600

_SYNC_LOCK = "sync.lock"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Recusa redirects — o token não segue para outro destino."""

    def redirect_request(self, *args, **kwargs):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def _now() -> datetime:
    return datetime.now(UTC)


def _backoff(attempts: int) -> str:
    delay = min(_BACKOFF_BASE_SECONDS * (2**attempts), _BACKOFF_CAP_SECONDS)
    return (_now() + timedelta(seconds=delay)).isoformat()


def _post(spec: CentralSpec, payload: bytes, token: str) -> tuple[int, dict | None]:
    """POST na API — devolve (status_http, corpo_json | None).

    Nunca propaga o token: erros carregam apenas código/motivo HTTP.
    """
    request = urllib.request.Request(
        f"{spec.url}/api/v1/agent/executions",
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Content-Length": str(len(payload)),
        },
        method="POST",
    )
    try:
        with _opener.open(request, timeout=spec.request_timeout_seconds) as response:
            body = response.read()
            try:
                return response.status, json.loads(body)
            except json.JSONDecodeError:
                return response.status, None
    except urllib.error.HTTPError as error:
        try:
            return error.code, json.loads(error.read() or b"{}")
        except json.JSONDecodeError:
            return error.code, None
        finally:
            error.close()


def _acquire_sync_locks(task_id: str):
    """Maintenance SH (coexiste com tarefas, bloqueia install) + lock EX
    da própria sincronização — dois syncs não disputam a fila."""
    import fcntl

    directory = task_lock_directory()
    directory.mkdir(parents=True, exist_ok=True)
    maintenance = (directory / "maintenance.lock").open("a")
    try:
        fcntl.flock(maintenance.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError as error:
        maintenance.close()
        raise ResticError("manutenção em andamento — sincronização recusada")
    sync_lock = (directory / _SYNC_LOCK).open("a")
    try:
        fcntl.flock(sync_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        sync_lock.close()
        maintenance.close()
        raise ResticError("outra sincronização já está em andamento")
    return maintenance, sync_lock


def run_sync(store_path: Path, central_path: Path, limit: int = 50) -> dict:
    """Uma rodada limitada de envio — agenda automática não entra aqui."""
    spec = load_central(central_path)
    locks = _acquire_sync_locks("_")
    try:
        store = ExecutionStore(Path(store_path))
        try:
            items = store.pending_outbox(
                spec.url, spec.agent_id, _now().isoformat(), limit
            )
            summary = {
                "endpoint": spec.url,
                "agent_id": spec.agent_id,
                "pending_total": len(items),
                "sent": [],
                "retry": [],
                "blocked": [],
                "skipped_credential": [],
            }
            if not items:
                return summary
            try:
                token = credential_from_environment_or_os(spec.token_environment)
            except CredentialError:
                # ausência do token não impede nada — só adia a rodada
                summary["skipped_credential"] = [
                    item["execution_id"] for item in items
                ]
                return summary

            for item in items:
                status, body = _post(spec, item["payload"].encode(), token)
                execution_id = item["execution_id"]
                if (
                    status in (200, 201)
                    and body
                    and body.get("status") in ("created", "duplicate")
                    and body.get("execution_id") == execution_id
                ):
                    store.outbox_sent(item["id"])
                    summary["sent"].append(execution_id)
                elif status in (401, 403):
                    store.outbox_blocked(
                        item["id"], "auth_blocked", f"HTTP {status}"
                    )
                    summary["blocked"].append(execution_id)
                elif status in (409, 413, 422):
                    store.outbox_blocked(
                        item["id"], "failed_permanent", f"HTTP {status}"
                    )
                    summary["blocked"].append(execution_id)
                else:
                    # rede, timeout, 5xx, resposta inesperada ou JSON
                    # inválido → retentativa limitada, nunca "entregue"
                    row = next(
                        r for r in store.outbox_state() if r["id"] == item["id"]
                    )
                    attempts = row["attempts"] + 1
                    if attempts >= _MAX_ATTEMPTS:
                        store.outbox_blocked(
                            item["id"],
                            "failed_permanent",
                            f"tentativas esgotadas ({status or 'sem resposta'})",
                        )
                        summary["blocked"].append(execution_id)
                    else:
                        store.outbox_retry(
                            item["id"],
                            f"HTTP {status or 'rede/timeout'}",
                            _backoff(attempts),
                        )
                        summary["retry"].append(execution_id)
            return summary
        finally:
            store.close()
    finally:
        for lock in locks:
            lock.close()


def outbox_json(store_path: Path) -> str:
    store = ExecutionStore(Path(store_path))
    try:
        return json.dumps(store.outbox_state(), ensure_ascii=False, indent=2)
    finally:
        store.close()
