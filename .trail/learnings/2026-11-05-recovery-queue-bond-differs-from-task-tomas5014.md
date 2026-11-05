---
title: Recovery queue bond differs from task-distribution bond
author: Tomas5014
date: 2026-11-05
branch: fix/recovery-verifier-bond
touches:
  - atualiza\303\247\303\265es.txt
  - tarefa-teste.json
---

## Decided
- Recovery queue bond = recovery._bond = SHA-256(url + '\0' + agent_id). For ea74d5d lab (https://localhost:18445, dd7d5bae-...) = 6ae76f7627b3ccb0ef34a4fddf646094616202970655b3e09ecc7c7f68cd1074.
- Task-distribution bond (b73f475a0294f140...) is a DIFFERENT rule (distribution._bond_key). Never compare recovery rows against its prefix.
- Verifier must compute expected bond from lab constants and compare full hash by equality — never prefix, never the DB-received value.

## Tried and ruled out
- startswith(prefix) of distribution bond in the reboot-proof verifier — failed in the real run on an intact queue; prior review missed it.

## Watch out
- trail check false positives: 14 conflicts on untouched api/test files (whole-branch matching).
- Reboot proof approved for NORMAL reboot only; SIGKILL/power-loss and abandoned running still out of scope.
