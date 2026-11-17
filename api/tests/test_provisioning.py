"""Preparação primária real no PostgreSQL; executor externo é tipado."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import shutil
import tempfile
import uuid

import pytest

from conftest import (
    ADMIN_TOKEN,
    csrf_headers,
    login,
    make_operator,
    requires_db,
)
from macip_backup_api.provisioning_routes import account_name
from macip_backup_api.provisioner import LabFileVault, LabPrimaryAdapter, run_once

pytestmark = requires_db

PROVISIONER = {
    "Authorization": "Bearer test-provisioner-token-1234567890abcdef"
}


def request_body(**overrides):
    body = {
        "id": str(uuid.uuid4()),
        "client_name": f"Cliente {uuid.uuid4().hex[:8]}",
        "installation_identifier": "pc-financeiro-01",
        "machine_name": "PC-FINANCEIRO",
        "suggested_time": "22:30",
    }
    body.update(overrides)
    return body


def prepare(client, body, headers=None):
    response = client.post(
        "/api/v1/clients/prepare",
        json=body,
        headers=headers or {"Authorization": f"Bearer {ADMIN_TOKEN}"},
    )
    assert response.status_code == 202, response.text
    return response.json()


def claim(client):
    response = client.post(
        "/api/v1/provisioner/operations/claim",
        json={"executor_id": "primary-a-lab"},
        headers=PROVISIONER,
    )
    assert response.status_code == 200, response.text
    return response.json()["operation"]


def success_for(operation):
    repository_id = "a" * 64
    account = account_name(
        uuid.UUID(operation["client_id"]), operation["installation_identifier"]
    )
    return {
        "claim_id": operation["claim_id"],
        "state": "succeeded",
        "error_code": None,
        "result": {
            "account_name": account,
            "repository_id": repository_id,
            "public_key_fingerprint": "SHA256:" + "A" * 43,
            "profile": {
                "nome": operation["client_name"],
                "repositorio": f"sftp:{account}@backup.lab:repository",
                "repositorio_id": repository_id,
                "known_hosts": "backup.lab ssh-ed25519 " + "A" * 44,
                "horario_sugerido": operation["suggested_time"],
            },
        },
    }


def finish(client, operation, body):
    return client.post(
        f"/api/v1/provisioner/operations/{operation['id']}/result",
        json=body,
        headers=PROVISIONER,
    )


class InProcessTransport:
    """Transporte de prova: mesma API real, sem abrir socket ou esconder o contrato."""

    def __init__(self, client):
        self.client = client

    def claim(self):
        return claim(self.client)

    def result(self, operation_id, body):
        response = self.client.post(
            f"/api/v1/provisioner/operations/{operation_id}/result",
            json=body,
            headers=PROVISIONER,
        )
        assert response.status_code == 200, response.text
        return response.json()


def test_two_clients_are_isolated_and_profile_stays_blocked(client):
    first = prepare(client, request_body(client_name="Cliente Alfa"))
    second = prepare(
        client,
        request_body(
            client_name="Cliente Beta",
            installation_identifier="pc-financeiro-01",
            machine_name="PC-BETA",
        ),
    )
    assert first["client_id"] != second["client_id"]
    assert first["installation"]["identifier"] == second["installation"]["identifier"]

    operations = [claim(client), claim(client)]
    for operation in operations:
        response = finish(client, operation, success_for(operation))
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["state"] == "primary_ready"
        assert body["protection_state"] == "pending"
        assert body["profile_available"] is False
        assert "profile" not in body and "vault_reference" not in body
        blocked = client.get(
            f"/api/v1/installations/{operation['installation_id']}/profile",
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        )
        assert blocked.status_code == 409
        assert "pendente" in blocked.json()["detail"]
        lab = client.get(
            f"/api/v1/lab/installations/{operation['installation_id']}/profile",
            headers=PROVISIONER,
        )
        assert lab.status_code == 200
        serialized = lab.text.lower()
        assert "password" not in serialized and "private" not in serialized


def test_two_simultaneous_requests_share_one_active_operation(client):
    base = request_body(client_name="Cliente Concorrente")
    bodies = [{**base, "id": str(uuid.uuid4())} for _ in range(2)]

    def send(body):
        return client.post(
            "/api/v1/clients/prepare",
            json=body,
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(send, bodies))
    assert [response.status_code for response in responses] == [202, 202]
    documents = [response.json() for response in responses]
    assert len({item["installation"]["id"] for item in documents}) == 1
    assert len({item["operation"]["id"] for item in documents}) == 1


def test_request_id_cannot_be_reused_for_different_input(client):
    body = request_body(client_name="Cliente Idempotente")
    prepare(client, body)
    changed = client.post(
        "/api/v1/clients/prepare",
        json={**body, "client_name": "Outro Cliente"},
        headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
    )
    assert changed.status_code == 409


def test_partial_failure_retry_and_late_results_do_not_regress(client):
    prepared = prepare(client, request_body(client_name="Cliente Retry"))
    first = claim(client)
    failed = {
        "claim_id": first["claim_id"],
        "state": "failed",
        "result": None,
        "error_code": "secret_missing",
    }
    response = finish(client, first, failed)
    assert response.status_code == 200
    assert response.json()["state"] == "failed"

    retry_id = str(uuid.uuid4())
    response = client.post(
        f"/api/v1/installations/{prepared['installation']['id']}/preparation/retry",
        json={"id": retry_id},
        headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
    )
    assert response.status_code == 202
    assert response.json()["operation"]["attempt"] == 2

    duplicate = finish(client, first, failed)
    assert duplicate.status_code == 200
    assert duplicate.json()["state"] == "pending"
    changed_late = finish(client, first, success_for(first))
    assert changed_late.status_code == 409

    second = claim(client)
    completed = finish(client, second, success_for(second))
    assert completed.status_code == 200
    assert completed.json()["state"] == "primary_ready"


def test_divergent_repository_identity_is_rejected_without_terminal_success(client):
    prepare(client, request_body(client_name="Cliente Divergência"))
    operation = claim(client)
    result = success_for(operation)
    result["result"]["profile"]["repositorio_id"] = "b" * 64
    response = finish(client, operation, result)
    assert response.status_code == 422
    current = client.get(
        f"/api/v1/installations/{operation['installation_id']}/preparation",
        headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
    )
    assert current.json()["state"] == "running"


def test_viewer_cannot_prepare_or_retry_and_cookie_admin_needs_csrf(
    client, admin_headers, monkeypatch
):
    monkeypatch.setenv("MACIP_API_SESSION_INSECURE_COOKIE", "1")
    viewer = make_operator(
        client,
        admin_headers,
        login="viewer-provision",
        role="viewer",
        password="senha-viewer-provision-1",
    )
    assert login(client, viewer["login"], viewer["password"]).status_code == 200
    response = client.post("/api/v1/clients/prepare", json=request_body())
    assert response.status_code == 403

    client.cookies.clear()
    admin = make_operator(
        client,
        admin_headers,
        login="admin-provision",
        role="admin",
        password="senha-admin-provision-1",
    )
    assert login(client, admin["login"], admin["password"]).status_code == 200
    no_csrf = client.post("/api/v1/clients/prepare", json=request_body())
    assert no_csrf.status_code == 403
    allowed = client.post(
        "/api/v1/clients/prepare",
        json=request_body(client_name="Cliente CSRF"),
        headers=csrf_headers(client),
    )
    assert allowed.status_code == 202


def test_executor_auth_and_invalid_error_do_not_echo_input(client):
    prepare(client, request_body(client_name="Cliente Seguro"))
    assert client.post(
        "/api/v1/provisioner/operations/claim",
        json={"executor_id": "primary-a-lab"},
    ).status_code == 401
    operation = claim(client)
    secret = "SEGREDO-SINTETICO-NAO-ECOAR"
    response = finish(
        client,
        operation,
        {
            "claim_id": operation["claim_id"],
            "state": "failed",
            "result": None,
            "error_code": secret,
        },
    )
    assert response.status_code == 422
    assert secret not in response.text
    success = success_for(operation)
    success["result"]["password"] = secret
    response = finish(client, operation, success)
    assert response.status_code == 422
    assert secret not in response.text


@pytest.mark.skipif(
    not shutil.which("restic") or not shutil.which("ssh-keygen"),
    reason="Restic/ssh-keygen locais indisponíveis",
)
def test_full_api_executor_restic_flow_is_idempotent(client):
    body = request_body(
        client_name="Cliente Integrado",
        installation_identifier="pc-integrado-01",
        machine_name="PC-INTEGRADO",
    )
    queued = prepare(client, body)
    with tempfile.TemporaryDirectory(
        prefix=".test-provisioning-e2e-", dir=Path.cwd()
    ) as directory:
        root = Path(directory)
        root.chmod(0o700)
        vault = LabFileVault(root / "vault")
        primary = LabPrimaryAdapter(
            root / "primary-a",
            host="backup.lab",
            known_hosts="backup.lab ssh-ed25519 " + "A" * 44,
        )
        result = run_once(InProcessTransport(client), vault, primary)
        assert result["status"] == "succeeded"

        completed = client.get(
            f"/api/v1/installations/{queued['installation']['id']}/preparation",
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        ).json()
        assert completed["state"] == "primary_ready"
        repository_id = completed["repository_id"]
        assert repository_id and len(repository_id) == 64

        repeated = prepare(client, {**body, "id": str(uuid.uuid4())})
        assert repeated["operation"]["id"] == queued["operation"]["id"]
        assert repeated["repository_id"] == repository_id
        assert client.post(
            "/api/v1/provisioner/operations/claim",
            json={"executor_id": "primary-a-lab"},
            headers=PROVISIONER,
        ).json() == {"operation": None}

        accounts = [path for path in (root / "primary-a").iterdir() if path.is_dir()]
        assert len(accounts) == 1
        marker = (accounts[0] / ".repository-id").read_text().strip()
        assert marker == repository_id
