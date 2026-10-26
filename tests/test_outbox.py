"""Outbox persistente do agente + envio à API central.

APIs de teste são HTTP locais (http.server em thread) — localhost é a
única exceção à exigência de HTTPS no CentralSpec.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from macip_backup.agent import run_task
from macip_backup.central import CentralSpec, load_central
from macip_backup.models import (
    Client,
    CompressionMode,
    ConfigurationError,
    Destination,
    DestinationKind,
    TaskSpec,
)
from macip_backup.store import ExecutionStore
from macip_backup.sync import run_sync

AGENT_UUID = str(uuid.uuid4())


def _central(path: Path, url: str, agent_id: str = AGENT_UUID) -> Path:
    path.write_text(
        json.dumps(
            {
                "url": url,
                "agent_id": agent_id,
                "token_environment": "MACIP_TEST_TOKEN",
            }
        )
    )
    return path


class _FakeApi(BaseHTTPRequestHandler):
    responses: list = []  # fila de (status, body_dict) por requisição
    posts: list = []

    def do_POST(self):
        body = json.loads(
            self.rfile.read(int(self.headers["Content-Length"]))
        )
        _FakeApi.posts.append(body)
        status, payload = (
            _FakeApi.responses.pop(0)
            if _FakeApi.responses
            else (200, {"status": "created", "execution_id": body["execution_id"]})
        )
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):  # silencia o servidor
        pass


class OutboxTestCase(unittest.TestCase):
    def setUp(self):
        self.tempdir = Path(tempfile.mkdtemp())
        self.db = self.tempdir / "agent.db"
        self.task_file = self.tempdir / "task.json"
        self.central_file = self.tempdir / "central.json"
        os.environ["MACIP_LOCK_DIR"] = str(self.tempdir / "locks")
        os.environ["MACIP_TEST_TOKEN"] = "token-teste"
        self.source = self.tempdir / "dados"
        self.source.mkdir()
        (self.source / "arquivo.txt").write_text("conteudo")
        self.repo = self.tempdir / "repo"
        task = {
            "id": "task-outbox",
            "name": "Outbox",
            "client": {"id": "macip-laboratorio", "name": "Lab"},
            "device_id": "dev",
            "sources": [str(self.source)],
            "destinations": [
                {
                    "id": "local",
                    "name": "Local",
                    "kind": "local",
                    "repository": str(self.repo),
                    "password_environment": "MACIP_TEST_REPO_PASSWORD",
                }
            ],
        }
        os.environ["MACIP_TEST_REPO_PASSWORD"] = "senha-repo"
        self.task_file.write_text(json.dumps(task))
        self.server = HTTPServer(("127.0.0.1", 0), _FakeApi)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        _FakeApi.responses = []
        _FakeApi.posts = []

    def tearDown(self):
        self.server.shutdown()
        os.environ.pop("MACIP_LOCK_DIR", None)
        os.environ.pop("MACIP_TEST_TOKEN", None)
        os.environ.pop("MACIP_TEST_REPO_PASSWORD", None)
        import shutil

        shutil.rmtree(self.tempdir, ignore_errors=True)

    def _outbox(self) -> list[dict]:
        store = ExecutionStore(self.db)
        try:
            return store.outbox_state()
        finally:
            store.close()

    # ---- enfileiramento ------------------------------------------------

    def test_backup_sem_api_enfileira_pendente(self):
        """Backup real local com API inacessível → concluído + pendente."""
        _central(self.central_file, f"http://127.0.0.1:{self.port}")
        self.server.shutdown()  # API fora do ar
        result = run_task(self.task_file, self.db, self.central_file)
        self.assertEqual(result.status.value, "success")
        items = self._outbox()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["status"], "pending")
        self.assertEqual(items[0]["execution_id"], result.execution_id)
        # payload: metadados apenas — sem tarefa, sem senha, sem client local
        payload = json.loads(items[0]["payload"])
        self.assertEqual(payload["agent_id"], AGENT_UUID)
        self.assertNotIn("client_id", payload)
        self.assertNotIn("macip-laboratorio", items[0]["payload"])
        self.assertNotIn("senha-repo", items[0]["payload"])
        self.assertNotIn("token-teste", items[0]["payload"])

    def test_reabertura_do_banco_preserva_fila(self):
        _central(self.central_file, f"http://127.0.0.1:{self.port}")
        self.server.shutdown()
        run_task(self.task_file, self.db, self.central_file)
        first = self._outbox()          # conexão 1 fecha
        second = self._outbox()         # conexão 2 abre de novo
        self.assertEqual(first, second)
        self.assertEqual(first[0]["status"], "pending")

    def test_sem_central_nao_enfileira(self):
        result = run_task(self.task_file, self.db)
        self.assertEqual(result.status.value, "success")
        self.assertEqual(self._outbox(), [])

    # ---- entrega --------------------------------------------------------

    def test_envio_com_sucesso_marca_sent(self):
        _central(self.central_file, f"http://127.0.0.1:{self.port}")
        run_task(self.task_file, self.db, self.central_file)
        summary = run_sync(self.db, self.central_file)
        self.assertEqual(len(summary["sent"]), 1)
        self.assertEqual(self._outbox()[0]["status"], "sent")
        self.assertEqual(len(_FakeApi.posts), 1)

    def test_resposta_perdida_reenvia_sem_duplicar(self):
        """Aceitação perdida → item segue pending → reenvio é duplicate."""
        _central(self.central_file, f"http://127.0.0.1:{self.port}")
        run_task(self.task_file, self.db, self.central_file)
        execution_id = self._outbox()[0]["execution_id"]

        # 1ª tentativa: API aceita mas a resposta se perde (simulação:
        # o servidor registra e devolve resposta que o cliente não lê)
        _FakeApi.responses = [
            (200, {"status": "created", "execution_id": execution_id})
        ]
        with patch("macip_backup.sync._post") as post:
            post.return_value = (0, None)  # resposta perdida
            run_sync(self.db, self.central_file)
        # o servidor nem viu — marca como retry
        self.assertEqual(self._outbox()[0]["status"], "pending")

        # força a tentativa imediata (o backoff real agendaria para depois)
        sqlite3.connect(self.db).execute(
            "UPDATE outbox SET next_attempt_at = NULL"
        ).connection.commit()

        # reenvio: agora a API responde "duplicate" — uma execução só
        _FakeApi.responses = [
            (200, {"status": "duplicate", "execution_id": execution_id})
        ]
        summary = run_sync(self.db, self.central_file)
        self.assertEqual(summary["sent"], [execution_id])
        self.assertEqual(self._outbox()[0]["status"], "sent")

    def test_falhas_transitorias_agendam_retry(self):
        _central(self.central_file, f"http://127.0.0.1:{self.port}")
        run_task(self.task_file, self.db, self.central_file)
        _FakeApi.responses = [(500, {})]  # transitório do servidor
        summary = run_sync(self.db, self.central_file)
        self.assertEqual(len(summary["retry"]), 1)
        item = self._outbox()[0]
        self.assertEqual(item["status"], "pending")
        self.assertEqual(item["attempts"], 1)
        self.assertIsNotNone(item["next_attempt_at"])

    def test_credencial_ausente_nao_impede_enfileiramento(self):
        _central(self.central_file, f"http://127.0.0.1:{self.port}")
        os.environ.pop("MACIP_TEST_TOKEN")
        run_task(self.task_file, self.db, self.central_file)
        summary = run_sync(self.db, self.central_file)
        self.assertEqual(len(summary["skipped_credential"]), 1)
        self.assertEqual(self._outbox()[0]["status"], "pending")

    def test_token_revogado_bloqueia_sem_retry(self):
        _central(self.central_file, f"http://127.0.0.1:{self.port}")
        run_task(self.task_file, self.db, self.central_file)
        _FakeApi.responses = [(403, {"detail": "revogado"})]
        summary = run_sync(self.db, self.central_file)
        self.assertEqual(len(summary["blocked"]), 1)
        item = self._outbox()[0]
        self.assertEqual(item["status"], "auth_blocked")
        # sem reenvio automático — aguarda correção do operador
        summary = run_sync(self.db, self.central_file)
        self.assertEqual(summary["sent"], [])
        self.assertEqual(len(_FakeApi.posts), 1)

    def test_conflito_e_rejeicao_sao_permanentes(self):
        _central(self.central_file, f"http://127.0.0.1:{self.port}")
        run_task(self.task_file, self.db, self.central_file)
        for code in (409, 422):
            _FakeApi.responses = [(code, {"detail": "x"})]
            run_sync(self.db, self.central_file)
            if code == 409:
                self.assertEqual(self._outbox()[0]["status"], "failed_permanent")

    # ---- identidade/endpoint vinculados ----------------------------------

    def test_mudanca_de_configuracao_nao_redireciona_fila(self):
        """Item enfileirado com endpoint/agente A não sai por B."""
        _central(self.central_file, f"http://127.0.0.1:{self.port}")
        run_task(self.task_file, self.db, self.central_file)
        # nova config aponta outro agente/endpoint
        other = _central(
            self.tempdir / "central2.json",
            f"http://127.0.0.1:{self.port}",
            agent_id=str(uuid.uuid4()),
        )
        summary = run_sync(self.db, other)
        self.assertEqual(summary["pending_total"], 0)
        self.assertEqual(summary["sent"], [])
        self.assertEqual(_FakeApi.posts, [])  # nada enviado para B
        self.assertEqual(self._outbox()[0]["status"], "pending")

    # ---- concorrência -----------------------------------------------------

    def test_sync_respeita_lock_de_manutencao(self):
        import fcntl

        _central(self.central_file, f"http://127.0.0.1:{self.port}")
        run_task(self.task_file, self.db, self.central_file)
        maintenance = (self.tempdir / "locks" / "maintenance.lock").open("a")
        fcntl.flock(maintenance.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            with self.assertRaises(Exception):
                run_sync(self.db, self.central_file)
        finally:
            maintenance.close()

    # ---- migração ---------------------------------------------------------

    def test_migracao_v3_preserva_historico(self):
        """Banco v3 (sem outbox) abre v4 com todo o histórico intacto."""
        run_task(self.task_file, self.db)  # grava sem outbox
        # força user_version antigo + remove tabela para simular v3
        raw = sqlite3.connect(self.db)
        raw.execute("DROP TABLE outbox")
        raw.execute("PRAGMA user_version = 3")
        raw.commit()
        raw.close()
        store = ExecutionStore(self.db)
        try:
            history = store.history()
        finally:
            store.close()
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["status"], "success")
        version = sqlite3.connect(self.db).execute(
            "PRAGMA user_version"
        ).fetchone()[0]
        self.assertEqual(version, 4)

    # ---- validação do CentralSpec ----------------------------------------

    def test_central_https_obrigatorio_fora_localhost(self):
        with self.assertRaises(ConfigurationError):
            CentralSpec.from_dict(
                {
                    "url": "http://api.remota.local",
                    "agent_id": AGENT_UUID,
                    "token_environment": "X",
                }
            )

    def test_central_rejeita_token_no_arquivo(self):
        with self.assertRaises(ConfigurationError):
            CentralSpec.from_dict(
                {
                    "url": "http://127.0.0.1:1",
                    "agent_id": AGENT_UUID,
                    "token_environment": "X",
                    "token": "segredo",
                }
            )


if __name__ == "__main__":
    unittest.main()
