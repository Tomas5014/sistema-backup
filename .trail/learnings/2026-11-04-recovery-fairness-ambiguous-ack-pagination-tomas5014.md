---
title: Recovery fairness + ambiguous ACK + pagination
author: Tomas5014
date: 2026-11-04
branch: fix/recovery-queue-fairness
touches:
  - atualiza\303\247\303\265es.txt
  - tarefa-teste.json
  - api/tests/test_recovery.py
---

## Decided
- Fair selection: ORDER BY (evaluated_at, rowid). Operations never evaluated (0) or evaluated longest ago selected first, independent of round interval. evaluated_at persisted in SQLite schema v3.
- Ambiguous ACK (200 with malformed ack) is NOT definitive conflict: defer by DEFERRED_SECONDS and retry SAME claim_id (idempotent in API). Only confirmed 409 blocks locally.
- Discovery beyond 100: API cursor pagination (after/limit/next_cursor). Agent persists cursor in discovery_cursor table, continues between rounds with DISCOVERY_BUDGET (5) requests per poll.
- Schema v2->v3: ALTER TABLE recovery_queue ADD COLUMN evaluated_at + CREATE TABLE discovery_cursor. Transactional, preserves rows.

## Tried and ruled out
- Increasing DEFERRED_SECONDS alone: does not guarantee fairness with 60s round interval.
- Blocking ambiguous ACK permanently: API may have registered claim correctly before response lost.
- Single GET without pagination: 100-op window hides operation 101.

## Watch out
- trail check flags api/tests/test_recovery.py:27 as "claim another operation" — false positive, that line is pagination cursor test.
- PG test container macip-api-test-pg: password pgtest, db macip_api_test, port 55432.
- E2E needs portal build (npm run build) before playwright; restic + chrome + pg all available here.
