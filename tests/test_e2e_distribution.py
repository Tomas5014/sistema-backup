"""Prova INTEGRADA de distribuição — ambiente descartável.

Dois testes:

- ``test_fluxo_completo_distribuicao`` — API real (uvicorn) + PG real +
  `pull-tasks`/`agent`/`sync` como subprocessos em HOME descartável.
  Aqui a reconciliação roda em MODO PREPARAÇÃO (`--no-activate`):
  escreve envelope, configuração candidata e units — o systemd não
  conhece units de um HOME falso. O backup Restic real executa a
  configuração candidata imutável `config-N.json` diretamente.

- ``test_timer_real_dispara_backup`` — systemd --user REAL: o timer
  DISPARA o backup sozinho (sem executar `agent` manualmente), com
  calendário do minuto seguinte, EnvironmentFile descartável e
  limpeza garantida das units criadas.

Sem MACIP_API_TEST_DATABASE_URL/restic → skip explícito; sem
`systemctl --user` funcional → o segundo teste falha claramente.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
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
requires_systemd = pytest.mark.skipif(
    shutil.which("systemctl") is None,
    reason="systemctl ausente — prova de timer impossível",
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
def api():
    """API real com PostgreSQL descartável — dados limpos por módulo."""
    db_name = TEST_URL.rsplit("/", 1)[-1].split("?")[0].lower()
    assert "test" in db_name, "banco deve parecer descartável"
    os.environ["MACIP_API_DATABASE_URL"] = TEST_URL
    os.environ["MACIP_API_ADMIN_TOKEN"] = ADMIN_TOKEN
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
        import time as _time

        deadline = _time.monotonic() + 10
        port = None
        while _time.monotonic() < deadline:
            for sock in getattr(thread.server, "servers", []):
                port = sock.sockets[0].getsockname()[1]
                break
            if port:
                break
            _time.sleep(0.05)
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


def _cli(home: Path, *args: str, extra_env: dict | None = None,
         expect_ok: bool = True):
    """Subprocesso REAL da CLI do agente."""
    env = {
        "PYTHONPATH": str(REPO_ROOT / "src"),
        "HOME": str(home),
        "MACIP_STATE": str(home / "state"),
        "MACIP_CACHE": str(home / "cache"),
        "MACIP_LOCK_DIR": str(home / "locks"),
        "MACIP_CONFIG": str(home / "config"),
        "PATH": os.environ.get("PATH", ""),
        **(extra_env or {}),
    }
    home.mkdir(parents=True, exist_ok=True)
    (home / "state").mkdir(parents=True, exist_ok=True)
    (home / "config").mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [sys.executable, "-m", "macip_backup.cli", *args],
        capture_output=True, text=True, env=env, timeout=180,
        cwd=str(home),
    )
    if expect_ok:
        assert result.returncode == 0, (
            f"CLI {args[0]} falhou ({result.returncode}): "
            f"{result.stderr[-2000:]}"
        )
    return result


def _json_out(result) -> dict:
    return json.loads(result.stdout)


def _setup_central_task(base_url, tmp_path, schedule):
    """cliente + agente + credencial + tarefa descartável (admin real)."""
    client = _admin(base_url, "POST", "/api/v1/clients",
                    json={"name": f"Cliente E2E {uuid.uuid4().hex[:8]}"})
    agent = _admin(base_url, "POST",
                   f"/api/v1/clients/{client['id']}/agents",
                   json={"name": f"vm-{uuid.uuid4().hex[:8]}"})
    credential = _admin(
        base_url, "POST",
        f"/api/v1/agents/{agent['id']}/credentials",
        json={"label": "e2e"})
    source_dir = tmp_path / "origem"
    source_dir.mkdir(parents=True, exist_ok=True)
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
            "schedule": schedule,
        })
    return client, agent, credential, task, repo_dir


def test_fluxo_completo_distribuicao(api, tmp_path):
    """cadastro → publish → pull PREPARAÇÃO → units+config candidata →
    backup Restic da configuração aplicada candidata → sync → resultado.
    A confirmação `applied` só aparece na prova com ativação real."""
    base_url, _seed = api
    home = tmp_path / "agent-home"
    store = tmp_path / "state" / "agent.db"
    central_path = tmp_path / "state" / "central.json"
    central_path.parent.mkdir(parents=True)

    client, agent, credential, task, repo_dir = _setup_central_task(
        base_url, tmp_path,
        {"kind": "daily", "timezone": "UTC", "hour": 3, "minute": 30},
    )
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

    # pull ANTES de publicar — nada desejado
    dry = _json_out(_cli(
        home, "pull-tasks", "--central", str(central_path),
        "--store", str(store), extra_env=env))
    assert dry["desired_total"] == 0

    # publicar rev 1 + pull em modo PREPARAÇÃO
    _admin(base_url, "POST", f"/api/v1/tasks/{task_id}/publish",
           json={"revision": 1, "expected_generation": 0})
    pull = _json_out(_cli(
        home, "pull-tasks", "--central", str(central_path),
        "--store", str(store), "--no-activate", extra_env=env))
    assert pull["prepared"][0]["task_id"] == task_id

    bond = pull["bond"]
    managed = home / "state" / "managed" / bond / "tasks.d" / task_id
    # preparação: configuração candidata e units gravadas; current.json
    # NÃO foi publicado e NADA foi confirmado
    assert (managed / "config-1.json").is_file()
    assert not (managed / "current.json").exists()
    units = home / "config" and home  # units vão para HOME/.config
    service = (home / ".config" / "systemd" / "user"
               / f"macip-backup-managed-{task_id}.service")
    timer = service.with_suffix(".timer")
    assert service.is_file() and timer.is_file()
    assert f"managed-task={task_id}" in service.read_text()
    assert "managed-bond=" in service.read_text()
    assert "OnCalendar=*-*-* 03:30:00 UTC" in timer.read_text()
    assert "Persistent=true" in timer.read_text()
    # API: sem confirmação — preparação não anuncia aplicação
    detail = _admin(base_url, "GET", f"/api/v1/tasks/{task_id}")
    assert detail["distribution"]["confirmed"] is None
    journal = (managed / "journal.jsonl").read_text()
    assert '"step": "prepared"' in journal
    assert '"step": "applied"' not in journal

    # backup RESTIC REAL sobre a configuração candidata (mesmo conteúdo
    # que o timer executará quando ativado — arquivo imutável)
    run = _json_out(_cli(
        home, "agent", str(managed / "config-1.json"),
        "--store", str(store), "--central", str(central_path),
        extra_env=env))
    assert run["status"] == "success"
    snapshot = run["destinations"][0]["snapshot_id"]
    assert snapshot and (repo_dir / "data").is_dir()

    # sync entrega o resultado à central
    sent = _json_out(_cli(
        home, "sync", "--central", str(central_path),
        "--store", str(store), extra_env=env))
    assert sent["sent"]
    executions = _admin(base_url, "GET", "/api/v1/executions?limit=10")
    ours = [e for e in executions if e["task_id"] == task_id]
    assert ours and ours[0]["status"] == "success"

    # edição → rev2 → publicação gen 2 → preparação atualiza candidata
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
    pull2 = _json_out(_cli(
        home, "pull-tasks", "--central", str(central_path),
        "--store", str(store), "--no-activate", extra_env=env))
    assert pull2["prepared"][0]["generation"] == 2
    doc2 = json.loads((managed / "config-2.json").read_text())
    assert doc2["name"] == "backup-descartavel-v2"
    assert (managed / "gen-1.json").is_file()  # anterior preservada
    assert "04:00" in timer.read_text()
    detail = _admin(base_url, "GET", f"/api/v1/tasks/{task_id}")
    assert detail["distribution"]["generation"] == 2
    assert detail["distribution"]["confirmed"] is None

    # suspensão em preparação: nenhum efeito real ainda
    _admin(base_url, "POST", f"/api/v1/tasks/{task_id}/suspend",
           json={"expected_generation": 2})
    pull3 = _json_out(_cli(
        home, "pull-tasks", "--central", str(central_path),
        "--store", str(store), "--no-activate", extra_env=env))
    assert pull3["prepared"]
    detail = _admin(base_url, "GET", f"/api/v1/tasks/{task_id}")
    assert detail["distribution"]["desired_action"] == "suspend"
    # snapshots do backup real preservados e listáveis
    snaps = _json_out(_cli(
        home, "snapshots", str(managed / "config-2.json"),
        "local", extra_env=env))
    assert any(
        s["id"].startswith(snapshot[:8]) or s["id"] == snapshot
        for s in snaps["snapshots"]
    )


@requires_systemd
def test_timer_real_dispara_backup(api, tmp_path):
    """systemd --user REAL: publicar → pull-tasks ATIVA o timer → o
    TIMER dispara o backup Restic → resultado → central mostra
    aplicada → rev2/suspensão pelo gerenciador real."""
    base_url, _seed = api
    unit_dir = Path.home() / ".config" / "systemd" / "user"

    # agenda: próximo minuto UTC — o timer dispara sem intervenção
    import datetime as _dt
    fire_at = _dt.datetime.now(_dt.UTC) + _dt.timedelta(minutes=2)
    client, agent, credential, task, repo_dir = _setup_central_task(
        base_url, tmp_path,
        {"kind": "daily", "timezone": "UTC",
         "hour": fire_at.hour, "minute": fire_at.minute},
    )
    task_id = task["id"]

    home = tmp_path / "agent-home"
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    config_dir = tmp_path / "agentcfg"
    config_dir.mkdir()
    store = state_dir / "agent.db"
    central_path = state_dir / "central.json"
    central_path.write_text(json.dumps({
        "url": base_url,
        "agent_id": agent["id"],
        "token_environment": "MACIP_API_TOKEN",
    }))
    # EnvironmentFile que as units lerão
    (config_dir / "agent.env").write_text(
        "\n".join([
            f"MACIP_API_TOKEN={credential['token']}",
            "MACIP_RESTIC_LOCAL=senha-restic-e2e",
            f"MACIP_STATE={state_dir}",
            f"MACIP_LOCK_DIR={state_dir}/locks",
            f"MACIP_CONFIG={config_dir}",
        ]) + "\n"
    )
    env = {
        "MACIP_API_TOKEN": credential["token"],
        "MACIP_RESTIC_LOCAL": "senha-restic-e2e",
    }
    # pull-tasks roda com MACIP_CONFIG apontando o agent.env
    # descartável — XDG_RUNTIME_DIR/DBUS vêm do ambiente do usuário
    # (necessários para `systemctl --user`)
    pull_env = dict(os.environ)
    pull_env.update({
        "PYTHONPATH": str(REPO_ROOT / "src"),
        "HOME": str(Path.home()),
        "MACIP_CONFIG": str(config_dir),
        "MACIP_STATE": str(state_dir),
        "MACIP_LOCK_DIR": str(state_dir / "locks"),
        "MACIP_API_TOKEN": credential["token"],
    })

    service = unit_dir / f"macip-backup-managed-{task_id}.service"
    timer = service.with_suffix(".timer")
    try:
        _admin(base_url, "POST", f"/api/v1/tasks/{task_id}/publish",
               json={"revision": 1, "expected_generation": 0})
        pull = subprocess.run(
            [sys.executable, "-m", "macip_backup.cli", "pull-tasks",
             "--central", str(central_path), "--store", str(store)],
            capture_output=True, text=True, env=pull_env, timeout=120,
        )
        assert pull.returncode == 0, pull.stderr[-2000:]
        summary = json.loads(pull.stdout)
        assert summary["applied"][0]["result"] == "applied", pull.stdout

        enabled = subprocess.run(
            ["systemctl", "--user", "is-enabled",
             f"macip-backup-managed-{task_id}.timer"],
            capture_output=True, text=True,
        )
        assert "enabled" in enabled.stdout

        # espera o TIMER disparar o service → execução TERMINADA no
        # SQLite (deadline controlado — nenhuma execução manual de
        # `agent`)
        import sqlite3 as _sql
        deadline = time.monotonic() + 200
        done = False
        rows = []
        while time.monotonic() < deadline and not done:
            if store.exists():
                conn = _sql.connect(store)
                rows = conn.execute(
                    "SELECT status FROM executions"
                ).fetchall()
                conn.close()
                done = bool(rows) and all(
                    r[0] != "running" for r in rows
                )
            if not done:
                time.sleep(2)
        if not done:
            unit_status = subprocess.run(
                ["systemctl", "--user", "status", "--no-pager",
                 f"macip-backup-managed-{task_id}.service"],
                capture_output=True, text=True,
            )
            logs = subprocess.run(
                ["journalctl", "--user", "--no-pager", "-n", "30", "-u",
                 f"macip-backup-managed-{task_id}.service"],
                capture_output=True, text=True,
            )
            raise AssertionError(
                f"timer não concluiu o backup em 200s — "
                f"execuções={rows} status={unit_status.stdout[-800:]} "
                f"journal={logs.stdout[-1500:]}"
            )

        # resultado segue pela outbox real → central
        sync = subprocess.run(
            [sys.executable, "-m", "macip_backup.cli", "sync",
             "--central", str(central_path), "--store", str(store)],
            capture_output=True, text=True, env=pull_env, timeout=60,
        )
        assert sync.returncode == 0, sync.stderr[-1000:]
        sent_info = json.loads(sync.stdout)
        import sqlite3 as _sql2
        conn = _sql2.connect(store)
        rows_all = conn.execute("SELECT status FROM executions").fetchall()
        outbox = conn.execute(
            "SELECT status, attempts, last_error FROM outbox"
        ).fetchall()
        conn.close()
        executions = _admin(base_url, "GET", "/api/v1/executions?limit=10")
        ours = [e for e in executions if e["task_id"] == task_id]
        assert ours and ours[0]["status"] == "success", (
            f"execuções locais={rows_all} outbox={outbox} "
            f"sync={sent_info} remotas={executions}"
        )

        # confirmação: revisão aplicada + last_applied visíveis
        detail = _admin(base_url, "GET", f"/api/v1/tasks/{task_id}")
        dist = detail["distribution"]
        assert dist["confirmed"]["status"] == "applied"
        assert dist["confirmed"]["applied_revision"] == 1
        assert dist["last_applied"]["revision"] == 1 or \
            dist["confirmed"]["applied_revision"] == 1

        # suspensão pelo gerenciador REAL
        _admin(base_url, "POST", f"/api/v1/tasks/{task_id}/suspend",
               json={"expected_generation": 1})
        pull2 = subprocess.run(
            [sys.executable, "-m", "macip_backup.cli", "pull-tasks",
             "--central", str(central_path), "--store", str(store)],
            capture_output=True, text=True, env=pull_env, timeout=120,
        )
        assert pull2.returncode == 0, pull2.stderr[-2000:]
        assert json.loads(pull2.stdout)["suspended"]
        disabled = subprocess.run(
            ["systemctl", "--user", "is-enabled",
             f"macip-backup-managed-{task_id}.timer"],
            capture_output=True, text=True,
        )
        assert "disabled" in disabled.stdout
        # configuração e snapshots preservados
        bond = json.loads(pull2.stdout)["bond"]
        managed = state_dir / "managed" / bond / "tasks.d" / task_id
        assert (managed / "current.json").is_file()
    finally:
        subprocess.run(
            ["systemctl", "--user", "disable", "--now",
             f"macip-backup-managed-{task_id}.timer"],
            capture_output=True, text=True,
        )
        service.unlink(missing_ok=True)
        timer.unlink(missing_ok=True)
        subprocess.run(["systemctl", "--user", "daemon-reload"],
                       capture_output=True)
