"""API central — PostgreSQL real, autenticação, isolamento e idempotência."""

from __future__ import annotations

import threading
import uuid

import pytest

from conftest import ADMIN_TOKEN, execution_payload, requires_db, second_agent

pytestmark = requires_db


def test_healthz(client):
    assert client.get("/healthz").status_code == 200
    assert client.get("/readyz").json() == {"database": "ok"}


def test_admin_auth_required(client):
    assert client.post("/api/v1/clients", json={"name": "x"}).status_code == 401
    assert (
        client.post(
            "/api/v1/clients",
            json={"name": "x"},
            headers={"Authorization": "Bearer errado"},
        ).status_code
        == 403
    )


def test_client_and_agent_provisioning(client, admin_headers):
    client_id = client.post(
        "/api/v1/clients", json={"name": "acme"}, headers=admin_headers
    ).json()["id"]
    agent_ = client.post(
        f"/api/v1/clients/{client_id}/agents",
        json={"name": "agente-a"},
        headers=admin_headers,
    )
    assert agent_.status_code == 201
    agents = client.get(
        f"/api/v1/clients/{client_id}/agents", headers=admin_headers
    ).json()
    assert [a["name"] for a in agents] == ["agente-a"]

    credential = client.post(
        f"/api/v1/agents/{agent_.json()['id']}/credentials",
        json={"label": "bootstrap"},
        headers=admin_headers,
    )
    assert credential.status_code == 201
    assert credential.json()["token"]  # entregue uma única vez


def test_duplicate_client_conflict(client, admin_headers):
    client.post("/api/v1/clients", json={"name": "dup"}, headers=admin_headers)
    again = client.post(
        "/api/v1/clients", json={"name": "dup"}, headers=admin_headers
    )
    assert again.status_code == 409


def test_token_stored_only_as_hash(client, admin_headers, agent):
    credentials = client.get(
        f"/api/v1/agents/{agent['agent_id']}/credentials", headers=admin_headers
    ).json()
    assert all("token" not in c for c in credentials)

    # banco: o hash registrado não é o token original
    import hashlib

    from sqlalchemy import text
    from macip_backup_api import db as db_module

    with db_module.engine().connect() as connection:
        rows = connection.execute(
            text("SELECT token_hash FROM agent_credentials")
        ).all()
    assert rows == [(hashlib.sha256(agent["token"].encode()).hexdigest(),)]


def test_invalid_and_revoked_token(client, agent):
    bad = client.post(
        "/api/v1/agent/heartbeat",
        headers={"Authorization": "Bearer token-invalido"},
    )
    assert bad.status_code == 403

    assert client.post("/api/v1/agent/heartbeat", headers=agent["headers"]).status_code == 204
    client.post(
        f"/api/v1/credentials/{agent['credential_id']}/revoke",
        headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
    )
    revoked = client.post("/api/v1/agent/heartbeat", headers=agent["headers"])
    assert revoked.status_code == 403  # revogação imediata


def test_execution_submission_full(client, agent):
    created = client.post(
        "/api/v1/agent/executions",
        json=execution_payload(),
        headers=agent["headers"],
    )
    assert created.status_code == 200, created.text
    assert created.json()["status"] == "created"

    # detalhe usa o UUID central — inequívoco entre agentes
    detail = client.get(
        f"/api/v1/executions/{created.json()['id']}",
        headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
    ).json()
    assert detail["status"] == "success"
    assert detail["destinations"][0]["attempts"][0]["snapshot_id"] == "abc123"


def test_same_execution_id_across_agents_and_clients(client, agent, admin_headers):
    """Unicidade é (agent_id, execution_id) — nunca global. Dois agentes
    com o mesmo execution_id permanecem consultáveis separadamente."""
    shared = str(uuid.uuid4())
    first = client.post(
        "/api/v1/agent/executions",
        json=execution_payload(execution_id=shared),
        headers=agent["headers"],
    )
    other = second_agent(client, admin_headers)
    second = client.post(
        "/api/v1/agent/executions",
        json=execution_payload(execution_id=shared, status="failed"),
        headers=other["headers"],
    )
    assert first.json()["status"] == second.json()["status"] == "created"
    assert first.json()["id"] != second.json()["id"]

    headers = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
    detail_a = client.get(f"/api/v1/executions/{first.json()['id']}", headers=headers).json()
    detail_b = client.get(f"/api/v1/executions/{second.json()['id']}", headers=headers).json()
    assert detail_a["status"] == "success"
    assert detail_b["status"] == "failed"
    assert detail_a["client_id"] != detail_b["client_id"]
    listed = client.get("/api/v1/executions", headers=headers).json()
    assert len(listed) == 2


def test_statuses_preserved(client, agent):
    for status in ("partial", "failed", "interrupted"):
        created = client.post(
            "/api/v1/agent/executions",
            json=execution_payload(
                status=status,
                destinations=[
                    {
                        "destination_id": "local",
                        "status": "interrupted" if status == "interrupted" else "failed",
                        "started_at_utc": "2026-10-26T10:00:01Z",
                        "finished_at_utc": "2026-10-26T10:04:59Z",
                    }
                ],
            ),
            headers=agent["headers"],
        )
        assert created.status_code == 200, created.text
    executions = client.get(
        "/api/v1/executions", headers={"Authorization": f"Bearer {ADMIN_TOKEN}"}
    ).json()
    assert {e["status"] for e in executions} == {"partial", "failed", "interrupted"}


def test_cross_client_rejected(client, agent, admin_headers):
    other = client.post(
        "/api/v1/clients", json={"name": "outro"}, headers=admin_headers
    ).json()["id"]
    rejected = client.post(
        "/api/v1/agent/executions",
        json=execution_payload(client_id=other),
        headers=agent["headers"],
    )
    assert rejected.status_code == 403

    other_agent = client.post(
        f"/api/v1/clients/{other}/agents", json={"name": "x"},
        headers=admin_headers,
    ).json()["id"]
    rejected = client.post(
        "/api/v1/agent/executions",
        json=execution_payload(agent_id=other_agent),
        headers=agent["headers"],
    )
    assert rejected.status_code == 403


def test_idempotent_redelivery(client, agent):
    payload = execution_payload()
    first = client.post("/api/v1/agent/executions", json=payload, headers=agent["headers"])
    second = client.post("/api/v1/agent/executions", json=payload, headers=agent["headers"])
    assert first.json()["status"] == "created"
    assert second.json()["status"] == "duplicate"

    executions = client.get(
        "/api/v1/executions", headers={"Authorization": f"Bearer {ADMIN_TOKEN}"}
    ).json()
    assert len(executions) == 1


def test_conflicting_payload_conflict(client, agent):
    payload = execution_payload()
    client.post("/api/v1/agent/executions", json=payload, headers=agent["headers"])
    conflict = execution_payload(execution_id=payload["execution_id"], status="failed")
    result = client.post(
        "/api/v1/agent/executions", json=conflict, headers=agent["headers"]
    )
    assert result.status_code == 409


def test_concurrent_same_execution_id(client, agent):
    payload = execution_payload()
    results = []

    def send():
        response = client.post(
            "/api/v1/agent/executions", json=payload, headers=agent["headers"]
        )
        results.append(response.status_code)

    threads = [threading.Thread(target=send) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(results) == [200, 200]  # created + duplicate — nunca duplicado
    executions = client.get(
        "/api/v1/executions", headers={"Authorization": f"Bearer {ADMIN_TOKEN}"}
    ).json()
    assert len(executions) == 1


def test_heartbeat_updates_last_seen(client, agent, admin_headers):
    before = client.get(
        f"/api/v1/agents/{agent['agent_id']}",
        headers=admin_headers,
    )
    assert client.post("/api/v1/agent/heartbeat", headers=agent["headers"]).status_code == 204
    agents = client.get("/api/v1/agents", headers=admin_headers).json()
    assert agents[0]["last_seen_at"] is not None


def test_pagination_and_filters(client, agent, admin_headers):
    for i in range(5):
        client.post(
            "/api/v1/agent/executions",
            json=execution_payload(
                task_id=f"task-{i % 2}", status="success" if i % 2 else "failed"
            ),
            headers=agent["headers"],
        )
    headers = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
    all_ = client.get("/api/v1/executions?limit=2&offset=0", headers=headers).json()
    assert len(all_) == 2
    assert len(client.get("/api/v1/executions?limit=2&offset=2", headers=headers).json()) == 2
    assert len(client.get("/api/v1/executions?status=failed", headers=headers).json()) == 3
    assert len(client.get("/api/v1/executions?task_id=task-0", headers=headers).json()) == 3
    assert (
        len(
            client.get(
                f"/api/v1/executions?client_id={agent['client_id']}",
                headers=headers,
            ).json()
        )
        == 5
    )
    assert (
        client.get(
            "/api/v1/executions?since=2026-10-27T00:00:00Z", headers=headers
        ).json()
        == []
    )


def test_validation_rejects_bad_input(client, agent):
    naive = execution_payload(started_at_utc="2026-10-26T10:00:00")  # sem tz
    assert client.post(
        "/api/v1/agent/executions", json=naive, headers=agent["headers"]
    ).status_code == 422
    bad_status = execution_payload(status="desconhecido")
    assert client.post(
        "/api/v1/agent/executions", json=bad_status, headers=agent["headers"]
    ).status_code == 422
    too_many = execution_payload(
        destinations=[execution_payload()["destinations"][0]] * 33
    )
    assert client.post(
        "/api/v1/agent/executions", json=too_many, headers=agent["headers"]
    ).status_code == 422


def test_tokens_never_logged(client, agent, admin_headers, caplog):
    """Nem token admin, nem token de agente, nem Authorization aparecem
    nos logs — em cenários de sucesso e de erro."""
    import logging

    with caplog.at_level(logging.DEBUG):
        client.post("/api/v1/agent/heartbeat", headers=agent["headers"])           # ok
        client.post("/api/v1/agent/heartbeat",
                    headers={"Authorization": "Bearer errado"})                   # 403
        client.post("/api/v1/clients", json={"name": "log-t"},
                    headers=admin_headers)                                        # ok admin
        client.post("/api/v1/agent/executions", json={"bad": 1},
                    headers=agent["headers"])                                     # 422
        client.post("/api/v1/agent/executions", json={"bad": 1},
                    headers={"Authorization": f"Bearer {ADMIN_TOKEN}"})           # admin token no canal errado

    leaked = " ".join(
        record.getMessage() for record in caplog.records
    )
    assert agent["token"] not in leaked
    assert ADMIN_TOKEN not in leaked
    assert "Bearer" not in leaked or "errado" not in leaked

    # a entrega intencional do token no provisionamento segue funcionando
    assert agent["token"]  # fixture provisionou — única exposição do segredo


def test_migration_preserves_data(client, agent):
    """Reaplicar migrate sobre banco com dados não apaga histórico."""
    client.post(
        "/api/v1/agent/executions", json=execution_payload(), headers=agent["headers"]
    )
    from macip_backup_api.db import migrate_now

    applied = migrate_now()
    assert applied == []  # nada novo — idempotente
    executions = client.get(
        "/api/v1/executions", headers={"Authorization": f"Bearer {ADMIN_TOKEN}"}
    ).json()
    assert len(executions) == 1


# ---- limite de corpo: por bytes recebidos, não pelo cabeçalho --------

import os


def _set_limit(bytes_: int) -> None:
    os.environ["MACIP_API_MAX_BODY_BYTES"] = str(bytes_)


def test_body_limits_below_exact_and_above(client, agent):
    payload = execution_payload()
    import json as _json

    raw = _json.dumps(payload).encode()
    _set_limit(len(raw) + 10)
    below = client.post(
        "/api/v1/agent/executions",
        content=raw[:-15],
        headers={**agent["headers"], "Content-Type": "application/json"},
    )
    assert below.status_code == 422  # JSON truncado — mas abaixo do limite

    _set_limit(len(raw))
    exact = client.post(
        "/api/v1/agent/executions",
        content=raw,
        headers={**agent["headers"], "Content-Type": "application/json"},
    )
    assert exact.status_code == 200, exact.text

    _set_limit(len(raw) - 1)
    over = client.post(
        "/api/v1/agent/executions",
        content=raw,
        headers={**agent["headers"], "Content-Type": "application/json"},
    )
    assert over.status_code == 413
    assert over.json()["detail"] == "corpo acima do limite"
    executions = client.get(
        "/api/v1/executions", headers={"Authorization": f"Bearer {ADMIN_TOKEN}"}
    ).json()
    assert len(executions) == 1  # só a do limite exato — nada gravado no 413


def test_body_limit_without_content_length(client, agent):
    """Envio chunked (sem Content-Length) — o limite mede bytes reais."""
    _set_limit(50)
    payload = execution_payload()
    import json as _json

    raw = _json.dumps(payload).encode()

    def chunks():
        yield raw[:40]
        yield raw[40:]

    response = client.post(
        "/api/v1/agent/executions",
        content=chunks(),
        headers={**agent["headers"], "Content-Type": "application/json"},
    )
    assert response.status_code == 413
    assert response.json()["detail"] == "corpo acima do limite"


def test_body_limit_many_small_chunks(client, agent):
    """Vários blocos pequenos cuja soma ultrapassa — leitura interrompida."""
    _set_limit(100)
    block = b" " * 20
    sent = []

    def chunks():
        for _ in range(50):
            sent.append(len(block))
            yield block

    response = client.post(
        "/api/v1/agent/executions",
        content=chunks(),
        headers={**agent["headers"], "Content-Type": "application/json"},
    )
    assert response.status_code == 413
    # TestClient serializa o generator no cliente — o corte de leitura
    # no servidor não é observável aqui; o observável é 413 + nada gravado
    executions = client.get(
        "/api/v1/executions", headers={"Authorization": f"Bearer {ADMIN_TOKEN}"}
    ).json()
    assert executions == []


def test_invalid_content_length_returns_400(client, agent):
    response = client.post(
        "/api/v1/agent/executions",
        content=b"{}",
        headers={
            **agent["headers"],
            "Content-Type": "application/json",
            "Content-Length": "nao-e-numero",
        },
    )
    assert response.status_code == 400


def test_declared_smaller_than_sent_still_capped(client, agent):
    """Content-Length declarado menor que o corpo real → mede os bytes."""
    _set_limit(100)
    response = client.post(
        "/api/v1/agent/executions",
        content=b"x" * 500,
        headers={
            **agent["headers"],
            "Content-Type": "application/json",
            "Content-Length": "10",
        },
    )
    # httpx pode impor o header ou enviar 500 bytes — o servidor deve
    # cortar pelo real (413) ou rejeitar (400/422), nunca 500/gravar
    assert response.status_code in (400, 413, 422)
    assert response.status_code != 500


def test_rejected_request_persists_nothing(client, agent, admin_headers):
    _set_limit(10)
    response = client.post(
        "/api/v1/agent/executions",
        content=b"_" * 100,
        headers={**agent["headers"], "Content-Type": "application/json"},
    )
    assert response.status_code == 413
    executions = client.get(
        "/api/v1/executions", headers=admin_headers
    ).json()
    assert executions == []
