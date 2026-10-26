"""Migrações sob PostgreSQL real: rollback com dados, retentativa,
concorrência e liberação do advisory lock por conexão independente."""

from __future__ import annotations

import threading
import uuid

import pytest
from sqlalchemy import create_engine, text

from conftest import TEST_URL, execution_payload, requires_db

pytestmark = requires_db

_ADVISORY_KEY = 727_203_001


@pytest.fixture(autouse=True)
def _cleanup_extra_versions(client):
    """Versões/tabelas aplicadas por migrações de teste não podem
    contaminar o próximo teste nem o start do app (versão futura)."""
    yield
    with _engine().begin() as connection:
        connection.execute(text("DELETE FROM schema_migrations WHERE version > 1"))
        for table in ("mig_test", "mig_probe", "mig_race"):
            connection.execute(text(f"DROP TABLE IF EXISTS {table}"))


def _engine():
    from macip_backup_api import db as db_module

    return db_module.engine()


def _advisory_locks_held() -> int:
    """Conta advisory locks da aplicação por conexão INDEPENDENTE —
    a própria sessão readquiriria o seu lock, prova insuficiente."""
    engine = create_engine(TEST_URL)
    try:
        with engine.connect() as connection:
            return connection.execute(
                text(
                    "SELECT count(*) FROM pg_locks "
                    "WHERE locktype='advisory' AND objid = :k"
                ),
                {"k": _ADVISORY_KEY},
            ).scalar()
    finally:
        engine.dispose()


def _history_snapshot() -> dict:
    engine = _engine()
    with engine.connect() as connection:
        def rows(table):
            return sorted(
                tuple(r)
                for r in connection.execute(
                    text(f"SELECT * FROM {table} ORDER BY 1")
                ).all()
            )

        return {
            "clients": rows("clients"),
            "agents": rows("agents"),
            "executions": rows("executions"),
            "destination_results": rows("destination_results"),
            "attempt_results": rows("attempt_results"),
        }


def _seed_history(client, agent) -> dict:
    """Cliente+agente+execução completa (destino+tentativa) via API."""
    response = client.post(
        "/api/v1/agent/executions",
        json=execution_payload(),
        headers=agent["headers"],
    )
    assert response.status_code == 200
    return _history_snapshot()


def test_failed_migration_rolls_back_with_full_history(client, agent, monkeypatch):
    """Falha após DDL: esquema revertido, versão não registrada e o
    histórico populado permanece byte a byte igual."""
    from macip_backup_api import migrations
    from macip_backup_api.db import migrate_now

    before = _seed_history(client, agent)

    monkeypatch.setattr(
        migrations,
        "MIGRATIONS",
        migrations.MIGRATIONS[:1]
        + [(2, "fail-mid",
            "CREATE TABLE mig_test (id int);"
            "SELECT coluna_inexistente FROM agents;")],
    )
    with pytest.raises(Exception):
        migrate_now()

    engine = _engine()
    with engine.connect() as connection:
        created = connection.execute(
            text(
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_name='mig_test'"
            )
        ).scalar()
        versions = {
            row[0]
            for row in connection.execute(
                text("SELECT version FROM schema_migrations")
            )
        }
    assert created == 0, "DDL da migração falha deveria reverter"
    assert 2 not in versions, "versão não registrada em falha"
    assert _history_snapshot() == before, "histórico alterado pelo rollback"
    assert _advisory_locks_held() == 0, "advisory lock preso após falha"

    # retentativa com a MESMA versão pendente, agora válida
    monkeypatch.undo()
    monkeypatch.setattr(
        migrations,
        "MIGRATIONS",
        migrations.MIGRATIONS[:1]
        + [(2, "probe", "CREATE TABLE mig_probe (id int);")],
    )
    assert migrate_now() == [2]
    with engine.connect() as connection:
        present = connection.execute(
            text(
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_name='mig_probe'"
            )
        ).scalar()
        v2 = connection.execute(
            text("SELECT count(*) FROM schema_migrations WHERE version=2")
        ).scalar()
    assert present == 1, "retentativa não aplicou a migração"
    assert v2 == 1, "versão registrada mais de uma vez"
    assert _history_snapshot() == before
    assert migrate_now() == []  # nada pendente


def test_concurrent_pending_migration_applies_once(client, monkeypatch):
    """Duas migrações realmente pendentes em conexões independentes —
    aplicada uma única vez, sem duplicar efeito nem versão."""
    from macip_backup_api import migrations
    from macip_backup_api.db import migrate_now

    monkeypatch.setattr(
        migrations,
        "MIGRATIONS",
        migrations.MIGRATIONS[:1]
        + [(2, "concurrent", "CREATE TABLE mig_race (id int);")],
    )
    results: list = []

    def run():
        try:
            results.append(migrate_now())
        except Exception as error:
            results.append(error)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not any(t.is_alive() for t in threads), "migrador travado — lock?"

    applied = [r for r in results if isinstance(r, list)]
    errors = [r for r in results if not isinstance(r, list)]
    assert not errors, errors
    assert sorted(applied[0] + applied[1]) == [2], results  # um aplicou, outro viu

    engine = _engine()
    with engine.connect() as connection:
        count = connection.execute(
            text("SELECT count(*) FROM schema_migrations WHERE version=2")
        ).scalar()
        present = connection.execute(
            text(
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_name='mig_race'"
            )
        ).scalar()
    assert count == 1
    assert present == 1
    assert _advisory_locks_held() == 0, "advisory lock preso após sucesso"


def test_future_version_rejected_without_changes(client):
    """schema_migrations com versão futura → rejeita e preserva tudo."""
    from macip_backup_api import migrations
    from macip_backup_api.db import migrate_now

    engine = _engine()
    before = _history_snapshot()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO schema_migrations (version, name) "
                "VALUES (9000, 'futuro')"
            )
        )
    with pytest.raises(migrations.MigrationError, match="não suportada"):
        migrate_now()

    with engine.begin() as connection:
        connection.execute(
            text("DELETE FROM schema_migrations WHERE version = 9000")
        )
    assert _history_snapshot() == before
    assert migrate_now() == []
