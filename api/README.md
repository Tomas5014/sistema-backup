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
(em `started_at_utc`, exigem timezone). Detalhes por **UUID central**:
`GET /api/v1/executions/{id}` (a unicidade é `(agent_id, execution_id)` —
`execution_id` NÃO é identificador global; agentes diferentes podem
enviar o mesmo). `GET /api/v1/agents` mostra `last_seen_at`.

`received_at`/`last_seen_at` usam **horário do servidor**; os horários
`started/finished_at_utc` informados pelo agente são preservados
separadamente (exigem timezone — naive é rejeitado).

## Operadores (portal)

Operadores são da **equipe interna do MSP** — login de clientes fica
para uma etapa posterior. Três credenciais coexistem sem se confundir:

| Credencial | Acesso |
|---|---|
| `Bearer <ADMIN_TOKEN>` | automação/provisionamento — contrato original preservado |
| Cookie `macip_session` | operador autenticado (`admin` escreve, `viewer` só consulta) |
| `Bearer <AGENT_TOKEN>` | somente `/api/v1/agent/*` — nunca vira operador |

**Sem usuário padrão.** O primeiro administrador nasce pelo
procedimento local — senha por `getpass` ou stdin, nunca por argumento:

```bash
macip-backup-api-operator create --login ana --name "Ana" --role admin
# recuperação administrativa (mesmo caminho, fora do HTTP):
macip-backup-api-operator reset-password --login ana   # revoga sessões
macip-backup-api-operator set-active --login ana --active true
macip-backup-api-operator list
```

Senhas em **Argon2id** (argon2-cffi) — o banco só guarda o hash.
Sessões são **opacas**: identificador aleatório forte renovado a cada
login, persistido só como SHA-256, cookie `HttpOnly` + `SameSite=Lax`
+ `Secure` (exceção local: `MACIP_API_SESSION_INSECURE_COOKIE=1`).
Expiração validada no servidor (`MACIP_API_SESSION_TTL_SECONDS`,
default 8 h); logout revoga de fato. Verificação de credencial,
criação de sessão, reset e desativação serializam na linha do
operador (`SELECT ... FOR UPDATE`, HTTP e CLI): concluído um reset
ou desativação, nenhuma sessão autenticada com a credencial anterior
sobrevive — inclusive a de um login que começou antes. Reativar um
operador nunca ressuscita sessões revogadas; mudança de perfil vale
já na requisição seguinte.

```bash
# 1) cookie CSRF pré-sessão → 2) login exige o par cookie+cabeçalho
curl -c jar http://127.0.0.1:8080/api/v1/auth/csrf
curl -b jar -c jar -X POST http://127.0.0.1:8080/api/v1/auth/login \
  -H 'X-CSRF-Token: <valor-do-cookie-macip_csrf>' \
  -H 'Content-Type: application/json' \
  -d '{"login": "ana", "password": "<senha>"}'

curl -b jar http://127.0.0.1:8080/api/v1/auth/me              # sessão
curl -b jar -X POST http://127.0.0.1:8080/api/v1/auth/logout \
  -H 'X-CSRF-Token: <valor-do-cookie-macip_csrf>'             # revoga
```

**CSRF**: toda escrita autenticada por cookie exige `X-CSRF-Token`
igual ao cookie `macip_csrf` (double-submit; o login também). Bearer
puro não usa cookie — sem exigência. Sem CORS credenciado.

**Rate limit de login**: falhas contadas em `login_attempts`
(PostgreSQL — vale entre processos da API): `MACIP_API_LOGIN_MAX_ATTEMPTS`
(default 5) por `MACIP_API_LOGIN_WINDOW_SECONDS` (default 900), por
login E por IP da conexão direta — cabeçalhos `X-Forwarded-*` não são
confiáveis. A contagem é **atômica**: dois advisory locks
transacionais (escopo login, depois IP — ordem fixa, sem deadlock)
serializam tentativas simultâneas; nenhuma lê contagem desatualizada.
Respostas de login são genéricas: credencial errada, login inexistente
e operador desativado recebem o mesmo `401`.

Administração de operadores (Bearer admin OU operador `admin`):

```bash
GET    /api/v1/operators                         # lista
POST   /api/v1/operators                         # cria {login,name,role,password}
PATCH  /api/v1/operators/<id>                    # name/role/active
POST   /api/v1/operators/<id>/reset-password     # redefinir senha
```

Leitura (`GET` clients/agents/credentials/executions): Bearer admin OU
qualquer operador ativo. Escrita administrativa: Bearer admin OU
operador `admin`. `viewer` recebe `403` em escrita. O Bearer admin
segue NÃO indo ao navegador — não é substituto da sessão.

## Tarefas (cadastro e versionamento)

O cadastro de tarefa guarda a configuração de backup NA CENTRAL —
ainda **não é distribuído nem aplicado** ao agente. O JSON exportado é
compatível com `TaskSpec.from_dict()` do agente atual.

### Modelo

- `tasks` (identidade estável): `id` (UUID aleatório — `uuid4`,
  gerado na criação e imutável), `agent_id`,
  `current_revision`, `archived_at`, `created_at`. O vínculo com o
  cliente deriva do agente (`task.agent.client_id`) e é validado no
  servidor.
- `task_revisions`: snapshot imutável (`config` JSONB), `revision`
  numerada desde 1, `contract_version` (1), `author_kind`
  (`admin`/`operator`), `author_ref` (login do operador ou `bearer`),
  `created_at` UTC, `client_name` (snapshot do nome do cliente —
  exportações históricas não dependem de nomes editáveis).
  Unicidade `(task_id, revision)` no banco; revision e ponteiro
  avançam na **mesma transação**. Revisões criadas antes da
  migração 4 guardam o nome vigente à época dela — o histórico
  anterior não era registrado e nada é inventado.

### Envelope de configuração

```json
{
  "name": "nome editável (nunca identificador)",
  "spec":  { "sources": [{"path": "/dados"}], "exclusions": ["*.tmp"],
             "compression": "auto|off|max",
             "destinations": [{"id": "d1", "name": "…", "kind":
                               "local|sftp|smb|s3", "repository": "...",
                               "password_environment": "PW", "endpoint_url": "...",
                               "retention": {"keep_last": 7}}],
             "timeouts": {...}, "retry": {...} },
  "schedule": { "kind": "manual|hourly|daily|weekly",
                "timezone": "America/Sao_Paulo", "hour": 2, "minute": 15,
                "weekday": 1 }
}
```

- `spec` é o documento `TaskSpec` SEM `id`/`client`/`device_id` —
  esses vêm da identidade central na exportação.
- `schedule` fica no envelope, fora do `TaskSpec` exportado. `hour`
  0–23, `minute` 0–59, `weekday` 0–6 (0 = segunda, padrão Python),
  `timezone` IANA válida (ex.: `America/Sao_Paulo`). Nenhum comando
  shell é aceito; a conversão para timers do agente é entrega futura.
- `password_environment`/`endpoint_url` apontam para segredos por
  referência — segredos nunca são persistidos, retornados ou logados.
- **Caminhos são conferidos lexicalmente** (nunca no filesystem da
  central): origens, `repository` local e `smb.mount_point` devem
  ser absolutos, sem `.`, `..`, `//` ou componente vazio — alias
  que o agente resolveria para dentro de uma origem é rejeitado;
  caracteres de controle
  (newline, NUL…) são rejeitados em qualquer campo de texto;
  espaços em caminhos são legítimos. `sftp.identity_file`,
  `smb.repository`, `s3.prefix` e `s3.endpoint` seguem regras
  próprias e credenciais embutidas em URL são proibidas.
- `retry.attempts` ≤ 64 — o mesmo teto do recebimento de
  resultados (`MAX_ATTEMPTS` em `schemas.py`).
- **422 não ecoa segredos**: `input`/`ctx`/`msg` mascaram qualquer
  chave com nome de segredo (password, private/secret/access_key,
  token, credential, identity…) em qualquer profundidade, incluindo
  campos desconhecidos rejeitados por `extra=forbid`, e userinfo de
  URL — o userinfo inteiro até o último `@` do segmento de
  autoridade é removido (`https://u:se@nha@host` →
  `https://***@host`), mesmo em URLs malformadas.

### Rotas

| Método | Rota | Permissão | Respostas |
|---|---|---|---|
| POST | `/api/v1/agents/{agent_id}/tasks` | admin | 201 / 400 agente-cliente / 403 CSRF+role / 422 validação |
| GET | `/api/v1/tasks` | viewer+ | 200 `[TaskRow]` — `?limit=&offset=&client_id=&agent_id=&archived=` |
| GET | `/api/v1/tasks/{id}` | viewer+ | 200 TaskDetail (`revision` atual embutida) / 404 |
| POST | `/api/v1/tasks/{id}/revisions` | admin | 201 / **409** expected_revision obsoleta / 409 arquivada / 404 / 422 |
| GET | `/api/v1/tasks/{id}/revisions` | viewer+ | 200 `[RevisionRow]` |
| GET | `/api/v1/tasks/{id}/revisions/{n}` | viewer+ | 200 / 404 |
| GET | `/api/v1/tasks/{id}/revisions/{n}/export` | viewer+ | 200 `tarefa.json` (attachment) |
| POST | `/api/v1/tasks/{id}/archive` | admin | 200 / 404 |

Tokens de AGENTE não autorizam nenhuma rota administrativa (403).

### Concorrência otimista

Toda transição de escrita (`POST …/revisions`, `POST …/archive`)
abre a transação com `SELECT … FOR UPDATE` na linha da tarefa —
edição, arquivamento e salvamento idêntico leem o estado vigente
atomicamente e ficam serializados na fila do lock. Não há janela
check→update: arquivamento que chegou primeiro bloqueia a edição
 seguinte (409) e vice-versa.

A nova revisão exige `expected_revision` vigente sob o lock; expected
obsoleto → **409**; a vencedora fica intacta. Salvamento **idêntico**
com expected válido não cria revisão: o documento é normalizado
(JSON canônico — chaves ordenadas, fontes como `{"path": …}`) e
comparado ao snapshot atual; se idêntico, responde `unchanged` —
mas só depois da revalidação da expected sob o lock, nunca por uma
leitura obsoleta.

### Distribuição gerenciada

Três conceitos separados: revisão **cadastrada** (mais recente),
revisão **publicada** e revisão **confirmada aplicada** pelo agente.
Criar/editar nunca publica — publicação e suspensão são ações
administrativas explícitas com CSRF + controle otimista de geração.

- `task_distributions` (1 linha/tarefa, criada sob demanda):
  `generation` monotônica que distingue publicação, atualização e
  suspensão mesmo com a mesma revisão; `published_revision`;
  `desired_action` (`none`/`apply`/`suspend`); colunas `confirmed_*`
  com a última confirmação **da geração vigente**.
- `task_confirmations`: log idempotente por `(task_id, generation)`
  com `payload_hash` — mesmo conteúdo → `duplicate`; conteúdo diverso
  para a mesma geração → 409.

**Rotas administrativas** (`require_admin_identity` + `csrf_guard`,
`FOR UPDATE` na linha da distribuição):

- `POST /api/v1/tasks/{id}/publish` `{revision, expected_generation}`
  — 409 em geração obsoleta, revisão inexistente → 404, arquivada → 409.
- `POST /api/v1/tasks/{id}/suspend` `{expected_generation}` —
  suspende só a agenda gerenciada: não apaga snapshots, configurações
  ou histórico e não interrompe execução. Disponível também em tarefa
  arquivada (arquivar não suspende por si).

**Rotas do agente** (identidade exclusivamente da credencial Bearer —
o agente só vê/confirma tarefas vinculadas a ele):

- `GET /api/v1/agent/tasks/desired?limit&offset` →
  `{agent_id, offset, limit, total, tasks[]}` — paginação explícita;
  ausência numa página não é autorização para desinstalar nada.
  Cada envelope traz `task_id`, `agent_id`, `generation`, `action`,
  `revision`, `contract_version`, `hash` (sha256 do JSON canônico do
  envelope sem hash) e, em `apply`, `task` (TaskSpec exportado —
  somente referências de credencial) + `schedule` separado.
- `POST /api/v1/agent/tasks/{id}/confirmations`
  `{generation, status: applied|failed, applied_revision?, detail?,
  agent_time?}` — geração futura → 409; geração antiga é registrada no
  log (`promoted: false`) sem rebaixar o estado confirmado; a vigente
  promove `confirmed_*`. `received_at` usa horário do servidor.

### Exportação (`tarefa.json`)

- `id` = UUID central da tarefa (não o nome);
- `client` = `{id, name}` central;
- `device_id` = UUID do agente;
- `name`/`spec` = snapshot daquela revisão;
- `schedule` **não** aparece (envelope separado);
- fontes normalizadas para `{"path": …}`; `password_environment`/`endpoint_url` por referência, sem segredo.

### Arquivamento

`POST …/archive` marca `archived_at` sem apagar histórico — todas as
revisões ficam consultáveis/exportáveis. Bloqueia novas revisões e a
edição na central. **Não** interrompe um backup em execução nem mexe
em agenda já instalada no agente (a config central ainda não é
distribuída).

## Segurança

- Token de agente entregue uma vez; armazenado só como SHA-256.
- `Authorization` nunca é registrado em log (uvicorn loga só status+path).
- Identidade vem da credencial autenticada — corpo que a contradiz → 403.
- Senhas/hashes/sessões/cookies não aparecem em logs nem respostas —
  campos sensíveis são mascarados também no `input` dos `422`.
- Auditoria (`macip_backup_api.audit`): login, falha, logout, criação/
  desativação de operador, mudança de perfil e reset — sem segredos.
- **HTTPS obrigatório** para comunicação fora de localhost — os endpoints
  de agente levam Bearer tokens e as sessões trafegam por cookie;
  sem TLS ambos circulam em claro.

## Testes

PostgreSQL real exclusivo — `MACIP_API_TEST_DATABASE_URL` obrigatório
e o nome do banco deve conter `test` (guarda contra limpeza destrutiva
em banco operacional):

```bash
podman run -d --name macip-api-test-pg \
  -e POSTGRES_PASSWORD=<senha-teste> -e POSTGRES_DB=macip_api_test \
  -p 55432:5432 docker.io/library/postgres:17-alpine

python3 -m venv .venv-api && . .venv-api/bin/activate
pip install -e 'api[test]'
MACIP_API_TEST_DATABASE_URL='postgresql://postgres:<senha-teste>@localhost:55432/macip_api_test' \
  python -m pytest api/tests/ -v
```

- Sem `MACIP_API_TEST_DATABASE_URL` → testes de integração **pulam
  explicitamente**; testes de schema rodam sempre.
- URL configurada mas inacessível → **falha clara** (não skip).
- SQLite não substitui — `ON CONFLICT`/`advisory lock` são específicos
  de PostgreSQL.
