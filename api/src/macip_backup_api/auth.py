"""Autenticação administrativa (Bearer de ambiente) × agentes (token).

- Admin: MACIP_API_ADMIN_TOKEN — comparação timing-safe.
- Agente: token aleatório entregue UMA vez no provisionamento;
  armazenamos apenas SHA-256 — revogação vale imediatamente.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import UTC, datetime

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from . import config
from .db import session
from .models import Agent, AgentCredential

_bearer = HTTPBearer(auto_error=False)


def new_agent_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def require_admin(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> None:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(401, "credencial administrativa ausente")
    if not hmac.compare_digest(
        credentials.credentials.encode(), config.admin_token().encode()
    ):
        raise HTTPException(403, "credencial administrativa inválida")


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
