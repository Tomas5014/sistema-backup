"""Resolução unificada de credenciais: ambiente ou keystore do SO.

A mesma interface atende `run`, `agent`, `snapshots`, `check`, `restore`
e `forget`. Nomes de segredos nunca viram valores nesta camada. A
consulta ao SO usa o kernel keyring via ``keyctl`` (keyutils): segredo
gravado por ``keyctl padd user macip.<nome> @s`` (lido de stdin, sem
argv nem histórico do shell). ``os.environ`` nunca é modificado — o
segredo só é injetado no ambiente do processo filho (restic).
"""

from __future__ import annotations

import os
import shutil
import subprocess

from .models import ConfigurationError

_KEYCTL_TIMEOUT_SECONDS = 10
_KEYCTL_NAMESPACE = "macip"


class CredentialError(ConfigurationError):
    """Credencial obrigatória ausente ou ilegível — falha não repetível."""


def _keyctl_lookup(
    name: str, timeout_seconds: int = _KEYCTL_TIMEOUT_SECONDS
) -> str | None:
    keyctl = shutil.which("keyctl")
    if keyctl is None:
        return None
    try:
        completed = subprocess.run(
            [keyctl, "pipe", f"%user:{_KEYCTL_NAMESPACE}.{name}"],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise CredentialError(
            f"keyctl não respondeu em {timeout_seconds}s para {name}"
        ) from error
    if completed.returncode != 0:
        return None
    # `keyctl pipe` devolve o payload exato — nunca normalizar o segredo.
    return completed.stdout or None


def credential_from_environment_or_os(name: str) -> str:
    """Resolve um segredo por nome: ambiente primeiro, kernel keyring depois."""
    value = os.environ.get(name)
    if value:
        return value
    value = _keyctl_lookup(name)
    if value:
        return value
    raise CredentialError(
        f"credencial ausente na variável de ambiente {name} e no kernel "
        f"keyring ({_KEYCTL_NAMESPACE}.{name})"
    )
