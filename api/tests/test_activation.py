"""Contratos da ativação com PostgreSQL real e execução A isolada."""

import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import hashlib
import json
import secrets
import uuid

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import text

from conftest import ADMIN_TOKEN, requires_db, csrf_headers, login, make_operator
from macip_backup_api.db import engine
from macip_backup_api.protection_routes import source_fingerprint
from macip_backup_api.models import InstallationPreparation
from sqlalchemy.orm import Session
from macip_backup_api.activation_executor import LabActivationExecutor, DeliveryIn
from test_provisioning import PROVISIONER, prepare, request_body, claim, finish, success_for

pytestmark = requires_db
ADMIN = {"Authorization": f"Bearer {ADMIN_TOKEN}"}


@pytest.fixture
def enabled_activation(monkeypatch):
    monkeypatch.setenv("MACIP_API_LAB_ACTIVATION", "1")
    monkeypatch.setenv("MACIP_API_LAB_PROTECTION", "1")
    monkeypatch.setenv("MACIP_API_ACTIVATION_PUBLIC_URL", "https://central.lab")
    monkeypatch.setenv("MACIP_API_ACTIVATION_DELIVERY_URL", "https://a.lab/v1/activate")


def fake_proof(installation):
    # Simulação explícita do emissor B. A prova integrada usa Restic em outro teste.
    with Session(engine()) as db:
        row = db.get(InstallationPreparation, uuid.UUID(installation))
        row.protected_copy = {"source": source_fingerprint(row), "proof": {"test": "simulation"}}
        db.commit()


def ready_installation(client):
    preparation = prepare(client, request_body())
    operation = claim(client)
    assert finish(client, operation, success_for(operation)).status_code == 200
    return preparation["installation"]["id"]


def issue(client, installation):
    response = client.post("/api/v1/activation-codes", json={"installation_id": installation}, headers=ADMIN)
    assert response.status_code == 201, response.text
    return response.json()


def body_for(code):
    raw = code["activation_code"].split("#macip-activation=")[1]
    # Forma binária Ed25519 válida para validação do contrato.
    key = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + secrets.token_bytes(32)
    return {"activation_code": raw, "activation_id": str(uuid.uuid4()), "device_id": str(uuid.uuid4()),
            "resume_secret": secrets.token_urlsafe(32),
            "public_key": "ssh-ed25519 " + base64.b64encode(key).decode()}


def redeem(client, body):
    return client.post("https://testserver/api/v1/activations/redeem", json=body)


def test_permission_csrf_lab_gate_and_no_proof(client, enabled_activation, monkeypatch):
    installation = ready_installation(client)
    assert client.post("/api/v1/activation-codes", json={"installation_id": installation}, headers=ADMIN).status_code == 409
    fake_proof(installation)
    monkeypatch.setenv("MACIP_API_SESSION_INSECURE_COOKIE", "1")
    op = make_operator(client, ADMIN, login="activation-viewer", role="viewer")
    assert login(client, op["login"], op["password"]).status_code == 200
    assert client.post("/api/v1/activation-codes", json={"installation_id": installation},
                       headers=csrf_headers(client)).status_code == 403
    client.cookies.clear()
    op = make_operator(client, ADMIN, login="activation-admin")
    assert login(client, op["login"], op["password"]).status_code == 200
    assert client.post("/api/v1/activation-codes", json={"installation_id": installation}).status_code == 403
    assert client.post("/api/v1/activation-codes", json={"installation_id": installation},
                       headers=csrf_headers(client)).status_code == 201
    monkeypatch.delenv("MACIP_API_LAB_ACTIVATION")
    assert client.post("/api/v1/activation-codes", json={"installation_id": installation}, headers=ADMIN).status_code == 404


def test_atomic_redeem_retry_binding_expiry_revocation_and_rate_limit(client, enabled_activation):
    installation = ready_installation(client)
    fake_proof(installation)
    code = issue(client, installation)
    body = body_for(code)
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: redeem(client, body), range(2)))
    assert [result.status_code for result in results] == [200, 200]
    assert redeem(client, body).json() == results[0].json()
    for field, value in (("public_key", body_for(code)["public_key"]),
                         ("device_id", str(uuid.uuid4())), ("resume_secret", secrets.token_urlsafe(32)),
                         ("activation_id", str(uuid.uuid4()))):
        assert redeem(client, {**body, field: value}).status_code == 409
    with engine().connect() as db:
        stored = db.execute(text("SELECT * FROM activation_codes WHERE id=:id"), {"id": code["id"]}).mappings().one()
        assert body["activation_code"] not in json.dumps(dict(stored), default=str)
        assert body["resume_secret"] not in json.dumps(dict(stored), default=str)
    invalid = redeem(client, {**body, "activation_code": "x" * 43})
    assert invalid.status_code == 403
    assert "x" * 43 not in invalid.text
    malformed = redeem(client, {**body, "activation_code": "secret-short"})
    assert malformed.status_code == 422
    assert "secret-short" not in malformed.text
    assert client.post("/api/v1/activation-codes/" + code["id"] + "/revoke", headers=ADMIN).status_code == 200
    assert redeem(client, body).status_code == 403
    expired = issue(client, installation)
    with engine().begin() as db:
        db.execute(text("UPDATE activation_codes SET expires_at=now()-interval '1 second' WHERE id=:id"),
                   {"id": expired["id"]})
    assert redeem(client, body_for(expired)).status_code == 410
    assert client.post("/api/v1/activations/redeem", json=body).status_code == 400
    for _ in range(22):
        last = redeem(client, {**body, "activation_code": "z" * 43})
    assert last.status_code == 429


def test_registration_fences_B_and_delivery_rechecks_proof(client, enabled_activation):
    installation = ready_installation(client)
    fake_proof(installation)
    code = issue(client, installation)
    body = body_for(code)
    assert redeem(client, body).status_code == 200
    auth = {"activation_id": body["activation_id"],
            "resume_hash": hashlib.sha256(body["resume_secret"].encode()).hexdigest()}
    endpoint = "/api/v1/activation-executor/"
    assert client.post(endpoint + "authorize", json=auth, headers=ADMIN).status_code == 403
    assert client.post(endpoint + "registered", json=auth, headers=PROVISIONER).status_code == 200
    assert client.post(endpoint + "delivered", json=auth, headers=PROVISIONER).status_code == 409
    assert client.get(f"/api/v1/installations/{installation}/profile", headers=ADMIN).status_code == 409
    assert redeem(client, body).status_code == 200
    fake_proof(installation)
    result = client.post(endpoint + "delivered", json=auth, headers=PROVISIONER)
    assert result.status_code == 200
    assert "restic_password" not in result.text
    assert body["resume_secret"] not in result.text
    assert client.post(endpoint + "delivered", json=auth, headers=PROVISIONER).status_code == 200
    other_installation = ready_installation(client)
    fake_proof(other_installation)
    other = body_for(issue(client, other_installation))
    assert redeem(client, {**other, "activation_id": body["activation_id"]}).status_code == 409


def test_two_machines_compete_for_one_code(client, enabled_activation):
    installation = ready_installation(client)
    fake_proof(installation)
    code = issue(client, installation)
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda body: redeem(client, body), [body_for(code), body_for(code)]))
    assert sorted(result.status_code for result in results) == [200, 409]
