"""Autenticação: agentes (Bearer token), Bearer administrativo,
operadores (sessão opaca por cookie).

- Agente: token aleatório entregue UMA vez no provisionamento;
  armazenamos apenas SHA-256 — revogação vale imediatamente.
- Admin: MACIP_API_ADMIN_TOKEN — comparação timing-safe.
- Operador: cookie HttpOnly com identificador opaco — no banco só o
  hash; perfil lido a cada requisição (mudança vale já na próxima).
  Cookie NUNCA autentica rota de agente e token de agente NUNCA vira
  sessão de operador.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import UTC, datetime

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from . import config
from .db import session
from .models import Agent, AgentCredential, Operator
from .operators import CSRF_COOKIE, CSRF_HEADER, SESSION_COOKIE, find_session

_bearer = HTTPBearer(auto_error=False)


def new_agent_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _admin_bearer_ok(
    credentials: HTTPAuthorizationCredentials | None,
) -> bool | None:
    """True Bearer admin válido · False Bearer presente inválido · None ausente."""
    if credentials is None or credentials.scheme.lower() != "bearer":
        return None
    return hmac.compare_digest(
        credentials.credentials.encode(), config.admin_token().encode()
    )


def current_operator(
    request: Request, db: Session = Depends(session)
) -> Operator | None:
    """Sessão válida de operador ativo — perfil lido a cada chamada."""
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    row = find_session(db, token)
    if row is None:
        return None
    operator = db.get(Operator, row.operator_id)
    if operator is None or not operator.active:
        return None
    return operator


def require_operator(
    operator: Operator | None = Depends(current_operator),
) -> Operator:
    if operator is None:
        raise HTTPException(401, "sessão ausente ou inválida")
    return operator


def require_admin(
    request: Request,
    db: Session = Depends(session),
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> None:
    """Escrita administrativa: Bearer de automação OU operador admin.

    Mantém o contrato anterior do Bearer — token de agente ou sessão
    de viewer não passam.
    """
    admin = _admin_bearer_ok(credentials)
    if admin:
        return
    operator = current_operator(request, db)
    if operator is not None:
        if operator.role == "admin":
            return
        raise HTTPException(403, "perfil sem permissão de escrita")
    if admin is False:
        raise HTTPException(403, "credencial administrativa inválida")
    raise HTTPException(401, "credencial administrativa ausente")


def require_viewer(
    request: Request,
    db: Session = Depends(session),
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> None:
    """Consulta: Bearer de automação OU qualquer operador ativo."""
    admin = _admin_bearer_ok(credentials)
    if admin:
        return
    if current_operator(request, db) is not None:
        return
    if admin is False:
        raise HTTPException(403, "credencial administrativa inválida")
    raise HTTPException(401, "credencial administrativa ausente")


# ---- CSRF -------------------------------------------------------------------
#
# Sessões por cookie exigem double-submit: toda escrita com cookie de
# sessão carrega `X-CSRF-Token` igual ao cookie `macip_csrf` (não
# HttpOnly — o JS lê e repete). Login tem guarda própria (ainda sem
# sessão): exige o mesmo par cookie+cabeçalho, obtido em GET /auth/csrf.


def csrf_guard(request: Request) -> None:
    """Escrita autenticada por cookie exige o par CSRF correspondente.

    Chamadas apenas com Bearer (automação) não carregam o cookie de
    sessão e passam — CSRF é uma ameaça de navegador.
    """
    if request.cookies.get(SESSION_COOKIE) is None:
        return
    _require_csrf_pair(request)


def csrf_pair_guard(request: Request) -> None:
    """Login (e outros fluxos pré-sessão): par cookie+cabeçalho obrigatório."""
    _require_csrf_pair(request)


def _require_csrf_pair(request: Request) -> None:
    cookie = request.cookies.get(CSRF_COOKIE)
    header = request.headers.get(CSRF_HEADER)
    if not cookie or not header or not hmac.compare_digest(header, cookie):
        raise HTTPException(403, "proteção CSRF: token ausente ou divergente")


def require_agent(
    db: Session = Depends(session),
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> Agent:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(401, "token de agente ausente")
    credential = db.execute(
        select(AgentCredential).where(
            AgentCredential.token_hash == token_hash(credentials.credentials)
        )
    ).scalar_one_or_none()
    if credential is None or credential.revoked_at is not None:
        raise HTTPException(403, "token inválido ou revogado")
    agent = db.get(Agent, credential.agent_id)
    if agent is None:
        raise HTTPException(403, "agente inexistente")
    # última comunicação em horário do servidor — toda chamada autenticada
    db.execute(
        update(Agent)
        .where(Agent.id == agent.id)
        .values(last_seen_at=datetime.now(UTC))
    )
    db.commit()
    db.refresh(agent)
    return agent
