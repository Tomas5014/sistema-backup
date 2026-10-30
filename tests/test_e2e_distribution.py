"""Prova INTEGRADA de distribuição — ambiente descartável, nada simulado.

API FastAPI real (uvicorn em 127.0.0.1) sobre PostgreSQL real →
cadastro/publicação via HTTP admin → agente REAL executando
``pull-tasks`` como subprocesso → units systemd escritas em um HOME
descartável (sem daemon-reload: a ativação é prova de homologação) →
``agent`` executando backup Restic local de verdade → resultado pela
outbox + ``sync`` → execução e confirmação gravadas na central → o
detalhe da tarefa (o que o portal exibe) mostra a revisão aplicada.

Também exercita atualização de revisão (gen 2) e suspensão (gen 3),
verificando que configuração anterior e histórico são preservados.

Requer MACIP_API_TEST_DATABASE_URL (guarda "test") e restic no PATH.
Sem restic → skip explícito; banco configurado e inacessível → FALHA.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import uuid
from pathlib import Path

import pytest
import uvicorn
from sqlalchemy import text

REPO_ROOT = Path(__file__).resolve().parents[1]
TEST_URL = os.environ.get("MACIP_API_TEST_DATABASE_URL")
ADMIN_TOKEN = "test-admin-e2e-dist-abc123"

requires_db = pytest.mark.skipif(
    TEST_URL is None,
    reason="MACIP_API_TEST_DATABASE_URL não definido — integração pendente",
)
requires_restic = pytest.mark.skipif(
    shutil.which("restic") is None,
    reason="binário restic ausente — prova real impossível",
)

pytestmark = [requires_db, requires_restic]


class _ApiServer(threading.Thread):
    def __init__(self, app):
        super().__init__(daemon=True)
        config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error")
        self.server = uvicorn.Server(config)
        self.port = None

    def run(self):
        self.server.run()


@pytest.fixture(scope="module")
def api(tmp_path_factory):
    """API real com PostgreSQL descartável — dados limpos por módulo."""
    db_name = TEST_URL.rsplit("/", 1)[-1].split("?")[0].lower()
    assert "test" in db_name, "banco deve parecer descartável"
    os.environ["MACIP_API_DATABASE_URL"] = TEST_URL
    os.environ["MACIP_API_ADMIN_TOKEN"] = ADMIN_TOKEN
    import sysconfig
    import site

    api_src = str(REPO_ROOT / "api" / "src")
    if api_src not in sys.path:
        sys.path.insert(0, api_src)
    from macip_backup_api import app as app_module
    from macip_backup_api import db as db_module

    db_module.set_engine(None)
    app = app_module.create_app()
    from fastapi.testclient import TestClient

    with TestClient(app) as seed:
        engine = db_module.engine()
        with engine.begin() as conn:
            for table in (
                "task_confirmations", "task_distributions",
                "task_revisions", "tasks", "executions",
                "agent_credentials", "agents", "clients",
            ):
                conn.execute(text(f"TRUNCATE {table} CASCADE"))
        thread = _ApiServer(app)
        thread.start()
        # espera o socket real abrir
        import time

        deadline = time.monotonic() + 10
        port = None
        while time.monotonic() < deadline:
            for sock in getattr(thread.server, "servers", []):
                port = sock.sockets[0].getsockname()[1]
                break
            if port:
                break
            time.sleep(0.05)
        assert port, "API não abriu porta"
        yield f"http://127.0.0.1:{port}", seed
        thread.server.should_exit = True
        thread.join(timeout=10)


def _admin(base_url, method, path, **kw):
    import urllib.request

    headers = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
    body = kw.pop("json", None)
    if body is not None:
        body = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        f"{base_url}{path}", data=body, headers=headers, method=method
    )
    with urllib.request.urlopen(req, timeout=10) as response:
        return json.loads(response.read())


def _cli(tmp_path: Path, *args: str, extra_env: dict | None = None):
    """Subprocesso REAL da CLI do agente no ambiente descartável."""
    env = {
        "PYTHONPATH": str(REPO_ROOT / "src"),
        "HOME": str(tmp_path / "home"),
        "MACIP_STATE": str(tmp_path / "state"),
        "MACIP_CACHE": str(tmp_path / "cache"),
        "MACIP_LOCK_DIR": str(tmp_path / "locks"),
        "PATH": os.environ.get("PATH", ""),
        **(extra_env or {}),
    }
    Path(env["HOME"]).mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [sys.executable, "-m", "macip_backup.cli", *args],
        capture_output=True, text=True, env=env, timeout=120,
        cwd=str(tmp_path),
    )
    assert result.returncode == 0, (
        f"CLI {args[0]} falhou ({result.returncode}): "
        f"{result.stderr[-2000:]}"
    )
    return json.loads(result.stdout)


def test_fluxo_completo_distribuicao(api, tmp_path):
    """cadastro → publish → pull → units → backup restic → sync →
    revisão aplicada exibida → update (gen2) → suspensão (gen3)."""
    base_url, _seed = api
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    store = state_dir / "agent.db"
    central_path = state_dir / "central.json"

    # 1. cadastro central (admin real)
    client = _admin(base_url, "POST", "/api/v1/clients",
                    json={"name": "Cliente E2E"})
    agent = _admin(base_url, "POST",
                   f"/api/v1/clients/{client['id']}/agents",
                   json={"name": "vm-descartavel"})
    credential = _admin(
        base_url, "POST",
        f"/api/v1/agents/{agent['id']}/credentials",
        json={"label": "e2e"})

    source_dir = tmp_path / "origem"
    source_dir.mkdir()
    (source_dir / "arquivo.txt").write_text("conteúdo real do backup")
    repo_dir = tmp_path / "repositorio"

    task = _admin(
        base_url, "POST", f"/api/v1/agents/{agent['id']}/tasks",
        json={
            "name": "backup-descartavel",
            "spec": {
                "sources": [str(source_dir)],
                "destinations": [{
                    "id": "local", "name": "disco local",
                    "kind": "local",
                    "repository": str(repo_dir),
                    "password_environment": "MACIP_RESTIC_LOCAL",
                    "retention": {"keep_last": 5},
                }],
            },
            "schedule": {"kind": "daily", "timezone": "UTC",
                         "hour": 3, "minute": 30},
        })
    task_id = task["id"]

    env = {
        "MACIP_API_TOKEN": credential["token"],
        "MACIP_RESTIC_LOCAL": "senha-restic-e2e",
    }
    central_path.write_text(json.dumps({
        "url": base_url,
        "agent_id": agent["id"],
        "token_environment": "MACIP_API_TOKEN",
    }))

    # 2. pull ANTES de publicar — nada a aplicar
    dry = _cli(tmp_path, "pull-tasks", "--central", str(central_path),
               "--store", str(store), "--no-activate", extra_env=env)
    assert dry["desired_total"] == 0

    # 3. publicar rev 1
    _admin(base_url, "POST", f"/api/v1/tasks/{task_id}/publish",
           json={"revision": 1, "expected_generation": 0})

    # 4. pull-tasks REAL: envelope → journal → units escritas
    pull = _cli(tmp_path, "pull-tasks", "--central", str(central_path),
                "--store", str(store), "--no-activate", extra_env=env)
    assert pull["applied"][0]["task_id"] == task_id
    managed = state_dir / "managed" / "tasks.d" / task_id
    assert (managed / "current.json").is_file()
    doc = json.loads((managed / "current.json").read_text())
    assert doc["id"] == task_id
    units = tmp_path / "home" / ".config" / "systemd" / "user"
    service = units / f"macip-backup-managed-{task_id}.service"
    timer = units / f"macip-backup-managed-{task_id}.timer"
    assert service.is_file() and timer.is_file()
    assert "managed-task=" + task_id in service.read_text()
    assert "OnCalendar=*-*-* 03:30:00 UTC" in timer.read_text()
    assert "Persistent=true" in timer.read_text()

    # confirmação chegou à central
    detail = _admin(base_url, "GET", f"/api/v1/tasks/{task_id}")
    assert detail["distribution"]["confirmed"]["status"] == "applied"
    assert detail["distribution"]["confirmed"]["applied_revision"] == 1

    # 5. backup RESTIC REAL executando a configuração aplicada
    run = _cli(tmp_path, "agent", str(managed / "current.json"),
               "--store", str(store), "--central", str(central_path),
               extra_env=env)
    assert run["status"] == "success"
    snapshot = run["destinations"][0]["snapshot_id"]
    assert snapshot and (repo_dir / "data").is_dir()  # repositório real

    # 6. sync envia o resultado à central
    sent = _cli(tmp_path, "sync", "--central", str(central_path),
                "--store", str(store), extra_env=env)
    assert sent["sent"]

    executions = _admin(base_url, "GET", "/api/v1/executions?limit=10")
    ours = [e for e in executions
            if e["task_id"] == task_id]
    assert ours and ours[0]["status"] == "success"

    # 7. edição rev 2 + publicação: geração 2, rev aplicada atualiza
    _admin(base_url, "POST", f"/api/v1/tasks/{task_id}/revisions",
           json={"expected_revision": 1,
                 "config": {
                     "name": "backup-descartavel-v2",
                     "spec": task["revision"]["config"]["spec"],
                     "schedule": {"kind": "daily", "timezone": "UTC",
                                  "hour": 4, "minute": 0},
                 }})
    _admin(base_url, "POST", f"/api/v1/tasks/{task_id}/publish",
           json={"revision": 2, "expected_generation": 1})
    pull2 = _cli(tmp_path, "pull-tasks", "--central", str(central_path),
                 "--store", str(store), "--no-activate", extra_env=env)
    assert pull2["applied"][0]["revision"] == 2
    doc2 = json.loads((managed / "current.json").read_text())
    assert doc2["name"] == "backup-descartavel-v2"
    assert (managed / "gen-1.json").is_file()  # revisão anterior preservada
    assert "04:00" in timer.read_text()
    detail = _admin(base_url, "GET", f"/api/v1/tasks/{task_id}")
    assert detail["distribution"]["generation"] == 2
    assert detail["distribution"]["confirmed"]["applied_revision"] == 2

    # 8. suspensão: só o timer sai; configuração/histórico preservados
    _admin(base_url, "POST", f"/api/v1/tasks/{task_id}/suspend",
           json={"expected_generation": 2})
    pull3 = _cli(tmp_path, "pull-tasks", "--central", str(central_path),
                 "--store", str(store), "--no-activate", extra_env=env)
    assert pull3["suspended"]
    assert (managed / "current.json").is_file()
    detail = _admin(base_url, "GET", f"/api/v1/tasks/{task_id}")
    assert detail["distribution"]["desired_action"] == "suspend"
    assert detail["distribution"]["confirmed"]["status"] == "applied"
    # snapshots do backup real preservados e listáveis
    snaps = _cli(tmp_path, "snapshots", str(managed / "current.json"),
                 "local", extra_env=env)
    assert any(s["id"].startswith(snapshot[:8]) or s["id"] == snapshot
               for s in snaps["snapshots"])


def test_units_habilitadas_no_systemd_real(api, tmp_path):
    """systemd --user REAL: timer habilitado e depois removido com
    limpeza garantida — prova a ativação que --no-activate adia."""
    import subprocess

    task_id = str(uuid.uuid4())
    unit_dir = Path.home() / ".config" / "systemd" / "user"
    names = [f"macip-backup-managed-{task_id}.{s}" for s in ("service", "timer")]
    paths = [unit_dir / n for n in names]
    try:
        from macip_backup.systemd import install_managed_task

        fake_central = tmp_path / "central.json"
        fake_central.write_text(json.dumps({
            "url": "http://127.0.0.1:1", "agent_id": str(uuid.uuid4()),
            "token_environment": "MACIP_API_TOKEN"}))
        result = install_managed_task(
            task_id,
            tmp_path / "state" / "managed" / "tasks.d" / task_id / "current.json",
            tmp_path / "agent.db",
            fake_central,
            str(uuid.uuid4()),
            # agenda distante — o timer NUNCA dispara durante o teste
            {"kind": "daily", "timezone": "UTC", "hour": 23, "minute": 59},
            activate=True,
        )
        assert result["activated"] is True
        enabled = subprocess.run(
            ["systemctl", "--user", "is-enabled",
             f"macip-backup-managed-{task_id}.timer"],
            capture_output=True, text=True,
        )
        assert "enabled" in enabled.stdout
    finally:
        subprocess.run(
            ["systemctl", "--user", "disable", "--now",
             f"macip-backup-managed-{task_id}.timer"],
            capture_output=True, text=True,
        )
        for path in paths:
            path.unlink(missing_ok=True)
        subprocess.run(["systemctl", "--user", "daemon-reload"],
                       capture_output=True)
