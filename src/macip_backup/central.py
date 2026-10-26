"""Configuração local da central — separada da tarefa de backup.

Sem este arquivo o agente comporta-se exatamente como antes: backup,
histórico e credenciais de repositório permanecem 100% locais.

O arquivo declara SOMENTE o endpoint, a identidade central do agente,
o NOME da variável de ambiente do token (nunca o token) e timeouts.
O token resolve por ambiente → kernel keyring e só durante o envio —
não pode ser gravado neste JSON.

```json
{
  "url": "https://backup-central.exemplo.local",
  "agent_id": "<uuid-central-do-agente>",
  "token_environment": "MACIP_API_TOKEN",
  "connect_timeout_seconds": 10,
  "request_timeout_seconds": 30
}
```

HTTPS é obrigatório fora de localhost — tokens Bearer não podem
transitar em claro. O UUID local da tarefa (`client_id` da tarefa, ex.:
`macip-laboratorio`) NÃO é a identidade central — não é enviado.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from .models import ConfigurationError


@dataclass(frozen=True)
class CentralSpec:
    url: str
    agent_id: str
    token_environment: str
    connect_timeout_seconds: int = 10
    request_timeout_seconds: int = 30

    @classmethod
    def from_dict(cls, data: dict, source: str = "config") -> "CentralSpec":
        if not isinstance(data, dict):
            raise ConfigurationError(f"{source}: objeto JSON obrigatório")
        url = data.get("url")
        if not url or not isinstance(url, str):
            raise ConfigurationError(f"{source}: 'url' ausente")
        parsed = urlparse(url)
        try:
            port = parsed.port
        except ValueError as error:
            raise ConfigurationError(f"{source}: porta inválida em 'url'") from error
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ConfigurationError(
                f"{source}: 'url' deve ser HTTP(S) com host válido"
            )
        if parsed.username is not None or parsed.password is not None:
            raise ConfigurationError(
                f"{source}: credenciais embutidas em 'url' não são aceitas"
            )
        if port is not None and not 1 <= port <= 65535:
            raise ConfigurationError(f"{source}: porta inválida em 'url'")
        local = parsed.hostname in ("localhost", "127.0.0.1", "::1")
        if parsed.scheme != "https" and not local:
            raise ConfigurationError(
                f"{source}: HTTPS obrigatório fora de localhost — "
                "o token do agente não pode transitar em claro"
            )
        agent_id = data.get("agent_id")
        try:
            agent_id = str(uuid.UUID(str(agent_id)))
        except (TypeError, ValueError) as error:
            raise ConfigurationError(
                f"{source}: 'agent_id' deve ser o UUID central do agente"
            ) from error
        token_env = data.get("token_environment")
        if not token_env or not isinstance(token_env, str):
            raise ConfigurationError(
                f"{source}: 'token_environment' deve nomear a variável "
                "que contém o token — o token NUNCA vai no arquivo"
            )
        if "token" in data:
            raise ConfigurationError(
                f"{source}: campo 'token' não é aceito — use "
                f"'token_environment' nomeando a variável"
            )
        return cls(
            url=url.rstrip("/"),
            agent_id=agent_id,
            token_environment=token_env,
            connect_timeout_seconds=cls._timeout(
                data.get("connect_timeout_seconds", 10),
                "connect_timeout_seconds",
                source,
            ),
            request_timeout_seconds=cls._timeout(
                data.get("request_timeout_seconds", 30),
                "request_timeout_seconds",
                source,
            ),
        )

    @staticmethod
    def _timeout(value: object, field: str, source: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ConfigurationError(f"{source}: '{field}' deve ser inteiro positivo")
        return value


def load_central(path: Path) -> CentralSpec:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ConfigurationError(f"configuração central inválida: {error}")
    return CentralSpec.from_dict(data, source=str(path))
