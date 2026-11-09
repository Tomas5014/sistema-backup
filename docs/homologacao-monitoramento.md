# Plano de homologação — monitoramento de tarefas (0.4.0dev2)

Execução posterior pelo operador, no laboratório isolado instalado por
[instalacao-monitoramento.md](instalacao-monitoramento.md). Cada
cenário declara preparação, ação, esperado, evidência e critério.
**Sequência executável:** diretórios, variáveis e gerações são
explícitas — nada fica para o operador adivinhar.

Esta homologação é do MONITORAMENTO — não confundir com os sete dias
do piloto SFTP (congelado em `ea74d5d`, sem este código).

## Convenções do plano

- Diretório vigente de TODOS os blocos: `cd ~/macip-lab-monitoramento/macip-backup-linux-0.4.0dev2/source/deploy/central`.
- Chamadas de API: `bash scripts/lab-curl.sh <admin|agent> <MÉTODO> <path> ['<json>']`
  — credencial por stdin (nunca argv), TLS verificado, redirects
  desativados, HTTP≠2xx sai com erro (registre o `# HTTP <code>`).
- **Fixtures REST:** confirmações/execuções postadas pela API são
  sinais de teste — NÃO são prova de backup Restic real.
- **Gerações:** toda escrita exige `expected_generation` atual — leia
  antes com `GET /api/v1/tasks/<id>` (`.distribution.generation`) e
  anote o valor usado. Nunca reaproveite geração já consumida.
- **Atualização do portal:** refletem a última consulta — após cada
  mutação, RECARREGUE a página/rota; não há atualização automática.
- **Tolerância:** `MACIP_API_MONITOR_TOLERANCE_SECONDS` em
  `secrets/api.env` (sugestão de laboratório: `60`). Anote o valor
  usado; cenários de `missing` exigem ESPERA REAL (ocorrência +
  tolerância) — não há controle de relógio na API de produção.
- **Registro por cenário:** data, SHA em execução
  (`verify-identity.sh`), agenda/fuso/ocorrência/tolerância/horários,
  respostas JSON e capturas.

Pré-requisito: `check.sh` e `verify-identity.sh` verdes; operadores
`lab-operador` (admin) e `lab-viewer` criados; cliente/agente `lab-*`
com credencial provisionada em `secrets/agent-token.env` (§5 do
runbook). Guarde `AGENT_ID` (UUID) da criação do agente.

Bloco de apoio — os IDs abaixo são sempre **UUIDs** devolvidos pela
API. O NOME da tarefa ("lab-c7") é apresentação; publicação,
confirmação, execução e leitura usam o UUID em variável explícita.
JSON de execução é montado por `python3` (sem concatenação frágil).

```bash
# cria tarefa e devolve o UUID — agenda e nº de destinos por cenário
mk_task() { # agent_uuid nome schedule-json destinos(1|2)
  bash scripts/lab-curl.sh admin POST "/api/v1/agents/$1/tasks" \
    "$(python3 - "$2" "$3" "$4" <<'PY'
import json, sys
dests = [{"id": "lab-dest", "name": "Disco lab", "kind": "local",
          "repository": "/srv/backup/lab",
          "password_environment": "MACIP_LAB_PW",
          "retention": {"keep_last": 7}}]
if sys.argv[3] == "2":
    dests.append({"id": "lab-dest2", "name": "Disco lab 2",
                  "kind": "local", "repository": "/srv/backup/lab2",
                  "password_environment": "MACIP_LAB_PW",
                  "retention": {"keep_last": 7}})
print(json.dumps({"name": sys.argv[1],
  "spec": {"sources": ["/dados/lab"], "destinations": dests},
  "schedule": json.loads(sys.argv[2])}))
PY
)" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])'
}

# confirmação: T=UUID da tarefa; REV=revisão ou null (suspensão)
confirm() { # task_uuid generation applied_revision|"null"
  bash scripts/lab-curl.sh agent POST "/api/v1/agent/tasks/$1/confirmations" \
    "{\"generation\":$2,\"attempt\":1,\"status\":\"applied\",\"applied_revision\":$3}"
}

# fixture de execução VÁLIDA no contrato: a execução aceita 'partial',
# mas destino/tentativa aceitam SÓ success|failed|interrupted — parcial
# é SEMÂNTICA de resultados mistos, nunca status em destino/tentativa.
# spec: one (1 destino) | mixed (lab-dest=success + lab-dest2=failed)
exec_fig() { # task_uuid exec_id status started finished spec
  python3 - "$@" <<'PY'
import json, sys
task_id, exec_id, status, started, finished, spec = sys.argv[1:7]
def dest(did, st):
    return {"destination_id": did, "status": st,
            "started_at_utc": started, "finished_at_utc": finished,
            "snapshot_id": f"snap-{exec_id}-{did}" if st == "success" else None,
            "attempts": [{"attempt": 1, "status": st,
                          "started_at_utc": started,
                          "finished_at_utc": finished,
                          "duration_seconds": 300,
                          "files_processed": 42,
                          "bytes_processed": 1048576}]}
SPECS = {"one":   [dest("lab-dest", status)],
         "mixed": [dest("lab-dest", "success"),
                   dest("lab-dest2", "failed")]}
print(json.dumps({"execution_id": exec_id, "task_id": task_id,
  "status": status, "started_at_utc": started,
  "finished_at_utc": finished, "destinations": SPECS[spec]}))
PY
}
# postar fixture → confira '# HTTP 2xx' impresso pelo lab-curl
post_exec() { bash scripts/lab-curl.sh agent POST \
  /api/v1/agent/executions "$(exec_fig "$@")"; }

gen() { bash scripts/lab-curl.sh admin GET "/api/v1/tasks/$1" \
  | python3 -c 'import json,sys;print(json.load(sys.stdin)["distribution"]["generation"])'; }
```

## Cenários

Registre sempre: UUID, geração usada em cada escrita, `# HTTP` das
respostas, e os horários reais do servidor.

### C1 — Tarefa não publicada
- **Prep:** `TASK_C1="$(mk_task "$AGENT_ID" lab-c1 \
  '{"kind":"daily","timezone":"UTC","hour":3,"minute":0}' 1)"`
  — NÃO publicar.
- **Ação:** recarregar lista `/tasks` e detalhe no portal;
  `lab-curl.sh admin GET /api/v1/monitoring/tasks/$TASK_C1`.
- **Esperado:** operacional `unpublished`; sem selo de agenda ativa.
- **Evidência:** JSON + captura.
- **Critério:** qualquer anúncio de agenda instalada → reprovado.

### C2 — Manual aplicada
- **Prep:** `TASK_C2="$(mk_task "$AGENT_ID" lab-c2 \
  '{"kind":"manual"}' 1)"`; publish rev 1
  (`expected_generation=0`) → `confirm "$TASK_C2" 1 1`; nova
  publicação (`expected_generation="$(gen "$TASK_C2")"`, rev manual)
  sem confirmar → geração pendente.
- **Esperado:** `pending_apply`; explicação de continuidade MANUAL,
  sem "agenda automática ativa"; `next_occurrence_utc` nulo.
- **Evidência:** JSON + captura. **Critério:** menção a timer
  automático → reprovado.

### C3 — Publicação pendente preserva a configuração anterior
- **Prep:** `TASK_C3="$(mk_task "$AGENT_ID" lab-c3 \
  '{"kind":"daily","timezone":"UTC","hour":2,"minute":0}' 1)"`;
  publish+`confirm "$TASK_C3" 1 1`; publish rev 2
  (`expected_generation="$(gen "$TASK_C3")"`) sem confirmar.
- **Esperado:** `pending_apply`; "agenda automática anterior continua
  ativa"; cobrança segue pela agenda ANTERIOR.
- **Critério:** cobrança pausada ou agenda nova antes da confirmação →
  reprovado.

### C4 — Agenda automática confirmada
- **Prep:** `TASK_C4="$(mk_task "$AGENT_ID" lab-c4 \
  '{"kind":"hourly","timezone":"UTC","minute":M}' 1)"` com M =
  minuto ~3–5 min no futuro; publish+`confirm "$TASK_C4" 1 1`
  ANTES de M — a âncora PRECISA preceder a ocorrência.
- **Esperado:** `scheduled` + `next_occurrence_utc` = próxima
  ocorrência UTC:M.
- **Evidência:** JSON + captura + horário do servidor registrado.

### C5 — Ocorrência dentro da tolerância
- **Prep:** mesma `$TASK_C4` (âncora anterior à ocorrência UTC:M).
- **Ação:** consultar entre UTC:M e UTC:M+tolerância.
- **Esperado:** `pending`/`within-tolerance`; `expected_occurrence_utc`
  e `result_deadline_utc` preenchidos e coerentes.
- **Critério:** perdeu a janela → refaça com o M da hora seguinte;
  inconclusivo se não reproduzido.

### C6 — Prazo vencido sem resultado → `missing` ≠ falha
- **Prep:** `$TASK_C4` SEM execução recebida na ocorrência UTC:M.
- **Ação:** consultar APÓS UTC:M+tolerância (espera real).
- **Esperado:** `missing`/`overdue`, selo âmbar "sem resultado",
  "não prova falha do backup"; NÃO vermelho, NÃO "agente offline".
- **Evidência:** JSON + captura do selo.

### C7 — Falha recebida ≠ ausência (fixture completa)
- **Prep:** `TASK_C7="$(mk_task "$AGENT_ID" lab-c7 \
  '{"kind":"hourly","timezone":"UTC","minute":M1}' 1)"`;
  publish+`confirm "$TASK_C7" 1 1` ANTES de UTC:M1 — âncora
  anterior à ocorrência é OBRIGATÓRIA para haver cobrança. Após
  UTC:M1 passar:
  `post_exec "$TASK_C7" exec-c7-1 failed "<M1+5s>" "<M1+125s>" one`
  → confira `# HTTP 200` (422 = fixture inválida, não falha do
  produto).
- **Esperado:** `failed`, selo vermelho "falha"; `last_execution`
  com `started_at_utc` = M1+5s.
- **Evidência:** JSON + captura + resposta do POST.
  **Critério:** âmbar/"sem resultado" → reprovado; se `no_history`,
  a âncora não precedeu a ocorrência → preparação errada, repetir.

### C8 — Resultado parcial (fixture completa, 2 destinos)
- **Prep:** `TASK_C8="$(mk_task "$AGENT_ID" lab-c8 \
  '{"kind":"hourly","timezone":"UTC","minute":M2}' 2)"` — spec com
  DOIS destinos (lab-dest, lab-dest2), pois `partial` é resultado
  misto. publish+`confirm "$TASK_C8" 1 1` antes de UTC:M2; depois:
  `post_exec "$TASK_C8" exec-c8-1 partial "<M2+5s>" "<M2+125s>" mixed`
  — execução `partial`, destinos `success`+`failed` (única forma
  válida no contrato; `partial` em destino é HTTP 422).
- **Esperado:** `partial`, selo âmbar "parcial" — NUNCA verde.
- **Critério:** "em dia"/verde → reprovado.

### C9 — Sucesso e recebimento tardio
- **Prep:** `TASK_C9="$(mk_task "$AGENT_ID" lab-c9 \
  '{"kind":"hourly","timezone":"UTC","minute":M3}' 1)"` e
  `TASK_C9B="$(mk_task "$AGENT_ID" lab-c9b \
  '{"kind":"hourly","timezone":"UTC","minute":M3}' 1)"` — mesma
  ocorrência UTC:M3; confirmar aplicação de ambas ANTES de M3.
- **Ação 1 (sucesso):** após M3 e ANTES do prazo:
  `post_exec "$TASK_C9" exec-c9-ok success "<M3+10s>" "<M3+130s>" one`
  → espera-se `ok`, `received_late=false`.
- **Ação 2 (tardio):** APÓS M3+tolerância, em `$TASK_C9B`:
  `post_exec "$TASK_C9B" exec-c9b-late success "<M3+10s>" "<M3+130s>" one`
  — `started_at_utc` dentro da janela da ocorrência, recebimento
  depois do prazo → espera-se `ok` + `received_late=true` e reason
  com sufixo `-late`.
- **Evidência:** JSONs das duas tarefas + horário real dos POSTs.
- **Critério:** tardio sem `received_late` → reprovado; fixture com
  início fora da janela da ocorrência não satisfaz a cobrança —
  corrija a fixture, não o esperado.

### C10 — Suspensão pendente e confirmada
- **Prep:** `TASK_C10="$(mk_task "$AGENT_ID" lab-c10 \
  '{"kind":"daily","timezone":"UTC","hour":1,"minute":0}' 1)"`;
  publish+`confirm "$TASK_C10" 1 1`. `suspend`
  (`expected_generation="$(gen "$TASK_C10")"`) → geração pendente.
- **Ação 1:** explicação "continua ativa até confirmar".
- **Ação 2:** `confirm "$TASK_C10" <gen-pendente> null` → `suspended`;
  nenhuma ocorrência nova esperada; sem `missing` após prazo.
- **Evidência:** JSON antes/depois.

### C11 — Republicação sobre suspensa, sem cobrança antes da retomada
- **Prep:** `$TASK_C10` suspensa. `suspend` de novo OU `publish`,
  ambos com `expected_generation="$(gen "$TASK_C10")"` atual —
  exercitar as duas formas.
- **Esperado:** `pending_suspend`/`pending_apply` com explicação
  "permanece/já estava suspensa"; `no_history`, jamais `missing`.
- **Critério:** "agenda continua ativa" ou cobrança → reprovado.

### C12 — Retomada confirmada reancora
- **Prep:** `publish` rev automática em `$TASK_C10`
  (`expected_generation="$(gen "$TASK_C10")"`) →
  `confirm "$TASK_C10" <gen> <rev>`.
- **Esperado:** `scheduled`; cobrança só para ocorrências APÓS a nova
  âncora — intervalo suspenso nunca cobrado retroativamente.
- **Evidência:** JSON com `expected_occurrence_utc` posterior à
  âncora registrada.

### C13 — Viewer lê; escrita recusada por AUTORIZAÇÃO (CSRF válido)
- **Prep:** login UI `lab-viewer`.
- **Ação 1:** lista/detalhe carregam acompanhamento.
- **Ação 2 (CSRF isolada):** POST sem `x-csrf-token` → 403 com
  detail contendo "CSRF".
- **Ação 3 (RBAC):** mesmo POST com `x-csrf-token` = cookie
  `macip_csrf` da sessão → 403 detail = "perfil sem permissão de
  escrita"; geração inalterada antes/depois.
- **Critério:** recusa só por CSRF não prova papel — exige detail
  RBAC + geração estável.

### C14 — Admin escreve pela mesma via (controle positivo)
- **Prep:** login UI `lab-operador`.
- **Ação:** POST equivalente com sessão+CSRF válidos → 200 e geração
  +1 — prova que a via funciona e a recusa de C13 foi papel.
- **Evidência:** resposta + gerações.

### C15 — Falha de carregamento da seção ≠ falha de backup

A spec `portal/e2e/monitoring-fault.spec.ts` injeta, só no navegador,
aborto SELETIVO e SIMULADO em `/api/v1/monitoring/*` — simulação de
falha de rede, NÃO indisponibilidade real da API.

- **Diretórios:** saia de `deploy/central` e vá para o portal da
  fonte extraída: `cd ~/macip-lab-monitoramento/macip-backup-linux-0.4.0dev2/source/portal`.
- **Pré-requisitos:** node+npm; `npm ci` (dependências do portal);
  `npm run build`; navegador — `channel:"chrome"` do sistema ou
  `npx playwright install chromium`; para HTTPS do laboratório, a CA
  entra num nssdb de HOME descartável (mesmo mecanismo de
  `prove.sh`: `certutil -A ... -i tls/lab-ca.pem` no HOME do teste,
  exportado como `MACIP_HOMOLOG_HOME`).
- **Credencial privada:** `MACIP_HOMOLOG_SECRETS` aponta o JSON 600
  `secrets/lab-operator.json` — escrito por
  `scripts/gen-operator-creds.sh` (senha digitada em prompt oculto) e
  lido DIRETAMENTE pela spec via parser JSON: a senha nunca passa por
  `source`/argv/histórico — `$`, aspas, `#`, `;`, `\`, `$(...)`
  chegam literalmente (limite do formato: sem quebra de linha).
  `MACIP_HOMOLOG_TASK` = UUID real (`$TASK_C7`, por exemplo).
- **Execução:** em subshell, sem variáveis herdadas que possam
  acionar outra stack:
  ```bash
  cd ~/macip-lab-monitoramento/macip-backup-linux-0.4.0dev2/source/portal
  (
    unset MACIP_API_TEST_DATABASE_URL MACIP_API_ORIGIN \
          MACIP_HOMOLOG_LOGIN MACIP_HOMOLOG_PASSWORD
    export MACIP_HOMOLOG_URL=https://localhost:19443 \
           MACIP_HOMOLOG_TASK="$TASK_C7" \
           MACIP_HOMOLOG_HOME=/tmp/nssdb-c15 \
           MACIP_HOMOLOG_SECRETS="$PWD/../deploy/central/secrets/lab-operator.json"
    npm run build && npx playwright test e2e/monitoring-fault.spec.ts
  )
  ```
- **Esperado:** (1) acompanhamento carregado antes da injeção;
  (2) com a falha ativa, tarefa e revisões respondem 200 e a seção
  mostra "Não foi possível carregar o acompanhamento" — sem selo de
  falha/missing; (3) controle: mantendo o aborto, nova consulta ainda
  falha — não satisfaz recuperação; (4) removida a interceptação,
  resposta 200 do endpoint da tarefa esperada e conteúdo renderizado.
- **Critério:** selo de backup derivado de erro de rede → reprovado;
  spec skipada (envs ausentes) ou browser/deps indisponíveis →
  INCONCLUSIVO declarado, nunca aprovado; saída "0 executados" não
  fecha o gate.

## Fechamento — regras rígidas

- **Categorias por cenário:** `aprovado`, `reprovado`,
  `pendente/inconclusivo`, `interrompido`. Uma exceção de escopo, se
  existir, deve ser EXPLÍCITA e nunca contar como cobertura executada.
- **Resultado global `aprovado`** exige TODOS os cenários obrigatórios
  `aprovado`. Justificativa não converte pendência em aprovação.
- Consolidar: tabela cenário × categoria, anexos (JSON, capturas,
  saídas `# HTTP`), tolerância usada, SHA em execução e divergências.
- A homologação deste plano NÃO aprova produção nem o piloto — é
  evidência para a decisão seguinte.
