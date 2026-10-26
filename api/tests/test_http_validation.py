"""Respostas de validação via HTTP bruto — independente de PostgreSQL.

Cobre o defeito real: corpos com valores não finitos (`1e309`, `NaN`,
`Infinity`, `-Infinity`) faziam a resposta 422 explodir na
serialização → 500. Deve retornar JSON válido com 422.

`json=` do cliente não serve — json.dumps pode rejeitar esses valores
antes do envio; por isso todo corpo vai como bytes.
"""

from __future__ import annotations

import json
import os
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def http_client(monkeypatch):
    os.environ["MACIP_API_ADMIN_TOKEN"] = "test-admin"
    os.environ["MACIP_API_DATABASE_URL"] = "postgresql://u:x@localhost:1/db"
    os.environ.pop("MACIP_API_MAX_BODY_BYTES", None)  # limite do suite vizinho
    from macip_backup_api import db as db_module
    from macip_backup_api.app import create_app
    from macip_backup_api.auth import require_agent, require_admin
    from macip_backup_api.db import session as session_dep

    db_module.set_engine(None)
    app = create_app()
    fake_agent = SimpleNamespace(id=uuid.uuid4(), client_id=uuid.uuid4())
    app.dependency_overrides[require_agent] = lambda: fake_agent
    app.dependency_overrides[require_admin] = lambda: None
    # sessão simulada — a validação rejeita ANTES de qualquer gravação;
    # se algo escapar, o mock devolve um id como uma linha criada
    mock_db = MagicMock()
    mock_db.execute.return_value.scalar_one_or_none.return_value = uuid.uuid4()
    mock_db.flush = lambda: None

    def fake_session():
        yield mock_db

    app.dependency_overrides[session_dep] = fake_session
    # sem context manager → lifespan não roda migrações (sem banco)
    return TestClient(app, raise_server_exceptions=False)


HEADERS = {"Authorization": "Bearer qualquer", "Content-Type": "application/json"}
BASE = (
    '"execution_id":"e1","task_id":"t","status":"success",'
    '"started_at_utc":"2026-10-26T10:00:00Z",'
    '"finished_at_utc":"2026-10-26T10:05:00Z"'
)
DEST_TEMPLATE = (
    '{{"destination_id":"d","status":"success",'
    '"started_at_utc":"2026-10-26T10:00:00Z",'
    '"finished_at_utc":"2026-10-26T10:05:00Z"{field}}}'
)


def _body(field: str) -> bytes:
    extra = f",{field}" if field else ""
    return (
        "{" + BASE + f',"destinations":[{DEST_TEMPLATE.format(field=extra)}]'
        + "}"
    ).encode()


def test_non_finite_duration_values(http_client):
    for literal in ("1e309", "NaN", "Infinity", "-Infinity"):
        response = http_client.post(
            "/api/v1/agent/executions",
            content=_body(f'"duration_seconds":{literal}'),
            headers=HEADERS,
        )
        assert response.status_code == 422, f"{literal} → {response.status_code}"
        detail = response.json()  # JSON válido — não pode explodir
        assert "detail" in detail
        # nenhum token nem Authorization no detalhe
        assert HEADERS["Authorization"] not in response.text
        assert "Bearer" not in response.text


def test_non_finite_in_integer_field(http_client):
    response = http_client.post(
        "/api/v1/agent/executions",
        content=_body('"files_processed":Infinity'),
        headers=HEADERS,
    )
    assert response.status_code == 422
    assert "detail" in response.json()


def _dest_valid() -> str:
    """Destino válido completo — nenhum campo extra, JSON bem formado."""
    return DEST_TEMPLATE.format(field="")


def test_common_validation_errors_still_422(http_client):
    # timezone naive → erro semântico de aware, não json_invalid
    naive = (
        "{" + BASE.replace(
            '"started_at_utc":"2026-10-26T10:00:00Z"',
            '"started_at_utc":"2026-10-26T10:00:00"',
        ) + f',"destinations":[{_dest_valid()}]' + "}"
    ).encode()
    response = http_client.post(
        "/api/v1/agent/executions", content=naive, headers=HEADERS
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail[0]["type"] == "value_error", detail
    assert "timezone" in detail[0]["msg"], detail
    assert detail[0]["loc"][-1] == "started_at_utc"

    # status fora do Literal → literal_error no campo correto
    bad = (
        "{" + BASE.replace(
            '"status":"success"', '"status":"xyz"'
        ) + f',"destinations":[{_dest_valid()}]' + "}"
    ).encode()
    response = http_client.post(
        "/api/v1/agent/executions", content=bad, headers=HEADERS
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail[0]["type"] == "literal_error", detail
    assert detail[0]["loc"][-1] == "status"


def test_malformed_json_returns_422(http_client):
    response = http_client.post(
        "/api/v1/agent/executions",
        content=b'{"execution_id": "e1", nao-e-json}',
        headers=HEADERS,
    )
    assert response.status_code == 422
    assert response.json()["detail"][0]["type"] == "json_invalid"


def test_valid_payload_not_rejected(http_client):
    response = http_client.post(
        "/api/v1/agent/executions",
        content=_body('"duration_seconds":1.5'),
        headers=HEADERS,
    )
    assert response.status_code != 422
    assert response.status_code == 200  # handler rodou sobre o mock
    assert response.json()["status"] == "created"
