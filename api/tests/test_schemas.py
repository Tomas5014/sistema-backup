"""Validação de schemas — independente de PostgreSQL, roda sempre."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from macip_backup_api.schemas import AttemptIn, DestinationIn, ExecutionIn

DEST = {
    "destination_id": "local",
    "status": "success",
    "started_at_utc": "2026-10-26T10:00:00Z",
    "finished_at_utc": "2026-10-26T10:05:00Z",
}


def _payload(**overrides) -> dict:
    base = {
        "execution_id": "exec-1",
        "task_id": "task",
        "status": "success",
        "started_at_utc": "2026-10-26T10:00:00Z",
        "finished_at_utc": "2026-10-26T10:05:00Z",
        "destinations": [DEST],
    }
    base.update(overrides)
    return base


def test_valid_minimal_payload():
    parsed = ExecutionIn(**_payload())
    assert parsed.destinations[0].attempts == []


def test_nullable_fields_and_zero_counters():
    parsed = ExecutionIn(
        **_payload(
            destinations=[
                {**DEST, "files_processed": 0, "bytes_processed": 0, "error": None}
            ]
        )
    )
    assert parsed.destinations[0].files_processed == 0


def test_interrupted_destination_without_attempts():
    parsed = ExecutionIn(
        **_payload(destinations=[{**DEST, "status": "interrupted", "attempts": []}])
    )
    assert parsed.destinations[0].status == "interrupted"


def test_integer_over_bigint_rejected():
    with pytest.raises(ValidationError):
        ExecutionIn(**_payload(destinations=[{**DEST, "files_processed": 10**20}]))


def test_integer_over_int_rejected():
    with pytest.raises(ValidationError):
        ExecutionIn(
            **_payload(destinations=[{**DEST, "snapshots_available": 2**32}])
        )
    with pytest.raises(ValidationError):
        ExecutionIn(
            **_payload(destinations=[{**DEST, "timeout_seconds": 2**32}])
        )


def test_non_finite_duration_rejected():
    for value in (float("inf"), float("nan"), float("-inf")):
        with pytest.raises(ValidationError):
            ExecutionIn(
                **_payload(destinations=[{**DEST, "duration_seconds": value}])
            )


def test_finished_before_started_rejected_everywhere():
    bad = {"started_at_utc": "2026-10-26T10:05:00Z", "finished_at_utc": "2026-10-26T10:00:00Z"}
    with pytest.raises(ValidationError):
        ExecutionIn(**_payload(**bad))                          # execução
    with pytest.raises(ValidationError):
        ExecutionIn(**_payload(destinations=[{**DEST, **bad}]))  # destino
    with pytest.raises(ValidationError):
        DestinationIn(
            **{
                **DEST,
                "attempts": [
                    {
                        "attempt": 1, "status": "failed",
                        **bad,
                    }
                ],
            }
        )                                                       # tentativa


def test_naive_timestamps_rejected():
    with pytest.raises(ValidationError):
        ExecutionIn(
            **_payload(started_at_utc="2026-10-26T10:00:00")
        )  # sem timezone


def test_status_literals():
    assert AttemptIn(
        attempt=1, status="failed",
        started_at_utc="2026-10-26T10:00:00Z",
        finished_at_utc="2026-10-26T10:00:00Z",
    ).status == "failed"
    with pytest.raises(ValidationError):
        ExecutionIn(**_payload(status="unknown"))
