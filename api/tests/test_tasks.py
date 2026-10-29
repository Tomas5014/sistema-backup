"""Cadastro/versionamento de tarefas — integração PostgreSQL REAL.

Cobre: criação e revisões, imutabilidade, conflito otimista 409,
atomicidade ponteiro+revisão, vínculo cliente/agente, permissões e
CSRF, rejeição de config inválida, segredos fora de respostas e
exportação compatível com o agente.

Requer MACIP_API_TEST_DATABASE_URL (guarda "test" do conftest).
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from tests.conftest import (
    csrf_headers,
    login,
    make_operator,
    requires_db,
)

pytestmark = requires_db


def task_payload(**overrides) -> dict:
    doc = {
        "name": "Financeiro diário",
        "spec": {
            "sources": ["/dados/financeiro"],
            "exclusions": ["*.tmp", "cache/**"],
            "compression": "auto",
            "destinations": [
                {
                    "id": "local",
                    "name": "Disco local",
                    "kind": "local",
                    "repository": "/srv/backup/local",
                    "password_environment": "MACIP_RESTIC_LOCAL",
                    "retention": {"keep_last": 7},
                }
            ],
        },
        "schedule": {"kind": "daily", "timezone": "America/Sao_Paulo",
                     "hour": 2, "minute": 15},
    }
    doc.update(overrides)
    return doc


def create_agent_env(client, admin_headers):
    c = client.post("/api/v1/clients", json={"name": f"c-{uuid.uuid4().hex[:6]}"},
                    headers=admin_headers)
    a = client.post(f"/api/v1/clients/{c.json()['id']}/agents",
                    json={"name": "ag-1"}, headers=admin_headers)
    return c.json()["id"], a.json()["id"]


def create_task(client, admin_headers, agent_id, **kw):
    return client.post(f"/api/v1/agents/{agent_id}/tasks",
                       json=task_payload(**kw), headers=admin_headers)


@pytest.fixture
def portal(client):
    """Client https — honra o flag Secure dos cookies de sessão."""
    from macip_backup_api import app as app_module
    from fastapi.testclient import TestClient

    return TestClient(app_module.app, base_url="https://testserver")


# ---- cadastro + revisões -----------------------------------------------------

def test_cria_revisao_1_vinculada_ao_cliente_do_agente(client, admin_headers):
    client_id, agent_id = create_agent_env(client, admin_headers)
    r = create_task(client, admin_headers, agent_id)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["agent_id"] == agent_id
    assert body["client_id"] == client_id  # derivado do agente, não do corpo
    assert body["current_revision"] == 1
    assert body["distributed"] is False
    assert body["revision"]["author_kind"] == "admin"
    assert body["revision"]["contract_version"] == 1


def test_edicao_cria_revisao_2_e_revisao_1_imutavel(client, admin_headers):
    _, agent_id = create_agent_env(client, admin_headers)
    task = create_task(client, admin_headers, agent_id).json()
    doc = task_payload(name="Financeiro diário v2")
    r = client.post(f"/api/v1/tasks/{task['id']}/revisions",
                    json={"expected_revision": 1, "config": doc},
                    headers=admin_headers)
    assert r.status_code == 201, r.text
    assert r.json()["current_revision"] == 2

    revs = client.get(f"/api/v1/tasks/{task['id']}/revisions", headers=admin_headers).json()
    assert [r_["revision"] for r_ in revs] == [2, 1]
    rev1 = client.get(f"/api/v1/tasks/{task['id']}/revisions/1", headers=admin_headers).json()
    assert rev1["config"]["name"] == "Financeiro diário"  # preservada
    rev2 = client.get(f"/api/v1/tasks/{task['id']}/revisions/2", headers=admin_headers).json()
    assert rev2["config"]["name"] == "Financeiro diário v2"


def test_duas_edicoes_na_mesma_revisao_uma_vence_outra_409(client, admin_headers):
    _, agent_id = create_agent_env(client, admin_headers)
    task = create_task(client, admin_headers, agent_id).json()
    a = client.post(f"/api/v1/tasks/{task['id']}/revisions",
                    json={"expected_revision": 1,
                          "config": task_payload(name="edição A")},
                    headers=admin_headers)
    b = client.post(f"/api/v1/tasks/{task['id']}/revisions",
                    json={"expected_revision": 1,
                          "config": task_payload(name="edição B")},
                    headers=admin_headers)
    assert a.status_code == 201
    assert b.status_code == 409, b.text
    # a vencedora é preservada
    got = client.get(f"/api/v1/tasks/{task['id']}", headers=admin_headers).json()
    assert got["revision"]["config"]["name"] == "edição A"


def test_salvamento_identico_nao_gera_revisao_extra(client, admin_headers):
    _, agent_id = create_agent_env(client, admin_headers)
    task = create_task(client, admin_headers, agent_id).json()
    r = client.post(f"/api/v1/tasks/{task['id']}/revisions",
                    json={"expected_revision": 1, "config": task_payload()},
                    headers=admin_headers)
    assert r.status_code == 201 and r.json()["unchanged"] is True
    assert r.json()["current_revision"] == 1
    revs = client.get(f"/api/v1/tasks/{task['id']}/revisions", headers=admin_headers).json()
    assert len(revs) == 1


def test_rollback_integral_se_revisao_falha_no_commit(client, admin_headers):
    """INSERT da revisão viola unicidade → ponteiro NÃO pode avançar."""
    _, agent_id = create_agent_env(client, admin_headers)
    task = create_task(client, admin_headers, agent_id).json()
    from macip_backup_api import db as db_module
    engine = db_module.engine()
    # reserva a revisão 2 fora da API — o INSERT seguinte conflita
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO task_revisions (task_id, revision, contract_version,"
            " config, author_kind, author_ref) VALUES (:t, 2, 1, '{}'::jsonb,"
            " 'admin', 'sabotage')"
        ), {"t": task["id"]})
    from sqlalchemy.exc import IntegrityError
    try:
        client.post(f"/api/v1/tasks/{task['id']}/revisions",
                    json={"expected_revision": 1,
                          "config": task_payload(name="avanço")},
                    headers=admin_headers)
    except IntegrityError:
        pass  # o INSERT conflita — a resposta só importa no banco
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT current_revision FROM tasks WHERE id=:t"),
            {"t": task["id"]},
        ).scalar_one()
    assert row == 1  # ponteiro preservado — rollback integral


def test_exporta_revisao_antiga_com_ids_centrais(client, admin_headers):
    client_id, agent_id = create_agent_env(client, admin_headers)
    task = create_task(client, admin_headers, agent_id).json()
    client.post(f"/api/v1/tasks/{task['id']}/revisions",
                json={"expected_revision": 1,
                      "config": task_payload(name="v2")},
                headers=admin_headers)
    r = client.get(f"/api/v1/tasks/{task['id']}/revisions/1/export", headers=admin_headers)
    assert r.status_code == 200
    doc = r.json()
    # UUIDs centrais — nunca nomes editáveis
    assert doc["id"] == task["id"]
    assert doc["device_id"] == agent_id
    assert doc["client"]["id"] == client_id
    assert doc["name"] == "Financeiro diário"  # rev 1, não v2
    assert "schedule" not in doc
    assert "distributed" not in doc
    # schedule fica no envelope da central, fora do TaskSpec exportado


def test_arquivar_preserva_revisoes_e_bloqueia_edicao(client, admin_headers):
    _, agent_id = create_agent_env(client, admin_headers)
    task = create_task(client, admin_headers, agent_id).json()
    r = client.post(f"/api/v1/tasks/{task['id']}/archive", headers=admin_headers)
    assert r.status_code == 200 and r.json()["archived"] is True
    # edição bloqueada
    r = client.post(f"/api/v1/tasks/{task['id']}/revisions",
                    json={"expected_revision": 1,
                          "config": task_payload(name="x")},
                    headers=admin_headers)
    assert r.status_code == 409
    # revisões e exportação continuam legíveis
    assert client.get(f"/api/v1/tasks/{task['id']}/revisions/1", headers=admin_headers).status_code == 200
    assert client.get(f"/api/v1/tasks/{task['id']}/revisions/1/export", headers=admin_headers).status_code == 200
    # filtro de arquivamento
    ativas = client.get("/api/v1/tasks?archived=false", headers=admin_headers).json()
    assert all(not t["archived"] for t in ativas)
    arquivadas = client.get("/api/v1/tasks?archived=true", headers=admin_headers).json()
    assert any(t["id"] == task["id"] for t in arquivadas)


def test_lista_filtra_por_cliente_e_agente(client, admin_headers):
    c1, a1 = create_agent_env(client, admin_headers)
    c2, a2 = create_agent_env(client, admin_headers)
    t1 = create_task(client, admin_headers, a1).json()
    create_task(client, admin_headers, a2, name="Outra tarefa")
    by_client = client.get(f"/api/v1/tasks?client_id={c1}", headers=admin_headers).json()
    assert [t["id"] for t in by_client] == [t1["id"]]
    by_agent = client.get(f"/api/v1/tasks?agent_id={a2}", headers=admin_headers).json()
    assert all(t["agent_id"] == a2 for t in by_agent)
    assert client.get(f"/api/v1/tasks?client_id={c2}&archived=false", headers=admin_headers)


# ---- permissões --------------------------------------------------------------

def test_escritas_exigem_admin_e_csrf(client, portal, admin_headers):
    _, agent_id = create_agent_env(client, admin_headers)
    # operador viewer: leitura ok, escrita proibida
    make_operator(client, admin_headers, login="lei", role="viewer",
                  password="senha-e-secreta-9")
    assert login(portal, "lei", "senha-e-secreta-9").status_code == 200
    headers = csrf_headers(portal)
    assert portal.get("/api/v1/tasks").status_code == 200
    r = portal.post(f"/api/v1/agents/{agent_id}/tasks",
                    json=task_payload(), headers=headers)
    assert r.status_code == 403
    # sem CSRF na sessão admin: bloqueado
    make_operator(client, admin_headers, login="adm", role="admin",
                  password="senha-e-secreta-9")
    assert login(portal, "adm", "senha-e-secreta-9").status_code == 200
    r = portal.post(f"/api/v1/agents/{agent_id}/tasks", json=task_payload())
    assert r.status_code == 403
    # token de AGENTE não abre rota administrativa
    r = client.post("/api/v1/agents/x/tasks",
                    json=task_payload(),
                    headers={"Authorization": "Bearer tok-agente-qualquer"})
    assert r.status_code in (401, 403)


def test_revisao_registra_autor_operador(client, portal, admin_headers):
    _, agent_id = create_agent_env(client, admin_headers)
    make_operator(client, admin_headers, login="admin2", role="admin",
                  password="senha-e-secreta-9")
    assert login(portal, "admin2", "senha-e-secreta-9").status_code == 200
    headers = csrf_headers(portal)
    r = portal.post(f"/api/v1/agents/{agent_id}/tasks",
                    json=task_payload(), headers=headers)
    assert r.status_code == 201, r.text
    assert r.json()["revision"]["author_kind"] == "operator"
    assert r.json()["revision"]["author_ref"] == "admin2"


# ---- validação estrutural ----------------------------------------------------

@pytest.mark.parametrize("path,doc", [
    ("campo desconhecido", task_payload(zz_top={"x": 1})),
    ("bool em inteiro", task_payload(spec={
        "sources": ["/a"], "destinations": [
            {"id": "d", "name": "d", "kind": "local",
             "repository": "/srv/b",
             "password_environment": "PW",
             "retention": {"keep_last": True}},
        ]})),
    ("repo dentro de origem", task_payload(spec={
        "sources": ["/srv/b"], "destinations": [
            {"id": "d", "name": "d", "kind": "local",
             "repository": "/srv/b/repo", "password_environment": "PW"}]})),
    ("s3 com credencial na url", task_payload(spec={
        "sources": ["/a"], "destinations": [
            {"id": "d", "name": "d", "kind": "s3",
             "password_environment": "PW",
             "s3": {"endpoint": "https://user:seg@minio.local",
                    "bucket": "bucket-x",
                    "access_key_environment": "AK",
                    "secret_key_environment": "SK"}}]})),
    ("destino sftp sem bloco sftp", task_payload(spec={
        "sources": ["/a"], "destinations": [
            {"id": "d", "name": "d", "kind": "sftp",
             "password_environment": "PW"}]})),
    ("destination.id duplicado", task_payload(spec={
        "sources": ["/a"], "destinations": [
            {"id": "d", "name": "d1", "kind": "local",
             "repository": "/srv/b1", "password_environment": "PW"},
            {"id": "d", "name": "d2", "kind": "local",
             "repository": "/srv/b2", "password_environment": "PW"}]})),
    ("agenda manual com hora", task_payload(schedule={"kind": "manual", "hour": 3})),
    ("timezone inexistente", task_payload(schedule={
        "kind": "daily", "timezone": "Marte/Olympus", "hour": 2, "minute": 0})),
    ("weekly dia 8", task_payload(schedule={
        "kind": "weekly", "timezone": "UTC",
        "weekdays": [8], "hour": 2, "minute": 0})),
    ("campo solto na spec", task_payload(spec={
        "sources": ["/a"], "zz_campo": 1, "destinations": [
            {"id": "d", "name": "d", "kind": "local",
             "repository": "/srv/b", "password_environment": "PW"}]})),
    ("senha embutida enviada", task_payload(spec={
        "sources": ["/a"], "destinations": [
            {"id": "d", "name": "d", "kind": "local",
             "repository": "/srv/b", "password_environment": "PW",
             "password": "SEGREDO-SENTINELA-31415"}]})),
    ("nome vazio", task_payload(name=" ")),
])
def test_config_invalida_rejeitada(client, admin_headers, path, doc):
    _, agent_id = create_agent_env(client, admin_headers)
    r = client.post(f"/api/v1/agents/{agent_id}/tasks", json=doc,
                    headers=admin_headers)
    assert r.status_code == 422, f"{path}: {r.status_code} {r.text}"
    assert "SEGREDO-SENTINELA-31415" not in r.text  # segredo não ecoa


def test_sentinela_aninhada_nao_aparece_na_resposta(client, admin_headers):
    """Segredos injetados em nível profundo não voltam no 422."""
    _, agent_id = create_agent_env(client, admin_headers)
    doc = task_payload(spec={
        "sources": ["/a"],
        "destinations": [{
            "id": "d", "name": "d", "kind": "sftp",
            "password_environment": "PW",
            "sftp": {
                "host": "h", "user": "u", "remote_path": "r",
                "identity_file": "/k", "known_hosts_file": "/kh",
                "chave_privada_conteudo": {
                    "password": "SEGREDO-NEST-777",
                    "nested": [{"secret": "SEGREDO-NEST-888"}],
                },
            },
        }],
    })
    r = client.post(f"/api/v1/agents/{agent_id}/tasks", json=doc,
                    headers=admin_headers)
    assert r.status_code == 422
    assert "SEGREDO-NEST" not in r.text


def test_tarefa_inexistente_e_agente_inexistente(client, admin_headers):
    assert client.post(
        f"/api/v1/agents/{uuid.uuid4()}/tasks",
        json=task_payload(), headers=admin_headers).status_code == 404
    tid = uuid.uuid4()
    assert client.get(f"/api/v1/tasks/{tid}", headers=admin_headers).status_code == 404
    assert client.post(f"/api/v1/tasks/{tid}/archive",
                       headers=admin_headers).status_code == 404
