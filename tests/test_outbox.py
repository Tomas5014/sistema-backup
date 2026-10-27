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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
from macip_backup.sync import queue_lock_name, run_sync
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
        if getattr(_FakeApi, "hold_marker", None):
            # POST já aceito — sinaliza e segura a resposta até o teste
            # liberar (ou até 30s, para não travar em falha do pai).
            # Só a PRIMEIRA requisição é segurada: o marcador se desarma
            # e os próximos POSTs (outra fila) fluem normalmente.
            marker, hold_file, release_file = _FakeApi.hold_marker
            _FakeApi.hold_marker = None
            marker.write_text("x")
            deadline = time.time() + 30
            while hold_file.exists() and not release_file.exists() and time.time() < deadline:
                time.sleep(0.05)
            _FakeApi.accepted_execution_ids.add(accepted)
        delay_before_close = _FakeApi.delay_before_close
        if delay_before_close:
            # Captura o valor antes do cliente expirar. O tearDown da prova
            # seguinte não pode trocar a decisão deste handler atrasado.
            _FakeApi.delay_before_close = 0.0
            time.sleep(delay_before_close)
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


class OutboxBase(unittest.TestCase):
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
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeApi)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        _FakeApi.responses = []
        _FakeApi.posts = []
        _FakeApi.accepted_execution_ids = set()
        _FakeApi.disconnect_after_accept = 0
        _FakeApi.delay_before_close = 0.0
        _FakeApi.hold_marker = None

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



class OutboxTestCase(OutboxBase):
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


# ---- locks do outbox-rearm ------------------------------------------------


class RearmLockTestCase(OutboxBase):
    """outbox-rearm compartilha maintenance-SH + sync-EX com o sync."""

    def _auth_blocked_item(self) -> dict:
        _central(self.central_file, f"http://127.0.0.1:{self.port}")
        run_task(self.task_file, self.db, self.central_file)
        _FakeApi.responses = [(401, {})]
        run_sync(self.db, self.central_file)
        return self._outbox()[0]

    def test_rearm_recusado_com_manutencao_ativa(self):
        import fcntl

        self._auth_blocked_item()
        before = self._outbox()[0].copy()
        maintenance = (
            self.tempdir / "locks" / "maintenance.lock"
        ).open("a")
        fcntl.flock(maintenance.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            with self.assertRaises(Exception):
                from macip_backup.sync import run_rearm

                run_rearm(self.db, self.central_file)
        finally:
            maintenance.close()
        # banco intocado — nem migração nem status alterado
        self.assertEqual(self._outbox()[0], before)

    def test_rearm_recusado_com_sync_ativo(self):
        import fcntl

        self._auth_blocked_item()
        sync_lock = (self.tempdir / "locks" / queue_lock_name(self.db, load_central(self.central_file))).open("a")
        fcntl.flock(sync_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            with self.assertRaises(Exception):
                from macip_backup.sync import run_rearm

                run_rearm(self.db, self.central_file)
        finally:
            sync_lock.close()

    def test_sync_recusado_com_rearm_ativo(self):
        import fcntl

        self._auth_blocked_item()
        # ocupa o lock DA FILA como faria um outbox-rearm em curso
        held = (self.tempdir / "locks" / queue_lock_name(self.db, load_central(self.central_file))).open("a")
        fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            with self.assertRaises(Exception):
                run_sync(self.db, self.central_file)
        finally:
            held.close()

    def test_rearm_funciona_apos_liberacao_dos_locks(self):
        self._auth_blocked_item()
        from macip_backup.sync import run_rearm

        result = run_rearm(self.db, self.central_file)
        self.assertEqual(len(result["rearmed"]), 1)
        self.assertEqual(self._outbox()[0]["status"], "pending")

    def test_falha_no_rearm_nao_deixa_lock_preso(self):
        self._auth_blocked_item()
        from macip_backup.sync import run_rearm

        # caminho que É um diretório → ExecutionStore falha ao abrir
        with self.assertRaises(Exception):
            run_rearm(self.tempdir, self.central_file)
        # sync.lock liberado → uma nova operação funciona
        result = run_rearm(self.db, self.central_file)
        self.assertEqual(len(result["rearmed"]), 1)

    def test_rearm_preserva_sent_e_identidade(self):
        _central(self.central_file, f"http://127.0.0.1:{self.port}")
        run_task(self.task_file, self.db, self.central_file)
        run_sync(self.db, self.central_file)           # sent
        _FakeApi.responses = [(401, {})]
        sqlite3.connect(self.db).execute(
            "UPDATE outbox SET status='auth_blocked' WHERE status='sent'"
        ).connection.commit()
        item = self._outbox()[0]
        # rearme manualmente marcado como auth_blocked com vínculo exato
        sqlite3.connect(self.db).execute(
            "UPDATE outbox SET status='sent' WHERE id=?", (item["id"],)
        ).connection.commit()
        from macip_backup.sync import run_rearm

        result = run_rearm(self.db, self.central_file)
        # 'sent' nunca é rearmado — reverter para auth_blocked não conta
        self.assertEqual(result["rearmed"], [])
        self.assertEqual(self._outbox()[0]["status"], "sent")


# ---- SIGKILL real entre aceite e confirmação -------------------------------


class KillRecoveryTestCase(OutboxBase):
    """Subprocesso recebe SIGKILL depois do servidor aceitar e antes
    de confirmar `sent` no SQLite."""

    def test_sigkill_apos_aceite_servidor_item_permance_reenviavel(self):
        import subprocess
        import fcntl

        _central(self.central_file, f"http://127.0.0.1:{self.port}")
        run_task(self.task_file, self.db, self.central_file)
        execution_id = self._outbox()[0]["execution_id"]

        # o servidor aceita, registra e trava a resposta até 'release'
        hold = self.tempdir / "hold"
        release = self.tempdir / "release"
        hold.write_text("x")
        _FakeApi.hold_marker = (self.tempdir / "accepted", hold, release)

        env = dict(
            os.environ,
            PYTHONPATH=str(Path(__file__).parents[1] / "src"),
            MACIP_LOCK_DIR=str(self.tempdir / "locks"),
            MACIP_TEST_TOKEN=os.environ["MACIP_TEST_TOKEN"],
        )
        process = subprocess.Popen(
            [
                sys.executable, "-c",
                "from macip_backup.sync import run_sync; "
                f"run_sync({str(self.db)!r}, {str(self.central_file)!r})",
            ],
            env=env,
        )
        # espera determinística: marcador só existe quando o servidor
        # já recebeu o POST e a resposta ainda não saiu
        marker = self.tempdir / "accepted"
        deadline = time.time() + 30
        while not marker.exists() and time.time() < deadline:
            self.assertIsNone(process.poll(), "sync morreu antes do POST")
            time.sleep(0.05)
        self.assertTrue(marker.exists(), "servidor nunca recebeu o POST")

        process.kill()                      # SIGKILL na janela
        process.wait(timeout=10)
        release.write_text("x")

        # kernel liberou os locks → lock da fila aceita EX de imediato
        probe = (self.tempdir / "locks" / queue_lock_name(self.db, load_central(self.central_file))).open("a")
        fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        probe.close()

        item = self._outbox()[0]
        self.assertEqual(item["status"], "pending")
        self.assertEqual(item["execution_id"], execution_id)
        self.assertEqual(len(_FakeApi.posts), 1)

        # novo processo: a API já tinha aceito → 'duplicate' → sent
        summary = run_sync(self.db, self.central_file)
        self.assertEqual(summary["sent"], [execution_id])
        self.assertEqual(len(_FakeApi.posts), 2)
        self.assertEqual(
            _FakeApi.posts[1]["execution_id"], execution_id
        )


# ---- exclusão por fila -----------------------------------------------------


class QueueConcurrencyTestCase(OutboxBase):
    """Filas = banco canônico + endpoint + UUID central. Filas distintas
    progridem em paralelo; a mesma fila mantém exclusão."""

    def _second_queue(self) -> Path:
        """Outra identidade central no mesmo banco → fila diferente."""
        return _central(
            self.tempdir / "central-B.json",
            f"http://127.0.0.1:{self.port}",
            agent_id=str(uuid.uuid4()),
        )

    def test_mesma_fila_por_caminhos_de_config_diferentes(self):
        """Dois arquivos com mesmo url+agente+store → mesmo lock."""
        _central(self.central_file, f"http://127.0.0.1:{self.port}")
        twin = _central(
            self.tempdir / "copia.json", f"http://127.0.0.1:{self.port}"
        )
        import fcntl

        from macip_backup.sync import queue_lock_name as qln

        lock_a = qln(self.db, load_central(self.central_file))
        lock_b = qln(self.db, load_central(twin))
        self.assertEqual(lock_a, lock_b)  # mesma fila = mesmo arquivo
        (self.tempdir / "locks").mkdir(exist_ok=True)
        held = (self.tempdir / "locks" / lock_a).open("a")
        fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            with self.assertRaises(Exception):
                run_sync(self.db, twin)
        finally:
            held.close()

    def test_fila_b_progride_enquanto_a_esta_preso_no_http(self):
        """A ocupa o servidor; B conclui SEM esperar A."""
        db_a, db_b = self.db, self.tempdir / "agent-B.db"
        central_a = _central(
            self.central_file, f"http://127.0.0.1:{self.port}"
        )
        central_b = self._second_queue()
        run_task(self.task_file, db_a, central_a)
        run_task(self.task_file, db_b, central_b)

        _FakeApi.hold_marker = (self.tempdir / "accepted", self.tempdir / "hold", self.tempdir / "release")
        (self.tempdir / "hold").write_text("x")
        done_a = []

        def sync_a():
            try:
                run_sync(db_a, central_a)
            finally:
                done_a.append(True)

        thread_a = threading.Thread(target=sync_a)
        thread_a.start()
        # espera A estar DENTRO do POST (marcador do servidor)
        deadline = time.time() + 30
        while not (self.tempdir / "accepted").exists() and time.time() < deadline:
            time.sleep(0.05)
        self.assertTrue((self.tempdir / "accepted").exists())

        # B — fila distinta — entrega enquanto A segura o servidor
        summary_b = run_sync(db_b, central_b)
        (self.tempdir / "release").write_text("x")
        thread_a.join(timeout=30)
        self.assertFalse(thread_a.is_alive())
        self.assertEqual(len(summary_b["sent"]), 1)
        self.assertTrue(done_a)

    def test_rodadas_coincidentes_nao_famintam_b(self):
        """Três rodadas disputadas: B entrega nas três — nunca perde a vez."""
        central_a = _central(
            self.central_file, f"http://127.0.0.1:{self.port}"
        )
        central_b = self._second_queue()
        db_b = self.tempdir / "agent-B.db"
        for _ in range(3):
            run_task(self.task_file, self.db, central_a)
            run_task(self.task_file, db_b, central_b)
        sent_a, sent_b = [], []

        def round_a():
            sent_a.append(run_sync(self.db, central_a)["sent"])

        def round_b():
            sent_b.append(run_sync(db_b, central_b)["sent"])

        threads = [
            threading.Thread(target=round_a),
            threading.Thread(target=round_b),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        self.assertFalse(any(t.is_alive() for t in threads))
        # B entregou seus 3 — nada ficou permanentemente pendente
        self.assertEqual(len([s for s in sent_b[0]]), 3)
        store_b = ExecutionStore(db_b)
        try:
            pending = [i for i in store_b.outbox_state() if i["status"] == "pending"]
        finally:
            store_b.close()
        self.assertEqual(pending, [])

    def test_duas_identidades_mesmo_banco_sao_filas_distintas(self):
        """Mesmo SQLite, dois UUIDs centrais → locks diferentes."""
        from macip_backup.sync import queue_lock_name as qln

        central_a = _central(
            self.central_file, f"http://127.0.0.1:{self.port}"
        )
        central_b = self._second_queue()
        self.assertNotEqual(
            qln(self.db, load_central(central_a)),
            qln(self.db, load_central(central_b)),
        )

    def test_sync_e_rearm_mesma_fila_se_excluem(self):
        """Rearme da fila bloqueia o sync da MESMA fila — não o de outra."""
        import fcntl

        _central(self.central_file, f"http://127.0.0.1:{self.port}")
        run_task(self.task_file, self.db, self.central_file)
        from macip_backup.sync import queue_lock_name as qln

        held = (
            self.tempdir / "locks"
            / qln(self.db, load_central(self.central_file))
        ).open("a")
        fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            with self.assertRaises(Exception):
                run_sync(self.db, self.central_file)
        finally:
            held.close()
