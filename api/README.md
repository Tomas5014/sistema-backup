# MAC IP Backup API — central de controle

Primeira etapa: cadastro de clientes/agentes e recebimento autenticado
de resultados de execução. **Pacote separado** — instalar o agente
Linux não exige FastAPI, PostgreSQL nem nada daqui.

## Limites desta etapa

- A API recebe **somente metadados operacionais** — backups seguem
  indo direto aos repositórios (Restic).
- Não recebe senhas Restic, chaves SSH, credenciais S3 nem a tarefa
  completa — schema explícito em `schemas.py`.
- Fora do escopo: distribuição de tarefas, comandos remotos,
  sincronização/outbox do agente, agendamento central, portal, Windows,
  upload de arquivos.

## Requisitos

- Python ≥ 3.11
- PostgreSQL ≥ 15
- `fastapi`, `uvicorn`, `sqlalchemy`, `psycopg` (ver pyproject)

## Configuração por ambiente

| Variável | Obrigatória | Descrição |
|---|---|---|
| `MACIP_API_DATABASE_URL` | sim | `postgresql://user:senha@host/db` |
| `MACIP_API_ADMIN_TOKEN` | sim | Bearer administrativo — **sem default** |
| `MACIP_API_MAX_BODY_BYTES` | não | limite de corpo (default 1 MiB) |

Sem senhas no Git/argumentos/exemplos. Gere valores fortes por ambiente:
`python3 -c "import secrets; print(secrets.token_urlsafe(32))"`.

## Execução local

```bash
python3 -m venv .venv-api && . .venv-api/bin/activate
pip install -e api/
export MACIP_API_DATABASE_URL='postgresql://<user>:<senha>@localhost/<db>'
export MACIP_API_ADMIN_TOKEN='<token-admin>'
macip-backup-api    # 127.0.0.1:8080 — migrações rodam no start
```

Migrações versionadas/transacionais em `schema_migrations` +
`pg_advisory_lock` — seguras com instâncias concorrentes e **nunca
apagam histórico**.

## Provisionamento (administrativo)

```bash
# cliente
curl -X POST http://127.0.0.1:8080/api/v1/clients \
  -H 'Authorization: Bearer <ADMIN_TOKEN>' -H 'Content-Type: application/json' \
  -d '{"name": "acme"}'                                    # → {"id": ...}

# agente do cliente
curl -X POST http://127.0.0.1:8080/api/v1/clients/<CLIENT_ID>/agents \
  -H 'Authorization: Bearer <ADMIN_TOKEN>' -d '{"name": "vm-01"}'

# credencial — o token aparece UMA única vez nesta resposta
curl -X POST http://127.0.0.1:8080/api/v1/agents/<AGENT_ID>/credentials \
  -H 'Authorization: Bearer <ADMIN_TOKEN>' -d '{"label": "bootstrap"}'
# → {"credential_id": "...", "token": "<TOKEN>"}  (armazenado só como SHA-256)

# revogação — efeito imediato
curl -X POST http://127.0.0.1:8080/api/v1/credentials/<CRED_ID>/revoke \
  -H 'Authorization: Bearer <ADMIN_TOKEN>'
```

## Endpoints do agente

```bash
# heartbeat — atualiza last_seen_at (horário do servidor)
curl -X POST http://127.0.0.1:8080/api/v1/agent/heartbeat \
  -H 'Authorization: Bearer <AGENT_TOKEN>'

# resultado de execução concluída — metadados apenas
curl -X POST http://127.0.0.1:8080/api/v1/agent/executions \
  -H 'Authorization: Bearer <AGENT_TOKEN>' -H 'Content-Type: application/json' \
  -d '{
    "execution_id": "<uuid-da-execucao>", "task_id": "task-diaria",
    "status": "success",
    "started_at_utc": "2026-10-26T10:00:00Z",
    "finished_at_utc": "2026-10-26T10:05:00Z",
    "destinations": [{"destination_id": "local", "status": "success",
      "started_at_utc": "...", "finished_at_utc": "...", "attempts": [...]}]
  }'
```

## Idempotência

`UNIQUE(agent_id, execution_id)` + `ON CONFLICT DO NOTHING` + hash do
payload — seguro com agente offline reenviando ou requisições
simultâneas:

| Situação | Código |
|---|---|
| Execução nova | `200 {"status": "created"}` |
| Reenvio idêntico | `200 {"status": "duplicate"}` — sem duplicar |
| Mesmo execution_id, conteúdo diferente | `409` |
| Credencial ausente/inválida/revogada | `401`/`403` |
| Identidade cruzada no corpo | `403` |
| Validação (timezone, tamanhos, itens) | `422` |
| Corpo acima do limite | `413` |

## Consultas administrativas

`GET /api/v1/executions` — paginação `limit`/`offset` (≤500), filtros
`client_id`, `agent_id`, `task_id`, `status`, `since`/`until`
(em `started_at_utc`). `GET /api/v1/executions/{id}` traz destinos e
tentativas. `GET /api/v1/agents` mostra `last_seen_at`.

`received_at`/`last_seen_at` usam **horário do servidor**; os horários
`started/finished_at_utc` informados pelo agente são preservados
separadamente (exigem timezone — naive é rejeitado).

## Segurança

- Token de agente entregue uma vez; armazenado só como SHA-256.
- `Authorization` nunca é registrado em log (uvicorn loga só status+path).
- Identidade vem da credencial autenticada — corpo que a contradiz → 403.
- **HTTPS obrigatório** para comunicação fora de localhost — os endpoints
  de agente levam Bearer tokens; sem TLS eles circulam em claro.

## Testes

PostgreSQL real exclusivo (`MACIP_API_TEST_DATABASE_URL`):

```bash
podman run -d --name macip-api-test-pg \
  -e POSTGRES_PASSWORD=<senha-teste> -e POSTGRES_DB=macip_api_test \
  -p 55432:5432 docker.io/library/postgres:17-alpine

cd api && ../.venv-api/bin/python -m pytest tests/ -v
```

Sem o banco → módulo inteiro pulado (integração registrada como pendente).
SQLite não substitui — `ON CONFLICT`/`advisory lock` são específicos de PG.
