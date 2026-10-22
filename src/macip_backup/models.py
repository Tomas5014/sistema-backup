"""Modelos de configuração sem credenciais persistidas."""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
import re
from typing import Any
from urllib.parse import urlsplit


class ConfigurationError(ValueError):
    """Configuração inválida para uma tarefa de backup."""


_SMB_PROBE_TIMEOUT_SECONDS = 10
_SMB_FS_TYPES = frozenset({"cifs", "smb3"})


class CompressionMode(StrEnum):
    AUTO = "auto"
    OFF = "off"
    MAX = "max"


class DestinationKind(StrEnum):
    LOCAL = "local"
    SFTP = "sftp"
    SMB = "smb"
    S3 = "s3"


@dataclass(frozen=True)
class OperationTimeouts:
    """Limites em segundos para operações do restic."""

    initialize_seconds: int = 120
    backup_seconds: int = 14_400
    snapshots_seconds: int = 120
    check_seconds: int = 3_600
    restore_seconds: int = 14_400
    forget_seconds: int = 3_600

    def __post_init__(self) -> None:
        for field_name, value in (
            ("timeouts.initialize_seconds", self.initialize_seconds),
            ("timeouts.backup_seconds", self.backup_seconds),
            ("timeouts.snapshots_seconds", self.snapshots_seconds),
            ("timeouts.check_seconds", self.check_seconds),
            ("timeouts.restore_seconds", self.restore_seconds),
            ("timeouts.forget_seconds", self.forget_seconds),
        ):
            _require_positive_integer(field_name, value)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "OperationTimeouts":
        return cls(
            initialize_seconds=_optional_positive_integer(
                data, "initialize_seconds", 120, "timeouts.initialize_seconds"
            ),
            backup_seconds=_optional_positive_integer(
                data, "backup_seconds", 14_400, "timeouts.backup_seconds"
            ),
            snapshots_seconds=_optional_positive_integer(
                data, "snapshots_seconds", 120, "timeouts.snapshots_seconds"
            ),
            check_seconds=_optional_positive_integer(
                data, "check_seconds", 3_600, "timeouts.check_seconds"
            ),
            restore_seconds=_optional_positive_integer(
                data, "restore_seconds", 14_400, "timeouts.restore_seconds"
            ),
            forget_seconds=_optional_positive_integer(
                data, "forget_seconds", 3_600, "timeouts.forget_seconds"
            ),
        )


@dataclass(frozen=True)
class Client:
    id: str
    name: str

    def __post_init__(self) -> None:
        _require_identifier("client.id", self.id)
        _require_text("client.name", self.name)


@dataclass(frozen=True)
class RetentionPolicy:
    """Política declarada por destino; aplicação será etapa posterior."""

    keep_last: int | None = None

    def __post_init__(self) -> None:
        _require_positive_integer(
            "retention.keep_last", self.keep_last, allow_none=True
        )


@dataclass(frozen=True)
class SFTPConnection:
    """Dados não secretos para o backend SFTP nativo do restic."""

    host: str
    port: int
    user: str
    remote_path: str
    identity_file: str
    known_hosts_file: str
    connect_timeout_seconds: int = 15
    server_alive_interval_seconds: int = 60
    server_alive_count_max: int = 3

    def __post_init__(self) -> None:
        _require_sftp_host("sftp.host", self.host)
        _require_sftp_user("sftp.user", self.user)
        _require_port("sftp.port", self.port)
        _require_remote_path("sftp.remote_path", self.remote_path)
        _require_file_reference("sftp.identity_file", self.identity_file)
        _require_file_reference("sftp.known_hosts_file", self.known_hosts_file)
        _require_positive_integer("sftp.connect_timeout_seconds", self.connect_timeout_seconds)
        _require_positive_integer(
            "sftp.server_alive_interval_seconds", self.server_alive_interval_seconds
        )
        _require_positive_integer(
            "sftp.server_alive_count_max", self.server_alive_count_max
        )

    @property
    def remote_path_is_absolute(self) -> bool:
        return self.remote_path.startswith("/")

    def validate_files_for_execution(self) -> None:
        for field_name, reference in (
            ("sftp.identity_file", self.identity_file),
            ("sftp.known_hosts_file", self.known_hosts_file),
        ):
            path = Path(reference).expanduser()
            if not path.is_file():
                raise ConfigurationError(f"{field_name} não existe ou não é arquivo: {path}")


@dataclass(frozen=True)
class SMBMount:
    """Identidade esperada de um ponto de montagem SMB gerenciado pelo sistema.

    A montagem (mount.cifs/fstab/automount) é responsabilidade do ambiente;
    aqui se confere que o ponto existe, é o compartilhamento esperado e que
    o marcador opcional responde dentro do limite — sem travar num volume
    cujo servidor caiu.
    """

    server: str
    share: str
    mount_point: str
    marker: str | None = None
    probe_timeout_seconds: int = _SMB_PROBE_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        _require_smb_server("smb.server", self.server)
        _require_smb_share("smb.share", self.share)
        _require_text("smb.mount_point", self.mount_point)
        if not self.mount_point.startswith("/"):
            raise ConfigurationError("smb.mount_point deve ser caminho absoluto")
        if self.marker is not None:
            _require_relative_subpath("smb.marker", self.marker)
        _require_positive_integer("smb.probe_timeout_seconds", self.probe_timeout_seconds)

    @property
    def mount_point_path(self) -> Path:
        return Path(self.mount_point).expanduser().resolve(strict=False)

    @property
    def expected_source(self) -> str:
        return f"//{self.server}/{self.share}"

    def validate_for_execution(self) -> None:
        mount = self.mount_point_path
        target, source, fstype = _findmnt_entry(mount, self.probe_timeout_seconds)
        if target != mount:
            raise ConfigurationError(f"smb.mount_point não é ponto de montagem: {mount}")
        if fstype.lower() not in _SMB_FS_TYPES:
            raise ConfigurationError(
                f"ponto de montagem não é CIFS/SMB (tipo {fstype}): {mount}"
            )
        if source.lower() != self.expected_source.lower():
            raise ConfigurationError(
                f"ponto de montagem expõe {source}, esperado {self.expected_source}"
            )
        if self.marker is not None:
            marker_path = mount / self.marker
            _probe_smb_path(marker_path, self.probe_timeout_seconds)


@dataclass(frozen=True)
class Source:
    """Origem de backup; opcionalmente exige um volume SMB montado."""

    path: Path
    smb: SMBMount | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.path, Path):
            object.__setattr__(self, "path", Path(self.path))

    def validate_for_execution(self) -> None:
        if self.smb is None:
            return
        self.smb.validate_for_execution()
        mount = self.smb.mount_point_path
        resolved = self.path.expanduser().resolve(strict=False)
        if resolved != mount and not resolved.is_relative_to(mount):
            raise ConfigurationError(
                f"origem SMB fora do ponto de montagem {mount}: {resolved}"
            )


@dataclass(frozen=True)
class S3Connection:
    """Dados não secretos para o backend S3 nativo do restic.

    Chaves de acesso chegam somente por variáveis de ambiente indicadas
    por `access_key_environment`/`secret_key_environment`, no mesmo
    padrão de `password_environment`: a configuração nunca guarda segredo.
    """

    endpoint: str
    bucket: str
    access_key_environment: str
    secret_key_environment: str
    prefix: str = ""
    region: str | None = None
    bucket_lookup: str | None = None
    storage_class: str | None = None

    def __post_init__(self) -> None:
        _require_s3_endpoint("s3.endpoint", self.endpoint)
        _require_s3_bucket("s3.bucket", self.bucket)
        _require_text("s3.access_key_environment", self.access_key_environment)
        _require_text("s3.secret_key_environment", self.secret_key_environment)
        if self.prefix:
            _require_remote_path("s3.prefix", self.prefix)
            if self.prefix.startswith("/"):
                raise ConfigurationError("s3.prefix deve ser relativo ao bucket")
        if self.region is not None:
            _require_text("s3.region", self.region)
            if any(character.isspace() for character in self.region):
                raise ConfigurationError("s3.region não pode conter espaços")
        if self.bucket_lookup is not None and self.bucket_lookup not in {
            "auto",
            "path",
            "dns",
        }:
            raise ConfigurationError("s3.bucket_lookup deve ser auto, path ou dns")
        if self.storage_class is not None:
            _require_text("s3.storage_class", self.storage_class)

    @property
    def repository_reference(self) -> str:
        base = f"s3:{self.endpoint}/{self.bucket}"
        return f"{base}/{self.prefix}" if self.prefix else base

    def validate_credentials_for_execution(self) -> None:
        for name in (
            self.access_key_environment,
            self.secret_key_environment,
        ):
            if not os.environ.get(name):
                raise ConfigurationError(
                    f"credencial ausente na variável de ambiente {name}"
                )


@dataclass(frozen=True)
class Destination:
    id: str
    name: str
    kind: DestinationKind
    repository: str | None
    password_environment: str
    retention: RetentionPolicy = field(default_factory=RetentionPolicy)
    sftp: SFTPConnection | None = None
    smb: SMBMount | None = None
    s3: S3Connection | None = None

    def __post_init__(self) -> None:
        _require_identifier("destination.id", self.id)
        _require_text("destination.name", self.name)
        _require_text("destination.password_environment", self.password_environment)
        if self.kind is DestinationKind.LOCAL:
            _require_text("destination.repository", self.repository)
            if self.sftp is not None or self.smb is not None or self.s3 is not None:
                raise ConfigurationError(
                    "destination.sftp/smb/s3 não é permitido em destino local"
                )
        elif self.kind is DestinationKind.SFTP:
            if self.repository is not None:
                raise ConfigurationError("destination.repository não é permitido em destino SFTP")
            if self.sftp is None:
                raise ConfigurationError("destination.sftp é obrigatório em destino SFTP")
            if self.smb is not None or self.s3 is not None:
                raise ConfigurationError("destination.smb/s3 não é permitido em destino SFTP")
        elif self.kind is DestinationKind.SMB:
            if self.sftp is not None or self.s3 is not None:
                raise ConfigurationError("destination.sftp/s3 não é permitido em destino SMB")
            if self.smb is None:
                raise ConfigurationError("destination.smb é obrigatório em destino SMB")
            _require_relative_subpath("destination.repository", self.repository)
        elif self.kind is DestinationKind.S3:
            if self.repository is not None:
                raise ConfigurationError("destination.repository não é permitido em destino S3")
            if self.s3 is None:
                raise ConfigurationError("destination.s3 é obrigatório em destino S3")
            if self.sftp is not None or self.smb is not None:
                raise ConfigurationError("destination.sftp/smb não é permitido em destino S3")
        else:
            raise ConfigurationError(f"tipo de destino não homologado nesta POC: {self.kind}")

    @property
    def repository_path(self) -> Path:
        if self.kind is DestinationKind.LOCAL and self.repository is not None:
            return Path(self.repository).expanduser().resolve(strict=False)
        if self.kind is DestinationKind.SMB and self.smb is not None:
            return (self.smb.mount_point_path / (self.repository or "")).resolve(strict=False)
        raise ConfigurationError(
            "destination.repository_path só existe para destino local ou SMB"
        )

    def validate_for_execution(self) -> None:
        """Valida pré-requisitos que pertencem somente a este destino."""
        if self.kind is DestinationKind.SFTP and self.sftp is not None:
            self.sftp.validate_files_for_execution()
        if self.kind is DestinationKind.SMB and self.smb is not None:
            self.smb.validate_for_execution()
        if self.kind is DestinationKind.S3 and self.s3 is not None:
            self.s3.validate_credentials_for_execution()


@dataclass(frozen=True)
class TaskSpec:
    id: str
    name: str
    client: Client
    device_id: str
    sources: tuple[Source, ...]
    exclusions: tuple[str, ...]
    compression: CompressionMode
    destinations: tuple[Destination, ...]
    timeouts: OperationTimeouts = field(default_factory=OperationTimeouts)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "sources",
            tuple(
                source if isinstance(source, Source) else Source(path=Path(source))
                for source in self.sources
            ),
        )
        _require_identifier("task.id", self.id)
        _require_text("task.name", self.name)
        _require_identifier("task.device_id", self.device_id)
        if not self.sources:
            raise ConfigurationError("task.sources deve conter ao menos uma origem")
        if not self.destinations:
            raise ConfigurationError("task.destinations deve conter ao menos um destino")
        if any(not isinstance(pattern, str) or not pattern.strip() for pattern in self.exclusions):
            raise ConfigurationError("task.exclusions deve conter apenas padrões não vazios")

        destination_ids = [destination.id for destination in self.destinations]
        if len(destination_ids) != len(set(destination_ids)):
            raise ConfigurationError("destination.id deve ser único por tarefa")

    @property
    def source_paths(self) -> tuple[Path, ...]:
        return tuple(source.path for source in self.sources)

    def validate_for_execution(self) -> None:
        resolved_sources = tuple(
            source.path.expanduser().resolve(strict=False) for source in self.sources
        )
        for source in resolved_sources:
            if not source.is_dir():
                raise ConfigurationError(f"origem inexistente ou não é diretório: {source}")
        for source in self.sources:
            source.validate_for_execution()

        self.validate_recovery_for_execution()

    def validate_recovery_for_execution(self) -> None:
        """Valida invariantes de repositório sem exigir as origens originais."""
        resolved_sources = tuple(
            source.path.expanduser().resolve(strict=False) for source in self.sources
        )

        for destination in self.destinations:
            if destination.kind in {DestinationKind.LOCAL, DestinationKind.SMB}:
                repository = destination.repository_path
                for source in resolved_sources:
                    if repository == source or repository.is_relative_to(source):
                        raise ConfigurationError(
                            "repositório não pode ficar dentro de uma origem: "
                            f"{repository}"
                        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TaskSpec":
        _require_object("configuração", data)
        client_data = _required_object(data, "client")
        sources_data = _required_list(data, "sources")
        exclusions_data = _optional_list(data, "exclusions")
        destinations_data = _required_list(data, "destinations")
        timeouts_data = _optional_object(data, "timeouts")

        client = Client(
            id=_required_text(client_data, "id", "client.id"),
            name=_required_text(client_data, "name", "client.name"),
        )
        sources = tuple(
            _source_from_dict(sources_data[index], index)
            for index in range(len(sources_data))
        )
        exclusions = tuple(
            _list_text_item(exclusions_data, index, "exclusions")
            for index in range(len(exclusions_data))
        )
        destinations = tuple(
            _destination_from_dict(item, index)
            for index, item in enumerate(destinations_data)
        )
        compression = _compression_from_value(data.get("compression", "auto"))
        return cls(
            id=_required_text(data, "id", "id"),
            name=_required_text(data, "name", "name"),
            client=client,
            device_id=_required_text(data, "device_id", "device_id"),
            sources=sources,
            exclusions=exclusions,
            compression=compression,
            destinations=destinations,
            timeouts=OperationTimeouts.from_dict(timeouts_data),
        )


def _require_identifier(field_name: str, value: str) -> None:
    _require_text(field_name, value)
    if any(character.isspace() for character in value):
        raise ConfigurationError(f"{field_name} não pode conter espaços")


def _require_text(field_name: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"{field_name} é obrigatório")


def _require_object(field_name: str, value: Any) -> None:
    if not isinstance(value, dict):
        raise ConfigurationError(f"{field_name} deve ser um objeto")


def _required_object(data: dict[str, Any], key: str) -> dict[str, Any]:
    if key not in data:
        raise ConfigurationError(f"{key} é obrigatório")
    value = data[key]
    _require_object(key, value)
    return value


def _optional_object(data: dict[str, Any], key: str) -> dict[str, Any]:
    if key not in data:
        return {}
    value = data[key]
    _require_object(key, value)
    return value


def _required_list(data: dict[str, Any], key: str) -> list[Any]:
    if key not in data:
        raise ConfigurationError(f"{key} é obrigatório")
    value = data[key]
    if not isinstance(value, list):
        raise ConfigurationError(f"{key} deve ser uma lista")
    return value


def _optional_list(data: dict[str, Any], key: str) -> list[Any]:
    if key not in data:
        return []
    value = data[key]
    if not isinstance(value, list):
        raise ConfigurationError(f"{key} deve ser uma lista")
    return value


def _required_text(data: dict[str, Any], key: str, field_name: str) -> str:
    if key not in data:
        raise ConfigurationError(f"{field_name} é obrigatório")
    value = data[key]
    _require_text(field_name, value)
    return value


def _list_text_item(values: list[Any], index: int, field_name: str) -> str:
    value = values[index]
    if not isinstance(value, str):
        raise ConfigurationError(f"{field_name}[{index}] deve ser texto")
    _require_text(f"{field_name}[{index}]", value)
    return value


def _destination_from_dict(value: Any, index: int) -> Destination:
    field_name = f"destinations[{index}]"
    _require_object(field_name, value)
    retention_data: dict[str, Any] = {}
    if "retention" in value:
        retention_data = value["retention"]
        _require_object(f"{field_name}.retention", retention_data)
    keep_last = retention_data.get("keep_last")
    if "keep_last" in retention_data:
        _require_positive_integer(f"{field_name}.retention.keep_last", keep_last, allow_none=True)
    kind = _destination_kind(value, field_name)
    if kind is DestinationKind.SFTP:
        sftp = _sftp_from_dict(value, field_name)
        smb = None
        s3 = None
    elif kind is DestinationKind.SMB:
        smb = _smb_from_dict(value, field_name)
        sftp = None
        s3 = None
    elif kind is DestinationKind.S3:
        s3 = _s3_from_dict(value, field_name)
        sftp = None
        smb = None
    else:
        sftp = None
        smb = None
        s3 = None
    if kind is not DestinationKind.SMB and "smb" in value:
        raise ConfigurationError(f"{field_name}.smb exige kind smb")
    if kind is not DestinationKind.SFTP and "sftp" in value:
        raise ConfigurationError(f"{field_name}.sftp exige kind sftp")
    if kind is not DestinationKind.S3 and "s3" in value:
        raise ConfigurationError(f"{field_name}.s3 exige kind s3")
    if kind in {DestinationKind.LOCAL, DestinationKind.SMB}:
        repository: str | None = _required_text(
            value, "repository", f"{field_name}.repository"
        )
    else:
        if "repository" in value and value["repository"] is not None:
            raise ConfigurationError(
                f"{field_name}.repository não é permitido em destino {kind}"
            )
        repository = None
    return Destination(
        id=_required_text(value, "id", f"{field_name}.id"),
        name=_required_text(value, "name", f"{field_name}.name"),
        kind=kind,
        repository=repository,
        password_environment=_required_text(
            value, "password_environment", f"{field_name}.password_environment"
        ),
        retention=RetentionPolicy(keep_last=keep_last),
        sftp=sftp,
        smb=smb,
        s3=s3,
    )


def _source_from_dict(value: Any, index: int) -> Source:
    field_name = f"sources[{index}]"
    if isinstance(value, str):
        _require_text(field_name, value)
        return Source(path=Path(value))
    if not isinstance(value, dict):
        raise ConfigurationError(f"{field_name} deve ser texto ou objeto")
    _reject_unknown_fields(value, {"path", "smb"}, field_name)
    path = _required_text(value, "path", f"{field_name}.path")
    smb = None
    if "smb" in value:
        smb = _smb_from_dict(value, field_name)
    return Source(path=Path(path), smb=smb)


def _smb_from_dict(data: dict[str, Any], parent_field: str) -> SMBMount:
    field_name = f"{parent_field}.smb"
    if "smb" not in data:
        raise ConfigurationError(f"{field_name} é obrigatório")
    smb = data["smb"]
    _require_object(field_name, smb)
    _reject_unknown_fields(
        smb,
        {"server", "share", "mount_point", "marker", "probe_timeout_seconds"},
        field_name,
    )
    marker = smb.get("marker")
    if "marker" in smb and marker is not None:
        _require_text(f"{field_name}.marker", marker)
    return SMBMount(
        server=_required_text(smb, "server", f"{field_name}.server"),
        share=_required_text(smb, "share", f"{field_name}.share"),
        mount_point=_required_text(smb, "mount_point", f"{field_name}.mount_point"),
        marker=marker,
        probe_timeout_seconds=_optional_positive_integer(
            smb, "probe_timeout_seconds", _SMB_PROBE_TIMEOUT_SECONDS,
            f"{field_name}.probe_timeout_seconds",
        ),
    )


def _s3_from_dict(data: dict[str, Any], parent_field: str) -> S3Connection:
    field_name = f"{parent_field}.s3"
    if "s3" not in data:
        raise ConfigurationError(f"{field_name} é obrigatório")
    s3 = data["s3"]
    _require_object(field_name, s3)
    _reject_unknown_fields(
        s3,
        {
            "endpoint",
            "bucket",
            "prefix",
            "region",
            "bucket_lookup",
            "storage_class",
            "access_key_environment",
            "secret_key_environment",
        },
        field_name,
    )
    prefix = s3.get("prefix", "")
    if "prefix" in s3:
        _require_text(f"{field_name}.prefix", prefix)
    region = s3.get("region")
    if "region" in s3 and region is not None:
        _require_text(f"{field_name}.region", region)
    bucket_lookup = s3.get("bucket_lookup")
    if "bucket_lookup" in s3 and bucket_lookup is not None:
        _require_text(f"{field_name}.bucket_lookup", bucket_lookup)
    storage_class = s3.get("storage_class")
    if "storage_class" in s3 and storage_class is not None:
        _require_text(f"{field_name}.storage_class", storage_class)
    return S3Connection(
        endpoint=_required_text(s3, "endpoint", f"{field_name}.endpoint"),
        bucket=_required_text(s3, "bucket", f"{field_name}.bucket"),
        access_key_environment=_required_text(
            s3, "access_key_environment", f"{field_name}.access_key_environment"
        ),
        secret_key_environment=_required_text(
            s3, "secret_key_environment", f"{field_name}.secret_key_environment"
        ),
        prefix=prefix,
        region=region,
        bucket_lookup=bucket_lookup,
        storage_class=storage_class,
    )


def _require_s3_endpoint(field_name: str, value: str) -> None:
    _require_text(field_name, value)
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ConfigurationError(f"{field_name} deve ser URL http(s) válida")
    if parsed.username or parsed.password:
        raise ConfigurationError(f"{field_name} não pode conter credenciais")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ConfigurationError(
            f"{field_name} não pode conter caminho, query ou fragmento"
        )
    try:
        parsed.port
    except ValueError as error:
        raise ConfigurationError(f"{field_name} contém porta inválida") from error


def _require_s3_bucket(field_name: str, value: str) -> None:
    _require_text(field_name, value)
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{1,61}[a-z0-9]", value):
        raise ConfigurationError(f"{field_name} não é nome de bucket válido")


def _findmnt_entry(mount_point: Path, timeout_seconds: int) -> tuple[Path, str, str]:
    findmnt = shutil.which("findmnt")
    if findmnt is None:
        raise ConfigurationError("findmnt não encontrado para validar montagem SMB")
    try:
        completed = subprocess.run(
            [
                findmnt,
                "-P",
                "-n",
                "--first-only",
                "-o",
                "TARGET,SOURCE,FSTYPE",
                "-T",
                str(mount_point),
            ],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise ConfigurationError(
            f"findmnt não respondeu em {timeout_seconds}s: {mount_point}"
        ) from error
    fields = dict(re.findall(r'(\w+)="([^"]*)"', completed.stdout))
    if completed.returncode != 0 or not fields.get("TARGET"):
        raise ConfigurationError(f"findmnt não localizou o ponto de montagem: {mount_point}")
    return (
        Path(fields["TARGET"]).resolve(strict=False),
        fields.get("SOURCE", ""),
        fields.get("FSTYPE", ""),
    )


def _probe_smb_path(path: Path, timeout_seconds: int) -> None:
    stat_binary = shutil.which("stat")
    if stat_binary is None:
        raise ConfigurationError("stat não encontrado para sondar volume SMB")
    try:
        completed = subprocess.run(
            [stat_binary, "-c", "%F", "--", str(path)],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise ConfigurationError(
            f"volume SMB não respondeu em {timeout_seconds}s: {path}"
        ) from error
    if completed.returncode != 0:
        raise ConfigurationError(f"marcador de volume SMB ausente: {path}")


def _destination_kind(data: dict[str, Any], field_name: str) -> DestinationKind:
    value = _required_text(data, "kind", f"{field_name}.kind")
    try:
        return DestinationKind(value)
    except ValueError as error:
        raise ConfigurationError(f"{field_name}.kind não é homologado: {value}") from error


def _sftp_from_dict(data: dict[str, Any], destination_field: str) -> SFTPConnection:
    field_name = f"{destination_field}.sftp"
    if "sftp" not in data:
        raise ConfigurationError(f"{field_name} é obrigatório")
    sftp = data["sftp"]
    _require_object(field_name, sftp)
    _reject_unknown_fields(
        sftp,
        {
            "host",
            "port",
            "user",
            "remote_path",
            "identity_file",
            "known_hosts_file",
            "connect_timeout_seconds",
            "server_alive_interval_seconds",
            "server_alive_count_max",
        },
        field_name,
    )
    return SFTPConnection(
        host=_required_text(sftp, "host", f"{field_name}.host"),
        port=_required_port_value(sftp, "port", f"{field_name}.port"),
        user=_required_text(sftp, "user", f"{field_name}.user"),
        remote_path=_required_text(sftp, "remote_path", f"{field_name}.remote_path"),
        identity_file=_required_text(sftp, "identity_file", f"{field_name}.identity_file"),
        known_hosts_file=_required_text(
            sftp, "known_hosts_file", f"{field_name}.known_hosts_file"
        ),
        connect_timeout_seconds=_optional_positive_integer(
            sftp, "connect_timeout_seconds", 15, f"{field_name}.connect_timeout_seconds"
        ),
        server_alive_interval_seconds=_optional_positive_integer(
            sftp,
            "server_alive_interval_seconds",
            60,
            f"{field_name}.server_alive_interval_seconds",
        ),
        server_alive_count_max=_optional_positive_integer(
            sftp,
            "server_alive_count_max",
            3,
            f"{field_name}.server_alive_count_max",
        ),
    )


def _compression_from_value(value: Any) -> CompressionMode:
    if not isinstance(value, str):
        raise ConfigurationError("compression deve ser texto")
    try:
        return CompressionMode(value)
    except ValueError as error:
        raise ConfigurationError("compression deve ser auto, off ou max") from error


def _optional_positive_integer(
    data: dict[str, Any], key: str, default: int, field_name: str
) -> int:
    if key not in data:
        return default
    value = data[key]
    _require_positive_integer(field_name, value)
    return value


def _require_positive_integer(
    field_name: str, value: Any, *, allow_none: bool = False
) -> None:
    if value is None and allow_none:
        return
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigurationError(f"{field_name} deve ser inteiro positivo")


def _required_port_value(data: dict[str, Any], key: str, field_name: str) -> int:
    if key not in data:
        raise ConfigurationError(f"{field_name} é obrigatório")
    value = data[key]
    _require_port(field_name, value)
    return value


def _require_port(field_name: str, value: Any) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65_535:
        raise ConfigurationError(f"{field_name} deve ser inteiro entre 1 e 65535")


def _require_remote_path(field_name: str, value: str) -> None:
    _require_text(field_name, value)
    if any(character.isspace() or ord(character) < 32 for character in value):
        raise ConfigurationError(f"{field_name} não pode conter espaços ou caracteres de controle")
    if value.startswith("~") or "//" in value or any(part == ".." for part in value.split("/")):
        raise ConfigurationError(f"{field_name} não é um caminho remoto permitido")


def _require_file_reference(field_name: str, value: str) -> None:
    _require_text(field_name, value)
    if "\x00" in value or any(ord(character) < 32 for character in value):
        raise ConfigurationError(f"{field_name} contém caractere inválido")


def _require_smb_server(field_name: str, value: str) -> None:
    _require_text(field_name, value)
    if any(character.isspace() or ord(character) < 32 for character in value) or any(
        character in value for character in "/\\@?#;:"
    ):
        raise ConfigurationError(f"{field_name} contém caracteres não permitidos")


def _require_smb_share(field_name: str, value: str) -> None:
    _require_text(field_name, value)
    if (
        any(ord(character) < 32 for character in value)
        or "/" in value
        or "\\" in value
    ):
        raise ConfigurationError(f"{field_name} contém caracteres não permitidos")


def _require_relative_subpath(field_name: str, value: Any) -> None:
    _require_text(field_name, value)
    if not isinstance(value, str) or value.startswith(("/", "~")):
        raise ConfigurationError(f"{field_name} deve ser caminho relativo seguro")
    if any(part in {"", ".", ".."} for part in value.split("/")) or "\\" in value:
        raise ConfigurationError(f"{field_name} contém segmento de caminho inválido")


def _require_sftp_user(field_name: str, value: str) -> None:
    _require_text(field_name, value)
    if not re.fullmatch(r"[A-Za-z0-9._-]+", value):
        raise ConfigurationError(f"{field_name} contém caracteres não permitidos")


def _require_sftp_host(field_name: str, value: str) -> None:
    _require_text(field_name, value)
    if any(character.isspace() or ord(character) < 32 for character in value) or any(
        character in value for character in "/@?#\\;"
    ):
        raise ConfigurationError(f"{field_name} contém caracteres não permitidos")


def _reject_unknown_fields(data: dict[str, Any], allowed: set[str], field_name: str) -> None:
    for key in data:
        if key not in allowed:
            raise ConfigurationError(f"{field_name}.{key} não é permitido")
