"""Contrato de tarefa versionada — validação estrutural no servidor.

Espelha `TaskSpec` do agente (src/macip_backup/models.py) como SCHEMA
PRÓPRIO da API: os pacotes se instalam separadamente e nenhum código de
produção importa o agente. Toda edição produz uma nova revisão; a
exportação gera o `tarefa.json` carregável por `TaskSpec.from_dict`.

Validação estrutural, nunca ambiental: a central NÃO verifica a
existência de caminhos, chaves, credenciais, montagens SMB ou
repositórios — eles pertencem ao agente Linux de execução. Apenas
restrições LEXICAIS são conferidas (repositório local dentro de uma
origem, URLs com credenciais embutidas, tipos exatos). Configuração
válida aqui NÃO significa destino conectado/homologado.

Segredos nunca entram: `password_environment`, `access_key_environment`
e `secret_key_environment` nomeiam a custódia (variável de ambiente ou
kernel keyring do agente); `identity_file`/`known_hosts_file` são
caminhos. Distintos de `token_environment` do central.json, que autentica
o agente na API.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    model_validator,
)


def _nonblank(value: str) -> str:
    """Espelha `_require_text` do agente: só espaço não é texto."""
    if not value.strip():
        raise ValueError("valor não pode ser só espaços")
    return value

CONTRACT_VERSION = 1

Ident = Annotated[
    StrictStr, Field(min_length=1, max_length=200, pattern=r"^\S+$")
]
Text = Annotated[
    StrictStr, Field(min_length=1, max_length=4000), AfterValidator(_nonblank)
]
ShortText = Annotated[
    StrictStr, Field(min_length=1, max_length=200), AfterValidator(_nonblank)
]
PosInt = Annotated[StrictInt, Field(gt=0)]
NonNegInt = Annotated[StrictInt, Field(ge=0)]
Port = Annotated[StrictInt, Field(ge=1, le=65535)]
EnvName = Annotated[
    StrictStr,
    Field(
        min_length=1,
        max_length=128,
        # nome da referência na custódia — nunca um valor secreto
        pattern=r"^[A-Za-z_][A-Za-z0-9_.-]*$",
    ),
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _remote_path_ok(value: str) -> bool:
    if any(character.isspace() or ord(character) < 32 for character in value):
        return False
    if value.startswith("~") or "//" in value:
        return False
    return not any(part == ".." for part in value.split("/"))


def _relative_subpath_ok(value: str) -> bool:
    if value.startswith(("/", "~")):
        return False
    return not any(
        part in {"", ".", ".."} for part in value.split("/")
    ) and "\\" not in value


class SMBMountIn(StrictModel):
    server: Annotated[
        StrictStr,
        Field(min_length=1, max_length=200, pattern=r"^[^\s/\\@?#;:]+$"),
    ]
    share: Annotated[
        StrictStr,
        Field(min_length=1, max_length=200, pattern=r"^[^/\\\x00-\x1f]+$"),
    ]
    mount_point: Text
    marker: ShortText | None = None
    probe_timeout_seconds: PosInt = 10

    @model_validator(mode="after")
    def _checks(self):
        if not self.mount_point.startswith("/"):
            raise ValueError("smb.mount_point deve ser caminho absoluto")
        if self.marker is not None and not _relative_subpath_ok(self.marker):
            raise ValueError("smb.marker deve ser caminho relativo seguro")
        return self


class SourceIn(StrictModel):
    path: Text
    smb: SMBMountIn | None = None

    @model_validator(mode="after")
    def _checks(self):
        # caminho do AGENTE Linux — conferência lexical, nunca fs local
        if not self.path.startswith("/"):
            raise ValueError("sources[].path deve ser caminho absoluto do agente")
        if self.path.rstrip("/") == "":
            raise ValueError("sources[].path não pode ser a raiz")
        if "//" in self.path or any(
            part == ".." for part in self.path.split("/")
        ):
            raise ValueError("sources[].path contém segmento não permitido")
        if self.smb is not None:
            mount = self.smb.mount_point.rstrip("/")
            if self.path != mount and not self.path.startswith(mount + "/"):
                raise ValueError("origem SMB fora do ponto de montagem declarado")
        return self


class SFTPIn(StrictModel):
    host: Annotated[
        StrictStr,
        Field(min_length=1, max_length=200, pattern=r"^[^\s/@?#\\;]+$"),
    ]
    port: Port = 22
    user: Annotated[
        StrictStr,
        Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._-]+$"),
    ]
    remote_path: ShortText
    identity_file: Text
    known_hosts_file: Text
    connect_timeout_seconds: PosInt = 15
    server_alive_interval_seconds: PosInt = 60
    server_alive_count_max: PosInt = 3

    @model_validator(mode="after")
    def _checks(self):
        if not _remote_path_ok(self.remote_path):
            raise ValueError("sftp.remote_path não é um caminho remoto permitido")
        return self


class S3In(StrictModel):
    endpoint: Text
    bucket: Annotated[
        StrictStr,
        Field(
            min_length=3,
            max_length=63,
            pattern=r"^[a-z0-9][a-z0-9._-]{1,61}[a-z0-9]$",
        ),
    ]
    access_key_environment: EnvName
    secret_key_environment: EnvName
    # prefix omitido = sem prefixo; o agente REJEITA "prefix": "" explícito
    prefix: ShortText | None = None
    region: ShortText | None = None
    bucket_lookup: Literal["auto", "path", "dns"] | None = None
    storage_class: ShortText | None = None

    @model_validator(mode="after")
    def _checks(self):
        parsed = urlsplit(self.endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("s3.endpoint deve ser URL http(s) válida")
        if parsed.username or parsed.password:
            raise ValueError("s3.endpoint não pode conter credenciais")
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise ValueError(
                "s3.endpoint não pode conter caminho, query ou fragmento"
            )
        try:
            parsed.port
        except ValueError as error:
            raise ValueError("s3.endpoint contém porta inválida") from error
        if self.prefix:
            if not _remote_path_ok(self.prefix):
                raise ValueError("s3.prefix não é um caminho remoto permitido")
            if self.prefix.startswith("/"):
                raise ValueError("s3.prefix deve ser relativo ao bucket")
        if self.region is not None and any(
            character.isspace() for character in self.region
        ):
            raise ValueError("s3.region não pode conter espaços")
        return self


class RetentionIn(StrictModel):
    keep_last: PosInt | None = None


class DestinationIn(StrictModel):
    id: Ident
    name: ShortText
    kind: Literal["local", "sftp", "smb", "s3"]
    repository: Text | None = None
    password_environment: EnvName
    retention: RetentionIn | None = None
    sftp: SFTPIn | None = None
    smb: SMBMountIn | None = None
    s3: S3In | None = None

    @model_validator(mode="after")
    def _coherence(self):
        others = {
            "local": ("sftp", "smb", "s3"),
            "sftp": ("smb", "s3"),
            "smb": ("sftp", "s3"),
            "s3": ("sftp", "smb"),
        }
        for field_name in others[self.kind]:
            if getattr(self, field_name) is not None:
                raise ValueError(
                    f"destination.{field_name} não é permitido em destino "
                    f"{self.kind}"
                )
        if self.kind == "local":
            if self.repository is None:
                raise ValueError("destination.repository é obrigatório em local")
            if not self.repository.startswith("/"):
                raise ValueError("repository local deve ser caminho absoluto")
        elif self.kind == "smb":
            if self.repository is None:
                raise ValueError("destination.repository é obrigatório em smb")
            if not _relative_subpath_ok(self.repository):
                raise ValueError("repository smb deve ser caminho relativo seguro")
            if self.smb is None:
                raise ValueError("destination.smb é obrigatório em destino SMB")
        else:
            if self.repository is not None:
                raise ValueError(
                    f"destination.repository não é permitido em destino "
                    f"{self.kind}"
                )
            if self.kind == "sftp" and self.sftp is None:
                raise ValueError("destination.sftp é obrigatório em destino SFTP")
            if self.kind == "s3" and self.s3 is None:
                raise ValueError("destination.s3 é obrigatório em destino S3")
        return self


class TimeoutsIn(StrictModel):
    initialize_seconds: PosInt = 120
    backup_seconds: PosInt = 14_400
    snapshots_seconds: PosInt = 120
    check_seconds: PosInt = 3_600
    restore_seconds: PosInt = 14_400
    forget_seconds: PosInt = 3_600


class RetryIn(StrictModel):
    attempts: PosInt = 1
    backoff_seconds: NonNegInt = 0


class TaskSpecIn(StrictModel):
    """Configuração do backup — os identificadores centrais (id,
    client.id, device_id) são derivados do vínculo no servidor, nunca
    do corpo recebido."""

    sources: list[
        Annotated[
            SourceIn,
            # forma do agente: "/path" ≡ {"path": "/path"}
            BeforeValidator(
                lambda value: {"path": value} if isinstance(value, str) else value
            ),
        ]
    ] = Field(min_length=1, max_length=32)
    destinations: list[DestinationIn] = Field(min_length=1, max_length=32)
    exclusions: list[ShortText] = Field(default_factory=list, max_length=200)
    compression: Literal["auto", "off", "max"] = "auto"
    timeouts: TimeoutsIn | None = None
    retry: RetryIn | None = None

    @model_validator(mode="after")
    def _cross(self):
        ids = [destination.id for destination in self.destinations]
        if len(ids) != len(set(ids)):
            raise ValueError("destination.id deve ser único por tarefa")
        # lexical: repositório local/smb não pode ficar dentro de origem
        source_paths = [
            source.path.rstrip("/") for source in self.sources
        ]
        for destination in self.destinations:
            if destination.kind == "local":
                repository = destination.repository.rstrip("/")
                roots = source_paths
            elif destination.kind == "smb":
                repository = (
                    f"{destination.smb.mount_point.rstrip('/')}/"
                    f"{destination.repository}"
                ).rstrip("/")
                roots = source_paths
            else:
                continue
            for source in roots:
                if repository == source or repository.startswith(source + "/"):
                    raise ValueError(
                        "repositório não pode ficar dentro de uma origem: "
                        f"{repository}"
                    )
        return self


# ---- agendamento estruturado ------------------------------------------------
#
# kind manual: sem horários — operador dispara a tarefa manualmente.
# kind hourly: minuto dentro de cada hora (0-59), em `timezone`.
# kind daily: hour/minute locais (0-23/0-59), em `timezone` IANA.
# kind weekly: weekdays na numeração ISO 8601 (1=segunda … 7=domingo),
#   + hour/minute locais, em `timezone` IANA.
# Fuso explícito e validado — sem cron/shell/systemd-analyze na API.
# A conversão para o scheduler do agente é etapa posterior.


IANATZ = Annotated[StrictStr, Field(min_length=1, max_length=64)]
Hour = Annotated[StrictInt, Field(ge=0, le=23)]
Minute = Annotated[StrictInt, Field(ge=0, le=59)]
Weekday = Annotated[StrictInt, Field(ge=1, le=7)]


class ScheduleIn(StrictModel):
    kind: Literal["manual", "hourly", "daily", "weekly"]
    timezone: IANATZ | None = None
    minute: Minute | None = None
    hour: Hour | None = None
    weekdays: list[Weekday] | None = Field(default=None, max_length=7)

    @model_validator(mode="after")
    def _coherence(self):
        if self.kind != "manual" and self.timezone is None:
            raise ValueError(
                f"schedule.timezone (IANA) é obrigatório em {self.kind}"
            )
        if self.timezone is not None:
            try:
                ZoneInfo(self.timezone)
            except (ZoneInfoNotFoundError, KeyError) as error:
                raise ValueError(
                    f"schedule.timezone não é fuso IANA: {self.timezone}"
                ) from error
        if self.kind == "manual":
            for field_name in ("minute", "hour", "weekdays"):
                if getattr(self, field_name) is not None:
                    raise ValueError(
                        f"schedule.{field_name} não é permitido em manual"
                    )
        elif self.kind == "hourly":
            if self.minute is None:
                raise ValueError("schedule.minute é obrigatório em hourly")
            if self.hour is not None or self.weekdays is not None:
                raise ValueError("schedule.hour/weekdays não são permitidos em hourly")
        elif self.kind == "daily":
            if self.hour is None or self.minute is None:
                raise ValueError("schedule.hour e schedule.minute são obrigatórios em daily")
            if self.weekdays is not None:
                raise ValueError("schedule.weekdays não é permitido em daily")
        else:  # weekly
            if not self.weekdays:
                raise ValueError("schedule.weekdays (1=seg..7=dom) é obrigatório em weekly")
            if len(set(self.weekdays)) != len(self.weekdays):
                raise ValueError("schedule.weekdays não pode repetir dia")
            if self.hour is None or self.minute is None:
                raise ValueError("schedule.hour e schedule.minute são obrigatórios em weekly")
        return self


class TaskConfigIn(StrictModel):
    """Envelope da revisão: configuração + agendamento (contrato v1)."""

    name: ShortText
    spec: TaskSpecIn
    schedule: ScheduleIn


def normalize(config: dict[str, Any]) -> str:
    """Forma canônica da revisão para a comparação de salvamento
    idêntico: chaves ordenadas e valores já normalizados pelo schema
    (defaults expandidos por model_dump antes de guardar)."""
    return json.dumps(config, sort_keys=True, separators=(",", ":"))


def export_taskspec(
    *,
    task_id: uuid.UUID,
    name: str,
    client_id: uuid.UUID,
    client_name: str,
    agent_id: uuid.UUID,
    spec: dict[str, Any],
) -> dict[str, Any]:
    """Mapeamento → `tarefa.json` carregável pelo agente atual:

      id        ← UUID CENTRAL da tarefa (não nome editável);
      client.id ← UUID CENTRAL do cliente (derivado do agente);
      device_id ← UUID CENTRAL do agente responsável;
      name      ← nome da revisão (texto, não identificador);
      spec      ← sources/exclusions/compression/destinations/
                  timeouts/retry validados no contrato central;
      schedule  ← NÃO exportado (envelope central, fora do TaskSpec).
    """
    return {
        "id": str(task_id),
        "name": name,
        "client": {"id": str(client_id), "name": client_name},
        "device_id": str(agent_id),
        **spec,
    }
