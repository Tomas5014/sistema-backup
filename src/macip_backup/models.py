"""Modelos de configuração sem credenciais persistidas."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any


class ConfigurationError(ValueError):
    """Configuração inválida para uma tarefa de backup."""


class CompressionMode(StrEnum):
    AUTO = "auto"
    OFF = "off"
    MAX = "max"


class DestinationKind(StrEnum):
    LOCAL = "local"


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
        if self.keep_last is not None and self.keep_last < 1:
            raise ConfigurationError("retention.keep_last deve ser maior que zero")


@dataclass(frozen=True)
class Destination:
    id: str
    name: str
    kind: DestinationKind
    repository: str
    password_environment: str
    retention: RetentionPolicy = field(default_factory=RetentionPolicy)

    def __post_init__(self) -> None:
        _require_identifier("destination.id", self.id)
        _require_text("destination.name", self.name)
        _require_text("destination.repository", self.repository)
        _require_text("destination.password_environment", self.password_environment)
        if self.kind is not DestinationKind.LOCAL:
            raise ConfigurationError(
                f"tipo de destino não homologado nesta POC: {self.kind}"
            )

    @property
    def repository_path(self) -> Path:
        return Path(self.repository).expanduser().resolve(strict=False)


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
            repository = destination.repository_path
            for source in resolved_sources:
                if repository == source or repository.is_relative_to(source):
                    raise ConfigurationError(
                        "repositório não pode ficar dentro de uma origem: "
                        f"{repository}"
                    )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TaskSpec":
        try:
            client_data = data["client"]
            client = Client(id=client_data["id"], name=client_data["name"])
            destinations = tuple(
                Destination(
                    id=item["id"],
                    name=item["name"],
                    kind=DestinationKind(item["kind"]),
                    repository=item["repository"],
                    password_environment=item["password_environment"],
                    retention=RetentionPolicy(
                        keep_last=item.get("retention", {}).get("keep_last")
                    ),
                )
                for item in data["destinations"]
            )
            return cls(
                id=data["id"],
                name=data["name"],
                client=client,
                device_id=data["device_id"],
                sources=tuple(Path(item) for item in data["sources"]),
                exclusions=tuple(data.get("exclusions", [])),
                compression=CompressionMode(data.get("compression", "auto")),
                destinations=destinations,
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ConfigurationError(f"configuração de tarefa inválida: {error}") from error


def _require_identifier(field_name: str, value: str) -> None:
    _require_text(field_name, value)
    if any(character.isspace() for character in value):
        raise ConfigurationError(f"{field_name} não pode conter espaços")


def _require_text(field_name: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"{field_name} é obrigatório")
