"""PostgreSQL REAL exclusivo para testes — sem SQLite.

MACIP_API_TEST_DATABASE_URL aponta um banco descartável (podman).
Sem ele → módulo inteiro pulado (integração registrada como pendente).
"""

from __future__ import annotations

import os
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

TEST_URL = os.environ.get(
    "MACIP_API_TEST_DATABASE_URL",
    "postgresql+psycopg://postgres:pgtest@localhost:55432/macip_api_test",
)
ADMIN_TOKEN = "test-admin-9f8e7d6c5b4a3210"


def _reachable() -> bool:
    try:
        engine = create_engine(TEST_URL, connect_args={"connect_timeout": 3})
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        engine.dispose()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _reachable(),
    reason="PostgreSQL de teste inacessível — integração pendente",
)


@pytest.fixture
def client():
    os.environ["MACIP_API_DATABASE_URL"] = TEST_URL
    os.environ["MACIP_API_ADMIN_TOKEN"] = ADMIN_TOKEN
    from macip_backup_api import app as app_module
    from macip_backup_api import db as db_module

    db_module.set_engine(None)  # engine nova apontando para o banco de teste
    with TestClient(app_module.create_app()) as client_:
        # limpa o banco exclusivo entre testes
        engine = db_module.engine()
        with engine.begin() as connection:
            for table in (
                "attempt_results",
                "destination_results",
                "executions",
                "agent_credentials",
                "agents",
                "clients",
            ):
                connection.execute(text(f"TRUNCATE {table} CASCADE"))
        yield client_


@pytest.fixture
def admin_headers() -> dict:
    return {"Authorization": f"Bearer {ADMIN_TOKEN}"}


@pytest.fixture
def agent(client, admin_headers):
    """Cliente + agente + token de um só uso."""
    created = client.post(
        "/api/v1/clients", json={"name": f"cliente-{uuid.uuid4().hex[:8]}"},
        headers=admin_headers,
    )
    assert created.status_code == 201, created.text
    client_id = created.json()["id"]
    created = client.post(
        f"/api/v1/clients/{client_id}/agents",
        json={"name": "agente-1"},
        headers=admin_headers,
    )
    agent_id = created.json()["id"]
    credential = client.post(
        f"/api/v1/agents/{agent_id}/credentials",
        json={"label": "teste"},
        headers=admin_headers,
    )
    return {
        "client_id": client_id,
        "agent_id": agent_id,
        "credential_id": credential.json()["credential_id"],
        "token": credential.json()["token"],
        "headers": {"Authorization": f"Bearer {credential.json()['token']}"},
    }


def execution_payload(**overrides) -> dict:
    base = {
        "execution_id": str(uuid.uuid4()),
        "task_id": "task-piloto",
        "status": "success",
        "started_at_utc": "2026-10-26T10:00:00Z",
        "finished_at_utc": "2026-10-26T10:05:00Z",
        "destinations": [
            {
                "destination_id": "local",
                "status": "success",
                "started_at_utc": "2026-10-26T10:00:01Z",
                "finished_at_utc": "2026-10-26T10:04:59Z",
                "duration_seconds": 298.0,
                "snapshot_id": "abc123",
                "files_processed": 10,
                "bytes_processed": 1024,
                "attempts": [
                    {
                        "attempt": 1,
                        "status": "success",
                        "started_at_utc": "2026-10-26T10:00:01Z",
                        "finished_at_utc": "2026-10-26T10:04:59Z",
                        "duration_seconds": 298.0,
                        "retryable": False,
                        "snapshot_id": "abc123",
                    }
                ],
            }
        ],
    }
    base.update(overrides)
    return base
