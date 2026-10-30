"""Distribuição gerenciada de tarefas — integração PostgreSQL REAL.

Publicação explícita (admin+CSRF), geração monotônica, consulta
paginada autenticada por credencial, confirmações idempotentes e
corrida FOR UPDATE entre decisões administrativas concorrentes.

Requer MACIP_API_TEST_DATABASE_URL (guarda "test" do conftest).
"""

from __future__ import annotations

import hashlib
import json
import uuid

import pytest
from sqlalchemy import text

from tests.conftest import requires_db
from tests.test_tasks import (
    create_agent_env,
    create_task,
    task_payload,
)

pytestmark = requires_db


def _agent_token(client, admin_headers, agent_id):
    credential = client.post(
        f"/api/v1/agents/{agent_id}/credentials",
        json={"label": "pull"},
        headers=admin_headers,
    )
    assert credential.status_code == 201, credential.text
    return credential.json()


def _desired(client, headers, **params):
    return client.get("/api/v1/agent/tasks/desired",
                      params=params, headers=headers)


def _confirm(client, headers, task_id, **kw):
    body = {"generation": kw.pop("generation", 1),
            "status": kw.pop("status", "applied"),
            **kw}
    return client.post(f"/api/v1/agent/tasks/{task_id}/confirmations",
                       json=body, headers=headers)


# ---- publicação explícita + geração -----------------------------------------

def test_criar_nao_publica_e_publicacao_e_explicita(client, admin_headers):
    """Cadastro recente aparece como NÃO publicada — sem linha no
    estado desejado e fora da consulta do agente."""
    _, agent_id = create_agent_env(client, admin_headers)
    cred = _agent_token(client, admin_headers, agent_id)
    agent_headers = {"Authorization": f"Bearer {cred['token']}"}
    task = create_task(client, admin_headers, agent_id).json()

    got = client.get(f"/api/v1/tasks/{task['id']}",
                     headers=admin_headers).json()
    assert got["distribution"]["generation"] == 0
    assert got["distribution"]["published_revision"] is None
    assert got["distribution"]["desired_action"] == "none"

    desired = _desired(client, agent_headers)
    assert desired.status_code == 200
    assert desired.json()["total"] == 0 and desired.json()["tasks"] == []

    # publica a rev 1 — ação explícita com expected_generation=0
    published = client.post(
        f"/api/v1/tasks/{task['id']}/publish",
        json={"revision": 1, "expected_generation": 0},
        headers=admin_headers,
    )
    assert published.status_code == 200, published.text
    dist = published.json()["distribution"]
    assert dist["generation"] == 1
    assert dist["published_revision"] == 1
    assert dist["desired_action"] == "apply"

    desired = _desired(client, agent_headers)
    assert desired.json()["total"] == 1
    envelope = desired.json()["tasks"][0]
    assert envelope["task_id"] == task["id"]
    assert envelope["agent_id"] == agent_id
    assert envelope["generation"] == 1
    assert envelope["action"] == "apply"
    assert envelope["revision"] == 1
    assert envelope["contract_version"] == 1
    # TaskSpec exportado + agenda separada + hash canônico
    assert envelope["task"]["id"] == task["id"]
    assert envelope["task"]["device_id"] == agent_id
    assert envelope["schedule"]["kind"] == "daily"
    same = dict(envelope)
    same.pop("hash")
    expected = hashlib.sha256(
        json.dumps(same, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert envelope["hash"] == expected
    # segredos nunca viajam — só referências de ambiente
    text_dump = json.dumps(envelope)
    assert "password_environment" in text_dump
    assert "PW" not in [d.get("password") for d in
                       envelope["task"]["destinations"]]


def test_editar_nao_altera_revisao_publicada(client, admin_headers):
    """Nova revisão cadastrada NÃO publica — o agente continua vendo a
    publicada até ação explícita."""
    _, agent_id = create_agent_env(client, admin_headers)
    task = create_task(client, admin_headers, agent_id).json()
    client.post(f"/api/v1/tasks/{task['id']}/publish",
                json={"revision": 1, "expected_generation": 0},
                headers=admin_headers)
    edit = client.post(
        f"/api/v1/tasks/{task['id']}/revisions",
        json={"expected_revision": 1,
              "config": task_payload(name="Nova edição")},
        headers=admin_headers,
    )
    assert edit.status_code == 201
    got = client.get(f"/api/v1/tasks/{task['id']}",
                     headers=admin_headers).json()
    assert got["current_revision"] == 2
    assert got["distribution"]["published_revision"] == 1
    assert got["distribution"]["generation"] == 1


def test_publish_espera_geracao_vigente(client, admin_headers):
    """expected_generation obsoleta → 409; retry do MESMO POST após
    sucesso também → 409 (não re-publica silenciosamente)."""
    _, agent_id = create_agent_env(client, admin_headers)
    task = create_task(client, admin_headers, agent_id).json()
    first = client.post(
        f"/api/v1/tasks/{task['id']}/publish",
        json={"revision": 1, "expected_generation": 0},
        headers=admin_headers)
    assert first.status_code == 200
    repeat = client.post(
        f"/api/v1/tasks/{task['id']}/publish",
        json={"revision": 1, "expected_generation": 0},
        headers=admin_headers)
    assert repeat.status_code == 409
    # atualização da MESMA revisão — nova geração distingue
    again = client.post(
        f"/api/v1/tasks/{task['id']}/publish",
        json={"revision": 1, "expected_generation": 1},
        headers=admin_headers)
    assert again.status_code == 200
    assert again.json()["distribution"]["generation"] == 2


def test_publish_revisao_inexistente_404(client, admin_headers):
    _, agent_id = create_agent_env(client, admin_headers)
    task = create_task(client, admin_headers, agent_id).json()
    response = client.post(
        f"/api/v1/tasks/{task['id']}/publish",
        json={"revision": 9, "expected_generation": 0},
        headers=admin_headers)
    assert response.status_code == 404


def test_arquivamento_preserva_e_suspensao_continua(client, admin_headers):
    """Arquivar não suspende — mas a suspensão segue DISPONÍVEL na
    tarefa arquivada; publicar em arquivada é 409."""
    _, agent_id = create_agent_env(client, admin_headers)
    task = create_task(client, admin_headers, agent_id).json()
    client.post(f"/api/v1/tasks/{task['id']}/publish",
                json={"revision": 1, "expected_generation": 0},
                headers=admin_headers)
    archive = client.post(f"/api/v1/tasks/{task['id']}/archive",
                          headers=admin_headers)
    assert archive.status_code == 200
    got = client.get(f"/api/v1/tasks/{task['id']}",
                     headers=admin_headers).json()
    assert got["distribution"]["desired_action"] == "apply"  # arquivar não suspendeu
    publish = client.post(
        f"/api/v1/tasks/{task['id']}/publish",
        json={"revision": 1, "expected_generation": 1},
        headers=admin_headers)
    assert publish.status_code == 409
    suspend = client.post(
        f"/api/v1/tasks/{task['id']}/suspend",
        json={"expected_generation": 1},
        headers=admin_headers)
    assert suspend.status_code == 200
    dist = suspend.json()["distribution"]
    assert dist["generation"] == 2
    assert dist["desired_action"] == "suspend"
    # suspend aparece para o agente como ação "suspend"
    cred = _agent_token(client, admin_headers, agent_id)
    desired = _desired(client, {"Authorization": f"Bearer {cred['token']}"})
    env = desired.json()["tasks"][0]
    assert env["action"] == "suspend" and env["generation"] == 2
    assert "task" not in env  # suspensão não carrega documento


# ---- isolamento por credencial ------------------------------------------------

def test_consulta_desired_isolada_por_agente(client, admin_headers):
    """O agente só vê tarefas vinculadas a ELE — nunca as de outro
    cliente nem as do próprio agente não publicadas."""
    _, agent_a = create_agent_env(client, admin_headers)
    _, agent_b = create_agent_env(client, admin_headers)
    task_a = create_task(client, admin_headers, agent_a).json()
    task_b = create_task(client, admin_headers, agent_b).json()
    client.post(f"/api/v1/tasks/{task_a['id']}/publish",
                json={"revision": 1, "expected_generation": 0},
                headers=admin_headers)
    client.post(f"/api/v1/tasks/{task_b['id']}/publish",
                json={"revision": 1, "expected_generation": 0},
                headers=admin_headers)
    cred_a = _agent_token(client, admin_headers, agent_a)
    desired = _desired(client, {"Authorization": f"Bearer {cred_a['token']}"})
    ids = [t["task_id"] for t in desired.json()["tasks"]]
    assert ids == [task_a["id"]]


def test_consulta_sem_credencial_ou_revogada(client, admin_headers):
    assert _desired(client, {}).status_code in (401, 403)
    _, agent_id = create_agent_env(client, admin_headers)
    cred = _agent_token(client, admin_headers, agent_id)
    headers = {"Authorization": f"Bearer {cred['token']}"}
    assert _desired(client, headers).status_code == 200
    revoke = client.post(
        f"/api/v1/credentials/{cred['credential_id']}/revoke",
        headers=admin_headers)
    assert revoke.status_code in (200, 204)
    assert _desired(client, headers).status_code == 403


def test_paginacao_desired_explicita(client, admin_headers):
    """limit/offset/total: ausência numa página é apenas recorte —
    o total diz que há mais itens."""
    _, agent_id = create_agent_env(client, admin_headers)
    tasks = [
        create_task(client, admin_headers, agent_id).json()["id"]
        for _ in range(3)
    ]
    for task_id in tasks:
        client.post(f"/api/v1/tasks/{task_id}/publish",
                    json={"revision": 1, "expected_generation": 0},
                    headers=admin_headers)
    cred = _agent_token(client, admin_headers, agent_id)
    page1 = _desired(client, {"Authorization": f"Bearer {cred['token']}"},
                     limit=2, offset=0).json()
    assert page1["total"] == 3 and len(page1["tasks"]) == 2
    page2 = _desired(client, {"Authorization": f"Bearer {cred['token']}"},
                     limit=2, offset=2).json()
    assert len(page2["tasks"]) == 1
    seen = {t["task_id"] for t in page1["tasks"] + page2["tasks"]}
    assert seen == set(tasks)


# ---- confirmações ------------------------------------------------------------

def test_confirmacao_promove_estado_da_geracao_vigente(client, admin_headers):
    _, agent_id = create_agent_env(client, admin_headers)
    cred = _agent_token(client, admin_headers, agent_id)
    agent_headers = {"Authorization": f"Bearer {cred['token']}"}
    task = create_task(client, admin_headers, agent_id).json()
    client.post(f"/api/v1/tasks/{task['id']}/publish",
                json={"revision": 1, "expected_generation": 0},
                headers=admin_headers)
    response = _confirm(
        client, agent_headers, task["id"], generation=1,
        applied_revision=1, agent_time="2026-10-30T12:00:00Z")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "recorded"
    assert response.json()["promoted"] is True
    got = client.get(f"/api/v1/tasks/{task['id']}",
                     headers=admin_headers).json()
    confirmed = got["distribution"]["confirmed"]
    assert confirmed["generation"] == 1
    assert confirmed["status"] == "applied"
    assert confirmed["applied_revision"] == 1
    assert confirmed["received_at"]  # horário do servidor
    assert confirmed["agent_time"] == "2026-10-30T12:00:00Z"


def test_confirmacao_idempotente_por_conteudo(client, admin_headers):
    _, agent_id = create_agent_env(client, admin_headers)
    cred = _agent_token(client, admin_headers, agent_id)
    agent_headers = {"Authorization": f"Bearer {cred['token']}"}
    task = create_task(client, admin_headers, agent_id).json()
    client.post(f"/api/v1/tasks/{task['id']}/publish",
                json={"revision": 1, "expected_generation": 0},
                headers=admin_headers)
    _confirm(client, agent_headers, task["id"], generation=1,
             applied_revision=1)
    again = _confirm(client, agent_headers, task["id"], generation=1,
                     applied_revision=1)
    assert again.status_code == 200
    assert again.json()["status"] == "duplicate"
    conflict = _confirm(client, agent_headers, task["id"], generation=1,
                        applied_revision=1, status="failed",
                        detail="outro conteúdo")
    assert conflict.status_code == 409


def test_confirmacao_geracao_antiga_registra_sem_promover(
        client, admin_headers):
    """Atrasada: entra no log, nunca rebaixa o estado confirmado."""
    _, agent_id = create_agent_env(client, admin_headers)
    cred = _agent_token(client, admin_headers, agent_id)
    agent_headers = {"Authorization": f"Bearer {cred['token']}"}
    task = create_task(client, admin_headers, agent_id).json()
    client.post(f"/api/v1/tasks/{task['id']}/publish",
                json={"revision": 1, "expected_generation": 0},
                headers=admin_headers)
    client.post(f"/api/v1/tasks/{task['id']}/publish",
                json={"revision": 1, "expected_generation": 1},
                headers=admin_headers)
    _confirm(client, agent_headers, task["id"], generation=2,
             applied_revision=1, detail="vigente")
    stale = _confirm(client, agent_headers, task["id"], generation=1,
                     applied_revision=1, detail="atrasada")
    assert stale.status_code == 200
    assert stale.json()["status"] == "recorded"
    assert stale.json()["promoted"] is False
    got = client.get(f"/api/v1/tasks/{task['id']}",
                     headers=admin_headers).json()
    confirmed = got["distribution"]["confirmed"]
    assert confirmed["generation"] == 2
    assert confirmed["detail"] == "vigente"
    # mas o log conserva a confirmação antiga
    from macip_backup_api import db as db_module
    with db_module.engine().connect() as conn:
        count = conn.execute(text(
            "SELECT count(*) FROM task_confirmations WHERE task_id=:t"),
            {"t": task["id"]}).scalar_one()
    assert count == 2


def test_confirmacao_geracao_futura_409(client, admin_headers):
    _, agent_id = create_agent_env(client, admin_headers)
    cred = _agent_token(client, admin_headers, agent_id)
    task = create_task(client, admin_headers, agent_id).json()
    client.post(f"/api/v1/tasks/{task['id']}/publish",
                json={"revision": 1, "expected_generation": 0},
                headers=admin_headers)
    response = _confirm(client, {"Authorization": f"Bearer {cred['token']}"},
                        task["id"], generation=5, applied_revision=1)
    assert response.status_code == 409


def test_confirmacao_tarefa_de_outro_agente_404(client, admin_headers):
    """Identidade só da credencial: confirmar tarefa alheia = 404
    (sem revelar existência)."""
    _, agent_a = create_agent_env(client, admin_headers)
    _, agent_b = create_agent_env(client, admin_headers)
    task_a = create_task(client, admin_headers, agent_a).json()
    client.post(f"/api/v1/tasks/{task_a['id']}/publish",
                json={"revision": 1, "expected_generation": 0},
                headers=admin_headers)
    cred_b = _agent_token(client, admin_headers, agent_b)
    response = _confirm(client, {"Authorization": f"Bearer {cred_b['token']}"},
                        task_a["id"], generation=1, applied_revision=1)
    assert response.status_code == 404
    # e a tarefa de B nem aparece na consulta de B sobre A:
    assert _desired(client, {"Authorization": f"Bearer {cred_b['token']}"}
                    ).json()["total"] == 0


def test_viewer_le_distribuicao_mas_nao_escreve(client, admin_headers):
    """Viewer (operador de consulta) lê o estado; publish/suspend e
    escrita exigem admin + CSRF."""
    from tests.conftest import csrf_headers, login, make_operator
    from macip_backup_api import app as app_module
    from fastapi.testclient import TestClient

    _, agent_id = create_agent_env(client, admin_headers)
    task = create_task(client, admin_headers, agent_id).json()
    make_operator(client, admin_headers, login="viewer1", role="viewer",
                  name="Viewer")
    portal = TestClient(app_module.app, base_url="https://testserver")
    assert login(portal, "viewer1", "senha-super-secreta-1").status_code == 200
    got = portal.get(f"/api/v1/tasks/{task['id']}")
    assert got.status_code == 200
    assert "distribution" in got.json()
    headers = csrf_headers(portal)
    response = portal.post(
        f"/api/v1/tasks/{task['id']}/publish",
        json={"revision": 1, "expected_generation": 0},
        headers=headers)
    assert response.status_code == 403


# ---- corrida FOR UPDATE entre decisões ----------------------------------------

def test_suspend_sem_publicacao_gera_envelope_suspend(client, admin_headers):
    """Suspender tarefa nunca publicada produz envelope suspend sem
    revisão — o agente só desativa o que houver gerenciado."""
    _, agent_id = create_agent_env(client, admin_headers)
    task = create_task(client, admin_headers, agent_id).json()
    cred = _agent_token(client, admin_headers, agent_id)
    agent_headers = {"Authorization": f"Bearer {cred['token']}"}

    r = client.post(f"/api/v1/tasks/{task['id']}/suspend",
                    json={"expected_generation": 0}, headers=admin_headers)
    assert r.status_code == 200
    got = client.get("/api/v1/agent/tasks/desired", headers=agent_headers).json()
    assert got["total"] == 1
    env = got["tasks"][0]
    assert env["action"] == "suspend"
    assert env["revision"] is None
    assert env["contract_version"] == 1
    assert "task" not in env


def test_corrida_publish_vs_suspend_decisao_deterministica(
        client, admin_headers):
    """Duas ações admin na MESMA geração em sessões independentes: a
    fila do lock da linha de distribuição decide — uma vence, a outra
    recebe 409, sem estado híbrido."""
    import threading
    import time
    from sqlalchemy import create_engine
    from tests.conftest import TEST_URL

    _, agent_id = create_agent_env(client, admin_headers)
    task = create_task(client, admin_headers, agent_id).json()
    client.post(f"/api/v1/tasks/{task['id']}/publish",
                json={"revision": 1, "expected_generation": 0},
                headers=admin_headers)

    # segura a linha de distribuição para enfileirar as chamadas
    engine = create_engine(TEST_URL)
    holder = engine.connect()
    holder.execute(text("BEGIN"))
    holder.execute(text(
        "SELECT task_id FROM task_distributions WHERE task_id=:t FOR UPDATE"),
        {"t": task["id"]})
    probe = engine.connect()  # conexão ÚNICA — sem vazar no loop
    results: dict[int, object] = {}
    threads = []
    try:
        calls = [
            lambda: client.post(
                f"/api/v1/tasks/{task['id']}/publish",
                json={"revision": 1, "expected_generation": 1},
                headers=admin_headers),
            lambda: client.post(
                f"/api/v1/tasks/{task['id']}/suspend",
                json={"expected_generation": 1},
                headers=admin_headers),
        ]
        deadline = time.monotonic() + 15
        for index, call in enumerate(calls):
            def run(i=index, c=call):
                results[i] = c()
            thread = threading.Thread(target=run, daemon=True)
            thread.start()
            threads.append(thread)
            # espera o lock pendente surgir — não um relógio arbitrário
            while time.monotonic() < deadline:
                waiting = probe.execute(text(
                    "SELECT count(*) FROM pg_locks WHERE NOT granted"
                )).scalar_one()
                if waiting >= index + 1:
                    break
                time.sleep(0.02)
            else:
                raise AssertionError("lock pendente não apareceu")
    finally:
        holder.execute(text("ROLLBACK"))
        holder.close()
        probe.close()
    for thread in threads:
        thread.join(timeout=15)
        assert not thread.is_alive()
    engine.dispose()

    codes = sorted(results[i].status_code for i in results)
    assert codes == [200, 409]
    got = client.get(f"/api/v1/tasks/{task['id']}",
                     headers=admin_headers).json()
    assert got["distribution"]["generation"] == 2
