"""Migrações sob PostgreSQL real: rollback de DDL, versões, corrida."""

from __future__ import annotations

import threading

import pytest
from sqlalchemy import text

from conftest import requires_db

pytestmark = requires_db


def _engine():
    from macip_backup_api import db as db_module

    return db_module.engine()


def test_failed_migration_rolls_back_ddl_and_version(client, monkeypatch):
    """Migração que falha DEPOIS de criar tabela → DDL revertido,
    versão não registrada, histórico intacto."""
    from macip_backup_api import migrations
    from macip_backup_api.db import migrate_now
    from sqlalchemy import text as sa_text

    engine = _engine()
    with engine.connect() as connection:
        before = connection.execute(sa_text("SELECT count(*) FROM agents")).scalar()

    # migração 2 falha DEPOIS do CREATE TABLE → txn deve reverter tudo
    good = migrations.MIGRATIONS[:1] + [
        (2, "fail-mid", "CREATE TABLE mig_test (id int); SELECT bad_column FROM agents;")
    ]
    monkeypatch.setattr(migrations, "MIGRATIONS", good)
    with pytest.raises(Exception):
        migrate_now()

    with engine.connect() as connection:
        exists = connection.execute(
            sa_text(
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_name='mig_test'"
            )
        ).scalar()
        versions = {
            row[0]
            for row in connection.execute(
                sa_text("SELECT version FROM schema_migrations")
            )
        }
        after = connection.execute(sa_text("SELECT count(*) FROM agents")).scalar()
    assert exists == 0, "DDL da migração falha deveria ter rolado back"
    assert 2 not in versions, "versão não pode ser registrada em falha"
    assert after == before, "histórico não pode ser afetado"

    # advisory lock liberado após falha → nova tentativa com a lista
    # real conclui normalmente
    monkeypatch.undo()
    assert migrate_now() == []


def test_concurrent_initialization_applies_once(client):
    """Duas inicializações concorrentes: advisory lock serializa,
    migração roda uma única vez e o lock é liberado."""
    from macip_backup_api.db import migrate_now

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
        thread.join()

    assert all(result == [] for result in results), results

    # lock realmente liberado — próxima migração não bloqueia
    assert migrate_now() == []


def test_future_version_rejected_without_changes(client):
    """schema_migrations com versão futura → rejeita e preserva tudo."""
    from macip_backup_api import migrations
    from macip_backup_api.db import migrate_now
    from sqlalchemy import text as sa_text

    engine = _engine()
    with engine.begin() as connection:
        connection.execute(
            sa_text("INSERT INTO schema_migrations (version, name) VALUES (9000, 'futuro')")
        )
    with pytest.raises(migrations.MigrationError, match="não suportada"):
        migrate_now()

    with engine.begin() as connection:
        connection.execute(
            sa_text("DELETE FROM schema_migrations WHERE version = 9000")
        )
    assert migrate_now() == []
