"""Distribuição gerenciada no agente: journal durável, retomada,
idempotência, suspensão, vínculo e confirmações reenviáveis.

Central falsa local (http.server em 127.0.0.1) — respostas e falhas
são controladas pelo teste. Units systemd vão para um HOME temporário
com ``activate=False`` (sem daemon-reload): a escrita é real, a
ativação fica para a prova de homologação.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from macip_backup.central import load_central
from macip_backup.distribution import (
    inspect,
    managed_directory,
    reconcile,
    status,
    _bond_key,
    _journal_state,
)
from macip_backup.models import ConfigurationError
from macip_backup.store import ExecutionStore
from macip_backup.systemd import (
    schedule_to_calendar,
    unit_directory,
)

AGENT_UUID = str(uuid.uuid4())
TASK_UUID = str(uuid.uuid4())
TASK_UUID_2 = str(uuid.uuid4())


def _taskspec_doc(task_id: str = TASK_UUID) -> dict:
    return {
        "id": task_id,
        "name": "tarefa gerenciada",
        "client": {"id": str(uuid.uuid4()), "name": "Cliente"},
        "device_id": AGENT_UUID,
        "sources": [{"path": "/dados"}],
        "destinations": [
            {
                "id": "local",
                "name": "disco",
                "kind": "local",
                "repository": "/srv/b",
                "password_environment": "PW",
            }
        ],
    }


def _envelope(
    task_id: str = TASK_UUID,
    generation: int = 1,
    action: str = "apply",
    revision: int = 1,
    schedule: dict | None = None,
    task_doc: dict | None = None,
    contract_version: int = 1,
    bogus_hash: bool = False,
) -> dict:
    env = {
        "task_id": task_id,
        "agent_id": AGENT_UUID,
        "generation": generation,
        "action": action,
        "revision": revision,
        "contract_version": contract_version,
    }
    if action == "apply":
        env["task"] = task_doc or _taskspec_doc(task_id)
        env["schedule"] = schedule or {
            "kind": "daily", "timezone": "UTC", "hour": 2, "minute": 15,
        }
    env["hash"] = hashlib.sha256(
        json.dumps(env, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if bogus_hash:
        env["hash"] = "0" * 64
    return env


class _CentralHandler(BaseHTTPRequestHandler):
    """Central falsa — o caso de teste configura `desired` e `fail`."""

    desired: list[dict] = []
    confirmations: list[dict] = []
    fail_post: bool = False  # perde a resposta HTTP do 1º POST
    post_409: bool = False

    def log_message(self, *_args):  # silêncio nos testes
        pass

    def do_GET(self):
        assert "/api/v1/agent/tasks/desired" in self.path
        # limit=200&offset=N
        offset = int(self.path.split("offset=")[1]) if "offset=" in self.path else 0
        tasks = type(self).desired
        page = tasks[offset : offset + 200]
        body = json.dumps(
            {
                "agent_id": AGENT_UUID,
                "offset": offset,
                "limit": 200,
                "total": len(tasks),
                "tasks": page,
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        assert "/confirmations" in self.path
        length = int(self.headers["Content-Length"])
        payload = json.loads(self.rfile.read(length))
        type(self).confirmations.append(payload)
        if type(self).post_409:
            self.send_response(409)
            self.wfile.write(b"{}")
            return
        if type(self).fail_post:
            type(self).fail_post = False
            self.connection.close()  # resposta perdida no meio
            return
        body = json.dumps(
            {"status": "recorded", "promoted": True,
             "current_generation": payload["generation"]}
        ).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class DistributionCase(unittest.TestCase):
    def setUp(self):
        self._home = TemporaryDirectory()
        self._state = TemporaryDirectory()
        home = Path(self._home.name)
        self.env = patch.dict(
            os.environ,
            {
                "HOME": str(home),
                "MACIP_STATE": str(Path(self._state.name) / "state"),
                "MACIP_API_TOKEN": "token-de-teste",
            },
        )
        self.env.start()
        _CentralHandler.desired = []
        _CentralHandler.confirmations = []
        _CentralHandler.fail_post = False
        _CentralHandler.post_409 = False
        self.server = HTTPServer(("127.0.0.1", 0), _CentralHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        state_dir = Path(self._state.name) / "state"
        state_dir.mkdir(parents=True)
        self.store_path = state_dir / "agent.db"
        self.central_path = state_dir / "central.json"
        self.central_path.write_text(
            json.dumps(
                {
                    "url": f"http://127.0.0.1:{self.server.server_port}",
                    "agent_id": AGENT_UUID,
                    "token_environment": "MACIP_API_TOKEN",
                }
            )
        )

    def tearDown(self):
        if self.server is not None:
            self.server.shutdown()
        self.env.stop()
        self._home.cleanup()
        self._state.cleanup()

    def _managed_root(self) -> Path:
        return managed_directory() / "tasks.d" / TASK_UUID


# ---- conversão de agenda ----------------------------------------------------


class ScheduleConversionTests(unittest.TestCase):
    def test_daily_com_fuso_iana(self):
        self.assertEqual(
            schedule_to_calendar(
                {"kind": "daily", "timezone": "America/Sao_Paulo",
                 "hour": 2, "minute": 15}
            ),
            "*-*-* 02:15:00 America/Sao_Paulo",
        )

    def test_weekly_dias_iso(self):
        self.assertEqual(
            schedule_to_calendar(
                {"kind": "weekly", "timezone": "UTC", "hour": 23,
                 "minute": 30, "weekdays": [1, 7]}
            ),
            "Mon,Sun 23:30:00 UTC",
        )

    def test_hourly_e_manual(self):
        self.assertEqual(
            schedule_to_calendar(
                {"kind": "hourly", "timezone": "UTC", "minute": 40}
            ),
            "*-*-* *:40:00 UTC",
        )
        self.assertIsNone(schedule_to_calendar({"kind": "manual"}))

    def test_kind_desconhecido(self):
        with self.assertRaises(ConfigurationError):
            schedule_to_calendar({"kind": "lunar"})


# ---- reconciliação ------------------------------------------------------------


class ReconcileTests(DistributionCase):
    def test_apply_grava_journal_units_e_confirma(self):
        _CentralHandler.desired = [_envelope()]
        summary = reconcile(
            self.central_path, self.store_path, activate=False
        )
        self.assertEqual(summary["applied"][0]["task_id"], TASK_UUID)
        task_dir = self._managed_root()
        # documento vigente referenciável pela unit
        doc = json.loads((task_dir / "current.json").read_text())
        self.assertEqual(doc["id"], TASK_UUID)
        self.assertTrue((task_dir / "gen-1.json").exists())
        state = _journal_state(task_dir)
        self.assertEqual(state["step"], "applied")
        # service gerenciado gravado com marcadores de vínculo
        service = (
            unit_directory()
            / f"macip-backup-managed-{TASK_UUID}.service"
        )
        text = service.read_text()
        self.assertIn(f"managed-task={TASK_UUID}", text)
        self.assertIn(f"central-agent={AGENT_UUID}", text)
        self.assertIn("current.json", text)
        timer = service.with_suffix(".timer")
        self.assertIn("OnCalendar=*-*-* 02:15:00 UTC", timer.read_text())
        self.assertIn("Persistent=true", timer.read_text())
        # confirmação enviada ao falso central
        self.assertEqual(
            _CentralHandler.confirmations[0]["generation"], 1
        )
        self.assertEqual(
            _CentralHandler.confirmations[0]["status"], "applied"
        )
        self.assertEqual(
            _CentralHandler.confirmations[0]["applied_revision"], 1
        )

    def test_manual_nao_cria_timer(self):
        _CentralHandler.desired = [
            _envelope(schedule={"kind": "manual"})
        ]
        reconcile(self.central_path, self.store_path, activate=False)
        service = (
            unit_directory()
            / f"macip-backup-managed-{TASK_UUID}.service"
        )
        self.assertTrue(service.exists())
        self.assertFalse(service.with_suffix(".timer").exists())

    def test_mesma_geracao_nao_duplica_efeitos(self):
        _CentralHandler.desired = [_envelope()]
        reconcile(self.central_path, self.store_path, activate=False)
        journal_lines = (
            self._managed_root() / "journal.jsonl"
        ).read_text().strip().splitlines()
        confirmations_before = len(_CentralHandler.confirmations)
        reconcile(self.central_path, self.store_path, activate=False)
        after = (
            self._managed_root() / "journal.jsonl"
        ).read_text().strip().splitlines()
        self.assertEqual(journal_lines, after)  # nada novo registrado
        # nenhuma confirmação nova disparada (a fila ficou vazia)
        self.assertEqual(
            len(_CentralHandler.confirmations), confirmations_before
        )

    def test_contrato_desconhecido_preserva_ultima_boa(self):
        _CentralHandler.desired = [_envelope()]
        reconcile(self.central_path, self.store_path, activate=False)
        good = (self._managed_root() / "current.json").read_text()
        # gen 2 com contrato incompatível
        _CentralHandler.desired = [
            _envelope(generation=2, contract_version=99)
        ]
        summary = reconcile(
            self.central_path, self.store_path, activate=False
        )
        self.assertTrue(summary["failed"])
        self.assertEqual(summary["failed"][0]["result"], "failed_validation")
        # última boa intocada
        self.assertEqual(
            (self._managed_root() / "current.json").read_text(), good
        )
        # falha registrada como confirmação failed
        confirm = _CentralHandler.confirmations[-1]
        self.assertEqual(confirm["status"], "failed")
        self.assertEqual(confirm["generation"], 2)

    def test_hash_divergente_rejeitado(self):
        _CentralHandler.desired = [_envelope(bogus_hash=True)]
        summary = reconcile(
            self.central_path, self.store_path, activate=False
        )
        self.assertEqual(
            summary["failed"][0]["result"], "failed_validation"
        )
        self.assertFalse((self._managed_root()).exists())

    def test_interrupcao_retoma_no_passo_que_falta(self):
        """Interrompe ENTRE os efeitos systemd e a gravação do
        current.json — a próxima rodada completa sem reinstalar units."""
        _CentralHandler.desired = [_envelope()]
        import macip_backup.distribution as distribution

        real_atomic = distribution._atomic_write
        calls = {"count": 0}

        def flaky(path, content):
            calls["count"] += 1
            if str(path).endswith("current.json"):
                raise OSError("crash simulado")
            return real_atomic(path, content)

        with patch.object(distribution, "_atomic_write", flaky):
            with self.assertRaises(OSError):
                distribution._apply_one(
                    load_central(self.central_path),
                    _envelope(),
                    self.central_path,
                    self.store_path,
                    managed_directory() / "tasks.d",
                    activate=False,
                )
        state = _journal_state(self._managed_root())
        self.assertEqual(state["step"], "units")
        self.assertFalse((self._managed_root() / "current.json").exists())
        # retomada: units não reinstaladas (idempotente), current gravado
        summary = reconcile(
            self.central_path, self.store_path, activate=False
        )
        self.assertEqual(summary["applied"][0]["result"], "applied")
        self.assertTrue(
            (self._managed_root() / "current.json").exists()
        )
        self.assertEqual(
            _journal_state(self._managed_root())["step"], "applied"
        )

    def test_resposta_perdida_confirma_na_rodada_seguinte(self):
        _CentralHandler.desired = [_envelope()]
        _CentralHandler.fail_post = True
        summary = reconcile(
            self.central_path, self.store_path, activate=False
        )
        # HTTP falhou no envio — aplicação local está OK e a fila guarda
        self.assertTrue(summary["applied"])
        store = ExecutionStore(self.store_path)
        pending = store.pending_confirmations(
            _bond_key(load_central(self.central_path)), "2999-01-01"
        )
        store.close()
        self.assertEqual(len(pending), 1)
        # o backoff atrasou a retentativa — o relógio avança e a fila
        # torna o item elegível de novo (sem dormir de verdade)
        connection = sqlite3.connect(self.store_path)
        connection.execute(
            "UPDATE confirmations SET next_attempt_at = NULL"
        )
        connection.commit()
        connection.close()
        # rodada seguinte reenvia idempotentemente
        reconcile(self.central_path, self.store_path, activate=False)
        self.assertEqual(
            _CentralHandler.confirmations[-1]["generation"], 1
        )
        store = ExecutionStore(self.store_path)
        self.assertEqual(
            store.pending_confirmations(
                _bond_key(load_central(self.central_path)), "2999-01-01"
            ),
            [],
        )
        store.close()

    def test_central_indisponivel_preserva_local(self):
        _CentralHandler.desired = [_envelope()]
        reconcile(self.central_path, self.store_path, activate=False)
        good = (self._managed_root() / "current.json").read_text()
        self.server.shutdown()  # central cai
        self.server = None
        summary = reconcile(
            self.central_path, self.store_path, activate=False
        )
        # fetch falhou → erro de comunicação, estado local intacto
        self.assertIsNotNone(summary["communication_error"])
        self.assertEqual(
            (self._managed_root() / "current.json").read_text(), good
        )
        service = (
            unit_directory()
            / f"macip-backup-managed-{TASK_UUID}.service"
        )
        self.assertTrue(service.exists())  # units continuam

    def test_suspend_desabilita_so_timer_da_tarefa(self):
        _CentralHandler.desired = [_envelope()]
        reconcile(self.central_path, self.store_path, activate=False)
        timer = (
            unit_directory()
            / f"macip-backup-managed-{TASK_UUID}.timer"
        )
        self.assertTrue(timer.exists())
        # suspend gen 2
        _CentralHandler.desired = [
            _envelope(generation=2, action="suspend")
        ]
        summary = reconcile(
            self.central_path, self.store_path, activate=False
        )
        self.assertEqual(summary["suspended"][0]["result"], "suspended")
        # service e configuração preservados — só o timer foi desligado
        service = timer.with_suffix(".service")
        self.assertTrue(service.exists())
        self.assertTrue(
            (self._managed_root() / "current.json").exists()
        )
        self.assertEqual(
            _CentralHandler.confirmations[-1]["status"], "applied"
        )
        self.assertEqual(
            _CentralHandler.confirmations[-1]["generation"], 2
        )

    def test_unit_alheia_nunca_sobrescrita(self):
        # unit com MESMO nome mas sem marcador de vínculo
        unit = (
            unit_directory()
            / f"macip-backup-managed-{TASK_UUID}.service"
        )
        unit.parent.mkdir(parents=True, exist_ok=True)
        unit.write_text("[Service]\nExecStart=/bin/false\n")
        _CentralHandler.desired = [_envelope()]
        summary = reconcile(
            self.central_path, self.store_path, activate=False
        )
        self.assertTrue(summary["failed"])
        self.assertIn("/bin/false", unit.read_text())  # preservada

    def test_vinculo_divergente_bloqueia_ate_rebind(self):
        _CentralHandler.desired = [_envelope()]
        reconcile(self.central_path, self.store_path, activate=False)
        # troca de central: outro endpoint, MESMO agente
        alt = self.central_path.parent / "central2.json"
        alt.write_text(
            json.dumps(
                {
                    "url": "http://127.0.0.1:59999",
                    "agent_id": AGENT_UUID,
                    "token_environment": "MACIP_API_TOKEN",
                }
            )
        )
        with self.assertRaises(ConfigurationError):
            reconcile(alt, self.store_path, activate=False)
        # --rebind explícito adota o novo vínculo
        _CentralHandler.desired = []
        other = HTTPServer(("127.0.0.1", 59999), _CentralHandler)
        threading.Thread(target=other.serve_forever, daemon=True).start()
        try:
            summary = reconcile(
                alt, self.store_path, activate=False, rebind=True
            )
            self.assertEqual(summary["desired_total"], 0)
        finally:
            other.shutdown()

    def test_inspect_sem_efeitos(self):
        _CentralHandler.desired = [_envelope()]
        report = inspect(self.central_path, self.store_path)
        self.assertEqual(report["desired"][0]["pending"], True)
        # nada escrito: nem journal, nem units, nem vínculo
        self.assertFalse((self._managed_root()).exists())
        self.assertFalse(
            (managed_directory() / "binding.json").exists()
        )

    def test_status_offline_monta_visao(self):
        _CentralHandler.desired = [_envelope()]
        reconcile(self.central_path, self.store_path, activate=False)
        report = status(self.central_path, self.store_path)
        self.assertEqual(report["tasks"][0]["task_id"], TASK_UUID)
        self.assertEqual(
            report["tasks"][0]["applied_generation"], 1
        )
        self.assertEqual(
            report["confirmations"][0]["status"], "sent"
        )

    def test_paginacao_multiplas_tarefas(self):
        _CentralHandler.desired = [
            _envelope(TASK_UUID, generation=1),
            _envelope(TASK_UUID_2, generation=1),
        ]
        summary = reconcile(
            self.central_path, self.store_path, activate=False
        )
        self.assertEqual(summary["desired_total"], 2)
        ids = {item["task_id"] for item in summary["applied"]}
        self.assertEqual(ids, {TASK_UUID, TASK_UUID_2})


if __name__ == "__main__":
    unittest.main()
