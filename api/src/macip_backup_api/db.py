"""Engine e sessão — PostgreSQL via SQLAlchemy/psycopg."""

from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session

from . import config
from .migrations import migrate

_engine: Engine | None = None


def engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = create_engine(config.database_url(), pool_size=5)
    return _engine


def set_engine(engine_: Engine | None) -> None:
    global _engine
    _engine = engine_


def session() -> Generator[Session]:
    with Session(engine()) as session_:
        yield session_


def migrate_now() -> list[int]:
    return migrate(engine())
