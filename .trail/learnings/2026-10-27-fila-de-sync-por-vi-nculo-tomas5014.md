---
title: Fila de sync por vínculo + homologação local
author: Tomas5014
date: 2026-10-27
branch: feature/agent-sync-timer
touches:
  - README.md
  - atualiza\303\247\303\265es.txt
  - registro-trabalho.md
  - src/macip_backup/sync.py
  - tests/test_outbox.py
  - tests/test_systemd.py
  - tarefa-teste.json
---

Fila de sync: identidade = (banco canônico, endpoint normalizado, UUID central) → locks/sync-<sha256>.lock. Global sync.lock foi substituído; manutenção SH preservada. systemd --user real aqui está ativo: testes SystemdUserIntegrationTests rodam de verdade (não pular). API+PG: podman postgres:17-alpine em 127.0.0.1:55432 + .venv-api/bin/python; MACIP_API_TEST_DATABASE_URL=postgresql+psycopg://postgres:test@127.0.0.1:55432/macip_test. Filas concorrentes exigem ThreadingHTTPServer no servidor fake — HTTPServer single-thread serializa POSTs retidos.
