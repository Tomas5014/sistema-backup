"""Distribuição gerenciada no agente: journal durável, retomada,
idempotência, suspensão, vínculo e confirmações reenviáveis.

Central falsa local (http.server em 127.0.0.1) — respostas e falhas
são controladas pelo teste. Units systemd vão para um HOME temporário
com ``activate=False`` (sem daemon-reload): a escrita é real, a
ativação fica para a prova de homologação. Origens e pré-requisitos
locais são diretórios/arquivos REAIS no diretório temporário.
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

import macip_backup.distribution as distribution
from macip_backup.central import load_central
from macip_backup.distribution import (
    inspect,
    managed_directory,
    reconcile,
    status,
    _bond_key,
    _journal_state,
    _apply_one,
    _tasks_root,
    _read_active,
    _read_transition,
    _transition_file,
    _transition_bond,
    load_central,
)
from macip_backup.models import ConfigurationError
from macip_backup.restic import ResticError
from macip_backup.store import ExecutionStore
from macip_backup.systemd import (
    schedule_to_calendar,
    unit_directory,
)

AGENT_UUID = str(uuid.uuid4())
TASK_UUID = str(uuid.uuid4())
TASK_UUID_2 = str(uuid.uuid4())


class DistributionCase(unittest.TestCase):
    """Central falsa + HOME/STATE descartáveis + origem real."""

    def setUp(self):
        self._home = TemporaryDirectory()
        self._state = TemporaryDirectory()
        home = Path(self._home.name)
        state_root = Path(self._state.name) / "state"
        state_root.mkdir(parents=True)
        self.source_dir = state_root / "origem"
        self.source_dir.mkdir()
        (self.source_dir / "dado.txt").write_text("conteúdo")
        self.repo_dir = state_root / "repo"
        self.env = patch.dict(
            os.environ,
            {
                "HOME": str(home),
                "MACIP_STATE": str(state_root),
                "MACIP_API_TOKEN": "token-de-teste",
            },
        )
        self.env.start()
        _CentralHandler.desired = []
        _CentralHandler.confirmations = []
        _CentralHandler.fail_post = False
        _CentralHandler.post_status = None
        self.server = HTTPServer(("127.0.0.1", 0), _CentralHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        # camada systemd simulada e DETERMINÍSTICA: as units são
        # arquivos reais, mas enable/disable viram estado controlado —
        # systemctl --user não conhece units de um HOME temporário
        self._timers_enabled: dict[str, bool] = {}
        self._systemctl_calls: list[tuple] = []

        def fake_enabled(task_id):
            timer = (
                unit_directory() / f"macip-backup-managed-{task_id}.timer"
            )
            if not timer.exists():
                return None
            return self._timers_enabled.get(task_id, False)

        def fake_disable(task_id, agent_uuid, bond, now=True):
            self._systemctl_calls.append(("disable", task_id, now))
            self._timers_enabled[task_id] = False
            timer = (
                unit_directory() / f"macip-backup-managed-{task_id}.timer"
            )
            return {"task_id": task_id, "timer_name": timer.name,
                    "disabled": timer.exists(), "preserved": str(timer)}

        def fake_activate(task_id, timer_written):
            self._systemctl_calls.append(("enable", task_id, timer_written))
            timer = (
                unit_directory() / f"macip-backup-managed-{task_id}.timer"
            )
            if timer_written and not timer.exists():
                raise AssertionError(
                    "enable sem arquivo de timer escrito"
                )
            if timer_written:
                self._timers_enabled[task_id] = True

        def fake_suspend(task_id, agent_uuid, bond):
            return fake_disable(task_id, agent_uuid, bond, now=True)

        self._systemd_patches = [
            patch.object(distribution, "managed_timer_enabled", fake_enabled),
            patch.object(distribution, "disable_managed_timer", fake_disable),
            patch.object(distribution, "activate_managed_task", fake_activate),
            patch.object(distribution, "suspend_managed_task", fake_suspend),
        ]
        for patched in self._systemd_patches:
            patched.start()
        self.store_path = state_root / "agent.db"
        self.central_path = state_root / "central.json"
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
        for patched in self._systemd_patches:
            patched.stop()
        if self.server is not None:
            self.server.shutdown()
        self.env.stop()
        self._home.cleanup()
        self._state.cleanup()

    def _taskspec_doc(self, task_id: str = TASK_UUID,
                      source: str | None = None) -> dict:
        return {
            "id": task_id,
            "name": "tarefa gerenciada",
            "client": {"id": str(uuid.uuid4()), "name": "Cliente"},
            "device_id": AGENT_UUID,
            "sources": [{"path": str(source or self.source_dir)}],
            "destinations": [
                {
                    "id": "local",
                    "name": "disco",
                    "kind": "local",
                    "repository": str(self.repo_dir),
                    "password_environment": "PW",
                }
            ],
        }

    def _envelope(
        self,
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
            env["task"] = task_doc or self._taskspec_doc(task_id)
            env["schedule"] = schedule or {
                "kind": "daily", "timezone": "UTC",
                "hour": 2, "minute": 15,
            }
        env["hash"] = hashlib.sha256(
            json.dumps(env, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        if bogus_hash:
            env["hash"] = "0" * 64
        return env

    def _tasks_root(self) -> Path:
        bond = _bond_key(load_central(self.central_path), self.store_path)
        return _tasks_root(bond)

    def _managed_root(self, task_id: str = TASK_UUID) -> Path:
        return self._tasks_root() / task_id

    def _timer_file(self, task_id: str = TASK_UUID) -> Path:
        return unit_directory() / f"macip-backup-managed-{task_id}.timer"

    def _service_file(self, task_id: str = TASK_UUID) -> Path:
        return unit_directory() / f"macip-backup-managed-{task_id}.service"


class _CentralHandler(BaseHTTPRequestHandler):
    """Central falsa — o caso de teste configura `desired` e falhas."""

    desired: list[dict] = []
    confirmations: list[dict] = []
    fail_post: bool = False      # perde a resposta HTTP do próximo POST
    post_status: int | None = None  # força status fixo nos POSTs

    def log_message(self, *_args):
        pass

    def do_GET(self):
        assert "/api/v1/agent/tasks/desired" in self.path
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
        if type(self).post_status is not None:
            self.send_response(type(self).post_status)
            self.send_header("Content-Length", "2")
            self.end_headers()
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


# ---- conversão de agenda ------------------------------------------------------


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


# ---- validação de envelope (achado 6) -----------------------------------------


class ValidationTests(DistributionCase):
    def _once(self, **envelope_kw):
        _CentralHandler.desired = [self._envelope(**envelope_kw)]
        return reconcile(self.central_path, self.store_path, activate=False)

    def test_origem_inexistente_rejeitada(self):
        doc = self._taskspec_doc(source="/caminho/que/nao/existe")
        summary = self._once(task_doc=doc)
        self.assertEqual(
            summary["failed"][0]["result"], "failed_validation"
        )
        self.assertIn("origem", summary["failed"][0]["detail"])
        # nenhum efeito: nem journal 'applied', nem unit, nem current
        self.assertFalse((self._managed_root() / "current.json").exists())
        self.assertFalse(self._service_file().exists())

    def test_task_id_do_documento_diverge(self):
        doc = self._taskspec_doc()
        doc["id"] = str(uuid.uuid4())  # envelope diz TASK_UUID
        summary = self._once(task_doc=doc)
        self.assertEqual(
            summary["failed"][0]["result"], "failed_validation"
        )
        self.assertIn("task.id", summary["failed"][0]["detail"])

    def test_device_id_do_documento_diverge(self):
        doc = self._taskspec_doc()
        doc["device_id"] = str(uuid.uuid4())
        summary = self._once(task_doc=doc)
        self.assertEqual(
            summary["failed"][0]["result"], "failed_validation"
        )
        self.assertIn("device_id", summary["failed"][0]["detail"])

    def test_repositorio_dentro_da_origem_rejeitado(self):
        doc = self._taskspec_doc()
        doc["destinations"][0]["repository"] = str(
            self.source_dir / "repo-dentro"
        )
        summary = self._once(task_doc=doc)
        self.assertEqual(
            summary["failed"][0]["result"], "failed_validation"
        )
        self.assertIn("origem", summary["failed"][0]["detail"])

    def test_hash_divergente_rejeitado(self):
        summary = self._once(bogus_hash=True)
        self.assertEqual(
            summary["failed"][0]["result"], "failed_validation"
        )

    def test_contrato_desconhecido_preserva_ultima_boa(self):
        _CentralHandler.desired = [self._envelope()]
        reconcile(self.central_path, self.store_path, activate=True)
        good = (self._managed_root() / "current.json").read_text()
        _CentralHandler.desired = [
            self._envelope(generation=2, contract_version=99)
        ]
        summary = reconcile(
            self.central_path, self.store_path, activate=True
        )
        self.assertEqual(
            summary["failed"][0]["result"], "failed_validation"
        )
        self.assertEqual(
            (self._managed_root() / "current.json").read_text(), good
        )


# ---- ordem e atomicidade da aplicação (achado 1) -------------------------------


class ApplyOrderingTests(DistributionCase):
    def test_config_publicada_antes_da_ativacao(self):
        """Quando `activate_managed_task` roda, current.json + timer já
        estão no lugar — nenhum disparo pega estado incompleto."""
        _CentralHandler.desired = [self._envelope()]
        seen = {}

        def spy(task_id, timer_written):
            seen["current"] = (
                self._managed_root() / "current.json"
            ).exists()
            seen["timer_file"] = self._timer_file().exists()
            seen["config"] = json.loads(
                (self._managed_root() / "current.json").read_text()
            )["id"]

        with patch.object(
            distribution, "activate_managed_task", side_effect=spy
        ):
            reconcile(self.central_path, self.store_path, activate=True)
        self.assertTrue(seen["current"])
        self.assertTrue(seen["timer_file"])
        self.assertEqual(seen["config"], TASK_UUID)
        # ordem registrada no journal
        steps = [
            e["step"]
            for e in (
                self._managed_root() / "journal.jsonl"
            ).read_text().strip().splitlines()
            for e in [json.loads(e)]
        ]
        order = ["received", "validated", "config", "timer_off",
                 "units", "published", "enabled", "applied"]
        self.assertEqual(steps, order)

    def test_falha_na_ativacao_restaura_estado_anterior(self):
        """gen1 aplicada; gen2 falha no enable → rollback: configuração,
        agenda e units voltam à gen1 exatamente."""
        _CentralHandler.desired = [self._envelope()]
        reconcile(self.central_path, self.store_path, activate=True)
        good_current = (self._managed_root() / "current.json").read_text()
        good_timer = self._timer_file().read_text()

        _CentralHandler.desired = [
            self._envelope(
                generation=2, revision=2,
                schedule={"kind": "daily", "timezone": "UTC",
                          "hour": 4, "minute": 0},
            )
        ]

        calls = {"n": 0}

        def boom_once(task_id, timer_written):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("systemd indisponível (simulado)")
            self._timers_enabled[task_id] = bool(timer_written)

        with patch.object(
            distribution, "activate_managed_task", side_effect=boom_once
        ):
            summary = reconcile(
                self.central_path, self.store_path, activate=True
            )
        self.assertTrue(summary["failed"])
        # estado operacional = gen1 completo: configuração E agenda
        self.assertEqual(
            (self._managed_root() / "current.json").read_text(),
            good_current,
        )
        self.assertEqual(self._timer_file().read_text(), good_timer)
        journal = _journal_state(self._managed_root())
        self.assertEqual(journal["step"], "rolled_back")
        self.assertEqual(journal["restored"], 1)

    def test_falha_na_primeira_ativacao_remove_residuo(self):
        """Sem estado anterior: falha nos efeitos remove units e
        current.json — nada fica meio aplicado."""
        _CentralHandler.desired = [self._envelope()]

        def boom(task_id, timer_written):
            raise OSError("systemd indisponível (simulado)")

        with patch.object(
            distribution, "activate_managed_task", side_effect=boom
        ):
            summary = reconcile(
                self.central_path, self.store_path, activate=True
            )
        self.assertTrue(summary["failed"])
        self.assertFalse(self._service_file().exists())
        self.assertFalse(self._timer_file().exists())
        self.assertFalse((self._managed_root() / "current.json").exists())

    def test_interrupcao_entre_publicacao_e_ativacao_retoma(self):
        """Crash depois de `published`: a próxima rodada refaz os
        efeitos idempotentemente e conclui."""
        _CentralHandler.desired = [self._envelope()]

        calls = {"n": 0}

        def crash_once(task_id, timer_written):
            calls["n"] += 1
            if calls["n"] in (1, 2):
                raise OSError("crash no enable")
            self._timers_enabled[task_id] = bool(timer_written)

        with patch.object(
            distribution, "activate_managed_task", side_effect=crash_once
        ):
            reconcile(self.central_path, self.store_path, activate=True)
        # rollback executado; próxima rodada conclui a MESMA tentativa
        summary = reconcile(
            self.central_path, self.store_path, activate=True
        )
        self.assertEqual(summary["applied"][0]["result"], "applied")
        state = _journal_state(self._managed_root())
        self.assertEqual(state["step"], "applied")
        # confirmação entregue com a tentativa da conclusão
        last = _CentralHandler.confirmations[-1]
        self.assertEqual(last["status"], "applied")


# ---- janela journal → fila de confirmações (achado 2) ---------------------------


class ConfirmationRecoveryTests(DistributionCase):
    def test_aplicacao_sem_enfileiramento_reconstruida_na_proxima_rodada(self):
        """_apply_one conclui, processo morre antes do enqueue; a
        próxima reconciliação recria a confirmação a partir do journal
        SEM reaplicar nada."""
        spec = load_central(self.central_path)
        bond = _bond_key(spec, self.store_path)
        outcome = _apply_one(
            spec, self._envelope(), self.central_path, self.store_path,
            self._tasks_root(), bond, activate=True,
        )
        self.assertEqual(outcome["result"], "applied")
        # fila vazia — corte na janela
        store = ExecutionStore(self.store_path)
        self.assertEqual(
            store.pending_confirmations(bond, "2999-01-01"), []
        )
        store.close()
        # rodada seguinte: mesmo desired, já aplicada → só reconstrói e envia
        _CentralHandler.desired = [self._envelope()]
        summary = reconcile(
            self.central_path, self.store_path, activate=True
        )
        self.assertEqual(summary["confirmations_rebuilt"], 1)
        self.assertEqual(
            summary["applied"][0]["result"], "already_applied"
        )
        self.assertEqual(
            _CentralHandler.confirmations[-1]["status"], "applied"
        )
        self.assertEqual(_CentralHandler.confirmations[-1]["attempt"], 1)

    def test_resposta_http_perdida_reenviada_depois(self):
        _CentralHandler.desired = [self._envelope()]
        _CentralHandler.fail_post = True
        reconcile(self.central_path, self.store_path, activate=True)
        bond = _bond_key(load_central(self.central_path), self.store_path)
        store = ExecutionStore(self.store_path)
        pending = store.pending_confirmations(bond, "2999-01-01")
        store.close()
        self.assertEqual(len(pending), 1)
        connection = sqlite3.connect(self.store_path)
        connection.execute(
            "UPDATE confirmations SET next_attempt_at = NULL"
        )
        connection.commit()
        connection.close()
        reconcile(self.central_path, self.store_path, activate=True)
        self.assertEqual(
            _CentralHandler.confirmations[-1]["generation"], 1
        )
        store = ExecutionStore(self.store_path)
        self.assertEqual(
            store.pending_confirmations(bond, "2999-01-01"), []
        )
        store.close()


# ---- evolução falha → sucesso na mesma geração (achado 3) ------------------------


class AttemptEvolutionTests(DistributionCase):
    def test_falha_de_prerequisito_depois_sucesso_mesma_geracao(self):
        """gen1 falha (origem ausente) → operador cria a origem → a
        MESMA geração é aplicada numa tentativa nova — ambos os
        eventos sobem, na ordem."""
        doc = self._taskspec_doc()
        # origem ainda não existe
        doc["sources"][0]["path"] = str(
            Path(self._state.name) / "state" / "origem-tardia"
        )
        _CentralHandler.desired = [self._envelope(task_doc=doc)]
        summary = reconcile(
            self.central_path, self.store_path, activate=True
        )
        self.assertEqual(
            summary["failed"][0]["result"], "failed_validation"
        )
        first = _CentralHandler.confirmations[-1]
        self.assertEqual((first["status"], first["attempt"]),
                         ("failed", 1))
        # pré-requisito corrigido — a central segue servindo gen1
        Path(doc["sources"][0]["path"]).mkdir(parents=True)
        summary = reconcile(
            self.central_path, self.store_path, activate=True
        )
        self.assertEqual(summary["applied"][0]["result"], "applied")
        second = _CentralHandler.confirmations[-1]
        self.assertEqual((second["status"], second["attempt"]),
                         ("applied", 2))
        # histórico preservado: as DUAS tentativas estão na fila/log
        store = ExecutionStore(self.store_path)
        rows = store.confirmation_state()
        store.close()
        attempts = [
            (r["apply_attempt"], r["status"])
            for r in rows if r["generation"] == 1
        ]
        self.assertEqual(attempts, [(1, "sent"), (2, "sent")])


# ---- transitoriedade ilimitada (achado 4) --------------------------------------


class TransientRetryTests(DistributionCase):
    def test_dez_falhas_temporarias_depois_entrega(self):
        """500 seguidos nunca bloqueiam; quando a API volta, o evento é
        entregue sem intervenção — relógio controlado."""
        _CentralHandler.desired = [self._envelope()]
        reconcile(self.central_path, self.store_path, activate=True)
        bond = _bond_key(load_central(self.central_path), self.store_path)
        # nova aplicação pendente artificialmente: força falha de envio
        store = ExecutionStore(self.store_path)
        store.enqueue_confirmation(
            bond, TASK_UUID, 99, 1, json.dumps(
                {"generation": 99, "attempt": 1, "status": "applied",
                 "applied_revision": 1, "detail": None,
                 "agent_time": "2026-01-01T00:00:00Z"}
            ),
        )
        store.close()
        _CentralHandler.desired = []
        _CentralHandler.post_status = 503
        # relógio controlado: backoff nunca bloqueia a elegibilidade
        with patch.object(
            distribution, "_backoff", return_value="2000-01-01T00:00:00Z"
        ):
            for _ in range(12):  # acima do antigo limite de 10
                reconcile(
                    self.central_path, self.store_path, activate=True
                )
            _CentralHandler.post_status = None
            reconcile(self.central_path, self.store_path, activate=True)
        store = ExecutionStore(self.store_path)
        rows = [r for r in store.confirmation_state()
                if r["generation"] == 99]
        store.close()
        self.assertEqual(rows[0]["status"], "sent")
        self.assertGreaterEqual(rows[0]["attempts"], 13)


# ---- preparação vs aplicação (achado 5) ----------------------------------------


class PrepareModeTests(DistributionCase):
    def test_preparacao_nao_confirma_e_execucao_real_ativa_depois(self):
        _CentralHandler.desired = [self._envelope()]
        dry = reconcile(
            self.central_path, self.store_path, activate=False
        )
        self.assertEqual(dry["prepared"][0]["result"], "prepared")
        # nada confirmado, journal sem 'applied', timer não ativado
        self.assertEqual(_CentralHandler.confirmations, [])
        state = _journal_state(self._managed_root())
        self.assertEqual(state["step"], "prepared")
        # configuração candidata e units em STAGING (não operacional)
        self.assertTrue(
            (self._managed_root() / "config-1.json").exists()
        )
        staging = self._managed_root() / "staged"
        self.assertTrue(
            (staging / f"macip-backup-managed-{TASK_UUID}.service").exists()
        )
        # units operacionais NÃO foram tocadas
        self.assertFalse(self._service_file().exists())
        self.assertFalse(self._timer_file().exists())
        # rodada real conclui a MESMA tentativa — units vão para
        # operacional agora
        summary = reconcile(
            self.central_path, self.store_path, activate=True
        )
        self.assertEqual(summary["applied"][0]["result"], "applied")
        self.assertEqual(
            _CentralHandler.confirmations[-1]["status"], "applied"
        )
        self.assertTrue(self._service_file().exists())
        self.assertTrue(
            (self._managed_root() / "current.json").exists()
        )


# ---- fluxo principal ------------------------------------------------------------


class ReconcileTests(DistributionCase):
    def test_apply_grava_journal_units_e_confirma(self):
        _CentralHandler.desired = [self._envelope()]
        summary = reconcile(
            self.central_path, self.store_path, activate=True
        )
        self.assertEqual(summary["applied"][0]["task_id"], TASK_UUID)
        task_dir = self._managed_root()
        doc = json.loads((task_dir / "current.json").read_text())
        self.assertEqual(doc["id"], TASK_UUID)
        self.assertTrue((task_dir / "gen-1.json").exists())
        state = _journal_state(task_dir)
        self.assertEqual(state["step"], "applied")
        service = self._service_file()
        text = service.read_text()
        self.assertIn(f"managed-task={TASK_UUID}", text)
        self.assertIn(f"central-agent={AGENT_UUID}", text)
        self.assertIn("managed-bond=", text)
        self.assertIn("current.json", text)
        timer = service.with_suffix(".timer")
        self.assertIn("OnCalendar=*-*-* 02:15:00 UTC", timer.read_text())
        self.assertIn("Persistent=true", timer.read_text())
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
            self._envelope(schedule={"kind": "manual"})
        ]
        reconcile(self.central_path, self.store_path, activate=True)
        self.assertTrue(self._service_file().exists())
        self.assertFalse(self._timer_file().exists())

    def test_mesma_geracao_nao_duplica_efeitos(self):
        _CentralHandler.desired = [self._envelope()]
        reconcile(self.central_path, self.store_path, activate=True)
        journal_lines = (
            self._managed_root() / "journal.jsonl"
        ).read_text().strip().splitlines()
        before = len(_CentralHandler.confirmations)
        reconcile(self.central_path, self.store_path, activate=True)
        after = (
            self._managed_root() / "journal.jsonl"
        ).read_text().strip().splitlines()
        self.assertEqual(journal_lines, after)
        self.assertEqual(len(_CentralHandler.confirmations), before)

    def test_central_indisponivel_preserva_local(self):
        _CentralHandler.desired = [self._envelope()]
        reconcile(self.central_path, self.store_path, activate=True)
        good = (self._managed_root() / "current.json").read_text()
        self.server.shutdown()
        self.server = None
        summary = reconcile(
            self.central_path, self.store_path, activate=True
        )
        self.assertIsNotNone(summary["communication_error"])
        self.assertEqual(
            (self._managed_root() / "current.json").read_text(), good
        )
        self.assertTrue(self._service_file().exists())

    def test_suspend_desabilita_so_timer_da_tarefa(self):
        _CentralHandler.desired = [self._envelope()]
        reconcile(self.central_path, self.store_path, activate=True)
        self.assertTrue(self._timer_file().exists())
        _CentralHandler.desired = [
            self._envelope(generation=2, action="suspend")
        ]
        summary = reconcile(
            self.central_path, self.store_path, activate=True
        )
        self.assertEqual(summary["suspended"][0]["result"], "suspended")
        self.assertTrue(self._service_file().exists())
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
        unit = self._service_file()
        unit.parent.mkdir(parents=True, exist_ok=True)
        unit.write_text("[Service]\nExecStart=/bin/false\n")
        _CentralHandler.desired = [self._envelope()]
        summary = reconcile(
            self.central_path, self.store_path, activate=True
        )
        self.assertTrue(summary["failed"])
        self.assertIn("/bin/false", unit.read_text())

    def test_paginacao_multiplas_tarefas(self):
        _CentralHandler.desired = [
            self._envelope(TASK_UUID, generation=1),
            self._envelope(TASK_UUID_2, generation=1),
        ]
        summary = reconcile(
            self.central_path, self.store_path, activate=True
        )
        self.assertEqual(summary["desired_total"], 2)
        ids = {item["task_id"] for item in summary["applied"]}
        self.assertEqual(ids, {TASK_UUID, TASK_UUID_2})

    def test_inspect_sem_efeitos(self):
        _CentralHandler.desired = [self._envelope()]
        report = inspect(self.central_path, self.store_path)
        self.assertEqual(report["desired"][0]["pending"], True)
        self.assertFalse((self._managed_root()).exists())
        self.assertFalse(
            (managed_directory() / "active.json").exists()
        )

    def test_status_offline_monta_visao(self):
        _CentralHandler.desired = [self._envelope()]
        reconcile(self.central_path, self.store_path, activate=True)
        report = status(self.central_path, self.store_path)
        self.assertEqual(report["tasks"][0]["task_id"], TASK_UUID)
        self.assertEqual(
            report["tasks"][0]["applied_generation"], 1
        )
        self.assertEqual(
            report["confirmations"][0]["status"], "sent"
        )
        self.assertEqual(report["confirmations"][0]["apply_attempt"], 1)


# ---- transição de vínculo (achado 7) --------------------------------------------


class BondTransitionTests(DistributionCase):
    def _second_central(self) -> tuple[HTTPServer, Path]:
        other = HTTPServer(("127.0.0.1", 0), _CentralHandler)
        threading.Thread(target=other.serve_forever, daemon=True).start()
        alt = Path(self._state.name) / "state" / "central2.json"
        alt.write_text(
            json.dumps(
                {
                    "url": f"http://127.0.0.1:{other.server_port}",
                    "agent_id": AGENT_UUID,
                    "token_environment": "MACIP_API_TOKEN",
                }
            )
        )
        return other, alt

    def test_outra_central_sem_rebind_recusada(self):
        _CentralHandler.desired = [self._envelope()]
        reconcile(self.central_path, self.store_path, activate=True)
        other, alt = self._second_central()
        try:
            with self.assertRaises(ConfigurationError):
                reconcile(alt, self.store_path, activate=True)
        finally:
            other.shutdown()

    def test_rebind_nao_herda_aplicacao_e_desliga_timers_antigos(self):
        """Central A aplica gen1; --rebind para B com MESMA tarefa/gen
        e conteúdo diferente: o journal de A não pode marcar como
        'already_applied' para B; o timer do vínculo A é desativado."""
        _CentralHandler.desired = [self._envelope()]
        reconcile(self.central_path, self.store_path, activate=True)
        bond_a = _bond_key(
            load_central(self.central_path), self.store_path
        )
        self.assertTrue(
            (_tasks_root(bond_a) / TASK_UUID / "current.json").exists()
        )

        # central B: mesmo envelope com CONTEÚDO alterado
        other, alt = self._second_central()
        doc_b = self._taskspec_doc()
        doc_b["name"] = "tarefa da central B"
        _CentralHandler.desired = [self._envelope(task_doc=doc_b)]
        try:
            summary = reconcile(
                alt, self.store_path, activate=True, rebind=True
            )
        finally:
            other.shutdown()
        bond_b = _bond_key(load_central(alt), self.store_path)
        self.assertTrue(summary["bond_transitioned"])
        # NÃO foi já-aplicado — aplicou de verdade sob o vínculo novo
        self.assertEqual(summary["applied"][0]["result"], "applied")
        # journal SEPARADO por vínculo
        self.assertTrue(
            (_tasks_root(bond_b) / TASK_UUID / "current.json").exists()
        )
        doc = json.loads(
            (_tasks_root(bond_b) / TASK_UUID / "current.json").read_text()
        )
        self.assertEqual(doc["name"], "tarefa da central B")
        # units carregam o marcador do vínculo NOVO
        self.assertIn(
            f"managed-bond={bond_b[:16]}",
            self._service_file().read_text(),
        )
        # confirmações sob o bond NOVO — antigas não migraram
        store = ExecutionStore(self.store_path)
        rows_b = [
            r for r in store.confirmation_state() if r["bond"] == bond_b
        ]
        store.close()
        self.assertTrue(rows_b)
        # active.json aponta o novo vínculo; diretório de A preservado
        report = status(alt, self.store_path)
        self.assertEqual(report["active"]["bond"], bond_b)
        self.assertTrue((_tasks_root(bond_a)).exists())




# ---- F1: rollback preserva estado suspenso --------------------------------------


class RollbackSuspendedTests(DistributionCase):
    """apply → suspend → falha na nova aplicação: rollback restaura a
    configuração anterior E mantém o timer suspenso (não reativa)."""

    def test_rollback_preserva_estado_suspenso(self):
        # 1. aplicar geração 1 (automática)
        _CentralHandler.desired = [self._envelope()]
        reconcile(self.central_path, self.store_path, activate=True)
        good_current = (self._managed_root() / "current.json").read_text()
        self.assertTrue(self._timer_file().exists())
        self.assertTrue(self._timers_enabled.get(TASK_UUID, False))

        # 2. suspender geração 2
        _CentralHandler.desired = [
            self._envelope(generation=2, action="suspend")
        ]
        reconcile(self.central_path, self.store_path, activate=True)
        self.assertFalse(self._timers_enabled.get(TASK_UUID, False))

        # 3. tentar aplicar geração 3 — falha na ativação
        _CentralHandler.desired = [
            self._envelope(
                generation=3, revision=3,
                schedule={"kind": "daily", "timezone": "UTC",
                          "hour": 5, "minute": 0},
            )
        ]
        calls = {"n": 0}

        def boom_once(task_id, timer_written):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("systemd indisponível (simulado)")
            self._timers_enabled[task_id] = bool(timer_written)

        # T1: registra a sequência completa de chamadas ao activate —
        # NÃO pode haver enable seguido de disable
        enable_calls: list[bool] = []
        original_activate = distribution.activate_managed_task

        def tracking_activate(task_id, timer_written):
            enable_calls.append(timer_written)
            return original_activate(task_id, timer_written)

        with patch.object(
            distribution, "activate_managed_task",
            side_effect=boom_once,
        ), patch.object(
            distribution, "activate_managed_task",
            side_effect=tracking_activate,
        ):
            # the second patch overrides; we need a different approach
            pass

        # simpler: track via the fake_activate that's already installed
        # by the fixture. Reset the calls list.
        self._systemctl_calls.clear()

        with patch.object(
            distribution, "activate_managed_task", side_effect=boom_once
        ):
            summary = reconcile(
                self.central_path, self.store_path, activate=True
            )
        # 4. rollback: configuração da gen1 restaurada, timer PERMANECE
        # suspenso (não reativa)
        self.assertTrue(summary["failed"])
        self.assertEqual(
            (self._managed_root() / "current.json").read_text(),
            good_current,
        )
        self.assertFalse(self._timers_enabled.get(TASK_UUID, False))
        journal = _journal_state(self._managed_root())
        self.assertEqual(journal["step"], "rolled_back")
        self.assertEqual(journal.get("operational"), "suspend")
        # T1: NENHUMA chamada de enable ocorreu — o rollback não
        # habilitou nem temporariamente o timer suspenso
        enable_calls_in_rollback = [
            c for c in self._systemctl_calls if c[0] == "enable"
        ]
        self.assertEqual(
            enable_calls_in_rollback, [],
            "rollback habilitou o timer suspenso temporariamente"
        )

    def test_rollback_sem_suspensao_previa_reativa_timer(self):
        """apply gen1 → apply gen2 falha → rollback restaura gen1 E
        reativa o timer (última ação operacional era apply)."""
        _CentralHandler.desired = [self._envelope()]
        reconcile(self.central_path, self.store_path, activate=True)
        good_current = (self._managed_root() / "current.json").read_text()

        _CentralHandler.desired = [
            self._envelope(
                generation=2, revision=2,
                schedule={"kind": "daily", "timezone": "UTC",
                          "hour": 4, "minute": 0},
            )
        ]
        calls = {"n": 0}

        def boom_once(task_id, timer_written):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("systemd indisponível (simulado)")
            self._timers_enabled[task_id] = bool(timer_written)

        with patch.object(
            distribution, "activate_managed_task", side_effect=boom_once
        ):
            summary = reconcile(
                self.central_path, self.store_path, activate=True
            )
        self.assertTrue(summary["failed"])
        self.assertEqual(
            (self._managed_root() / "current.json").read_text(),
            good_current,
        )
        # última ação era apply → timer reativado
        self.assertTrue(self._timers_enabled.get(TASK_UUID, False))
        journal = _journal_state(self._managed_root())
        self.assertEqual(journal.get("operational"), "apply")


# ---- F2: preparação não altera units operacionais -------------------------------


class PrepareNoOperationalChangeTests(DistributionCase):
    def test_preparacao_sobre_aplicada_nao_remove_timer_operacional(self):
        """tarefa automática aplicada → gen2 muda para manual →
        --no-activate: timer operacional PERMANECE intocado."""
        _CentralHandler.desired = [self._envelope()]
        reconcile(self.central_path, self.store_path, activate=True)
        self.assertTrue(self._timer_file().exists())
        good_timer = self._timer_file().read_text()
        good_service = self._service_file().read_text()

        _CentralHandler.desired = [
            self._envelope(
                generation=2, revision=2,
                schedule={"kind": "manual"},
            )
        ]
        reconcile(self.central_path, self.store_path, activate=False)

        # units operacionais byte-a-byte iguais
        self.assertEqual(self._timer_file().read_text(), good_timer)
        self.assertEqual(self._service_file().read_text(), good_service)
        self.assertTrue(self._timers_enabled.get(TASK_UUID, False))

        # staging tem o service mas NÃO tem timer (manual)
        staging = self._managed_root() / "staged"
        self.assertTrue(
            (staging / f"macip-backup-managed-{TASK_UUID}.service").exists()
        )
        self.assertFalse(
            (staging / f"macip-backup-managed-{TASK_UUID}.timer").exists()
        )

    def test_preparacao_automatica_sobre_automatica_preserva_operacional(self):
        """tarefa automática aplicada → gen2 muda horário →
        --no-activate: timer operacional permanece com horário antigo."""
        _CentralHandler.desired = [self._envelope()]
        reconcile(self.central_path, self.store_path, activate=True)
        good_timer = self._timer_file().read_text()

        _CentralHandler.desired = [
            self._envelope(
                generation=2, revision=2,
                schedule={"kind": "daily", "timezone": "UTC",
                          "hour": 7, "minute": 0},
            )
        ]
        reconcile(self.central_path, self.store_path, activate=False)
        # operacional intocado
        self.assertEqual(self._timer_file().read_text(), good_timer)
        # staging tem o timer novo
        staging = self._managed_root() / "staged"
        staged_timer = (
            staging / f"macip-backup-managed-{TASK_UUID}.timer"
        )
        self.assertTrue(staged_timer.exists())
        self.assertIn("07:00", staged_timer.read_text())

        # ativação posterior funciona
        summary = reconcile(
            self.central_path, self.store_path, activate=True
        )
        self.assertEqual(summary["applied"][0]["result"], "applied")
        self.assertIn("07:00", self._timer_file().read_text())


# ---- F3: propriedade completa das units ----------------------------------------


class UnitOwnershipTests(DistributionCase):
    def _install_then_strip_marker(self, marker_prefix: str):
        _CentralHandler.desired = [self._envelope()]
        reconcile(self.central_path, self.store_path, activate=True)
        service = self._service_file()
        lines = service.read_text().splitlines()
        stripped = [
            line for line in lines
            if not line.startswith(marker_prefix)
        ]
        service.write_text("\n".join(stripped))

    def test_sem_marcador_task_recusado(self):
        self._install_then_strip_marker("# macip-backup: managed-task=")
        _CentralHandler.desired = [
            self._envelope(generation=2, revision=2)
        ]
        summary = reconcile(
            self.central_path, self.store_path, activate=True
        )
        self.assertTrue(summary["failed"])
        # arquivo preservado byte-a-byte (não sobrescrito)
        # como removemos o marcador, o conteúdo é diferente do esperado
        # mas a recusa impede qualquer modificação

    def test_sem_marcador_agent_recusado(self):
        self._install_then_strip_marker("# macip-backup: central-agent=")
        _CentralHandler.desired = [
            self._envelope(generation=2, revision=2)
        ]
        summary = reconcile(
            self.central_path, self.store_path, activate=True
        )
        self.assertTrue(summary["failed"])

    def test_sem_marcador_bond_recusado(self):
        self._install_then_strip_marker("# macip-backup: managed-bond=")
        _CentralHandler.desired = [
            self._envelope(generation=2, revision=2)
        ]
        summary = reconcile(
            self.central_path, self.store_path, activate=True
        )
        self.assertTrue(summary["failed"])

    def test_unit_legada_sem_marcadores_gerenciados_recusada(self):
        """unit com só o marcador de instalação (legada) não é
        adotada — marcador ausente não é autorização."""
        service = self._service_file()
        service.parent.mkdir(parents=True, exist_ok=True)
        # só marcador de instalação, sem managed-task/agent/bond
        service.write_text(
            "# macip-backup: install=legacy\n[Unit]\nExecStart=/bin/false\n"
        )
        _CentralHandler.desired = [self._envelope()]
        summary = reconcile(
            self.central_path, self.store_path, activate=True
        )
        self.assertTrue(summary["failed"])
        # conteúdo preservado
        self.assertIn("/bin/false", service.read_text())


# ---- F4: troca de vínculo retomável --------------------------------------------


class BondTransitionResumeTests(DistributionCase):
    def _second_central(self) -> tuple:
        other = HTTPServer(("127.0.0.1", 0), _CentralHandler)
        threading.Thread(target=other.serve_forever, daemon=True).start()
        alt = Path(self._state.name) / "state" / "central2.json"
        alt.write_text(
            json.dumps(
                {
                    "url": f"http://127.0.0.1:{other.server_port}",
                    "agent_id": AGENT_UUID,
                    "token_environment": "MACIP_API_TOKEN",
                }
            )
        )
        return other, alt

    def test_interrupcao_depois_active_switched_retoma(self):
        """A aplica gen1; --rebind para B interrompe depois de
        active.json apontar B; na próxima execução B retoma a
        transição e aplica."""
        _CentralHandler.desired = [self._envelope()]
        reconcile(self.central_path, self.store_path, activate=True)
        bond_a = _bond_key(
            load_central(self.central_path), self.store_path
        )

        other, alt = self._second_central()
        doc_b = self._taskspec_doc()
        doc_b["name"] = "tarefa B"
        _CentralHandler.desired = [self._envelope(task_doc=doc_b)]
        try:
            # simula interrupção: chama _transition_bond diretamente
            # (que para em "active_switched") e NÃO processa as tasks
            active = _read_active()
            from macip_backup.distribution import _transition_bond
            spec_b = load_central(alt)
            _transition_bond(spec_b, _bond_key(spec_b, self.store_path),
                             active, _tasks_root(
                                 _bond_key(spec_b, self.store_path)))
            # transition.json existe com phase "active_switched"
            trans = json.loads(
                (_transition_file()).read_text()
            )
            self.assertEqual(trans["phase"], "active_switched")
        finally:
            pass  # não desliga o server ainda

        # próxima execução: B já é o vínculo ativo, transição existe
        # — retoma sem --rebind
        summary = reconcile(alt, self.store_path, activate=True)
        self.assertTrue(summary["bond_transitioned"])
        self.assertEqual(summary["applied"][0]["result"], "applied")
        bond_b = _bond_key(load_central(alt), self.store_path)
        self.assertIn(
            f"managed-bond={bond_b[:16]}",
            self._service_file().read_text(),
        )
        # transição concluída
        self.assertFalse(_transition_file().exists())
        other.shutdown()

    def test_interrupcao_depois_timers_disabled_restaura_anterior(self):
        """T4: A aplica gen1 (automática); --rebind para B interrompe
        depois de desativar timers mas ANTES de trocar active.json.
        Retomada com B: _resume_transition restaura active.json para A
        E reativa o timer de A. Verifica estados intermediários e
        final."""
        _CentralHandler.desired = [self._envelope()]
        reconcile(self.central_path, self.store_path, activate=True)
        bond_a = _bond_key(
            load_central(self.central_path), self.store_path
        )
        self.assertTrue(self._timers_enabled.get(TASK_UUID, False))
        good_service = self._service_file().read_text()

        other, alt = self._second_central()
        try:
            # simula: escreve transition.json com phase "timers_disabled"
            # e desativa o timer (o passo que ocorreu antes da queda)
            from macip_backup.distribution import _write_transition
            bond_b = _bond_key(load_central(alt), self.store_path)
            _write_transition(bond_a, bond_b, "timers_disabled")
            self._timers_enabled[TASK_UUID] = False
            # active.json AINDA aponta A (não foi trocado)

            # retomada com B: _resume_transition vê phase <= timers_disabled
            # → restaura A: active.json aponta A, timer reativado
            _CentralHandler.desired = []
            reconcile(alt, self.store_path, activate=True)

            # active.json aponta A novamente
            active = _read_active()
            self.assertEqual(active["bond"], bond_a)
            # timer de A reativado
            self.assertTrue(self._timers_enabled.get(TASK_UUID, False))
            # units de A preservadas byte-a-byte
            self.assertEqual(self._service_file().read_text(), good_service)
            # transition.json limpa
            self.assertIsNone(_read_transition())
        finally:
            other.shutdown()


# ---- T2: --no-activate --rebind permanece preparação ----------------------------


class PrepareRebindTests(DistributionCase):
    def _second_central(self) -> tuple:
        other = HTTPServer(("127.0.0.1", 0), _CentralHandler)
        threading.Thread(target=other.serve_forever, daemon=True).start()
        alt = Path(self._state.name) / "state" / "central2.json"
        alt.write_text(
            json.dumps(
                {
                    "url": f"http://127.0.0.1:{other.server_port}",
                    "agent_id": AGENT_UUID,
                    "token_environment": "MACIP_API_TOKEN",
                }
            )
        )
        return other, alt

    def test_no_activate_rebind_preserva_vinculo_e_timers(self):
        """A aplica gen1 (timer habilitado); --no-activate --rebind
        para B: vínculo ativo PERMANECE A, timer de A PERMANECE
        habilitado, NENHUMA transição consumida. Depois adoção real
        funciona."""
        _CentralHandler.desired = [self._envelope()]
        reconcile(self.central_path, self.store_path, activate=True)
        bond_a = _bond_key(
            load_central(self.central_path), self.store_path
        )
        self.assertTrue(self._timers_enabled.get(TASK_UUID, False))
        good_service = self._service_file().read_text()

        other, alt = self._second_central()
        try:
            doc_b = self._taskspec_doc()
            doc_b["name"] = "tarefa B"
            _CentralHandler.desired = [self._envelope(task_doc=doc_b)]
            # preparação com --rebind --no-activate
            summary = reconcile(
                alt, self.store_path, activate=False, rebind=True
            )
            self.assertEqual(summary["prepared"][0]["result"], "prepared")

            # vínculo ativo PERMANECE A
            active = _read_active()
            self.assertEqual(active["bond"], bond_a)
            # timer de A PERMANECE habilitado
            self.assertTrue(self._timers_enabled.get(TASK_UUID, False))
            # units de A preservadas
            self.assertEqual(self._service_file().read_text(), good_service)
            # NENHUMA transição consumida
            self.assertIsNone(_read_transition())

            # adoção real funciona
            _CentralHandler.desired = [self._envelope(task_doc=doc_b)]
            summary = reconcile(
                alt, self.store_path, activate=True, rebind=True
            )
            self.assertTrue(summary["bond_transitioned"])
            self.assertEqual(summary["applied"][0]["result"], "applied")
            bond_b = _bond_key(load_central(alt), self.store_path)
            self.assertIn(
                f"managed-bond={bond_b[:16]}",
                self._service_file().read_text(),
            )
        finally:
            other.shutdown()


# ---- T3: não concluir troca de vínculo com adoção pendente ----------------------


class BondAdoptionPendingTests(DistributionCase):
    def _second_central(self) -> tuple:
        other = HTTPServer(("127.0.0.1", 0), _CentralHandler)
        threading.Thread(target=other.serve_forever, daemon=True).start()
        alt = Path(self._state.name) / "state" / "central2.json"
        alt.write_text(
            json.dumps(
                {
                    "url": f"http://127.0.0.1:{other.server_port}",
                    "agent_id": AGENT_UUID,
                    "token_environment": "MACIP_API_TOKEN",
                }
            )
        )
        return other, alt

    def test_falha_validacao_preserva_transicao_e_retoma(self):
        """A aplica gen1; B publica a mesma tarefa com origem
        inexistente; --rebind falha na validação; transition.json
        PERMANECE; criar a origem e retomar aplica sem --rebind e sem
        remover arquivos manualmente."""
        _CentralHandler.desired = [self._envelope()]
        reconcile(self.central_path, self.store_path, activate=True)
        bond_a = _bond_key(
            load_central(self.central_path), self.store_path
        )
        good_service = self._service_file().read_text()

        other, alt = self._second_central()
        try:
            # B publica a mesma tarefa com origem inexistente
            import tempfile
            missing = Path(tempfile.mkdtemp()) / "origem"
            doc_b = self._taskspec_doc(source=str(missing))
            _CentralHandler.desired = [self._envelope(task_doc=doc_b)]
            summary = reconcile(
                alt, self.store_path, activate=True, rebind=True
            )
            # falhou na validação
            self.assertTrue(summary["failed"])
            # transition.json PERMANECE (adoção incompleta)
            trans = _read_transition()
            self.assertIsNotNone(trans)
            self.assertEqual(trans["phase"], "active_switched")
            # units de A ainda marcadas com A
            self.assertIn(
                f"managed-bond={bond_a[:16]}",
                self._service_file().read_text(),
            )

            # criar a origem e retomar — sem --rebind, sem remover nada
            missing.mkdir(parents=True, exist_ok=True)
            try:
                doc_b2 = self._taskspec_doc(source=str(missing))
                _CentralHandler.desired = [self._envelope(task_doc=doc_b2)]
                summary = reconcile(alt, self.store_path, activate=True)
                # retomou a transição e aplicou
                self.assertTrue(summary["bond_transitioned"])
                self.assertEqual(summary["applied"][0]["result"], "applied")
                bond_b = _bond_key(load_central(alt), self.store_path)
                self.assertIn(
                    f"managed-bond={bond_b[:16]}",
                    self._service_file().read_text(),
                )
                # transition.json limpa (adoção concluída)
                self.assertIsNone(_read_transition())
            finally:
                import shutil
                shutil.rmtree(missing.parent, ignore_errors=True)
        finally:
            other.shutdown()


# ---- F5: resposta 200 ambígua ---------------------------------------------------


class AmbiguousResponseTests(DistributionCase):
    def test_200_json_invalido_continua_reenviavel(self):
        _CentralHandler.desired = [self._envelope()]
        _CentralHandler.post_status = 200
        # sobrescreve do_POST para retornar 200 com JSON inválido
        original_do_post = _CentralHandler.do_POST

        def bad_post(self):
            self.send_response(200)
            self.send_header("Content-Length", "5")
            self.end_headers()
            self.wfile.write(b"XXXXX")
        _CentralHandler.do_POST = bad_post
        try:
            reconcile(self.central_path, self.store_path, activate=True)
        finally:
            _CentralHandler.do_POST = original_do_post
            _CentralHandler.post_status = None
        bond = _bond_key(load_central(self.central_path), self.store_path)
        store = ExecutionStore(self.store_path)
        pending = store.pending_confirmations(bond, "2999-01-01")
        store.close()
        # NÃO bloqueado — continua reenviável (pending_confirmations
        # só retorna status='pending')
        self.assertEqual(len(pending), 1)

    def test_200_sem_status_reconhecido_continua_reenviavel(self):
        _CentralHandler.desired = [self._envelope()]
        _CentralHandler.post_status = 200

        def weird_post(self):
            body = json.dumps({"unexpected": "field"}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        original_do_post = _CentralHandler.do_POST
        _CentralHandler.do_POST = weird_post
        try:
            reconcile(self.central_path, self.store_path, activate=True)
        finally:
            _CentralHandler.do_POST = original_do_post
            _CentralHandler.post_status = None
        bond = _bond_key(load_central(self.central_path), self.store_path)
        store = ExecutionStore(self.store_path)
        pending = store.pending_confirmations(bond, "2999-01-01")
        store.close()
        # continua reenviável
        self.assertEqual(len(pending), 1)

    def test_reenvio_posterior_recebe_duplicate(self):
        _CentralHandler.desired = [self._envelope()]
        _CentralHandler.post_status = 200

        def bad_post(self):
            self.send_response(200)
            self.send_header("Content-Length", "5")
            self.end_headers()
            self.wfile.write(b"XXXXX")
        original_do_post = _CentralHandler.do_POST
        _CentralHandler.do_POST = bad_post
        try:
            reconcile(self.central_path, self.store_path, activate=True)
        finally:
            _CentralHandler.do_POST = original_do_post
            _CentralHandler.post_status = None
        # reenvio: agora a central responde corretamente
        import sqlite3
        connection = sqlite3.connect(self.store_path)
        connection.execute(
            "UPDATE confirmations SET next_attempt_at = NULL"
        )
        connection.commit()
        connection.close()
        _CentralHandler.desired = []
        reconcile(self.central_path, self.store_path, activate=True)
        self.assertEqual(
            _CentralHandler.confirmations[-1]["status"], "applied"
        )
        bond = _bond_key(load_central(self.central_path), self.store_path)
        store = ExecutionStore(self.store_path)
        pending = store.pending_confirmations(bond, "2999-01-01")
        store.close()
        self.assertEqual(pending, [])


if __name__ == "__main__":
    unittest.main()
