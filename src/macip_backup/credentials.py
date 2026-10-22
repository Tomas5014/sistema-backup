"""Resolução de credenciais: variável de ambiente ou keystore do SO.

Nomes de segredos nunca viram valores nesta camada. A consulta ao SO usa
o kernel keyring via ``keyctl`` (keyutils): segredo gravado uma vez por
``keyctl add user macip.<nome> 'segredo' @s`` — nada em arquivos de
tarefa nem em logs. systemd LoadCredential/Secret Service podem ser
adicionados depois atrás da mesma interface.
"""

from __future__ import annotations

import os
import shutil
import subprocess

from .restic import ResticError

_KEYCTL_TIMEOUT_SECONDS = 10
_KEYCTL_NAMESPACE = "macip"


def _keyctl_lookup(name: str, timeout_seconds: int = _KEYCTL_TIMEOUT_SECONDS) -> str | None:
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
        raise ResticError(
            f"keyctl não respondeu em {timeout_seconds}s para {name}"
        ) from error
    if completed.returncode != 0:
        return None
    secret = completed.stdout.strip()
    return secret or None


def credential_from_environment_or_os(name: str) -> str:
    """Resolve um segredo por nome: ambiente primeiro, kernel keyring depois."""
    value = os.environ.get(name)
    if value:
        return value
    value = _keyctl_lookup(name)
    if value:
        return value
    raise ResticError(
        f"credencial ausente na variável de ambiente {name} e no kernel "
        f"keyring ({_KEYCTL_NAMESPACE}.{name})"
    )
