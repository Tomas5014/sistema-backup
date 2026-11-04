---
title: "Migration proof: factory injection + exception preservation"
author: Tomas5014
date: 2026-11-04
branch: fix/recovery-migration-proof
touches:
  - atualiza\303\247\303\265es.txt
  - tarefa-teste.json
---

## Decided
- Failure injection must use sqlite3.connect(factory=ConnectionSubclass); conn.execute = fn fails with AttributeError (read-only in Python 3.14) and makes tests pass vacuously.
- PRAGMA user_version IS transactional in SQLite (rollback undoes it) — proven empirically.
- _database cleanup: single except does rollback (tolerant of no txn) + close; never rollback on closed conn, never mask original exception.

## Tried and ruled out
- patching sqlite3.Connection.execute (immutable type in Python 3.14)
- conn.execute = fn instance assign (read-only)

## Watch out
- trail check false positives on api/tests/*.py pagination cursor tests.
- PG test container: pgtest/macip_api_test@55432.
- Mutation check: removing BEGIN IMMEDIATE makes 4 failure tests detect partial schema — observable rollback, not code presence.
