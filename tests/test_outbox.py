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
import time
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace
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
from macip_backup.orchestrator import ExecutionStatus

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
    accepted_execution_ids: set[str] = set()
    disconnect_after_accept = 0
    delay_before_close = 0.0

    def do_POST(self):
        body = json.loads(
            self.rfile.read(int(self.headers["Content-Length"]))
        )
        _FakeApi.posts.append(body)
        accepted = body["execution_id"]
        if _FakeApi.delay_before_close:
            time.sleep(_FakeApi.delay_before_close)
            self.close_connection = True
            return
        if _FakeApi.disconnect_after_accept:
            _FakeApi.disconnect_after_accept -= 1
            _FakeApi.accepted_execution_ids.add(accepted)
            self.close_connection = True
            return
        status, payload = (
            _FakeApi.responses.pop(0)
            if _FakeApi.responses
            else (
                200,
                {
                    "status": "duplicate" if accepted in _FakeApi.accepted_execution_ids else "created",
                    "execution_id": accepted,
                },
            )
        )
        if status in (200, 201) and isinstance(payload, dict) and payload.get("status") in {
            "created", "duplicate",
        }:
            _FakeApi.accepted_execution_ids.add(accepted)
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
        _FakeApi.accepted_execution_ids = set()
        _FakeApi.disconnect_after_accept = 0
        _FakeApi.delay_before_close = 0.0

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
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

        # A API realmente recebe e aceita, mas encerra a conexão antes da
        # confirmação local. O cliente deve manter a entrega reenviável.
        _FakeApi.disconnect_after_accept = 1
        summary = run_sync(self.db, self.central_file)
        self.assertEqual(summary["retry"], [execution_id])
        self.assertEqual(len(_FakeApi.posts), 1)
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
        self.assertEqual(len(_FakeApi.posts), 2)
        self.assertEqual(_FakeApi.accepted_execution_ids, {execution_id})

    def test_interrupcao_apos_aceite_remoto_mantem_item_reenviavel(self):
        """Interrupção entre POST aceito e confirmação SQLite não marca sent."""
        _central(self.central_file, f"http://127.0.0.1:{self.port}")
        run_task(self.task_file, self.db, self.central_file)
        execution_id = self._outbox()[0]["execution_id"]
        from macip_backup.store import ExecutionStore as Store

        with patch.object(Store, "outbox_sent", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                run_sync(self.db, self.central_file)
        self.assertEqual(len(_FakeApi.posts), 1)
        self.assertEqual(self._outbox()[0]["status"], "pending")
        summary = run_sync(self.db, self.central_file)
        self.assertEqual(summary["sent"], [execution_id])
        self.assertEqual(len(_FakeApi.posts), 2)
        self.assertEqual(_FakeApi.accepted_execution_ids, {execution_id})

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

    def test_rede_json_invalido_e_continuidade_nao_aborta_rodada(self):
        _central(self.central_file, f"http://127.0.0.1:{self.port}")
        run_task(self.task_file, self.db, self.central_file)
        run_task(self.task_file, self.db, self.central_file)
        with patch(
            "macip_backup.sync._post",
            side_effect=[(0, None), (200, ["não é objeto"])],
        ):
            summary = run_sync(self.db, self.central_file)
        self.assertEqual(len(summary["retry"]), 2)
        self.assertEqual([item["attempts"] for item in self._outbox()], [1, 1])

    def test_json_invalido_da_resposta_nunca_confirma_entrega(self):
        _central(self.central_file, f"http://127.0.0.1:{self.port}")
        run_task(self.task_file, self.db, self.central_file)

        class InvalidJsonResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b"{invalido"

        with patch("macip_backup.sync._opener.open", return_value=InvalidJsonResponse()):
            summary = run_sync(self.db, self.central_file)
        self.assertEqual(len(summary["retry"]), 1)
        self.assertEqual(self._outbox()[0]["status"], "pending")

    def test_conexao_recusada_registra_tentativa_sem_expor_token(self):
        # Um endpoint sem listener produz URLError tratado dentro de _post.
        import socket

        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        _central(self.central_file, f"http://127.0.0.1:{port}")
        run_task(self.task_file, self.db, self.central_file)
        summary = run_sync(self.db, self.central_file)
        item = self._outbox()[0]
        self.assertEqual(len(summary["retry"]), 1)
        self.assertEqual(item["attempts"], 1)
        self.assertNotIn(os.environ["MACIP_TEST_TOKEN"], item["last_error"])

    def test_timeout_real_registra_tentativa(self):
        _central(self.central_file, f"http://127.0.0.1:{self.port}")
        central = json.loads(self.central_file.read_text())
        central["connect_timeout_seconds"] = 1
        central["request_timeout_seconds"] = 1
        self.central_file.write_text(json.dumps(central))
        run_task(self.task_file, self.db, self.central_file)
        _FakeApi.delay_before_close = 1.2
        summary = run_sync(self.db, self.central_file)
        self.assertEqual(len(summary["retry"]), 1)
        self.assertEqual(self._outbox()[0]["attempts"], 1)

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

    def test_conflito_rejeicao_e_payload_invalido_sao_permanentes(self):
        _central(self.central_file, f"http://127.0.0.1:{self.port}")
        for _ in range(3):
            run_task(self.task_file, self.db, self.central_file)
        _FakeApi.responses = [(409, {"detail": "x"}), (413, {"detail": "x"}), (422, {"detail": "x"})]
        summary = run_sync(self.db, self.central_file)
        self.assertEqual(len(summary["blocked"]), 3)
        self.assertEqual(len(_FakeApi.posts), 3)
        self.assertEqual(
            [item["status"] for item in self._outbox()],
            ["failed_permanent", "failed_permanent", "failed_permanent"],
        )

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

    def test_orfa_mantem_vinculo_a_com_configuracao_b_ou_ausente(self):
        central_a = _central(self.central_file, f"http://127.0.0.1:{self.port}")
        task = TaskSpec.from_dict(json.loads(self.task_file.read_text()))
        spec_a = load_central(central_a)
        store = ExecutionStore(self.db, outbox=(spec_a.url, spec_a.agent_id))
        store.record_start(task, "orfa-a", "2026-10-26T10:00:00+00:00")
        store.close()
        other = _central(
            self.tempdir / "central-b.json", f"http://127.0.0.1:{self.port}", str(uuid.uuid4())
        )
        spec_b = load_central(other)
        # Recuperação iniciada com B não pode redirecionar a órfã A.
        store = ExecutionStore(self.db, outbox=(spec_b.url, spec_b.agent_id))
        store.mark_stale_interrupted(task.id, "2026-10-26T10:01:00+00:00")
        item = store.outbox_state()[0]
        store.close()
        self.assertEqual(item["endpoint"], spec_a.url)
        self.assertEqual(item["agent_id"], spec_a.agent_id)
        self.assertEqual(run_sync(self.db, other)["sent"], [])
        self.assertEqual(run_sync(self.db, central_a)["sent"], ["orfa-a"])

    def test_execucao_sem_central_nao_ganha_destino_posterior(self):
        task = TaskSpec.from_dict(json.loads(self.task_file.read_text()))
        store = ExecutionStore(self.db)
        store.record_start(task, "sem-central", "2026-10-26T10:00:00+00:00")
        store.mark_stale_interrupted(task.id, "2026-10-26T10:01:00+00:00")
        self.assertEqual(store.outbox_state(), [])
        store.close()

    def test_finalizacao_congela_historico_e_payload(self):
        _central(self.central_file, f"http://127.0.0.1:{self.port}")
        task = TaskSpec.from_dict(json.loads(self.task_file.read_text()))
        spec = load_central(self.central_file)
        store = ExecutionStore(self.db, outbox=(spec.url, spec.agent_id))
        store.record_start(task, "cancelada", "2026-10-26T10:00:00+00:00")
        result = SimpleNamespace(
            execution_id="cancelada",
            status=ExecutionStatus.SUCCESS,
            finished_at_utc="2026-10-26T10:01:00+00:00",
            destinations=(),
        )
        store.record_finish(result, interruption_detail="sinal 15")
        frozen = store.outbox_state()[0]["payload"]
        store.mark_interrupted("cancelada", "2026-10-26T10:02:00+00:00", "tarde")
        history = store.history()[0]
        self.assertEqual(history["status"], "interrupted")
        self.assertEqual(history["finished_at_utc"], "2026-10-26T10:01:00+00:00")
        self.assertEqual(json.loads(frozen)["status"], "interrupted")
        self.assertEqual(store.outbox_state()[0]["payload"], frozen)
        store.close()

    def test_falha_ao_enfileirar_desfaz_conclusao(self):
        task = TaskSpec.from_dict(json.loads(self.task_file.read_text()))
        store = ExecutionStore(self.db, outbox=("http://127.0.0.1:1", AGENT_UUID))
        store.record_start(task, "rollback", "2026-10-26T10:00:00+00:00")
        result = SimpleNamespace(
            execution_id="rollback", status=ExecutionStatus.SUCCESS,
            finished_at_utc="2026-10-26T10:01:00+00:00", destinations=(),
        )
        with patch.object(store, "_enqueue_locked", side_effect=sqlite3.OperationalError("x")):
            with self.assertRaises(sqlite3.OperationalError):
                store.record_finish(result)
        self.assertEqual(store.history()[0]["status"], "running")
        store.close()

    def test_rearm_exige_vinculo_e_preserva_tentativas(self):
        _central(self.central_file, f"http://127.0.0.1:{self.port}")
        run_task(self.task_file, self.db, self.central_file)
        _FakeApi.responses = [(403, {})]
        run_sync(self.db, self.central_file)
        before = self._outbox()[0]
        store = ExecutionStore(self.db)
        other = ("http://127.0.0.1:9", str(uuid.uuid4()))
        self.assertEqual(store.rearm_outbox(*other), [])
        spec = load_central(self.central_file)
        self.assertEqual(store.rearm_outbox(spec.url, spec.agent_id), [before["execution_id"]])
        after = store.outbox_state()[0]
        store.close()
        self.assertEqual(after["status"], "pending")
        self.assertEqual(after["attempts"], before["attempts"])
        self.assertEqual(after["payload"], before["payload"])
        self.assertEqual(after["last_error"], before["last_error"])

    def test_rearm_cli_exige_central_correspondente(self):
        import io

        _central(self.central_file, f"http://127.0.0.1:{self.port}")
        run_task(self.task_file, self.db, self.central_file)
        _FakeApi.responses = [(401, {})]
        run_sync(self.db, self.central_file)
        from macip_backup.cli import main

        stdout = io.StringIO()
        with patch("sys.argv", [
            "macip-backup-poc", "outbox-rearm", "--store", str(self.db),
            "--central", str(self.central_file),
        ]), patch("sys.stdout", stdout):
            self.assertEqual(main(), 0)
        self.assertEqual(len(json.loads(stdout.getvalue())["rearmed"]), 1)
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

    def test_migracao_v4_preserva_historico_e_outbox(self):
        """Banco v4 abre v5 sem inferir intenção para histórico antigo."""
        _central(self.central_file, f"http://127.0.0.1:{self.port}")
        run_task(self.task_file, self.db, self.central_file)
        before = self._outbox()
        # Reconstrói o formato v4: outbox existe, executions ainda não tem
        # intenção persistida.
        raw = sqlite3.connect(self.db)
        for column in ("outbox_enabled", "outbox_endpoint", "outbox_agent_id"):
            raw.execute(f"ALTER TABLE executions DROP COLUMN {column}")
        raw.execute("PRAGMA user_version = 4")
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
        self.assertEqual(version, 5)
        columns = {row[1] for row in sqlite3.connect(self.db).execute("PRAGMA table_info(executions)")}
        self.assertIn("outbox_endpoint", columns)
        self.assertEqual(self._outbox(), before)

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

    def test_central_rejeita_tipos_url_e_timeouts_invalidos(self):
        invalidos = (
            [],
            {"url": "https://user:pass@host", "agent_id": AGENT_UUID, "token_environment": "X"},
            {"url": "https://host:0", "agent_id": AGENT_UUID, "token_environment": "X"},
            {"url": "https://host", "agent_id": AGENT_UUID, "token_environment": "X", "connect_timeout_seconds": True},
            {"url": "https://host", "agent_id": AGENT_UUID, "token_environment": "X", "request_timeout_seconds": 1.5},
            {"url": "https://host", "agent_id": AGENT_UUID, "token_environment": "X", "request_timeout_seconds": 0},
        )
        for config in invalidos:
            with self.subTest(config=config), self.assertRaises(ConfigurationError):
                CentralSpec.from_dict(config)

    def test_limit_deve_ser_positivo(self):
        _central(self.central_file, f"http://127.0.0.1:{self.port}")
        for limit in (0, -1, True, 1.5):
            with self.subTest(limit=limit), self.assertRaises(ConfigurationError):
                run_sync(self.db, self.central_file, limit=limit)


if __name__ == "__main__":
    unittest.main()
