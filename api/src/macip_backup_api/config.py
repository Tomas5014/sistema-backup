"""Configuração por ambiente — segredos SÓ via variáveis de ambiente.

MACIP_API_DATABASE_URL   postgresql+psycopg://... (obrigatório)
MACIP_API_ADMIN_TOKEN    Bearer administrativo (obrigatório, sem default)
MACIP_API_MAX_BODY_BYTES limite de corpo (default 1 MiB)

Sessões de operadores:
MACIP_API_SESSION_TTL_SECONDS      expiração da sessão (default 28800)
MACIP_API_SESSION_INSECURE_COOKIE  "1" remove o flag Secure — exceção
                                   EXPLÍCITA para desenvolvimento local;
                                   produção exige o default (Secure).
MACIP_API_LOGIN_MAX_ATTEMPTS       falhas por janela (default 5)
MACIP_API_LOGIN_WINDOW_SECONDS     janela da limitação (default 900)
"""

from __future__ import annotations

import os


class ConfigError(RuntimeError):
    pass


def database_url() -> str:
    url = os.environ.get("MACIP_API_DATABASE_URL", "")
    if not url:
        raise ConfigError("MACIP_API_DATABASE_URL ausente")
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    if not url.startswith("postgresql+psycopg://"):
        raise ConfigError("MACIP_API_DATABASE_URL deve ser postgresql://...")
    return url


def admin_token() -> str:
    token = os.environ.get("MACIP_API_ADMIN_TOKEN", "")
    if not token:
        raise ConfigError(
            "MACIP_API_ADMIN_TOKEN ausente — defina um token forte por ambiente"
        )
    return token


def max_body_bytes() -> int:
    return int(os.environ.get("MACIP_API_MAX_BODY_BYTES", "1048576"))


def session_ttl_seconds() -> int:
    return int(os.environ.get("MACIP_API_SESSION_TTL_SECONDS", "28800"))


def session_cookie_secure() -> bool:
    """Secure é o default. A exceção local exige env EXPLÍCITA."""
    return os.environ.get("MACIP_API_SESSION_INSECURE_COOKIE") != "1"


def login_max_attempts() -> int:
    return int(os.environ.get("MACIP_API_LOGIN_MAX_ATTEMPTS", "5"))


def login_window_seconds() -> int:
    return int(os.environ.get("MACIP_API_LOGIN_WINDOW_SECONDS", "900"))
