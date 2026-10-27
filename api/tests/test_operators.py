"""Operadores: Argon2id, sessões opacas, CSRF, rate limit, permissões.

PostgreSQL real — nada de SQLite. Cookie Secure é honrado por clientes
reais; por isso o portal usa base_url https://testserver.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from conftest import (
    ADMIN_TOKEN,
    TEST_URL,
    csrf_headers,
    login,
    make_operator,
    requires_db,
)

pytestmark = requires_db


@pytest.fixture
def portal(client):
    """Client https — honra o flag Secure dos cookies de sessão."""
    from macip_backup_api import app as app_module

    return TestClient(app_module.app, base_url="https://testserver")


def _exec(sql: str, **params):
    """SQL direto no banco de teste — verifica persistência real."""
    from macip_backup_api import db as db_module

    with db_module.engine().begin() as connection:
        return connection.execute(text(sql), params)


def _operator_row(login_name: str):
    return _exec(
        "SELECT id, login, role, active, password_hash FROM operators "
        "WHERE login = :l",
        l=login_name,
    ).one_or_none()


# ---- modelo e unicidade -----------------------------------------------------


def test_login_normalizado_e_unico(client, admin_headers):
    make_operator(client, admin_headers, login="  Ana.Silva  ")
    row = _operator_row("ana.silva")
    assert row is not None, "login não foi normalizado no banco"
    assert row.login == "ana.silva"

    again = client.post(
        "/api/v1/operators",
        json={
            "login": "ANA.SILVA",  # normaliza para o MESMO login
            "name": "Outra",
            "role": "viewer",
            "password": "outra-senha-123",
        },
        headers=admin_headers,
    )
    assert again.status_code == 409


def test_unicidade_sob_criacao_concorrente(client):
    """Duas inserções simultâneas — a UNIQUE do PostgreSQL decide uma."""
    from sqlalchemy.orm import Session

    from macip_backup_api import db as db_module
    from macip_backup_api.models import Operator
    from macip_backup_api.operators import hash_password

    results = []

    def create():
        try:
            with Session(db_module.engine()) as db:
                db.add(
                    Operator(
                        id=uuid.uuid4(),
                        login="concorrente",
                        name="X",
                        password_hash=hash_password("senha-concorrente-1"),
                        role="viewer",
                        active=True,
                    )
                )
                db.commit()
                results.append("ok")
        except Exception:
            results.append("conflict")

    threads = [threading.Thread(target=create) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert sorted(results) == ["conflict", "ok"], results
    assert _exec("SELECT count(*) FROM operators WHERE login='concorrente'").scalar() == 1


def test_senha_persistida_apenas_como_hash(client, admin_headers):
    password = "senha-invisivel-42"
    make_operator(client, admin_headers, login="hash-check", password=password)
    row = _operator_row("hash-check")
    assert row.password_hash != password
    assert row.password_hash.startswith("$argon2id$")
    # a senha em claro não está em NENHUMA coluna da linha
    dumped = _exec(
        "SELECT * FROM operators WHERE login='hash-check'"
    ).one()
    assert password not in str(dumped)


# ---- login e sessão ----------------------------------------------------------


def test_login_valido_emite_cookies_e_me(portal, admin_headers):
    make_operator(portal, admin_headers, login="ana", name="Ana")
    response = login(portal, "  ANA  ", "senha-super-secreta-1")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["login"] == "ana"
    # o identificador NUNCA sai em JSON — só no cookie HttpOnly
    assert "token" not in body and "session" not in body

    set_cookie = ";".join(response.headers.get_list("set-cookie"))
    assert "macip_session=" in set_cookie
    assert "httponly" in set_cookie.lower()
    assert "secure" in set_cookie.lower()
    assert "samesite=lax" in set_cookie.lower()

    me = portal.get("/api/v1/auth/me")
    assert me.status_code == 200
    assert me.json()["login"] == "ana"
    assert _operator_row("ana").id is not None
    last = _exec("SELECT last_login_at FROM operators WHERE login='ana'").scalar()
    assert last is not None


def test_login_errado_e_inexistente_sao_genericos(portal, admin_headers):
    make_operator(portal, admin_headers, login="gen")
    wrong = login(portal, "gen", "senha-errada-1")
    missing = login(portal, "ninguem", "senha-errada-1")
    assert wrong.status_code == missing.status_code == 401
    assert wrong.json() == missing.json()  # não revela existência

    _exec("UPDATE operators SET active = false WHERE login='gen'")
    inactive = login(portal, "gen", "senha-super-secreta-1")
    assert inactive.status_code == 401
    assert inactive.json() == wrong.json()  # nem a desativação vaza


def test_login_invalido_ainda_consume_tempo_de_hash(portal, admin_headers):
    """Login inexistente passa por verify — tempo não vaza existência."""
    import time

    start = time.monotonic()
    login(portal, "inexistente", "qualquer-senha-1")
    elapsed = time.monotonic() - start
    assert elapsed > 0.01, "verify não rodou — timing vaza existência"


def test_sessao_renovada_a_cada_login(portal, admin_headers):
    make_operator(portal, admin_headers, login="renova")
    first = login(portal, "renova", "senha-super-secreta-1")
    token_a = portal.cookies.get("macip_session")
    second = login(portal, "renova", "senha-super-secreta-1")
    token_b = portal.cookies.get("macip_session")
    assert first.status_code == second.status_code == 200
    assert token_a != token_b, "identificador não foi renovado no login"
    # persistidos só os hashes — nunca os tokens
    hashes = {
        r[0]
        for r in _exec("SELECT token_hash FROM operator_sessions").all()
    }
    assert token_a not in hashes and token_b not in hashes


def test_logout_revoga_sessao(portal, admin_headers):
    make_operator(portal, admin_headers, login="sai")
    login(portal, "sai", "senha-super-secreta-1")
    token = portal.cookies.get("macip_session")
    logout = portal.post(
        "/api/v1/auth/logout", headers=csrf_headers(portal)
    )
    assert logout.status_code == 204
    revoked = _exec(
        "SELECT revoked_at FROM operator_sessions WHERE token_hash = :h",
        h=hashlib.sha256(token.encode()).hexdigest(),
    ).scalar()
    assert revoked is not None, "logout não revogou a sessão"
    # cookie limpo no cliente → próxima chamada sem sessão
    assert portal.get("/api/v1/auth/me").status_code == 401


def test_sessao_expirada_e_revogada_recusadas(portal, admin_headers):
    make_operator(portal, admin_headers, login="exp")
    row = _operator_row("exp")
    # sessão "válida" mas expirada — inserida direto no banco
    _exec(
        "INSERT INTO operator_sessions (id, operator_id, token_hash, expires_at) "
        "VALUES (:i, :o, :h, :e)",
        i=str(uuid.uuid4()),
        o=str(row.id),
        h=hashlib.sha256(b"token-expirado").hexdigest(),
        e=datetime.now(UTC) - timedelta(seconds=1),
    )
    portal.cookies.set("macip_session", "token-expirado")
    assert portal.get("/api/v1/auth/me").status_code == 401


def test_desativar_revoga_sessoes_e_bloqueia(portal, admin_headers):
    op = make_operator(portal, admin_headers, login="desativa")
    login(portal, "desativa", "senha-super-secreta-1")
    assert portal.get("/api/v1/auth/me").status_code == 200

    portal.patch(
        f"/api/v1/operators/{op['id']}",
        json={"active": False},
        headers={**admin_headers, **csrf_headers(portal)},
    )
    # sessão viva morre junto com o operador
    assert portal.get("/api/v1/auth/me").status_code == 401
    assert login(portal, "desativa", "senha-super-secreta-1").status_code == 401


def test_reset_senha_revoga_sessoes(portal, admin_headers):
    op = make_operator(portal, admin_headers, login="reset")
    login(portal, "reset", "senha-super-secreta-1")
    portal.post(
        f"/api/v1/operators/{op['id']}/reset-password",
        json={"password": "senha-nova-forte-9"},
        headers={**admin_headers, **csrf_headers(portal)},
    )
    assert portal.get("/api/v1/auth/me").status_code == 401  # sessão morta
    assert login(portal, "reset", "senha-super-secreta-1").status_code == 401
    assert login(portal, "reset", "senha-nova-forte-9").status_code == 200


def test_mudanca_de_perfil_vale_na_proxima_requisicao(portal, admin_headers):
    op = make_operator(portal, admin_headers, login="vira-viewer")
    login(portal, "vira-viewer", "senha-super-secreta-1")
    assert portal.get("/api/v1/auth/me").json()["role"] == "admin"

    portal.patch(
        f"/api/v1/operators/{op['id']}",
        json={"role": "viewer"},
        headers={**admin_headers, **csrf_headers(portal)},
    )
    assert portal.get("/api/v1/auth/me").json()["role"] == "viewer"
    # e a escrita passa a ser negada já na requisição seguinte
    denied = portal.post(
        "/api/v1/clients",
        json={"name": "x"},
        headers=csrf_headers(portal),
    )
    assert denied.status_code == 403


# ---- permissões --------------------------------------------------------------


def test_viewer_consulta_mas_nao_escreve(portal, admin_headers):
    make_operator(portal, admin_headers, login="leitor", role="viewer")
    login(portal, "leitor", "senha-super-secreta-1")
    csrf = csrf_headers(portal)

    assert portal.get("/api/v1/clients").status_code == 200
    assert portal.get("/api/v1/agents").status_code == 200
    assert portal.get("/api/v1/executions").status_code == 200

    assert portal.post(
        "/api/v1/clients", json={"name": "x"}, headers=csrf
    ).status_code == 403
    assert portal.post(
        "/api/v1/operators",
        json={
            "login": "novo",
            "name": "Novo",
            "role": "viewer",
            "password": "senha-do-novo-1",
        },
        headers=csrf,
    ).status_code == 403


def test_operador_admin_executa_administracao(client, portal, admin_headers):
    op = make_operator(portal, admin_headers, login="admin2")
    login(portal, "admin2", "senha-super-secreta-1")
    csrf = csrf_headers(portal)
    created = portal.post(
        "/api/v1/clients", json={"name": "via-operador"}, headers=csrf
    )
    assert created.status_code == 201
    listed = portal.get("/api/v1/operators")
    assert any(o["login"] == "admin2" for o in listed.json())
    # e gerencia operadores — inclusive o próprio viewer
    # (client sem sessão: Bearer puro; portal com sessão exigiria CSRF)
    viewer = make_operator(client, admin_headers, login="subordinado", role="viewer")
    patch = portal.patch(
        f"/api/v1/operators/{viewer['id']}",
        json={"role": "admin"},
        headers=csrf,
    )
    assert patch.status_code == 200
    assert patch.json()["role"] == "admin"


def test_separacao_agente_operador_bearer(client, portal, admin_headers, agent):
    """Três credenciais, três mundos — nenhuma atravessa a fronteira."""
    make_operator(client, admin_headers, login="separa")
    login(portal, "separa", "senha-super-secreta-1")

    # token de AGENTE não abre recurso de operador — `client` não tem
    # sessão; o Bearer de agente é o único candidato
    assert client.get(
        "/api/v1/auth/me", headers=agent["headers"]
    ).status_code == 401
    assert client.get(
        "/api/v1/operators", headers=agent["headers"]
    ).status_code == 403

    # cookie de OPERADOR não autentica rota de agente
    heartbeat = portal.post("/api/v1/agent/heartbeat")
    assert heartbeat.status_code == 401

    # Bearer administrativo segue com seu contrato próprio
    assert client.get("/api/v1/clients", headers=admin_headers).status_code == 200
    # mas não vira sessão de operador
    assert client.get(
        "/api/v1/auth/me", headers=admin_headers
    ).status_code == 401


# ---- CSRF --------------------------------------------------------------------


def test_escrita_com_sessao_exige_csrf(portal, admin_headers):
    make_operator(portal, admin_headers, login="csrf-user")
    login(portal, "csrf-user", "senha-super-secreta-1")

    without = portal.post("/api/v1/clients", json={"name": "x"})
    assert without.status_code == 403, "escrita sem X-CSRF-Token passou"

    wrong = portal.post(
        "/api/v1/clients",
        json={"name": "x"},
        headers={"X-CSRF-Token": "token-divergente"},
    )
    assert wrong.status_code == 403

    ok = portal.post(
        "/api/v1/clients", json={"name": "x"}, headers=csrf_headers(portal)
    )
    assert ok.status_code == 201


def test_logout_tambem_exige_csrf(portal, admin_headers):
    make_operator(portal, admin_headers, login="csrf-out")
    login(portal, "csrf-out", "senha-super-secreta-1")
    assert portal.post("/api/v1/auth/logout").status_code == 403


def test_login_exige_par_csrf(portal, admin_headers):
    make_operator(portal, admin_headers, login="csrf-in")
    missing = portal.post(
        "/api/v1/auth/login",
        json={"login": "csrf-in", "password": "senha-super-secreta-1"},
    )
    assert missing.status_code == 403
    portal.get("/api/v1/auth/csrf")
    divergent = portal.post(
        "/api/v1/auth/login",
        json={"login": "csrf-in", "password": "senha-super-secreta-1"},
        headers={"X-CSRF-Token": "outro"},
    )
    assert divergent.status_code == 403


# ---- rate limit (em banco — entre processos) ----------------------------------


def test_rate_limit_bloqueia_e_nao_vaza_existencia(portal, admin_headers, monkeypatch):
    monkeypatch.setenv("MACIP_API_LOGIN_MAX_ATTEMPTS", "3")
    make_operator(portal, admin_headers, login="alvo")
    for _ in range(3):
        assert login(portal, "alvo", "errada").status_code == 401
    limited = login(portal, "alvo", "senha-super-secreta-1")
    assert limited.status_code == 429
    # login inexistente recebe a MESMA resposta — não há pista
    limited_missing = login(portal, "ninguem-aqui", "x")
    assert limited_missing.status_code == 429


def test_rate_limit_vale_entre_processos(portal, admin_headers):
    """Contador em PostgreSQL — 'outro processo' = outra conexão."""
    make_operator(portal, admin_headers, login="dist")
    for _ in range(5):
        login(portal, "dist", "errada")
    # conexão independente prova que o estado está no banco
    count = _exec(
        "SELECT count(*) FROM login_attempts WHERE login='dist'"
    ).scalar()
    assert count == 5
    assert login(portal, "dist", "senha-super-secreta-1").status_code == 429


# ---- segredos ------------------------------------------------------------------


def test_422_nao_ecoa_senha(portal, admin_headers):
    """Campo sensível no input de um 422 vira mascarado."""
    csrf = csrf_headers(portal)
    short = portal.post(
        "/api/v1/auth/login",
        json={"login": "x", "password": "segredo-a-mascarar" * 20},
        headers=csrf,
    )
    assert short.status_code == 422
    assert "segredo-a-mascarar" not in short.text

    create = portal.post(
        "/api/v1/operators",
        json={
            "login": "masc",
            "name": "M",
            "role": "viewer",
            "password": "curta",  # < 8 → 422 carregando o input
        },
        headers={**admin_headers, **csrf},
    )
    assert create.status_code == 422
    assert "curta" not in create.text


def test_logs_e_respostas_sem_segredos(portal, admin_headers, caplog):
    password = "senha-ausente-dos-logs-7"
    make_operator(portal, admin_headers, login="auditado", password=password)
    with caplog.at_level(logging.INFO, "macip_backup_api.audit"):
        login(portal, "auditado", password)
        login(portal, "auditado", "senha-errada-9")
        portal.post(
            "/api/v1/auth/logout", headers=csrf_headers(portal)
        )
    text_logs = "\n".join(r.getMessage() for r in caplog.records)
    assert "login ok" in text_logs and "login falhou" in text_logs
    assert "logout" in text_logs
    assert password not in text_logs
    assert "senha-errada-9" not in text_logs
    # respostas do fluxo não ecoam Authorization, token ou a senha
    login_response = login(portal, "auditado", password)
    assert password not in login_response.text
    assert "authorization" not in login_response.text.lower()


# ---- bootstrap local ------------------------------------------------------------


def test_bootstrap_cli_cria_primeiro_admin(portal):
    """Procedimento local: senha por stdin, operador criado no banco."""
    import subprocess
    import sys as _sys
    from pathlib import Path

    result = subprocess.run(
        [
            _sys.executable,
            "-m",
            "macip_backup_api.operator_cli",
            "create",
            "--login",
            "Bootstrap",
            "--name",
            "Primeiro",
            "--role",
            "admin",
            "--password-stdin",
        ],
        input="senha-do-bootstrap-1\n",
        capture_output=True,
        text=True,
        timeout=60,
        env={
            "PATH": "/usr/bin:/bin",
            "PYTHONPATH": str(Path(__file__).parents[1] / "src"),
            "MACIP_API_DATABASE_URL": (
                TEST_URL
                if TEST_URL.startswith("postgresql+psycopg://")
                else "postgresql+psycopg://" + TEST_URL.removeprefix("postgresql://")
            ),
            "MACIP_API_ADMIN_TOKEN": ADMIN_TOKEN,
        },
    )
    assert result.returncode == 0, result.stderr
    assert "senha-do-bootstrap-1" not in result.stdout + result.stderr
    row = _operator_row("bootstrap")
    assert row is not None and row.role == "admin"
    # senha NUNCA entra como argumento — só por stdin
    assert "--password" not in result.args or "stdin" in " ".join(result.args)


# ---- concorrência real (PostgreSQL, conexões independentes) -------------------


def _fresh_portal(ip: str) -> TestClient:
    """Client independente com IP próprio — conexão direta simulada."""
    from macip_backup_api import app as app_module

    return TestClient(
        app_module.app, base_url="https://testserver", client=(ip, 50000)
    )


def test_rate_limit_atomico_mesmo_login_ips_diferentes(portal, admin_headers, monkeypatch):
    """7 tentativas concorrentes do MESMO login com limite 3:
    exatamente 3 registradas — nenhuma lê contagem desatualizada."""
    monkeypatch.setenv("MACIP_API_LOGIN_MAX_ATTEMPTS", "3")
    make_operator(portal, admin_headers, login="alvo-conc")
    barrier = threading.Barrier(7)
    statuses: list[int] = []

    def attempt(n):
        c = _fresh_portal(f"10.0.0.{n}")
        c.get("/api/v1/auth/csrf")
        barrier.wait(timeout=30)
        statuses.append(
            c.post(
                "/api/v1/auth/login",
                json={"login": "alvo-conc", "password": "errada-9"},
                headers={"X-CSRF-Token": c.cookies.get("macip_csrf")},
            ).status_code
        )

    threads = [threading.Thread(target=attempt, args=(n,)) for n in range(7)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    assert not any(t.is_alive() for t in threads)

    recorded = _exec(
        "SELECT count(*) FROM login_attempts WHERE login='alvo-conc'"
    ).scalar()
    assert recorded == 3, f"limite 3 — gravadas {recorded}"
    assert statuses.count(401) == 3 and statuses.count(429) == 4


def test_rate_limit_atomico_mesmo_ip_logins_diferentes(portal, admin_headers, monkeypatch):
    """Limite por IP: 5 logins diferentes, mesma origem, limite 3."""
    monkeypatch.setenv("MACIP_API_LOGIN_MAX_ATTEMPTS", "3")
    for n in range(5):
        make_operator(portal, admin_headers, login=f"lote-{n}")
    barrier = threading.Barrier(5)
    statuses: list[int] = []

    def attempt(n):
        c = _fresh_portal("10.7.7.7")
        c.get("/api/v1/auth/csrf")
        barrier.wait(timeout=30)
        statuses.append(
            c.post(
                "/api/v1/auth/login",
                json={"login": f"lote-{n}", "password": "errada-9"},
                headers={"X-CSRF-Token": c.cookies.get("macip_csrf")},
            ).status_code
        )

    threads = [threading.Thread(target=attempt, args=(n,)) for n in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)

    recorded = _exec(
        "SELECT count(*) FROM login_attempts WHERE ip='10.7.7.7'"
    ).scalar()
    assert recorded == 3
    assert statuses.count(401) == 3 and statuses.count(429) == 2


def test_rate_limit_janela_expira(portal, admin_headers):
    """Tentativas antigas fora da janela não contam — volta a 401."""
    make_operator(portal, admin_headers, login="janela")
    _exec(
        "INSERT INTO login_attempts (login, ip, attempted_at) "
        "SELECT 'janela', 'testclient', now() - interval '1 hour' "
        "FROM generate_series(1, 9)"
    )
    # sem expirar seria 429 — expirado volta a contar do zero
    assert login(portal, "janela", "errada-9").status_code == 401
    # e a poda removeu as antigas: só a nova permanece
    assert _exec(
        "SELECT count(*) FROM login_attempts WHERE login='janela'"
    ).scalar() == 1


def test_reset_na_janela_do_login_nao_deixa_sessao_da_senha_antiga(
    portal, admin_headers, monkeypatch
):
    """Corrida real: login pausado entre verificar e criar sessão.

    O FOR UPDATE serializa: o reset espera a linha, conclui depois do
    login e revoga INCLUSIVE a sessão recém-criada — depois do reset
    nada autenticado com a senha antiga sobrevive.
    """
    import macip_backup_api.app as app_module

    op = make_operator(portal, admin_headers, login="corrida")
    created = threading.Event()
    release = threading.Event()
    original = app_module.create_session

    def paused(db_, operator):
        created.set()
        release.wait(timeout=60)
        return original(db_, operator)

    monkeypatch.setattr(app_module, "create_session", paused)

    result: dict = {}

    def do_login():
        c = _fresh_portal("10.3.3.3")
        result["login"] = login(c, "corrida", "senha-super-secreta-1")
        result["client"] = c

    def do_reset():
        c2 = _fresh_portal("10.4.4.4")
        result["reset"] = c2.post(
            f"/api/v1/operators/{op['id']}/reset-password",
            json={"password": "senha-nova-corrida-3"},
            headers={**admin_headers, **csrf_headers(c2)},
        )

    t_login = threading.Thread(target=do_login)
    t_login.start()
    assert created.wait(timeout=60), "login não chegou ao ponto de corrida"

    t_reset = threading.Thread(target=do_reset)
    t_reset.start()
    release.set()  # login cria sessão e comita → reset destrava
    t_login.join(timeout=60)
    t_reset.join(timeout=60)
    assert not t_login.is_alive() and not t_reset.is_alive()

    # o login antigo concluiu com sucesso…
    assert result["login"].status_code == 200
    assert result["reset"].status_code == 204
    # …mas o reset revogou a sessão que a senha antiga acabou de criar
    assert result["client"].get("/api/v1/auth/me").status_code == 401
    # senha antiga recusada; senha nova funciona
    assert login(portal, "corrida", "senha-super-secreta-1").status_code == 401
    assert login(portal, "corrida", "senha-nova-corrida-3").status_code == 200


def test_desativar_reativar_nao_ressuscita_sessao(portal, admin_headers):
    op = make_operator(portal, admin_headers, login="revive")
    login(portal, "revive", "senha-super-secreta-1")
    token = portal.cookies.get("macip_session")
    csrf = csrf_headers(portal)
    admin_csrf = {**admin_headers, **csrf}

    portal.patch(
        f"/api/v1/operators/{op['id']}",
        json={"active": False},
        headers=admin_csrf,
    )
    portal.patch(
        f"/api/v1/operators/{op['id']}",
        json={"active": True},
        headers=admin_csrf,
    )
    # reativado, mas a sessão revogada NÃO volta
    portal.cookies.set("macip_session", token)
    assert portal.get("/api/v1/auth/me").status_code == 401
    revoked = _exec(
        "SELECT count(*) FROM operator_sessions "
        "WHERE token_hash = :h AND revoked_at IS NOT NULL",
        h=hashlib.sha256(token.encode()).hexdigest(),
    ).scalar()
    assert revoked == 1
    # login novo funciona normalmente após reativação
    assert login(portal, "revive", "senha-super-secreta-1").status_code == 200


def test_reset_pela_cli_local_tambem_revoga(portal, admin_headers):
    """A CLI local (FOR UPDATE) não deixa sessão da senha antiga."""
    import subprocess
    import sys as _sys
    from pathlib import Path

    make_operator(portal, admin_headers, login="cli-reset")
    login(portal, "cli-reset", "senha-super-secreta-1")
    assert portal.get("/api/v1/auth/me").status_code == 200

    result = subprocess.run(
        [
            _sys.executable, "-m", "macip_backup_api.operator_cli",
            "reset-password", "--login", "cli-reset", "--password-stdin",
        ],
        input="senha-cli-nova-4\n",
        capture_output=True,
        text=True,
        timeout=60,
        env={
            "PATH": "/usr/bin:/bin",
            "PYTHONPATH": str(Path(__file__).parents[1] / "src"),
            "MACIP_API_DATABASE_URL": (
                TEST_URL
                if TEST_URL.startswith("postgresql+psycopg://")
                else "postgresql+psycopg://" + TEST_URL.removeprefix("postgresql://")
            ),
            "MACIP_API_ADMIN_TOKEN": ADMIN_TOKEN,
        },
    )
    assert result.returncode == 0, result.stderr
    assert portal.get("/api/v1/auth/me").status_code == 401
    assert login(portal, "cli-reset", "senha-super-secreta-1").status_code == 401
    assert login(portal, "cli-reset", "senha-cli-nova-4").status_code == 200
