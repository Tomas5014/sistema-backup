"""Integração REAL agente → API central → PostgreSQL descartável.

Requisitos (senão: skip explícito, homologação pendente):
- MACIP_API_TEST_DATABASE_URL — PostgreSQL exclusivo de teste;
- pacote macip_backup_api instalável (api/ no repositório);
- uvicorn acessível em .venv-api.

Fluxo: sobe a API real → provisiona cliente/agente/token por HTTP
administrativo → backup local real enfileira → sync → admin consulta
destinos/tentativas → reenvio confirma 1 execução central.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

REPO = Path(__file__).parents[1]
TEST_DB = os.environ.get("MACIP_API_TEST_DATABASE_URL")
API_VENV_PYTHON = REPO / ".venv-api" / "bin" / "python"
ADMIN_TOKEN = "test-admin-outbox"


def _request(url: str, method: str = "GET", body: dict | None = None, token: str = ADMIN_TOKEN) -> tuple[int, dict]:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method=method,
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.status, json.loads(response.read())


@unittest.skipUnless(
    TEST_DB and API_VENV_PYTHON.exists(),
    "integração API+PG pendente — definir MACIP_API_TEST_DATABASE_URL e .venv-api",
)
class OutboxIntegrationTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tempdir = Path(tempfile.mkdtemp())
        # porta livre sob demanda — um número fixo colide com túneis
        # e outros serviços do host de desenvolvimento
        with __import__("socket").socket() as probe:
            probe.bind(("127.0.0.1", 0))
            cls.port = probe.getsockname()[1]
        env = dict(
            os.environ,
            MACIP_API_DATABASE_URL=TEST_DB,
            MACIP_API_ADMIN_TOKEN=ADMIN_TOKEN,
        )
        cls.api = subprocess.Popen(
            [
                str(API_VENV_PYTHON), "-m", "uvicorn",
                "macip_backup_api.app:app",
                "--host", "127.0.0.1", "--port", str(cls.port),
            ],
            cwd=REPO / "api" / "src",
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                urllib.request.urlopen(
                    f"http://127.0.0.1:{cls.port}/readyz", timeout=2
                )
                break
            except Exception:
                time.sleep(0.5)
        else:
            raise RuntimeError("API de teste não subiu em 30s")

    @classmethod
    def tearDownClass(cls):
        cls.api.terminate()
        cls.api.wait(timeout=10)
        shutil.rmtree(cls.tempdir, ignore_errors=True)

    def setUp(self):
        self.db = self.tempdir / f"agent-{uuid.uuid4().hex[:6]}.db"
        os.environ["MACIP_LOCK_DIR"] = str(self.tempdir / "locks")
        source = self.tempdir / f"dados-{uuid.uuid4().hex[:6]}"
        source.mkdir()
        (source / "arquivo.txt").write_text("integração")
        task = {
            "id": f"task-{uuid.uuid4().hex[:6]}",
            "name": "Integração",
            "client": {"id": "macip-laboratorio", "name": "Lab"},
            "device_id": "dev",
            "sources": [str(source)],
            "destinations": [
                {
                    "id": "local",
                    "name": "Local",
                    "kind": "local",
                    "repository": str(self.tempdir / f"repo-{uuid.uuid4().hex[:6]}"),
                    "password_environment": "MACIP_INT_REPO_PASSWORD",
                }
            ],
        }
        os.environ["MACIP_INT_REPO_PASSWORD"] = "senha-int"
        self.task_file = self.tempdir / f"task-{uuid.uuid4().hex[:6]}.json"
        self.task_file.write_text(json.dumps(task))

    def tearDown(self):
        os.environ.pop("MACIP_LOCK_DIR", None)
        os.environ.pop("MACIP_INT_REPO_PASSWORD", None)
        os.environ.pop("MACIP_API_TOKEN", None)

    def test_end_to_end_backup_sync_consulta(self):
        """Provisiona → backup → sync → admin consulta → reenvio = 1 execução."""
        base = f"http://127.0.0.1:{self.port}"
        _, client = _request(
            f"{base}/api/v1/clients", "POST",
            {"name": f"cli-{uuid.uuid4().hex[:6]}"},
        )
        _, agent = _request(
            f"{base}/api/v1/clients/{client['id']}/agents", "POST",
            {"name": "agente-int"},
        )
        _, credential = _request(
            f"{base}/api/v1/agents/{agent['id']}/credentials", "POST", {},
        )
        os.environ["MACIP_API_TOKEN"] = credential["token"]
        central = self.tempdir / "central.json"
        central.write_text(
            json.dumps(
                {
                    "url": base,
                    "agent_id": agent["id"],
                    "token_environment": "MACIP_API_TOKEN",
                }
            )
        )

        from macip_backup.agent import run_task
        from macip_backup.sync import run_sync

        result = run_task(self.task_file, self.db, central)
        assert result.status.value == "success"

        summary = run_sync(self.db, central)
        assert summary["sent"] == [result.execution_id]

        # admin consulta: destinos e tentativas preservados
        _, executions = _request(f"{base}/api/v1/executions")
        matches = [e for e in executions if e["execution_id"] == result.execution_id]
        assert len(matches) == 1
        _, detail = _request(f"{base}/api/v1/executions/{matches[0]['id']}")
        assert detail["status"] == "success"
        assert detail["destinations"][0]["attempts"]

        # reenvio: a mesma execução — nunca duplicada
        sqlite3.connect(self.db).execute(
            "UPDATE outbox SET status='pending', next_attempt_at=NULL"
        ).connection.commit()
        summary = run_sync(self.db, central)
        assert summary["sent"] == [result.execution_id]
        _, executions = _request(f"{base}/api/v1/executions")
        assert (
            len([e for e in executions if e["execution_id"] == result.execution_id])
            == 1
        )



    def test_sigkill_entre_aceite_e_confirmacao_local(self):
        """API real: subprocesso aceito pela central é morto antes de
        confirmar `sent` — a reexecução reconhece a duplicata."""
        base = f"http://127.0.0.1:{self.port}"
        _, client = _request(
            f"{base}/api/v1/clients", "POST",
            {"name": f"kill-{uuid.uuid4().hex[:6]}"},
        )
        _, agent = _request(
            f"{base}/api/v1/clients/{client['id']}/agents", "POST",
            {"name": "agente-kill"},
        )
        _, credential = _request(
            f"{base}/api/v1/agents/{agent['id']}/credentials", "POST", {},
        )
        os.environ["MACIP_API_TOKEN"] = credential["token"]
        central = self.tempdir / "central-kill.json"
        central.write_text(
            json.dumps(
                {
                    "url": base,
                    "agent_id": agent["id"],
                    "token_environment": "MACIP_API_TOKEN",
                }
            )
        )

        from macip_backup.agent import run_task
        from macip_backup.sync import run_sync

        result = run_task(self.task_file, self.db, central)
        assert result.status.value == "success"

        # O pai segura a escrita no SQLite: o sync do subprocesso consegue
        # ler a fila e POSTar, mas trava no UPDATE do 'sent' — a janela
        # entre aceite remoto e confirmação local é determinística.
        holder = sqlite3.connect(self.db)
        holder.execute("BEGIN IMMEDIATE")
        holder.execute("UPDATE outbox SET attempts = attempts WHERE 0")

        env = dict(
            os.environ,
            PYTHONPATH=str(REPO / "src"),
            MACIP_LOCK_DIR=str(self.tempdir / "locks"),
        )
        import signal
        import subprocess

        process = subprocess.Popen(
            [
                sys.executable, "-c",
                "from macip_backup.sync import run_sync; "
                f"run_sync({str(self.db)!r}, {str(central)!r})",
            ],
            env=env,
        )
        try:
            # espera a execução existir na central (aceite confirmado)
            deadline = time.time() + 60
            present = False
            while time.time() < deadline:
                assert process.poll() is None, "sync morreu antes do aceite"
                try:
                    _, executions = _request(
                        f"{base}/api/v1/executions?status=success"
                    )
                    present = any(
                        e["execution_id"] == result.execution_id
                        for e in executions
                    )
                except Exception:
                    present = False
                if present:
                    break
                time.sleep(0.5)
            assert present, "central nunca recebeu a execução"
            process.kill()                      # SIGKILL na janela
            process.wait(timeout=10)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            holder.rollback()
            holder.close()

        # item continua reenviável, identidade intacta
        check = sqlite3.connect(self.db)
        rows = check.execute(
            "SELECT status, payload, endpoint, agent_id FROM outbox"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "pending", rows
        assert rows[0][2] == base and rows[0][3] == agent["id"]

        summary = run_sync(self.db, central)
        assert summary["sent"] == [result.execution_id]
        _, executions = _request(f"{base}/api/v1/executions")
        assert (
            len([e for e in executions if e["execution_id"] == result.execution_id])
            == 1
        )

if __name__ == "__main__":
    unittest.main()
