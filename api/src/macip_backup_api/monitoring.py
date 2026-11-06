"""Acompanhamento de tarefas — cálculo de leitura, sem efeitos colaterais.

Duas dimensões independentes:

- operational_state — o que a distribuição confirma como instalado:
    unpublished      nunca publicada (desired_action == 'none')
    pending_apply    publicada; aplicação ainda não confirmada
    manual           aplicada; agenda confirmada é 'manual'
    scheduled        aplicada; agenda automática confirmada
    pending_suspend  suspensão pedida; confirmação pendente
    suspended        suspensão confirmada (timer desligado)

- result_state — o que o histórico RECEBIDO mostra:
    no_history  nenhuma execução recebida
    awaiting    agenda ativa; ainda não há ocorrência vencida
    pending     ocorrência vencida dentro da tolerância de recebimento
    ok          ocorrência vencida coberta por resultado 'success'
    failed      coberta por 'failed' ou 'interrupted'
    partial     coberta por 'partial'
    missing     vencida e sem resultado recebido após o prazo

Verdades do cálculo:
- O relógio é INJETÁVEL (now): âncora e prazos usam timestamps
  atribuídos pelo servidor (last_applied_at, received_at). Nada usa o
  horário do navegador.
- `started_at_utc`/`finished_at_utc` SÃO informados pelo agente — a
  associação execução↔ocorrência depende deles. A política de
  coerência abaixo limita o estrago de um relógio de agente errado.
- Marco inicial: ocorrências só são esperadas DEPOIS de
  `last_applied_at` — a confirmação de que a agenda foi instalada.
  Tarefa recém-publicada não herda cobrança histórica. ATENÇÃO:
  last_applied_* é histórico de configuração, NÃO prova de timer
  ativo — quem decide se há agenda viva é `last_effect` (abaixo).
- Último efeito operacional (`last_effect`): a confirmação aplicada
  mais recente de task_confirmations — 'apply' = agenda instalada,
  'suspend' = timer desligado, None = nada confirmado. Suspensão
  confirmada permanece em efeito ATÉ uma retomada ser confirmada:
  república pendente não cobra uma agenda suspensa; falha na retomada
  idem. Enquanto o último efeito confirmado for 'apply', a agenda
  anterior continua acompanhada durante publicações/suspensões
  pendentes.
- Uma execução satisfaz a ocorrência O somente se
  `started_at_utc >= O` E for temporalmente válida (abaixo) —
  execução anterior à janela não conta.
- COERÊNCIA TEMPORAL: uma execução é válida quando
  `started_at_utc <= received_at + FUTURE_SKEW_SECONDS` — ela não
  pode ter sido recebida muito ANTES de começar. Linhas inválidas
  (ex.: "sucesso de amanhã recebido hoje") são não-evidência: não
  satisfazem ocorrência, não vencem a ordenação nem entram em
  last_execution/last_success. O relógio do agente pode estar até
  FUTURE_SKEW_SECONDS adiantado sem prejuízo.
- Várias execuções válidas na mesma janela: vence a de maior
  (started_at_utc, received_at) — um sucesso posterior corrige um
  estado de falha; um resultado antigo recebido fora de ordem não
  regride um estado já satisfeito.
- 'missing' = resultado NÃO RECEBIDO no prazo. Não prova que o backup
  falhou nem que o agente está offline — pode ser envio pendente na
  outbox. O contrato também não distingue disparo agendado de manual:
  qualquer resultado recebido satisfaz a ocorrência.

Agenda interpretada no fuso IANA do contrato (timezone obrigatório);
instâncias comparadas sempre em UTC. Horário inexistente no salto de
verão é pulado (como o OnCalendar do systemd); horário ambíguo usa a
primeira ocorrência (fold=0).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Iterable
from zoneinfo import ZoneInfo

OPERATIONAL_STATES = (
    "unpublished",
    "pending_apply",
    "manual",
    "scheduled",
    "pending_suspend",
    "suspended",
)
RESULT_STATES = (
    "no_history",
    "awaiting",
    "pending",
    "ok",
    "failed",
    "partial",
    "missing",
)

_MAX_SCAN_DAYS = 400
_MAX_SCAN_HOURS = 48

# Desvio máximo aceito entre o início informado pelo agente e o
# recebimento pelo servidor: started_at_utc <= received_at + skew.
# 120 s cobre drift de NTP sem deixar uma execução "futura" real
# satisfazer ocorrências — a granularidade das agendas é de minutos.
FUTURE_SKEW_SECONDS = 120

RESULT_REASONS = {
    "no_history": "nenhum resultado recebido para a tarefa",
    "awaiting": "agendamento ativo; próxima ocorrência ainda não venceu",
    "pending": "ocorrência vencida dentro da tolerância de recebimento",
    "ok": "resultado de sucesso recebido para a ocorrência",
    "failed": "resultado com falha recebido para a ocorrência",
    "partial": "resultado parcial recebido — não é 'em dia'",
    "missing": "sem resultado recebido após o prazo — não prova falha do backup",
}


def tolerance_seconds() -> int:
    """Tolerância de recebimento configurável — padrão 30 min.

    O prazo inclui a expectativa de RECEBIMENTO do resultado na
    central (início + execução + envio), não apenas o início do
    backup. Não é um SLA: é o critério objetivo de 'ausência de
    resultado'. Valores fora de 1..86400 são recusados na partida.
    """
    raw = os.environ.get("MACIP_API_MONITOR_TOLERANCE_SECONDS", "1800")
    try:
        value = int(raw)
    except ValueError as error:
        raise ValueError(
            "MACIP_API_MONITOR_TOLERANCE_SECONDS deve ser inteiro"
        ) from error
    if not 1 <= value <= 86400:
        raise ValueError(
            "MACIP_API_MONITOR_TOLERANCE_SECONDS fora de 1..86400"
        )
    return value


# ---- ocorrências ------------------------------------------------------------


def _local_instant(tz: ZoneInfo, day: date, at: time) -> datetime | None:
    """Instante UTC do horário local `day`/`at` — None se inexistente
    (salto de verão), primeira ocorrência se ambíguo."""
    naive = datetime.combine(day, at)
    aware = naive.replace(tzinfo=tz, fold=0)
    instant = aware.astimezone(UTC)
    if instant.astimezone(tz).replace(tzinfo=None) != naive:
        return None  # horário pulado pelo fuso — como o systemd
    return instant


def next_occurrence(schedule: dict, after_utc: datetime) -> datetime | None:
    """Primeira ocorrência ESTRITAMENTE posterior a `after_utc`."""
    kind = schedule.get("kind")
    if kind in (None, "manual"):
        return None
    tz = ZoneInfo(schedule["timezone"])
    local = after_utc.astimezone(tz)
    if kind == "hourly":
        for hours in range(_MAX_SCAN_HOURS):
            point = local + timedelta(hours=hours)
            instant = _local_instant(
                tz, point.date(), time(hour=point.hour, minute=schedule["minute"])
            )
            if instant is not None and instant > after_utc:
                return instant
        return None
    at = time(hour=schedule["hour"], minute=schedule["minute"])
    weekdays = set(schedule.get("weekdays") or [])
    for days in range(_MAX_SCAN_DAYS):
        day = local.date() + timedelta(days=days)
        if kind == "weekly" and day.isoweekday() not in weekdays:
            continue
        instant = _local_instant(tz, day, at)
        if instant is not None and instant > after_utc:
            return instant
    return None


def previous_occurrence(schedule: dict, before_utc: datetime) -> datetime | None:
    """Última ocorrência <= `before_utc`."""
    kind = schedule.get("kind")
    if kind in (None, "manual"):
        return None
    tz = ZoneInfo(schedule["timezone"])
    local = before_utc.astimezone(tz)
    if kind == "hourly":
        for hours in range(_MAX_SCAN_HOURS + 1):
            point = local - timedelta(hours=hours)
            instant = _local_instant(
                tz, point.date(), time(hour=point.hour, minute=schedule["minute"])
            )
            if instant is not None and instant <= before_utc:
                return instant
        return None
    at = time(hour=schedule["hour"], minute=schedule["minute"])
    weekdays = set(schedule.get("weekdays") or [])
    for days in range(_MAX_SCAN_DAYS):
        day = local.date() - timedelta(days=days)
        if kind == "weekly" and day.isoweekday() not in weekdays:
            continue
        instant = _local_instant(tz, day, at)
        if instant is not None and instant <= before_utc:
            return instant
    return None


# ---- estado operacional -----------------------------------------------------


def operational_state(distribution: Any | None, applied_schedule: dict | None) -> str:
    """Estado da distribuição confirmada — nunca do POST administrativo."""
    if distribution is None or distribution.desired_action == "none":
        return "unpublished"
    confirmed = (
        distribution.confirmed_generation == distribution.generation
        and distribution.confirmed_status == "applied"
    )
    if distribution.desired_action == "suspend":
        return "suspended" if confirmed else "pending_suspend"
    if not confirmed:
        return "pending_apply"
    if applied_schedule is None:
        return "pending_apply"  # confirmação sem revisão aplicável
    return "manual" if applied_schedule.get("kind") == "manual" else "scheduled"


def operational_reason(state: str, distribution: Any | None) -> str:
    if state == "unpublished":
        return "tarefa nunca publicada para o agente"
    if state == "pending_apply":
        if (
            distribution is not None
            and distribution.confirmed_generation == distribution.generation
            and distribution.confirmed_status == "failed"
        ):
            return "aplicação publicada falhou no agente — agenda anterior pode continuar ativa"
        return "aplicação publicada; aguardando confirmação do agente"
    if state == "pending_suspend":
        return "suspensão solicitada; agenda anterior continua ativa até confirmar"
    if state == "suspended":
        return "suspensão confirmada — nenhuma ocorrência nova é esperada"
    if state == "manual":
        return "agenda confirmada é manual — sem ocorrências automáticas"
    return "agendamento confirmado como ativo pelo agente"


# ---- estado dos resultados --------------------------------------------------


@dataclass(frozen=True)
class Monitoring:
    operational_state: str
    operational_reason: str
    result_state: str
    reason_code: str
    detail: str
    last_execution: dict | None
    last_success: dict | None
    expected_occurrence_utc: datetime | None
    result_deadline_utc: datetime | None
    next_occurrence_utc: datetime | None
    received_late: bool
    computed_at_utc: datetime
    tolerance_seconds: int
    schedule: dict | None
    basis_revision: int | None


def _execution_brief(row: Any) -> dict:
    return {
        "id": str(row.id),
        "execution_id": row.execution_id,
        "status": row.status,
        "started_at_utc": row.started_at_utc,
        "finished_at_utc": row.finished_at_utc,
        "received_at": row.received_at,
    }


def _valid_execution(row: Any) -> bool:
    """Coerência temporal: a execução não pode ter sido recebida muito
    antes de seu início alegado. `received_at` (servidor) é a âncora
    confiável; `started_at_utc` (agente) é aceito com skew limitado."""
    if row.started_at_utc is None or row.received_at is None:
        return False
    return (
        row.started_at_utc
        <= row.received_at + timedelta(seconds=FUTURE_SKEW_SECONDS)
    )


def compute(
    *,
    distribution: Any | None,
    applied_schedule: dict | None,
    basis_revision: int | None,
    executions: Iterable[Any],
    last_success: Any | None,
    last_effect: str | None,
    now: datetime,
    tolerance: int,
) -> Monitoring:
    """Cálculo determinístico — `now` injetável, timestamps do servidor.

    `executions`: histórico RECEBIDO da tarefa (qualquer ordem);
    `applied_schedule`/`basis_revision`: config confirmada aplicada
    (task_revisions[last_applied_revision]); `distribution` pode ser
    None (nunca publicada); `last_effect` é o último efeito operacional
    confirmado ('apply' | 'suspend' | None) conforme o log de
    confirmações — decide se existe agenda viva para cobrar."""
    executions = list(executions)
    ordered = sorted(
        executions,
        key=lambda e: (e.started_at_utc, e.received_at),
        reverse=True,
    )
    # linhas temporalmente inválidas não são evidência: ficam fora da
    # seleção de last_* e das candidatas de ocorrência
    valid = [e for e in ordered if _valid_execution(e)]
    last_execution = _execution_brief(valid[0]) if valid else None
    last_ok = _execution_brief(last_success) if last_success else None
    op = operational_state(distribution, applied_schedule)
    base = {
        "operational_state": op,
        "operational_reason": operational_reason(op, distribution),
        "last_execution": last_execution,
        "last_success": last_ok,
        "received_late": False,
        "computed_at_utc": now,
        "tolerance_seconds": tolerance,
        "schedule": applied_schedule,
        "basis_revision": basis_revision,
        "expected_occurrence_utc": None,
        "result_deadline_utc": None,
        "next_occurrence_utc": None,
    }

    monitoring = (
        applied_schedule is not None
        and applied_schedule.get("kind") != "manual"
        and distribution is not None
        and distribution.last_applied_at is not None
        and last_effect == "apply"
        # o que manda é o efeito CONFIRMADO, não a ação desejada:
        # suspensão confirmada vigora até retomada confirmada; agenda
        # ativa segue acompanhada durante pending_apply/pending_suspend
    )
    if not monitoring:
        # sem agenda confirmada: o histórico é só informativo — nunca
        # cobrança. Aplica a manual, suspenso, não publicado e
        # aplicação pendente sem config instalada.
        if not valid:
            state, code = "no_history", "no-executions"
        else:
            state = {
                "success": "ok",
                "partial": "partial",
                "failed": "failed",
                "interrupted": "failed",
            }[valid[0].status]
            code = f"result-{valid[0].status}"
        return Monitoring(
            result_state=state,
            reason_code=code,
            detail=RESULT_REASONS[state],
            **base,
        )

    anchor = distribution.last_applied_at
    due = previous_occurrence(applied_schedule, now)
    next_occ = next_occurrence(applied_schedule, now)
    if due is not None and due <= anchor:
        due = None  # ocorrências anteriores à instalação não são cobradas

    if due is None:
        state = "no_history" if not ordered else "awaiting"
        code = "no-executions" if not ordered else "awaiting-occurrence"
        base["next_occurrence_utc"] = next_occ
        return Monitoring(
            result_state=state,
            reason_code=code,
            detail=RESULT_REASONS[state],
            **base,
        )

    deadline = due + timedelta(seconds=tolerance)
    base["expected_occurrence_utc"] = due
    base["result_deadline_utc"] = deadline
    base["next_occurrence_utc"] = next_occ
    candidates = [e for e in valid if e.started_at_utc >= due]
    chosen = candidates[0] if candidates else None
    if chosen is None:
        state = "pending" if now <= deadline else "missing"
        code = "within-tolerance" if state == "pending" else "overdue"
        return Monitoring(
            result_state=state,
            reason_code=code,
            detail=RESULT_REASONS[state],
            **base,
        )
    state = {
        "success": "ok",
        "partial": "partial",
        "failed": "failed",
        "interrupted": "failed",
    }[chosen.status]
    late = chosen.received_at > deadline
    base["received_late"] = late
    return Monitoring(
        result_state=state,
        reason_code=f"result-{chosen.status}" + ("-late" if late else ""),
        detail=RESULT_REASONS[state]
        + ("; recebido após o prazo" if late else ""),
        **base,
    )
