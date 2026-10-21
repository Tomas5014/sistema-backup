"""Modelos de configuração sem credenciais persistidas."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
import re
from typing import Any


class ConfigurationError(ValueError):
    """Configuração inválida para uma tarefa de backup."""


class CompressionMode(StrEnum):
    AUTO = "auto"
    OFF = "off"
    MAX = "max"


class DestinationKind(StrEnum):
    LOCAL = "local"
    SFTP = "sftp"


@dataclass(frozen=True)
class OperationTimeouts:
    """Limites em segundos para operações do restic."""

    initialize_seconds: int = 120
    backup_seconds: int = 14_400
    snapshots_seconds: int = 120
    check_seconds: int = 3_600
    restore_seconds: int = 14_400

    def __post_init__(self) -> None:
        for field_name, value in (
            ("timeouts.initialize_seconds", self.initialize_seconds),
            ("timeouts.backup_seconds", self.backup_seconds),
            ("timeouts.snapshots_seconds", self.snapshots_seconds),
            ("timeouts.check_seconds", self.check_seconds),
            ("timeouts.restore_seconds", self.restore_seconds),
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
class Destination:
    id: str
    name: str
    kind: DestinationKind
    repository: str | None
    password_environment: str
    retention: RetentionPolicy = field(default_factory=RetentionPolicy)
    sftp: SFTPConnection | None = None

    def __post_init__(self) -> None:
        _require_identifier("destination.id", self.id)
        _require_text("destination.name", self.name)
        _require_text("destination.password_environment", self.password_environment)
        if self.kind is DestinationKind.LOCAL:
            _require_text("destination.repository", self.repository)
            if self.sftp is not None:
                raise ConfigurationError("destination.sftp não é permitido em destino local")
        elif self.kind is DestinationKind.SFTP:
            if self.repository is not None:
                raise ConfigurationError("destination.repository não é permitido em destino SFTP")
            if self.sftp is None:
                raise ConfigurationError("destination.sftp é obrigatório em destino SFTP")
        else:
            raise ConfigurationError(f"tipo de destino não homologado nesta POC: {self.kind}")

    @property
    def repository_path(self) -> Path:
        if self.kind is not DestinationKind.LOCAL or self.repository is None:
            raise ConfigurationError("destination.repository_path só existe para destino local")
        return Path(self.repository).expanduser().resolve(strict=False)

    def validate_for_execution(self) -> None:
        """Valida pré-requisitos que pertencem somente a este destino."""
        if self.kind is DestinationKind.SFTP and self.sftp is not None:
            self.sftp.validate_files_for_execution()


@dataclass(frozen=True)
class TaskSpec:
    id: str
    name: str
    client: Client
    device_id: str
    sources: tuple[Path, ...]
    exclusions: tuple[str, ...]
    compression: CompressionMode
    destinations: tuple[Destination, ...]
    timeouts: OperationTimeouts = field(default_factory=OperationTimeouts)

    def __post_init__(self) -> None:
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

    def validate_for_execution(self) -> None:
        resolved_sources = tuple(
            source.expanduser().resolve(strict=False) for source in self.sources
        )
        for source in resolved_sources:
            if not source.is_dir():
                raise ConfigurationError(f"origem inexistente ou não é diretório: {source}")

        for destination in self.destinations:
            if destination.kind is DestinationKind.LOCAL:
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
            Path(_list_text_item(sources_data, index, "sources"))
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
    sftp = _sftp_from_dict(value, field_name) if kind is DestinationKind.SFTP else None
    if kind is DestinationKind.LOCAL:
        repository: str | None = _required_text(
            value, "repository", f"{field_name}.repository"
        )
    else:
        if "repository" in value and value["repository"] is not None:
            raise ConfigurationError(f"{field_name}.repository não é permitido em destino SFTP")
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
    )


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
