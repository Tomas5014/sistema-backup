"""Acompanhamento de tarefas — semântica + integração PostgreSQL real.

Duas camadas separadas:
- unidade pura (sem banco): ocorrências IANA/DST e compute() com
  relógio injetável — cobrem bordas temporais sem esperar tempo real;
- integração (requires_db): rotas HTTP reais — o relógio é viajado por
  monkeypatch de app.datetime, que também grava last_applied_at.

A ausência de resultado NUNCA é apresentada como falha do backup nem
como agente offline — testes abaixo verificam essa distinção.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from macip_backup_api import monitoring

from tests.conftest import (
    execution_payload,
    login,
    make_operator,
    requires_db,
)
from tests.test_tasks import task_payload

Z = "%Y-%m-%dT%H:%M:%SZ"


def _at(text: str) -> datetime:
    return datetime.strptime(text, Z).replace(tzinfo=UTC)


def _exec(status: str, started: str, received: str | None = None):
    start = _at(started)
    return SimpleNamespace(
        id=uuid.uuid4(),
        execution_id=str(uuid.uuid4()),
        status=status,
        started_at_utc=start,
        finished_at_utc=start,
        received_at=_at(received) if received else start,
    )


def _dist(
    action: str = "apply",
    generation: int = 1,
    confirmed_status: str | None = "applied",
    last_applied_revision: int | None = 1,
    last_applied_at=None,
):
    return SimpleNamespace(
        desired_action=action,
        generation=generation,
        published_revision=1,
        confirmed_generation=generation if confirmed_status else None,
        confirmed_status=confirmed_status,
        confirmed_revision=last_applied_revision,
        last_applied_revision=last_applied_revision,
        last_applied_at=last_applied_at,
    )


def _compute(dist, schedule, executions=(), last_ok=None, now="2026-03-10T12:00:00Z",
             tolerance=1800, basis=1, last_effect="apply"):
    return monitoring.compute(
        distribution=dist,
        applied_schedule=schedule,
        basis_revision=basis,
        executions=executions,
        last_success=last_ok,
        last_effect=last_effect,
        now=_at(now),
        tolerance=tolerance,
    )


DAILY_SP_14 = {"kind": "daily", "timezone": "America/Sao_Paulo",
               "hour": 14, "minute": 0}  # 17:00 UTC


# ---- unidade: ocorrências ----------------------------------------------------


def test_daily_occurrence_uses_iana_timezone_not_browser():
    # 14:00 em São Paulo = 17:00 UTC — a ocorrência é interpretada no
    # fuso do CONTRATO, nunca no fuso do observador
    nxt = monitoring.next_occurrence(DAILY_SP_14, _at("2026-03-09T18:00:00Z"))
    assert nxt == _at("2026-03-10T17:00:00Z")
    assert monitoring.previous_occurrence(
        DAILY_SP_14, _at("2026-03-09T18:00:00Z")
    ) == _at("2026-03-09T17:00:00Z")


def test_hourly_occurrence_minute():
    sched = {"kind": "hourly", "timezone": "UTC", "minute": 45}
    assert monitoring.next_occurrence(sched, _at("2026-03-09T10:44:00Z")) == _at(
        "2026-03-09T10:45:00Z"
    )
    assert monitoring.previous_occurrence(
        sched, _at("2026-03-09T10:44:59Z")
    ) == _at("2026-03-09T09:45:00Z")


def test_weekly_occurrence_filters_weekdays():
    sched = {"kind": "weekly", "timezone": "UTC", "hour": 8, "minute": 0,
             "weekdays": [1, 5]}  # seg e sex
    # quarta 2026-03-11 12:00 UTC → próxima é sexta
    nxt = monitoring.next_occurrence(sched, _at("2026-03-11T12:00:00Z"))
    assert nxt == _at("2026-03-13T08:00:00Z")
    # a anterior é segunda
    assert monitoring.previous_occurrence(
        sched, _at("2026-03-11T12:00:00Z")
    ) == _at("2026-03-09T08:00:00Z")


def test_dst_gap_skips_nonexistent_local_time():
    # 2026-03-08 02:30 não existe em New York (salto) — como o systemd,
    # a ocorrência do dia é pulada; a próxima válida é 09/03
    sched = {"kind": "daily", "timezone": "America/New_York",
             "hour": 2, "minute": 30}
    nxt = monitoring.next_occurrence(sched, _at("2026-03-08T05:00:00Z"))
    assert nxt == _at("2026-03-09T06:30:00Z")
    assert monitoring.previous_occurrence(
        sched, _at("2026-03-09T05:00:00Z")
    ) == _at("2026-03-07T07:30:00Z")


def test_dst_ambiguous_uses_first_occurrence():
    # 2026-11-01 01:30 ocorre duas vezes — fold=0, a primeira
    sched = {"kind": "daily", "timezone": "America/New_York",
             "hour": 1, "minute": 30}
    assert monitoring.next_occurrence(
        sched, _at("2026-11-01T04:00:00Z")
    ) == _at("2026-11-01T05:30:00Z")


def test_manual_has_no_occurrences():
    sched = {"kind": "manual"}
    assert monitoring.next_occurrence(sched, _at("2026-03-09T00:00:00Z")) is None
    assert monitoring.previous_occurrence(sched, _at("2026-03-09T00:00:00Z")) is None


# ---- unidade: estado operacional --------------------------------------------


def test_operational_states_from_distribution():
    assert _compute(None, None).operational_state == "unpublished"
    assert _compute(_dist("none"), None).operational_state == "unpublished"
    assert _compute(
        _dist("apply", confirmed_status=None), DAILY_SP_14
    ).operational_state == "pending_apply"
    failed = _compute(
        _dist("apply", confirmed_status="failed"), DAILY_SP_14
    )
    assert failed.operational_state == "pending_apply"
    assert "falhou" in failed.operational_reason
    assert _compute(_dist("apply"), {"kind": "manual"}).operational_state == "manual"
    assert _compute(_dist("apply"), DAILY_SP_14).operational_state == "scheduled"
    assert _compute(
        _dist("suspend", confirmed_status=None), DAILY_SP_14
    ).operational_state == "pending_suspend"
    assert _compute(_dist("suspend"), DAILY_SP_14).operational_state == "suspended"


# ---- unidade: resultados ------------------------------------------------------


def test_result_no_history_vs_awaiting():
    # agenda confirmada, nada recebido e nada vencido → no_history;
    # com histórico recebido → awaiting
    anchor = _at("2026-03-09T12:00:00Z")
    dist = _dist(last_applied_at=anchor)
    empty = _compute(dist, DAILY_SP_14, now="2026-03-09T13:00:00Z")
    assert empty.result_state == "no_history"
    assert empty.next_occurrence_utc == _at("2026-03-09T17:00:00Z")
    old = _exec("success", "2026-03-01T17:00:00Z")
    waiting = _compute(dist, DAILY_SP_14, [old], last_ok=old,
                       now="2026-03-09T13:00:00Z")
    assert waiting.result_state == "awaiting"
    assert waiting.reason_code == "awaiting-occurrence"


def test_result_pending_then_missing_at_exact_tolerance_boundary():
    anchor = _at("2026-03-09T12:00:00Z")
    dist = _dist(last_applied_at=anchor)
    due = _at("2026-03-09T17:00:00Z")
    # no instante do prazo ainda é tolerância; um segundo depois, falta
    at_deadline = _compute(dist, DAILY_SP_14, now="2026-03-09T17:30:00Z")
    assert at_deadline.result_state == "pending"
    assert at_deadline.expected_occurrence_utc == due
    assert at_deadline.result_deadline_utc == _at("2026-03-09T17:30:00Z")
    late = _compute(dist, DAILY_SP_14, now="2026-03-09T17:30:01Z")
    assert late.result_state == "missing"
    assert late.reason_code == "overdue"
    assert "não prova falha" in late.detail


def test_result_success_partial_failed_mapping():
    anchor = _at("2026-03-09T12:00:00Z")
    dist = _dist(last_applied_at=anchor)
    now = "2026-03-09T17:10:00Z"
    ok = _compute(dist, DAILY_SP_14,
                  [_exec("success", "2026-03-09T17:00:20Z")], now=now)
    assert ok.result_state == "ok" and ok.reason_code == "result-success"
    part = _compute(dist, DAILY_SP_14,
                    [_exec("partial", "2026-03-09T17:00:20Z")], now=now)
    assert part.result_state == "partial"
    failed = _compute(dist, DAILY_SP_14,
                      [_exec("failed", "2026-03-09T17:00:20Z")], now=now)
    assert failed.result_state == "failed"
    interrupted = _compute(dist, DAILY_SP_14,
                           [_exec("interrupted", "2026-03-09T17:00:20Z")], now=now)
    assert interrupted.result_state == "failed"
    assert interrupted.reason_code == "result-interrupted"


def test_execution_started_before_occurrence_does_not_satisfy():
    anchor = _at("2026-03-09T12:00:00Z")
    dist = _dist(last_applied_at=anchor)
    early = _exec("success", "2026-03-09T16:59:00Z")
    result = _compute(dist, DAILY_SP_14, [early],
                      now="2026-03-09T18:00:00Z")
    assert result.result_state == "missing"  # execução anterior não conta


def test_multiple_results_same_window_latest_started_wins():
    anchor = _at("2026-03-09T12:00:00Z")
    dist = _dist(last_applied_at=anchor)
    now = "2026-03-09T18:00:00Z"
    fail_then_ok = _compute(
        dist, DAILY_SP_14,
        [_exec("failed", "2026-03-09T17:00:20Z"),
         _exec("success", "2026-03-09T17:40:00Z")],
        now=now,
    )
    assert fail_then_ok.result_state == "ok"
    # fora de ordem: a falha chegou DEPOIS — ainda vence quem começou por último
    out_of_order = _compute(
        dist, DAILY_SP_14,
        [_exec("success", "2026-03-09T17:40:00Z", "2026-03-09T17:45:00Z"),
         _exec("failed", "2026-03-09T17:00:20Z", "2026-03-09T17:50:00Z")],
        now=now,
    )
    assert out_of_order.result_state == "ok"


def test_late_received_success_marks_received_late():
    anchor = _at("2026-03-09T12:00:00Z")
    dist = _dist(last_applied_at=anchor)
    late = _exec("success", "2026-03-09T17:00:20Z", "2026-03-09T18:00:00Z")
    result = _compute(dist, DAILY_SP_14, [late],
                      now="2026-03-09T18:30:00Z")
    assert result.result_state == "ok"
    assert result.received_late is True
    assert result.reason_code == "result-success-late"


def test_occurrences_before_apply_anchor_are_never_charged():
    # âncora = last_applied_at: publicada hoje não cobra ocorrência de ontem
    anchor = _at("2026-03-09T18:30:00Z")
    dist = _dist(last_applied_at=anchor)
    result = _compute(dist, DAILY_SP_14, now="2026-03-09T19:00:00Z")
    assert result.result_state == "no_history"
    assert result.next_occurrence_utc == _at("2026-03-10T17:00:00Z")
    # e uma execução feita ANTES da instalação não satisfaz a próxima
    old = _exec("success", "2026-03-09T17:00:10Z")
    result2 = _compute(dist, DAILY_SP_14, [old],
                       now="2026-03-10T18:00:00Z")
    assert result2.result_state == "missing"


def test_revision_change_reanchors_without_reinterpreting_history():
    # nova aplicação confirmada move a âncora: execuções da agenda antiga
    # não satisfazem a ocorrência da agenda nova
    anchor_new = _at("2026-03-10T12:30:00Z")
    dist = _dist(generation=3, last_applied_revision=4,
                 last_applied_at=anchor_new)
    old_exec = _exec("success", "2026-03-10T11:00:00Z")
    hourly = {"kind": "hourly", "timezone": "UTC", "minute": 0}
    result = _compute(dist, hourly, [old_exec], now="2026-03-10T13:31:00Z",
                      basis=4)
    # devida 13:00 (> anchor); a execução das 11h é anterior → missing
    assert result.result_state == "missing"
    assert result.expected_occurrence_utc == _at("2026-03-10T13:00:00Z")


def test_suspended_and_manual_are_informational_not_charged():
    anchor = _at("2026-03-09T12:00:00Z")
    suspended = _dist("suspend", last_applied_at=anchor)
    result = _compute(suspended, DAILY_SP_14, now="2026-03-10T19:00:00Z",
                      last_effect="suspend")
    assert result.operational_state == "suspended"
    assert result.result_state == "no_history"  # sem cobrança
    manual = _dist("apply", last_applied_at=anchor)
    manual_result = _compute(manual, {"kind": "manual"},
                             now="2026-03-10T19:00:00Z")
    assert manual_result.operational_state == "manual"
    assert manual_result.result_state == "no_history"


def test_pending_suspend_keeps_monitoring_old_schedule():
    anchor = _at("2026-03-09T12:00:00Z")
    dist = _dist("suspend", confirmed_status=None, last_applied_at=anchor)
    result = _compute(dist, DAILY_SP_14, now="2026-03-09T18:00:00Z")
    assert result.operational_state == "pending_suspend"
    assert result.result_state == "missing"  # timer antigo ainda ativo


# ---- integração: rotas + PostgreSQL real -------------------------------------

# `received_at` usa o relógio do BANCO (func.now()) — os instantes
# "falsos" abaixo são calculados a partir do agora real, nunca fixos.

pytestmark = requires_db


class _FrozenNow(datetime):
    """Relógio viajável — datetime.now(tz) dentro de app.py → VALUE."""
    VALUE = datetime.now(UTC)

    @classmethod
    def now(cls, tz=None):
        return cls.VALUE if tz is None else cls.VALUE.astimezone(tz)


def _freeze(monkeypatch, instant: datetime):
    _FrozenNow.VALUE = instant
    import macip_backup_api.app as app_module

    monkeypatch.setattr(app_module, "datetime", _FrozenNow)


def _schedule_due_at(instant: datetime) -> dict:
    """Agenda diária UTC cuja ocorrência de hoje é `instant` (minuto)."""
    local = instant.astimezone(UTC)
    return {"kind": "daily", "timezone": "UTC",
            "hour": local.hour, "minute": local.minute}


def _apply(client, admin_headers, agent_headers, agent_id, schedule,
           monkeypatch, applied_at):
    """Fluxo real: cria tarefa → publica → agente confirma aplicação
    com `last_applied_at` = applied_at (relógio injetado)."""
    _freeze(monkeypatch, applied_at)
    created = client.post(
        f"/api/v1/agents/{agent_id}/tasks",
        json=task_payload(schedule=schedule),
        headers=admin_headers,
    )
    assert created.status_code == 201, created.text
    task = created.json()
    published = client.post(
        f"/api/v1/tasks/{task['id']}/publish",
        json={"revision": 1, "expected_generation": 0},
        headers=admin_headers,
    )
    assert published.status_code == 200, published.text
    confirmed = client.post(
        f"/api/v1/agent/tasks/{task['id']}/confirmations",
        json={"generation": 1, "attempt": 1, "status": "applied",
              "applied_revision": 1},
        headers=agent_headers,
    )
    assert confirmed.status_code == 200, confirmed.text
    return task


def test_monitoring_requires_viewer_not_agent_token(client, agent,
                                                    admin_headers):
    task = client.post(
        f"/api/v1/agents/{agent['agent_id']}/tasks",
        json=task_payload(), headers=admin_headers,
    ).json()
    anon = client.get(f"/api/v1/monitoring/tasks/{task['id']}")
    assert anon.status_code in (401, 403)
    # token de agente não lê acompanhamento — é credencial do dispositivo
    as_agent = client.get(
        f"/api/v1/monitoring/tasks/{task['id']}", headers=agent["headers"]
    )
    assert as_agent.status_code == 403


def test_operational_progression_publish_to_apply(client, agent,
                                                  admin_headers, monkeypatch):
    task = client.post(
        f"/api/v1/agents/{agent['agent_id']}/tasks",
        json=task_payload(), headers=admin_headers,
    ).json()
    body = client.get(f"/api/v1/monitoring/tasks/{task['id']}", headers=admin_headers).json()
    assert body["operational_state"] == "unpublished"
    assert body["result_state"] == "no_history"
    assert body["last_execution"] is None

    client.post(
        f"/api/v1/tasks/{task['id']}/publish",
        json={"revision": 1, "expected_generation": 0},
        headers=admin_headers,
    )
    body = client.get(f"/api/v1/monitoring/tasks/{task['id']}", headers=admin_headers).json()
    assert body["operational_state"] == "pending_apply"
    # publicar NÃO anuncia agenda ativa — falta a confirmação do agente

    real = datetime.now(UTC).replace(second=0, microsecond=0)
    anchor = real - timedelta(hours=3)
    future = real + timedelta(hours=2)
    _freeze(monkeypatch, anchor)
    confirmed = client.post(
        f"/api/v1/agent/tasks/{task['id']}/confirmations",
        json={"generation": 1, "attempt": 1, "status": "applied",
              "applied_revision": 1},
        headers=agent["headers"],
    )
    assert confirmed.status_code == 200, confirmed.text
    # agenda aplicada = a da revisão 1 (diária 02:15 SP) — ocorrência de
    # hoje é anterior à âncora → nada a cobrar ainda
    _freeze(monkeypatch, real)
    body = client.get(f"/api/v1/monitoring/tasks/{task['id']}", headers=admin_headers).json()
    assert body["operational_state"] == "scheduled"
    assert body["result_state"] == "no_history"
    assert body["basis_revision"] == 1
    assert body["next_occurrence_utc"] is not None


def test_result_cycle_pending_missing_success_via_api(client, agent,
                                                      admin_headers,
                                                      monkeypatch):
    real = datetime.now(UTC)
    due = (real - timedelta(hours=1)).replace(second=0, microsecond=0)
    anchor = real - timedelta(hours=3)
    schedule = _schedule_due_at(due)
    task = _apply(client, admin_headers, agent["headers"],
                  agent["agent_id"], schedule, monkeypatch, anchor)

    _freeze(monkeypatch, due + timedelta(minutes=10))  # dentro da tolerância
    body = client.get(f"/api/v1/monitoring/tasks/{task['id']}", headers=admin_headers).json()
    assert body["result_state"] == "pending"
    assert body["expected_occurrence_utc"] is not None
    assert body["reason_code"] == "within-tolerance"

    _freeze(monkeypatch, due + timedelta(hours=2))  # prazo vencido
    body = client.get(f"/api/v1/monitoring/tasks/{task['id']}", headers=admin_headers).json()
    assert body["result_state"] == "missing"
    assert body["reason_code"] == "overdue"
    assert "não prova falha" in body["detail"]

    # resultado chega depois do prazo — sucesso vence e marca received_late
    posted = client.post(
        "/api/v1/agent/executions",
        json=execution_payload(
            task_id=task["id"],
            status="success",
            started_at_utc=(due + timedelta(seconds=15)).strftime(Z),
            finished_at_utc=(due + timedelta(minutes=20)).strftime(Z),
        ),
        headers=agent["headers"],
    )
    assert posted.status_code in (200, 201), posted.text
    body = client.get(f"/api/v1/monitoring/tasks/{task['id']}", headers=admin_headers).json()
    assert body["result_state"] == "ok"
    assert body["last_execution"]["status"] == "success"
    # received_at é o AGORA real do banco — após o prazo → atrasado
    assert body["received_late"] is True


def test_suspend_pending_then_confirmed(client, agent, admin_headers,
                                        monkeypatch):
    real = datetime.now(UTC)
    due = (real - timedelta(hours=1)).replace(second=0, microsecond=0)
    anchor = real - timedelta(hours=3)
    task = _apply(client, admin_headers, agent["headers"],
                  agent["agent_id"], _schedule_due_at(due), monkeypatch,
                  anchor)
    dist = client.get(
        f"/api/v1/tasks/{task['id']}", headers=admin_headers
    ).json()["distribution"]
    suspended = client.post(
        f"/api/v1/tasks/{task['id']}/suspend",
        json={"expected_generation": dist["generation"]},
        headers=admin_headers,
    )
    assert suspended.status_code == 200, suspended.text
    new_gen = suspended.json()["distribution"]["generation"]

    _freeze(monkeypatch, due + timedelta(hours=2))
    body = client.get(f"/api/v1/monitoring/tasks/{task['id']}", headers=admin_headers).json()
    assert body["operational_state"] == "pending_suspend"
    assert body["result_state"] == "missing"  # timer antigo segue ativo

    confirmed = client.post(
        f"/api/v1/agent/tasks/{task['id']}/confirmations",
        json={"generation": new_gen, "attempt": 1,
              "status": "applied", "applied_revision": None},
        headers=agent["headers"],
    )
    assert confirmed.status_code == 200, confirmed.text
    _freeze(monkeypatch, due + timedelta(days=2))
    body = client.get(f"/api/v1/monitoring/tasks/{task['id']}", headers=admin_headers).json()
    assert body["operational_state"] == "suspended"
    assert body["result_state"] == "no_history"  # sem cobrança nova


def test_list_monitoring_filters_pagination_and_constant_queries(
    client, agent, admin_headers, monkeypatch,
):
    real = datetime.now(UTC)
    due = (real - timedelta(hours=1)).replace(second=0, microsecond=0)
    anchor = real - timedelta(hours=3)
    schedule = _schedule_due_at(due)
    for _ in range(6):
        _apply(client, admin_headers, agent["headers"], agent["agent_id"],
               schedule, monkeypatch, anchor)
    _freeze(monkeypatch, due + timedelta(hours=2))

    from macip_backup_api import db as db_module
    from sqlalchemy import event

    engine = db_module.engine()
    counts = {"n": 0}

    @event.listens_for(engine, "before_cursor_execute")
    def _count(*_):
        counts["n"] += 1

    try:
        page = client.get("/api/v1/monitoring/tasks?limit=3&offset=0",
                        headers=admin_headers)
        assert page.status_code == 200, page.text
        first = counts["n"]
        page2 = client.get("/api/v1/monitoring/tasks?limit=3&offset=3",
                         headers=admin_headers)
        second = counts["n"] - first
    finally:
        event.remove(engine, "before_cursor_execute", _count)

    assert len(page.json()) == 3 and len(page2.json()) == 3
    # consultas em lote: número FIXO por página, independente do volume
    assert first <= 10 and second <= 10
    assert all(r["result_state"] == "missing" for r in page.json())

    by_agent = client.get(
        f"/api/v1/monitoring/tasks?agent_id={agent['agent_id']}&limit=1",
        headers=admin_headers,
    )
    assert len(by_agent.json()) == 1
    # agente inexistente → página vazia, não erro
    empty = client.get(
        f"/api/v1/monitoring/tasks?agent_id={uuid.uuid4()}",
        headers=admin_headers,
    )
    assert empty.json() == []


def test_manual_task_is_informational_not_charged(client, agent,
                                                  admin_headers, monkeypatch):
    real = datetime.now(UTC)
    task = _apply(client, admin_headers, agent["headers"],
                  agent["agent_id"], {"kind": "manual"}, monkeypatch,
                  real - timedelta(hours=3))
    body = client.get(f"/api/v1/monitoring/tasks/{task['id']}", headers=admin_headers).json()
    assert body["operational_state"] == "manual"
    assert body["result_state"] == "no_history"
    assert body["next_occurrence_utc"] is None


def test_tolerance_env_configured_and_invalid_refused(client, agent,
                                                      admin_headers,
                                                      monkeypatch):
    real = datetime.now(UTC)
    due = (real - timedelta(hours=1)).replace(second=0, microsecond=0)
    anchor = real - timedelta(hours=3)
    task = _apply(client, admin_headers, agent["headers"],
                  agent["agent_id"], _schedule_due_at(due), monkeypatch,
                  anchor)
    monkeypatch.setenv("MACIP_API_MONITOR_TOLERANCE_SECONDS", "120")
    _freeze(monkeypatch, due + timedelta(minutes=10))
    body = client.get(f"/api/v1/monitoring/tasks/{task['id']}", headers=admin_headers).json()
    assert body["tolerance_seconds"] == 120
    assert body["result_state"] == "missing"  # prazo era due+2min

    monkeypatch.setenv("MACIP_API_MONITOR_TOLERANCE_SECONDS", "lixo")
    with pytest.raises(ValueError, match="TOLERANCE"):
        client.get(f"/api/v1/monitoring/tasks/{task['id']}",
                   headers=admin_headers)  # config inválida falha explícito


@pytest.fixture
def portal(client):
    """Client https — cookies de sessão têm flag Secure."""
    from fastapi.testclient import TestClient

    import macip_backup_api.app as app_module

    return TestClient(app_module.app, base_url="https://testserver")


def test_viewer_session_reads_monitoring(client, portal, admin_headers):
    make_operator(client, admin_headers, login="v-mon", role="viewer")
    assert login(portal, "v-mon", "senha-super-secreta-1").status_code == 200
    listed = portal.get("/api/v1/monitoring/tasks")
    assert listed.status_code == 200
    # viewer não escreve — POST de publicação segue negado pela sessão
    denied = portal.post("/api/v1/tasks/x/publish", json={})
    assert denied.status_code in (400, 403, 422)


# ---- defeito B: coerência temporal (started vs recebido) ---------------------

DAILY_UTC_14 = {"kind": "daily", "timezone": "UTC", "hour": 14, "minute": 0}


def test_future_success_does_not_satisfy_current_occurrence():
    # sucesso "de amanhã" recebido hoje: não pode cobrir a ocorrência de
    # hoje — started_at_utc é informado pelo agente e incoerente com o
    # recebimento do servidor
    anchor = _at("2026-11-06T10:00:00Z")
    dist = _dist(last_applied_at=anchor)
    future = _exec("success", "2026-11-07T14:00:00Z", "2026-11-06T15:00:00Z")
    result = _compute(dist, DAILY_UTC_14, [future], now="2026-11-06T15:00:00Z")
    assert result.result_state == "missing"


def test_future_row_does_not_mask_valid_failure():
    anchor = _at("2026-11-06T10:00:00Z")
    dist = _dist(last_applied_at=anchor)
    failed = _exec("failed", "2026-11-06T14:00:30Z", "2026-11-06T14:30:00Z")
    future = _exec("success", "2026-11-07T14:00:00Z", "2026-11-06T15:00:00Z")
    result = _compute(dist, DAILY_UTC_14, [failed, future],
                      now="2026-11-06T15:05:00Z")
    assert result.result_state == "failed"  # a falha válida prevalece


def test_skew_boundary_start_vs_server_receipt():
    # tolerância de desvio: o relógio do agente pode estar até 120 s à
    # frente do recebimento; além disso a linha é temporalmente inválida
    anchor = _at("2026-11-06T10:00:00Z")
    dist = _dist(last_applied_at=anchor)
    at_limit = _exec("success", "2026-11-06T14:02:00Z", "2026-11-06T14:00:00Z")
    over_limit = _exec("success", "2026-11-06T14:02:01Z", "2026-11-06T14:00:00Z")
    ok = _compute(dist, DAILY_UTC_14, [at_limit], now="2026-11-06T14:03:00Z")
    assert ok.result_state == "ok"  # +120 s: dentro da tolerância
    bad = _compute(dist, DAILY_UTC_14, [over_limit], now="2026-11-06T14:40:00Z")
    assert bad.result_state == "missing"  # +121 s: inválido, não satisfaz


def test_valid_late_receipt_and_out_of_order_still_work():
    anchor = _at("2026-11-06T10:00:00Z")
    dist = _dist(last_applied_at=anchor)
    late = _exec("success", "2026-11-06T14:00:20Z", "2026-11-06T15:00:00Z")
    result = _compute(dist, DAILY_UTC_14, [late], now="2026-11-06T15:10:00Z")
    assert result.result_state == "ok" and result.received_late is True
    # fora de ordem: sucesso mais novo vence falha recebida depois
    ooo = _compute(
        dist, DAILY_UTC_14,
        [_exec("success", "2026-11-06T14:40:00Z", "2026-11-06T14:45:00Z"),
         _exec("failed", "2026-11-06T14:00:20Z", "2026-11-06T14:50:00Z")],
        now="2026-11-06T15:10:00Z",
    )
    assert ooo.result_state == "ok"


# ---- defeito A: dono da execução ---------------------------------------------

def test_foreign_agent_same_task_id_does_not_contaminate(client, agent,
                                                       admin_headers,
                                                       monkeypatch):
    """Execução do agente B com o task_id textual da tarefa de A não
    entra no histórico nem no last_success de A — nem entre clientes
    diferentes, nem dentro do mesmo cliente."""
    real = datetime.now(UTC)
    due = (real - timedelta(hours=1)).replace(second=0, microsecond=0)
    anchor = real - timedelta(hours=3)
    task = _apply(client, admin_headers, agent["headers"],
                  agent["agent_id"], _schedule_due_at(due), monkeypatch,
                  anchor)
    _freeze(monkeypatch, due + timedelta(hours=2))

    foreign_same_client = client.post(
        f"/api/v1/clients/{agent['client_id']}/agents",
        json={"name": "ag-irmao"}, headers=admin_headers,
    ).json()
    cred_same = client.post(
        f"/api/v1/agents/{foreign_same_client['id']}/credentials",
        json={}, headers=admin_headers,
    ).json()
    from tests.conftest import second_agent
    other_client = second_agent(client, admin_headers)

    for headers in (
        {"Authorization": f"Bearer {cred_same['token']}"},
        other_client["headers"],
    ):
        posted = client.post(
            "/api/v1/agent/executions",
            json=execution_payload(
                task_id=task["id"], status="success",
                started_at_utc=(due + timedelta(seconds=15)).strftime(Z),
                finished_at_utc=(due + timedelta(minutes=5)).strftime(Z),
            ),
            headers=headers,
        )
        assert posted.status_code in (200, 201), posted.text

    body = client.get(f"/api/v1/monitoring/tasks/{task['id']}",
                      headers=admin_headers).json()
    assert body["result_state"] == "missing"  # sucesso alheio não satisfaz
    assert body["last_execution"] is None
    assert body["last_success"] is None

    # execução do PRÓPRIO dono continua contando
    legit = client.post(
        "/api/v1/agent/executions",
        json=execution_payload(
            task_id=task["id"], status="success",
            started_at_utc=(due + timedelta(seconds=20)).strftime(Z),
            finished_at_utc=(due + timedelta(minutes=5)).strftime(Z),
        ),
        headers=agent["headers"],
    )
    assert legit.status_code in (200, 201)
    body = client.get(f"/api/v1/monitoring/tasks/{task['id']}",
                      headers=admin_headers).json()
    assert body["result_state"] == "ok"
    assert body["last_success"]["status"] == "success"


def test_many_foreign_rows_do_not_push_legit_out_of_window(client, agent,
                                                           admin_headers,
                                                           monkeypatch):
    """70 execuções alheias mais novas não expulsam a legítima da
    janela de 64: a restrição de dono é aplicada ANTES do limite."""
    real = datetime.now(UTC)
    due = (real - timedelta(hours=1)).replace(second=0, microsecond=0)
    anchor = real - timedelta(hours=3)
    task = _apply(client, admin_headers, agent["headers"],
                  agent["agent_id"], _schedule_due_at(due), monkeypatch,
                  anchor)
    _freeze(monkeypatch, due + timedelta(hours=2))

    legit = client.post(
        "/api/v1/agent/executions",
        json=execution_payload(
            task_id=task["id"], status="success",
            started_at_utc=(due + timedelta(seconds=15)).strftime(Z),
            finished_at_utc=(due + timedelta(minutes=5)).strftime(Z),
        ),
        headers=agent["headers"],
    )
    assert legit.status_code in (200, 201)

    from tests.conftest import second_agent
    foreign = second_agent(client, admin_headers)
    for i in range(70):
        posted = client.post(
            "/api/v1/agent/executions",
            json=execution_payload(
                task_id=task["id"], status="failed",
                # mais recentes que a legítima — dominariam a janela
                started_at_utc=(due + timedelta(minutes=30, seconds=i))
                .strftime(Z),
                finished_at_utc=(due + timedelta(minutes=35, seconds=i))
                .strftime(Z),
            ),
            headers=foreign["headers"],
        )
        assert posted.status_code in (200, 201)

    body = client.get(f"/api/v1/monitoring/tasks/{task['id']}",
                      headers=admin_headers).json()
    assert body["result_state"] == "ok"
    assert body["last_execution"]["status"] == "success"


# ---- defeito B no coletor: futuros não escondem a legítima -------------------

def test_future_rows_do_not_hide_legit_in_collector(client, agent,
                                                    admin_headers,
                                                    monkeypatch):
    """Janela de 64 despreza linhas temporalmente inválidas: 70
    execuções 'futuras' do próprio agente não escondem a legítima."""
    real = datetime.now(UTC)
    due = (real - timedelta(hours=1)).replace(second=0, microsecond=0)
    anchor = real - timedelta(hours=3)
    task = _apply(client, admin_headers, agent["headers"],
                  agent["agent_id"], _schedule_due_at(due), monkeypatch,
                  anchor)
    _freeze(monkeypatch, due + timedelta(hours=2))

    for i in range(70):
        posted = client.post(
            "/api/v1/agent/executions",
            json=execution_payload(
                task_id=task["id"], status="success",
                started_at_utc=(real + timedelta(days=1, seconds=i))
                .strftime(Z),
                finished_at_utc=(real + timedelta(days=1, seconds=i + 60))
                .strftime(Z),
            ),
            headers=agent["headers"],
        )
        assert posted.status_code in (200, 201)

    body = client.get(f"/api/v1/monitoring/tasks/{task['id']}",
                      headers=admin_headers).json()
    # nenhuma execução temporalmente válida para a ocorrência → missing
    assert body["result_state"] == "missing"

    legit = client.post(
        "/api/v1/agent/executions",
        json=execution_payload(
            task_id=task["id"], status="success",
            started_at_utc=(due + timedelta(seconds=15)).strftime(Z),
            finished_at_utc=(due + timedelta(minutes=5)).strftime(Z),
        ),
        headers=agent["headers"],
    )
    assert legit.status_code in (200, 201)
    body = client.get(f"/api/v1/monitoring/tasks/{task['id']}",
                      headers=admin_headers).json()
    assert body["result_state"] == "ok"
    assert body["last_execution"]["started_at_utc"] == (
        due + timedelta(seconds=15)
    ).strftime(Z)


# ---- defeito C: república/suspensão sobre agenda suspensa --------------------

def _confirm(client, headers, task_id, generation, status="applied",
             applied_revision=1):
    return client.post(
        f"/api/v1/agent/tasks/{task_id}/confirmations",
        json={"generation": generation, "attempt": 1, "status": status,
              "applied_revision": applied_revision},
        headers=headers,
    )


def test_republish_over_suspended_does_not_charge(client, agent,
                                                  admin_headers, monkeypatch):
    """Aplicada → suspensa (confirmado) → republicada pendente: a
    suspensão permanece e NADA é cobrado até a retomada confirmar."""
    real = datetime.now(UTC)
    due = (real - timedelta(hours=1)).replace(second=0, microsecond=0)
    anchor = real - timedelta(hours=3)
    task = _apply(client, admin_headers, agent["headers"],
                  agent["agent_id"], _schedule_due_at(due), monkeypatch,
                  anchor)

    # suspensão pedida e confirmada pelo agente (geração 2)
    suspended = client.post(
        f"/api/v1/tasks/{task['id']}/suspend",
        json={"expected_generation": 1}, headers=admin_headers,
    )
    assert suspended.status_code == 200, suspended.text
    confirmed = _confirm(client, agent["headers"], task["id"], 2,
                         applied_revision=None)
    assert confirmed.status_code == 200, confirmed.text

    # república a revisão 1 — geração 3 apply ainda NÃO confirmada
    republished = client.post(
        f"/api/v1/tasks/{task['id']}/publish",
        json={"revision": 1, "expected_generation": 2},
        headers=admin_headers,
    )
    assert republished.status_code == 200, republished.text
    assert republished.json()["distribution"]["generation"] == 3

    _freeze(monkeypatch, due + timedelta(hours=2))
    body = client.get(f"/api/v1/monitoring/tasks/{task['id']}",
                      headers=admin_headers).json()
    assert body["operational_state"] == "pending_apply"
    # a agenda segue suspensa — nenhuma cobrança durante a retomada
    assert body["result_state"] != "missing"
    assert body["result_state"] == "no_history"

    # retomada confirmada: a cobrança volta SÓ a partir da nova âncora
    resume_at = due + timedelta(days=2)
    _freeze(monkeypatch, resume_at)
    confirmed = _confirm(client, agent["headers"], task["id"], 3,
                         applied_revision=1)
    assert confirmed.status_code == 200, confirmed.text
    body = client.get(f"/api/v1/monitoring/tasks/{task['id']}",
                      headers=admin_headers).json()
    assert body["operational_state"] == "scheduled"
    # a ocorrência no instante da retomada não é cobrada (<= âncora);
    # a PRIMEIRA cobrada é a do dia seguinte — intervalo suspenso fora
    _freeze(monkeypatch, resume_at + timedelta(days=1, hours=3))
    body = client.get(f"/api/v1/monitoring/tasks/{task['id']}",
                      headers=admin_headers).json()
    assert body["result_state"] == "missing"


def test_suspend_again_over_suspended_does_not_charge(client, agent,
                                                      admin_headers,
                                                      monkeypatch):
    """Suspender de novo uma tarefa já suspensa: pending_suspend sem
    cobrança nova."""
    real = datetime.now(UTC)
    due = (real - timedelta(hours=1)).replace(second=0, microsecond=0)
    anchor = real - timedelta(hours=3)
    task = _apply(client, admin_headers, agent["headers"],
                  agent["agent_id"], _schedule_due_at(due), monkeypatch,
                  anchor)
    client.post(f"/api/v1/tasks/{task['id']}/suspend",
                json={"expected_generation": 1}, headers=admin_headers)
    _confirm(client, agent["headers"], task["id"], 2,
             applied_revision=None)
    again = client.post(
        f"/api/v1/tasks/{task['id']}/suspend",
        json={"expected_generation": 2}, headers=admin_headers,
    )
    assert again.status_code == 200, again.text
    _freeze(monkeypatch, due + timedelta(hours=2))
    body = client.get(f"/api/v1/monitoring/tasks/{task['id']}",
                      headers=admin_headers).json()
    assert body["operational_state"] == "pending_suspend"
    assert body["result_state"] != "missing"


def test_pending_apply_over_active_agenda_keeps_monitoring(client, agent,
                                                           admin_headers,
                                                           monkeypatch):
    """Agenda ativa + nova publicação pendente: a agenda ANTERIOR
    confirmada continua sendo acompanhada (não zera a cobrança)."""
    real = datetime.now(UTC)
    due = (real - timedelta(hours=1)).replace(second=0, microsecond=0)
    anchor = real - timedelta(hours=3)
    task = _apply(client, admin_headers, agent["headers"],
                  agent["agent_id"], _schedule_due_at(due), monkeypatch,
                  anchor)
    republished = client.post(
        f"/api/v1/tasks/{task['id']}/publish",
        json={"revision": 1, "expected_generation": 1},
        headers=admin_headers,
    )
    assert republished.status_code == 200, republished.text
    _freeze(monkeypatch, due + timedelta(hours=2))
    body = client.get(f"/api/v1/monitoring/tasks/{task['id']}",
                      headers=admin_headers).json()
    assert body["operational_state"] == "pending_apply"
    assert body["result_state"] == "missing"  # agenda anterior ativa


def test_pending_suspend_over_active_agenda_keeps_monitoring(client, agent,
                                                             admin_headers,
                                                             monkeypatch):
    """Suspensão pendente sobre agenda ativa: o timer antigo continua
    ativo até confirmar — a cobrança permanece."""
    real = datetime.now(UTC)
    due = (real - timedelta(hours=1)).replace(second=0, microsecond=0)
    anchor = real - timedelta(hours=3)
    task = _apply(client, admin_headers, agent["headers"],
                  agent["agent_id"], _schedule_due_at(due), monkeypatch,
                  anchor)
    suspended = client.post(
        f"/api/v1/tasks/{task['id']}/suspend",
        json={"expected_generation": 1}, headers=admin_headers,
    )
    assert suspended.status_code == 200, suspended.text
    _freeze(monkeypatch, due + timedelta(hours=2))
    body = client.get(f"/api/v1/monitoring/tasks/{task['id']}",
                      headers=admin_headers).json()
    assert body["operational_state"] == "pending_suspend"
    assert body["result_state"] == "missing"


def test_resume_failed_attempt_keeps_suspended_state(client, agent,
                                                     admin_headers,
                                                     monkeypatch):
    """Retomada com tentativa falha: continua suspenso, sem cobrança."""
    real = datetime.now(UTC)
    due = (real - timedelta(hours=1)).replace(second=0, microsecond=0)
    anchor = real - timedelta(hours=3)
    task = _apply(client, admin_headers, agent["headers"],
                  agent["agent_id"], _schedule_due_at(due), monkeypatch,
                  anchor)
    client.post(f"/api/v1/tasks/{task['id']}/suspend",
                json={"expected_generation": 1}, headers=admin_headers)
    _confirm(client, agent["headers"], task["id"], 2,
             applied_revision=None)
    client.post(f"/api/v1/tasks/{task['id']}/publish",
                json={"revision": 1, "expected_generation": 2},
                headers=admin_headers)
    failed = _confirm(client, agent["headers"], task["id"], 3,
                      status="failed", applied_revision=None)
    assert failed.status_code == 200, failed.text
    _freeze(monkeypatch, due + timedelta(hours=2))
    body = client.get(f"/api/v1/monitoring/tasks/{task['id']}",
                      headers=admin_headers).json()
    assert body["operational_state"] == "pending_apply"
    assert body["result_state"] != "missing"


# ---- defeito C puro: efeito confirmado vs ação desejada ----------------------

def test_pure_republish_over_suspended_stays_uncharged():
    # pending_apply sobre agenda suspensa: efeito confirmado 'suspend'
    # preserva a suspensão até a retomada confirmar
    anchor = _at("2026-11-06T10:00:00Z")
    dist = _dist("apply", confirmed_status=None, last_applied_at=anchor)
    result = _compute(dist, DAILY_UTC_14, now="2026-11-06T16:00:00Z",
                      last_effect="suspend")
    assert result.operational_state == "pending_apply"
    assert result.result_state == "no_history"


def test_pure_pending_suspend_over_suspended_stays_uncharged():
    anchor = _at("2026-11-06T10:00:00Z")
    dist = _dist("suspend", confirmed_status=None, last_applied_at=anchor)
    result = _compute(dist, DAILY_UTC_14, now="2026-11-06T16:00:00Z",
                      last_effect="suspend")
    assert result.operational_state == "pending_suspend"
    assert result.result_state == "no_history"


def test_pure_pending_apply_over_active_keeps_monitoring():
    # agenda ativa confirmada + republicação pendente: segue cobrando
    anchor = _at("2026-11-06T10:00:00Z")
    dist = _dist("apply", confirmed_status=None, last_applied_at=anchor)
    result = _compute(dist, DAILY_UTC_14, now="2026-11-06T16:00:00Z",
                      last_effect="apply")
    assert result.operational_state == "pending_apply"
    assert result.result_state == "missing"


def test_pure_resume_reanchors_without_charging_suspended_interval():
    # retomada confirmada em T2: ocorrências anteriores a T2 (intervalo
    # suspenso) nunca são cobradas; a próxima vencida após T2 é
    anchor = _at("2026-11-08T12:00:00Z")  # retomada confirmada agora
    dist = _dist("apply", last_applied_at=anchor)
    # devida 07/11 14:00 (intervalo suspenso) é anterior à âncora →
    # nunca cobrada, mesmo já vencida
    result = _compute(dist, DAILY_UTC_14, now="2026-11-08T13:00:00Z",
                      last_effect="apply")
    assert result.result_state == "no_history"
    # depois de 09/11 14:00 + tolerância → cobrança volta
    result2 = _compute(dist, DAILY_UTC_14, now="2026-11-09T15:00:00Z",
                       last_effect="apply")
    assert result2.result_state == "missing"


def test_pure_unknown_effect_does_not_fabricate_active_agenda():
    # efeito desconhecido (None): não inventa agenda ativa — incerteza
    # explícita vira histórico informativo, não cobrança
    anchor = _at("2026-11-06T10:00:00Z")
    dist = _dist("apply", last_applied_at=anchor)
    result = _compute(dist, DAILY_UTC_14, now="2026-11-06T16:00:00Z",
                      last_effect=None)
    assert result.result_state != "missing"


# ---- ajuste A: explicação operacional coerente com o efeito confirmado -------

def test_pure_reason_pending_suspend_over_already_suspended():
    # suspensão JÁ confirmada + novo pedido pendente: a explicação não
    # pode afirmar "agenda anterior continua ativa" — ela está suspensa
    anchor = _at("2026-11-06T10:00:00Z")
    dist = _dist("suspend", confirmed_status=None, last_applied_at=anchor)
    result = _compute(dist, DAILY_UTC_14, now="2026-11-06T16:00:00Z",
                      last_effect="suspend")
    assert result.operational_state == "pending_suspend"
    assert result.result_state == "no_history"  # nada cobrado
    assert "ativa" not in result.operational_reason
    assert "suspensa" in result.operational_reason


def test_pure_reason_resume_pending_over_suspended():
    # retomada publicada sobre agenda suspensa: explicação diz que
    # permanece suspensa até confirmar — nunca "aguardando" genérico
    anchor = _at("2026-11-06T10:00:00Z")
    dist = _dist("apply", confirmed_status=None, last_applied_at=anchor)
    result = _compute(dist, DAILY_UTC_14, now="2026-11-06T16:00:00Z",
                      last_effect="suspend")
    assert result.operational_state == "pending_apply"
    assert "suspensa" in result.operational_reason


def test_pure_reason_failed_resume_over_suspended():
    # tentativa de retomada falhou: explicação registra a falha E a
    # suspensão que permanece em efeito
    anchor = _at("2026-11-06T10:00:00Z")
    dist = _dist("apply", confirmed_status="failed",
                 last_applied_at=anchor)
    result = _compute(dist, DAILY_UTC_14, now="2026-11-06T16:00:00Z",
                      last_effect="suspend")
    assert result.operational_state == "pending_apply"
    assert "falhou" in result.operational_reason
    assert "suspensa" in result.operational_reason


def test_pure_reason_pending_suspend_over_active_keeps_continuity():
    # agenda REALMENTE ativa + suspensão pendente: a explicação de
    # continuidade é a correta e deve ser preservada
    anchor = _at("2026-11-06T10:00:00Z")
    dist = _dist("suspend", confirmed_status=None, last_applied_at=anchor)
    result = _compute(dist, DAILY_UTC_14, now="2026-11-06T16:00:00Z",
                      last_effect="apply")
    assert result.operational_state == "pending_suspend"
    assert "ativa" in result.operational_reason


def test_pure_reason_pending_apply_over_active_keeps_continuity():
    anchor = _at("2026-11-06T10:00:00Z")
    dist = _dist("apply", confirmed_status=None, last_applied_at=anchor)
    result = _compute(dist, DAILY_UTC_14, now="2026-11-06T16:00:00Z",
                      last_effect="apply")
    assert result.operational_state == "pending_apply"
    assert "anterior" in result.operational_reason


def test_pure_reason_unknown_effect_states_uncertainty():
    # efeito desconhecido: incerteza explícita — não afirma timer ativo
    # nem suspenso
    anchor = _at("2026-11-06T10:00:00Z")
    dist = _dist("suspend", confirmed_status=None, last_applied_at=anchor)
    result = _compute(dist, DAILY_UTC_14, now="2026-11-06T16:00:00Z",
                      last_effect=None)
    assert result.operational_state == "pending_suspend"
    assert "ativa" not in result.operational_reason
    assert "suspensa" not in result.operational_reason


def test_reason_pending_suspend_after_confirmed_suspension_via_api(
        client, agent, admin_headers, monkeypatch):
    """Cenário contraditório pela API real: suspensão confirmada →
    nova suspensão pendente — a explicação não pode dizer que a agenda
    anterior continua ativa."""
    real = datetime.now(UTC)
    due = (real - timedelta(hours=1)).replace(second=0, microsecond=0)
    anchor = real - timedelta(hours=3)
    task = _apply(client, admin_headers, agent["headers"],
                  agent["agent_id"], _schedule_due_at(due), monkeypatch,
                  anchor)
    client.post(f"/api/v1/tasks/{task['id']}/suspend",
                json={"expected_generation": 1}, headers=admin_headers)
    _confirm(client, agent["headers"], task["id"], 2,
             applied_revision=None)  # suspensão CONFIRMADA (gen 2)
    again = client.post(f"/api/v1/tasks/{task['id']}/suspend",
                        json={"expected_generation": 2},
                        headers=admin_headers)  # novo pedido (gen 3)
    assert again.status_code == 200, again.text
    _freeze(monkeypatch, due + timedelta(hours=2))
    body = client.get(f"/api/v1/monitoring/tasks/{task['id']}",
                      headers=admin_headers).json()
    assert body["operational_state"] == "pending_suspend"
    assert body["result_state"] == "no_history"
    assert "continua ativa" not in body["operational_reason"]
    assert "suspensa" in body["operational_reason"]


# ---- ajuste B: last_success passa pela validade temporal --------------------

def test_pure_invalid_last_success_not_reported():
    # o mesmo sucesso inválido chega por executions e last_success:
    # não pode aparecer como evidência em lugar nenhum
    anchor = _at("2026-11-06T10:00:00Z")
    dist = _dist(last_applied_at=anchor)
    future = _exec("success", "2026-11-07T14:00:00Z", "2026-11-06T15:00:00Z")
    result = _compute(dist, DAILY_UTC_14, [future], last_ok=future,
                      now="2026-11-06T15:00:00Z")
    assert result.result_state == "missing"
    assert result.last_execution is None
    assert result.last_success is None


def test_pure_invalid_last_success_filtered_in_informational_branch():
    # ramo informativo (manual): a regra também vale fora da cobrança
    manual = _dist("apply", last_applied_at=_at("2026-11-06T10:00:00Z"))
    future = _exec("success", "2026-11-07T14:00:00Z", "2026-11-06T15:00:00Z")
    result = _compute(manual, {"kind": "manual"}, [future], last_ok=future,
                      now="2026-11-06T15:00:00Z")
    assert result.last_success is None
    assert result.last_execution is None


def test_pure_valid_last_success_reported_even_off_occurrence():
    # sucesso válido ANTIGO não satisfaz a ocorrência devida mas segue
    # informativo em last_success (o SQL o busca fora da janela de 64)
    anchor = _at("2026-11-06T10:00:00Z")
    dist = _dist(last_applied_at=anchor)
    old_ok = _exec("success", "2026-11-05T14:00:00Z", "2026-11-05T14:30:00Z")
    result = _compute(dist, DAILY_UTC_14, [old_ok], last_ok=old_ok,
                      now="2026-11-06T15:00:00Z")
    assert result.result_state == "missing"
    assert result.last_success is not None
    assert result.last_success["status"] == "success"


def test_pure_last_success_skew_boundaries():
    anchor = _at("2026-11-06T10:00:00Z")
    dist = _dist(last_applied_at=anchor)
    at_limit = _exec("success", "2026-11-06T14:02:00Z",
                     "2026-11-06T14:00:00Z")      # +120 s: válido
    over = _exec("success", "2026-11-06T14:02:01Z",
                 "2026-11-06T14:00:00Z")          # +121 s: inválido
    ok = _compute(dist, DAILY_UTC_14, [at_limit], last_ok=at_limit,
                  now="2026-11-06T14:03:00Z")
    assert ok.last_success is not None
    bad = _compute(dist, DAILY_UTC_14, [over], last_ok=over,
                   now="2026-11-06T14:40:00Z")
    assert bad.last_success is None


# ---- ajuste C: avançar `now` não revalida registro rejeitado ----------------

def test_rejected_record_stays_invalid_when_now_advances():
    # a validade compara timestamps PERSISTIDOS (started vs received) —
    # `received_at` não avança com `now`: esperar não torna a linha
    # evidência. O registro permanece no histórico, só não é usado.
    anchor = _at("2026-11-06T10:00:00Z")
    dist = _dist(last_applied_at=anchor)
    future = _exec("success", "2026-11-08T14:00:00Z", "2026-11-06T15:00:00Z")
    r1 = _compute(dist, DAILY_UTC_14, [future], last_ok=future,
                  now="2026-11-06T16:00:00Z")
    r2 = _compute(dist, DAILY_UTC_14, [future], last_ok=future,
                  now="2026-11-09T16:00:00Z")  # 3 dias DEPOIS do início
    assert r1.last_execution is None and r1.last_success is None
    assert r2.last_execution is None and r2.last_success is None
    assert r2.result_state == "missing"  # sem evidência, segue devendo


# ---- achado A: explicação não pode anunciar agenda automática se a
# configuração aplicada é manual ------------------------------------------------

MANUAL = {"kind": "manual"}


def test_pure_reason_manual_applied_pending_apply_is_manual_not_timer():
    # manual aplicada + nova publicação pendente: a continuidade é
    # MANUAL — a explicação não pode anunciar agenda automática ativa
    anchor = _at("2026-11-06T10:00:00Z")
    dist = _dist("apply", confirmed_status=None, last_applied_at=anchor)
    result = _compute(dist, MANUAL, now="2026-11-06T16:00:00Z",
                      last_effect="apply")
    assert result.operational_state == "pending_apply"
    assert "manual" in result.operational_reason
    assert "ativa" not in result.operational_reason
    assert result.next_occurrence_utc is None  # coerente: não há agenda


def test_pure_reason_manual_applied_pending_suspend_no_auto_claim():
    anchor = _at("2026-11-06T10:00:00Z")
    dist = _dist("suspend", confirmed_status=None, last_applied_at=anchor)
    result = _compute(dist, MANUAL, now="2026-11-06T16:00:00Z",
                      last_effect="apply")
    assert result.operational_state == "pending_suspend"
    assert "manual" in result.operational_reason
    assert "ativa" not in result.operational_reason


def test_pure_reason_manual_applied_auto_revision_pending_stays_manual():
    # publicação de revisão AUTOMÁTICA ainda não confirmada: a
    # configuração em vigor continua manual — não anuncia timer antes
    # da confirmação do agente
    anchor = _at("2026-11-06T10:00:00Z")
    dist = _dist("apply", confirmed_status=None, last_applied_at=anchor)
    result = _compute(dist, MANUAL, now="2026-11-06T16:00:00Z",
                      last_effect="apply")
    assert "manual" in result.operational_reason


def test_pure_reason_auto_applied_manual_revision_pending_keeps_auto():
    # espelho: automática aplicada + revisão manual pendente — a agenda
    # automática anterior segue sendo a referência até confirmar
    anchor = _at("2026-11-06T10:00:00Z")
    dist = _dist("apply", confirmed_status=None, last_applied_at=anchor)
    result = _compute(dist, DAILY_UTC_14, now="2026-11-06T16:00:00Z",
                      last_effect="apply")
    assert result.operational_state == "pending_apply"
    assert "ativa" in result.operational_reason


def test_pure_reason_unknown_applied_agenda_states_uncertainty():
    # efeito 'apply' confirmado mas configuração anterior indisponível:
    # incerteza explícita — sem afirmar agenda ativa nem manual
    anchor = _at("2026-11-06T10:00:00Z")
    dist = _dist("suspend", confirmed_status=None, last_applied_at=anchor)
    result = _compute(dist, None, now="2026-11-06T16:00:00Z",
                      last_effect="apply")
    assert result.operational_state == "pending_suspend"
    assert "indisponível" in result.operational_reason
    assert "ativa" not in result.operational_reason


def test_reason_manual_applied_pending_apply_via_api(client, agent,
                                                     admin_headers,
                                                     monkeypatch):
    """Fluxo real HTTP/PG: tarefa manual aplicada → nova publicação
    pendente — a explicação fala de continuidade MANUAL, nunca de
    agenda automática ativa."""
    real = datetime.now(UTC)
    anchor = real - timedelta(hours=3)
    task = _apply(client, admin_headers, agent["headers"],
                  agent["agent_id"],
                  {"kind": "manual", "timezone": "UTC"}, monkeypatch,
                  anchor)
    republished = client.post(
        f"/api/v1/tasks/{task['id']}/publish",
        json={"revision": 1, "expected_generation": 1},
        headers=admin_headers,
    )
    assert republished.status_code == 200, republished.text
    _freeze(monkeypatch, real + timedelta(hours=1))
    body = client.get(f"/api/v1/monitoring/tasks/{task['id']}",
                      headers=admin_headers).json()
    assert body["operational_state"] == "pending_apply"
    assert "manual" in body["operational_reason"]
    assert "continua ativa" not in body["operational_reason"]
    assert body["next_occurrence_utc"] is None
