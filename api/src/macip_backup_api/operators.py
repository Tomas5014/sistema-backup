"""Operadores internos: Argon2id, sessões opacas, rate limit, auditoria.

- Senhas: argon2-cffi (Argon2id) — biblioteca mantida, sem cripto própria.
- Sessões: identificador aleatório forte; persistimos SÓ o SHA-256.
- Limitação de login: tabela `login_attempts` — vale entre processos.
- Auditoria: logger dedicado, jamais com segredos.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, VerifyMismatchError
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.orm import Session

from . import config
from .models import LoginAttempt, Operator, OperatorSession

audit = logging.getLogger("macip_backup_api.audit")

ROLES = {"admin", "viewer"}
SESSION_COOKIE = "macip_session"
CSRF_COOKIE = "macip_csrf"
CSRF_HEADER = "x-csrf-token"

_hasher = PasswordHasher()  # Argon2id — parâmetros mantidos da biblioteca
_dummy_hash: str | None = None


def normalize_login(login: str) -> str:
    """Login único por normalização: trim + minúsculas."""
    return login.strip().lower()


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError):
        return False


def _verify_or_burn_time(password_hash: str | None, password: str) -> bool:
    """Sempre executa um verify — tempo igual para login inexistente."""
    global _dummy_hash
    if password_hash is None:
        if _dummy_hash is None:
            _dummy_hash = _hasher.hash("dummy-timing-equalization")
        password_hash = _dummy_hash
    ok = verify_password(password_hash, password)
    return ok


# ---- sessões ---------------------------------------------------------------


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def session_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(db: Session, operator: Operator) -> tuple[OperatorSession, str]:
    """Nova sessão = identificador novo (renovado a cada login)."""
    token = new_session_token()
    row = OperatorSession(
        id=uuid.uuid4(),
        operator_id=operator.id,
        token_hash=session_hash(token),
        expires_at=datetime.now(UTC)
        + timedelta(seconds=config.session_ttl_seconds()),
    )
    db.add(row)
    return row, token


def revoke_sessions(db: Session, operator_id: uuid.UUID) -> None:
    db.execute(
        update(OperatorSession)
        .where(
            OperatorSession.operator_id == operator_id,
            OperatorSession.revoked_at.is_(None),
        )
        .values(revoked_at=datetime.now(UTC))
    )


def find_session(db: Session, token: str) -> OperatorSession | None:
    row = db.execute(
        select(OperatorSession).where(
            OperatorSession.token_hash == session_hash(token)
        )
    ).scalar_one_or_none()
    now = datetime.now(UTC)
    if row is None or row.revoked_at is not None or row.expires_at <= now:
        return None
    return row


# ---- limitação de login (entre processos — em banco) -----------------------


class LoginRateLimited(Exception):
    pass


def check_login_rate(db: Session, login: str, ip: str) -> None:
    """Contagem atômica entre processos: dois advisory locks
    TRANSACIONAIS — um por login, um por IP — adquiridos em ordem
    fixa (login depois IP, sem deadlock). A verificação e a
    gravação posterior da tentativa correm dentro da transação do
    request: tentativas simultâneas do mesmo login/IP serializam —
    nenhuma ultrapassa o limite por leitura desatualizada."""
    for key in (f"login:{login}", f"ip:{ip}"):
        db.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
            {"k": key},
        )
    window = datetime.now(UTC) - timedelta(seconds=config.login_window_seconds())
    limit = config.login_max_attempts()
    for field, value in ((LoginAttempt.login, login), (LoginAttempt.ip, ip)):
        recent = db.execute(
            select(func.count()).select_from(LoginAttempt).where(
                field == value, LoginAttempt.attempted_at >= window
            )
        ).scalar_one()
        if recent >= limit:
            raise LoginRateLimited


def record_failed_attempt(db: Session, login: str, ip: str) -> None:
    db.add(LoginAttempt(login=login, ip=ip))
    db.execute(  # poda — tabela permanece pequena
        delete(LoginAttempt).where(
            LoginAttempt.attempted_at
            < datetime.now(UTC) - timedelta(seconds=config.login_window_seconds())
        )
    )


def clear_attempts(db: Session, login: str, ip: str) -> None:
    db.execute(
        delete(LoginAttempt).where(
            LoginAttempt.login == login, LoginAttempt.ip == ip
        )
    )
