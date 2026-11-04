---
title: "Recovery queue progress: deferred_until + per-op lock handling"
author: Tomas5014
date: 2026-11-04
branch: fix/recovery-queue-progress
touches:
  - atualiza\303\247\303\265es.txt
  - tarefa-teste.json
  - portal/e2e/recovery.spec.ts
---

## Decided
- acquire_task_lock() must be inside per-operation try in recovery.work(); a busy task defers, never aborts the batch.
- Persistent deferral via deferred_until (SQLite queue schema v1->v2, transactional ALTER preserving rows/events) is the mechanism to stop an ineligible item monopolizing rounds. Deferred items are excluded from selection.
- --limit = max operations selected per round for evaluation (lock + claim), not work executed.
- Definitive claim conflict (409), ambiguous response, invalid contract -> local permanent block (_BLOCKED = 2^62). Transient (0/429/5xx) and busy task -> defer 30s.
- API GET /agent/recovery lists pending before running so a held claim does not hide new ops in the 100-item window.

## Tried and ruled out
- Swapping claim or taking another executor's operation: refused (would repeat restore effect).
- Adding claim expiration: refused (could repeat a restore whose effect may have occurred).
- Deleting records to unblock: refused.

## Watch out
- PG test container macip-api-test-pg uses password pgtest, db macip_api_test, port 55432 (NOT the password test/db macip_test from the older learning).
- E2E Playwright (portal/e2e/recovery.spec.ts) was NOT re-run this session; needs Chrome+API+PG17+Restic together.
- setuptools missing in .venv -> test_install wheel build fails on base too (pre-existing infra, not this change).
