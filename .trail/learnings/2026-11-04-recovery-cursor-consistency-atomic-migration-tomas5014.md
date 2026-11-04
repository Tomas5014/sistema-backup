---
title: Recovery cursor consistency + atomic migration
author: Tomas5014
date: 2026-11-04
branch: fix/recovery-cursor-atomic-migration
touches:
  - atualiza\303\247\303\265es.txt
  - tarefa-teste.json
  - api/tests/test_recovery.py
  - api/tests/test_api.py
---

## Decided
- Cursor pagination: ORDER BY, cursor and continuation predicate must represent the SAME total immutable order — (created_at, id). State priority in ORDER BY breaks when state changes between pages (pending->running moves the op, causing omission or duplication).
- Atomic SQLite migration: BEGIN IMMEDIATE serializes poll/worker; user_version updated only after all ALTER/CREATE; rollback undoes everything on failure; user_version re-read after lock detects concurrent migration.
- Partially migrated DBs from previous implementation (evaluated_at exists but user_version=2) are completed without data loss.

## Tried and ruled out
- ORDER BY (state='running', created_at, id) with row constructor: PG rejects boolean > smallint; state change between pages causes duplication.
- DDL without explicit transaction: partial migration on failure, user_version not rolled back.

## Watch out
- trail check flags api/tests/test_recovery.py:27,113 and api/tests/test_api.py:248 as conflicts — false positives (pagination cursor tests, not claim takeover).
- PG test container macip-api-test-pg: password pgtest, db macip_api_test, port 55432.
- sqlite3.Connection.execute is immutable in Python 3.14; patch the instance via sqlite3.connect wrapper, not the class.
