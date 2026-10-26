"""Configuração por ambiente — segredos SÓ via variáveis de ambiente.

MACIP_API_DATABASE_URL   postgresql+psycopg://... (obrigatório)
MACIP_API_ADMIN_TOKEN    Bearer administrativo (obrigatório, sem default)
MACIP_API_MAX_BODY_BYTES limite de corpo (default 1 MiB)
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
