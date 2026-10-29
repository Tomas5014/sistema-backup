"""PostgreSQL REAL exclusivo para testes — sem SQLite.

MACIP_API_TEST_DATABASE_URL aponta um banco descartável.
- env ausente → testes de integração pulados explicitamente;
- env presente mas inacessível/inválida → FALHA clara (não skip);
- o nome do banco deve conter "test" — guarda contra apontar um
  banco operacional para a limpeza destrutiva entre testes.
"""

from __future__ import annotations

import os
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

TEST_URL = os.environ.get("MACIP_API_TEST_DATABASE_URL")
ADMIN_TOKEN = "test-admin-9f8e7d6c5b4a3210"

# sem URL → pula explicitamente; com URL morta → fixture falha claro
requires_db = pytest.mark.skipif(
    TEST_URL is None,
    reason="MACIP_API_TEST_DATABASE_URL não definido — integração PostgreSQL pendente",
)


def _connect():
    engine = create_engine(TEST_URL, connect_args={"connect_timeout": 3})
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    finally:
        engine.dispose()


@pytest.fixture
def client():
    if TEST_URL is None:
        pytest.skip("MACIP_API_TEST_DATABASE_URL não definido")
    db_name = TEST_URL.rsplit("/", 1)[-1].split("?")[0].lower()
    if "test" not in db_name:
        pytest.fail(
            f"banco '{db_name}' não parece descartável — recusando limpeza; "
            "aponte MACIP_API_TEST_DATABASE_URL para um banco de teste"
        )
    try:
        _connect()
    except Exception as error:
        pytest.fail(f"PostgreSQL de teste inacessível: {error}")

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
                "task_revisions",
                "tasks",
                "login_attempts",
                "operator_sessions",
                "operators",
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


def second_agent(client, admin_headers):
    """Agente de OUTRO cliente — para colisão de execution_id."""
    created = client.post(
        "/api/v1/clients", json={"name": f"outro-{uuid.uuid4().hex[:8]}"},
        headers=admin_headers,
    )
    client_id = created.json()["id"]
    created = client.post(
        f"/api/v1/clients/{client_id}/agents",
        json={"name": "agente-outro"},
        headers=admin_headers,
    )
    agent_id = created.json()["id"]
    credential = client.post(
        f"/api/v1/agents/{agent_id}/credentials",
        json={},
        headers=admin_headers,
    )
    return {
        "client_id": client_id,
        "agent_id": agent_id,
        "credential_id": credential.json()["credential_id"],
        "token": credential.json()["token"],
        "headers": {"Authorization": f"Bearer {credential.json()['token']}"},
    }


def csrf_headers(client) -> dict:
    """Par CSRF: GET emite o cookie; cabeçalho repete o valor."""
    assert client.get("/api/v1/auth/csrf").status_code == 204
    token = client.cookies.get("macip_csrf")
    assert token, "cookie macip_csrf ausente"
    return {"X-CSRF-Token": token}


def make_operator(client, admin_headers, login="operador", role="admin",
                  password="senha-super-secreta-1", name="Operador"):
    """Cria operador via API administrativa (Bearer)."""
    response = client.post(
        "/api/v1/operators",
        json={
            "login": login,
            "name": name,
            "role": role,
            "password": password,
        },
        headers=admin_headers,
    )
    assert response.status_code == 201, response.text
    return {**response.json(), "password": password}


def login(client, login: str, password: str):
    """Login real com par CSRF — mantém a sessão nos cookies do client."""
    headers = csrf_headers(client)
    return client.post(
        "/api/v1/auth/login",
        json={"login": login, "password": password},
        headers=headers,
    )


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
