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
             tolerance=1800, basis=1):
    return monitoring.compute(
        distribution=dist,
        applied_schedule=schedule,
        basis_revision=basis,
        executions=executions,
        last_success=last_ok,
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
    result = _compute(suspended, DAILY_SP_14, now="2026-03-10T19:00:00Z")
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
