"""Recuperação assíncrona: API+PostgreSQL reais, sem executar Restic em HTTP."""
import uuid

import pytest

from tests.conftest import csrf_headers, login, make_operator, requires_db, second_agent
from tests.test_tasks import create_task, task_payload

pytestmark = requires_db
SNAPSHOT = "a" * 64


def request(client, admin_headers, task_id, kind="snapshots", **values):
    payload = {"id": str(uuid.uuid4()), "kind": kind, "destination_id": "local"}
    payload.update({"revision": 1} if kind == "snapshots" else {})
    payload.update(values)
    return client.post(f"/api/v1/tasks/{task_id}/recovery", json=payload, headers=admin_headers)


def setup(client, admin_headers, agent):
    response = create_task(client, admin_headers, agent["agent_id"])
    assert response.status_code == 201
    return response.json()["id"]


def finish_query(client, admin_headers, agent, task_id):
    operation = request(client, admin_headers, task_id).json()
    claim_id = str(uuid.uuid4())
    base = f"/api/v1/agent/recovery/{operation['id']}"
    claim = client.post(base + "/claim", json={"claim_id": claim_id}, headers=agent["headers"])
    assert claim.status_code == 200, claim.text
    result = {"claim_id": claim_id, "state": "succeeded", "result": {"snapshots": [
        {"id": SNAPSHOT, "time": "2026-11-04T10:00:00Z", "tags": [f"task:{task_id}"]}
    ]}}
    response = client.post(base + "/result", json=result, headers=agent["headers"])
    assert response.status_code == 200, response.text
    return operation, claim_id, result


def test_query_idempotency_typed_inputs_and_snapshot_catalog(client, admin_headers, agent):
    task_id = setup(client, admin_headers, agent)
    operation_id = str(uuid.uuid4())
    first = request(client, admin_headers, task_id, id=operation_id)
    assert first.status_code == 202, first.text
    assert first.json()["state"] == "pending" and "config" not in first.json()
    assert request(client, admin_headers, task_id, id=operation_id).json()["id"] == operation_id
    assert request(client, admin_headers, task_id, id=operation_id, destination_id="other").status_code == 409
    assert request(client, admin_headers, task_id, target="/tmp/portal-cannot-choose").status_code == 422
    assert request(client, admin_headers, task_id, revision=True).status_code == 422
    assert request(client, admin_headers, task_id, destination_id="other").status_code == 422
    rows = client.get("/api/v1/agent/recovery", headers=agent["headers"]).json()["operations"]
    assert len(rows) == 1 and rows[0]["config"]["id"] == task_id
    assert rows[0]["config"]["destinations"][0]["repository"] == "/srv/backup/local"


def test_restore_immutable_query_double_click_and_terminal_replay(client, admin_headers, agent):
    task_id = setup(client, admin_headers, agent)
    query, _, _ = finish_query(client, admin_headers, agent, task_id)
    changed = task_payload()
    changed["spec"]["destinations"][0]["repository"] = "/srv/backup/different"
    assert client.post(f"/api/v1/tasks/{task_id}/revisions", json={
        "expected_revision": 1, "config": changed}, headers=admin_headers).status_code == 201
    restored = request(client, admin_headers, task_id, "restore", query_id=query["id"], snapshot_id=SNAPSHOT)
    assert restored.status_code == 202, restored.text
    operation = restored.json()
    assert operation["revision"] == 1
    duplicate = request(client, admin_headers, task_id, "restore", query_id=query["id"], snapshot_id=SNAPSHOT)
    assert duplicate.json()["id"] == operation["id"]
    envelope = client.get("/api/v1/agent/recovery", headers=agent["headers"]).json()["operations"][0]
    assert envelope["config"]["destinations"][0]["repository"] == "/srv/backup/local"
    base = f"/api/v1/agent/recovery/{operation['id']}"
    claim_id = str(uuid.uuid4())
    assert client.post(base + "/claim", json={"claim_id": claim_id}, headers=agent["headers"]).status_code == 200
    assert client.post(base + "/claim", json={"claim_id": str(uuid.uuid4())}, headers=agent["headers"]).status_code == 409
    event = {"claim_id": claim_id, "state": "succeeded", "result": {"target": "/recovery/new-operation"}}
    for _ in range(2):
        result = client.post(base + "/result", json=event, headers=agent["headers"])
        assert result.status_code == 200 and result.json()["state"] == "succeeded"
    late = {"claim_id": claim_id, "state": "interrupted", "error": "abandoned"}
    assert client.post(base + "/result", json=late, headers=agent["headers"]).status_code == 409
    assert client.get("/api/v1/agent/recovery", headers=agent["headers"]).json()["operations"] == []
    assert request(client, admin_headers, task_id, "restore", query_id=query["id"], snapshot_id=SNAPSHOT).json()["id"] == operation["id"]


def test_foreign_agent_cannot_receive_claim_or_confirm(client, admin_headers, agent):
    task_id = setup(client, admin_headers, agent)
    operation = request(client, admin_headers, task_id).json()
    other = second_agent(client, admin_headers)
    assert client.get("/api/v1/agent/recovery", headers=other["headers"]).json()["operations"] == []
    for endpoint, payload in (("claim", {"claim_id": str(uuid.uuid4())}),
                              ("result", {"claim_id": str(uuid.uuid4()), "state": "failed", "error": "repository_error"})):
        assert client.post(f"/api/v1/agent/recovery/{operation['id']}/{endpoint}", json=payload,
                           headers=other["headers"]).status_code == 404


def test_snapshot_context_and_errors_are_typed(client, admin_headers, agent):
    task_id = setup(client, admin_headers, agent)
    operation = request(client, admin_headers, task_id).json()
    claim_id = str(uuid.uuid4())
    base = f"/api/v1/agent/recovery/{operation['id']}"
    assert client.post(base + "/claim", json={"claim_id": claim_id}, headers=agent["headers"]).status_code == 200
    bad = {"claim_id": claim_id, "state": "succeeded", "result": {
        "snapshots": [{"id": SNAPSHOT, "tags": ["task:another"]}]}}
    assert client.post(base + "/result", json=bad, headers=agent["headers"]).status_code == 422
    assert request(client, admin_headers, task_id, "restore", query_id=operation["id"], snapshot_id=SNAPSHOT).status_code == 422
    rejected = client.post(base + "/result", json={"claim_id": claim_id, "state": "failed",
        "error": "password=synthetic-secret"}, headers=agent["headers"])
    assert rejected.status_code == 422 and "synthetic-secret" not in rejected.text
    assert client.post(base + "/result", json={"claim_id": claim_id, "state": "failed",
        "error": "repository_error"}, headers=agent["headers"]).status_code == 200


def test_viewer_reads_without_writes_and_admin_cookie_requires_csrf(client, admin_headers, agent):
    from fastapi.testclient import TestClient
    from macip_backup_api.app import app
    task_id = setup(client, admin_headers, agent)
    make_operator(client, admin_headers, login="recovery-viewer", role="viewer")
    make_operator(client, admin_headers, login="recovery-admin", role="admin")
    with TestClient(app, base_url="https://testserver") as portal:
        assert login(portal, "recovery-viewer", "senha-super-secreta-1").status_code == 200
        assert portal.get(f"/api/v1/tasks/{task_id}/recovery").status_code == 200
        body = {"id": str(uuid.uuid4()), "kind": "snapshots", "revision": 1, "destination_id": "local"}
        assert portal.post(f"/api/v1/tasks/{task_id}/recovery", json=body,
                           headers=csrf_headers(portal)).status_code == 403
        assert login(portal, "recovery-admin", "senha-super-secreta-1").status_code == 200
        assert portal.post(f"/api/v1/tasks/{task_id}/recovery", json=body).status_code == 403
        assert portal.post(f"/api/v1/tasks/{task_id}/recovery", json=body,
                           headers=csrf_headers(portal)).status_code == 202


def test_no_points_is_distinct_from_pending_and_failure(client, admin_headers, agent):
    task_id = setup(client, admin_headers, agent)
    operation = request(client, admin_headers, task_id).json()
    claim_id = str(uuid.uuid4())
    base = f"/api/v1/agent/recovery/{operation['id']}"
    assert client.post(base + "/claim", json={"claim_id": claim_id}, headers=agent["headers"]).status_code == 200
    assert client.post(base + "/result", json={"claim_id": claim_id, "state": "succeeded",
        "result": {"snapshots": []}}, headers=agent["headers"]).status_code == 200
    assert request(client, admin_headers, task_id, "restore", query_id=operation["id"], snapshot_id=SNAPSHOT).status_code == 422
    listed = client.get(f"/api/v1/tasks/{task_id}/recovery", headers=admin_headers).json()
    assert listed[0]["state"] == "succeeded" and listed[0]["result"]["snapshots"] == []


def test_pending_operations_are_listed_before_running(client, admin_headers, agent):
    """Operações pendentes não podem ser escondidas por running: a janela
    de descoberta do agente lista pendentes primeiro, para que um claim
    ocupado não monopolize a primeira posição e impeça o progresso."""
    task_id = setup(client, admin_headers, agent)
    first = request(client, admin_headers, task_id).json()
    claim_id = str(uuid.uuid4())
    base = f"/api/v1/agent/recovery/{first['id']}"
    assert client.post(base + "/claim", json={"claim_id": claim_id}, headers=agent["headers"]).status_code == 200
    pending = request(client, admin_headers, task_id).json()
    operations = client.get("/api/v1/agent/recovery", headers=agent["headers"]).json()["operations"]
    states = [op["state"] for op in operations]
    assert states == ["pending", "running"]
    assert operations[0]["id"] == pending["id"]
    assert operations[1]["id"] == first["id"]


def test_pagination_discovers_beyond_first_page(client, admin_headers, agent):
    """Operações além da primeira página (100) devem ser descobertas via
    cursor. Prova com PostgreSQL real: 3 páginas de 50, cursor estável."""
    task_id = setup(client, admin_headers, agent)
    ids = []
    for _ in range(150):
        ids.append(request(client, admin_headers, task_id).json()["id"])
    page = client.get("/api/v1/agent/recovery?limit=50", headers=agent["headers"]).json()
    assert len(page["operations"]) == 50
    assert page["next_cursor"] is not None
    seen = {op["id"] for op in page["operations"]}
    cursor = page["next_cursor"]
    while cursor:
        page = client.get(f"/api/v1/agent/recovery?limit=50&after={cursor}",
                          headers=agent["headers"]).json()
        for op in page["operations"]:
            assert op["id"] not in seen
            seen.add(op["id"])
        cursor = page["next_cursor"]
    assert len(seen) == 150
    assert seen == set(ids)


def test_pagination_cursor_invalid_returns_404(client, admin_headers, agent):
    task_id = setup(client, admin_headers, agent)
    request(client, admin_headers, task_id)
    bad = uuid.uuid4()
    response = client.get(f"/api/v1/agent/recovery?after={bad}", headers=agent["headers"])
    assert response.status_code == 404
