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
runbook).

Bloco de apoio — helper de confirmação e fixture de execução:

```bash
# confirmar geração G da tarefa T como aplicada (rev ou null=suspend)
confirm() { # task_id generation applied_revision|"null"
  bash scripts/lab-curl.sh agent POST "/api/v1/agent/tasks/$1/confirmations" \
    "{\"generation\":$2,\"attempt\":1,\"status\":\"applied\",\"applied_revision\":$3}"
}
# fixture de execução: task_id execution_id status started finished
exec_fig() { # task_id exec_id status started finished
  bash scripts/lab-curl.sh agent POST /api/v1/agent/executions '{
    "execution_id": "'"$2"'", "task_id": "'"$1"'", "status": "'"$3"'",
    "started_at_utc": "'"$4"'", "finished_at_utc": "'"$5"'",
    "destinations": [{"destination_id":"lab-dest","status":"'"$3"'",
      "started_at_utc":"'"$4"'","finished_at_utc":"'"$5"'",
      "snapshot_id":"snap-'"$2"'","attempts":[{"attempt":1,"status":"'"$3"'",
      "started_at_utc":"'"$4"'","finished_at_utc":"'"$5"'",
      "duration_seconds":300,"snapshot_id":"snap-'"$2"'",
      "files_processed":42,"bytes_processed":1048576}]}]}'
}
gen() { bash scripts/lab-curl.sh admin GET "/api/v1/tasks/$1" \
  | python3 -c 'import json,sys;print(json.load(sys.stdin)["distribution"]["generation"])'; }
```

## Cenários

### C1 — Tarefa não publicada
- **Prep:** `lab-curl.sh admin POST /api/v1/agents/<agent_id>/tasks` com
  doc agenda automática; NÃO publicar. Anote `id` da resposta.
- **Ação:** recarregar lista `/tasks` e detalhe no portal.
- **Esperado:** operacional `unpublished`; sem selo de agenda ativa.
- **Evidência:** JSON `GET /monitoring/tasks/<id>` + captura.
- **Critério:** qualquer anúncio de agenda instalada → reprovado.

### C2 — Manual aplicada
- **Prep:** `lab-c2` `schedule={"kind":"manual"}`; publish rev 1
  (`expected_generation=0`) → `confirm T G1 1`; nova publicação
  (`expected_generation=G1`, rev manual) sem confirmar → gen pendente.
- **Esperado:** `pending_apply`; explicação de continuidade MANUAL,
  sem "agenda automática ativa"; `next_occurrence_utc` nulo.
- **Evidência:** JSON + captura. **Critério:** menção a timer
  automático → reprovado.

### C3 — Publicação pendente preserva a configuração anterior
- **Prep:** `lab-c3` diária UTC; publish+confirm gen 1; publish rev 2
  (`expected_generation=1`) sem confirmar.
- **Esperado:** `pending_apply`; "agenda automática anterior continua
  ativa"; cobrança segue pela agenda ANTERIOR.
- **Critério:** cobrança pausada ou agenda nova antes da confirmação →
  reprovado.

### C4 — Agenda automática confirmada
- **Prep:** `lab-c4` `{"kind":"hourly","timezone":"UTC","minute":M}`
  com M = minuto ~3–5 min no futuro; publish+confirm ANTES de M.
- **Esperado:** `scheduled` + `next_occurrence_utc` = próxima
  ocorrência UTC:M.
- **Evidência:** JSON + captura + horário do servidor registrado.

### C5 — Ocorrência dentro da tolerância
- **Prep:** mesma `lab-c4` (âncora anterior à ocorrência UTC:M).
- **Ação:** consultar entre UTC:M e UTC:M+tolerância.
- **Esperado:** `pending`/`within-tolerance`; `expected_occurrence_utc`
  e `result_deadline_utc` preenchidos e coerentes.
- **Critério:** perdeu a janela → refaça com o M da hora seguinte;
  inconclusivo se não reproduzido.

### C6 — Prazo vencido sem resultado → `missing` ≠ falha
- **Prep:** `lab-c4` SEM execução recebida na ocorrência UTC:M.
- **Ação:** consultar APÓS UTC:M+tolerância (espera real).
- **Esperado:** `missing`/`overdue`, selo âmbar "sem resultado",
  "não prova falha do backup"; NÃO vermelho, NÃO "agente offline".
- **Evidência:** JSON + captura do selo.

### C7 — Falha recebida ≠ ausência (fixture completa)
- **Prep:** `lab-c7` horária UTC:M′; publish+confirm gen 1 ANTES de
  UTC:M′ (âncora anterior à ocorrência — obrigatório para haver
  cobrança). Após UTC:M′ passar:
  `exec_fig lab-c7 exec-c7-1 failed "<M′+5s>" "<M′+125s>"`.
  Confira `# HTTP 200` na resposta do POST.
- **Esperado:** `failed`, selo vermelho "falha"; `last_execution`
  com `started_at_utc` = M′+5s.
- **Evidência:** JSON + captura + resposta do POST.
  **Critério:** âmbar/"sem resultado" → reprovado; se `no_history`,
  a âncora não precedeu a ocorrência → preparação errada, repetir.

### C8 — Resultado parcial (fixture completa)
- **Prep:** `lab-c8` horária UTC:M″; mesmo fluxo; POST `partial`.
- **Esperado:** `partial`, selo âmbar "parcial" — NUNCA verde.
- **Critério:** "em dia"/verde → reprovado.

### C9 — Sucesso e recebimento tardio
- **Prep:** `lab-c9` horária UTC:M‴; confirmar aplicação ANTES de M‴.
- **Ação 1 (sucesso):** após M‴: `exec_fig lab-c9 exec-c9-ok success
  "<M‴+10s>" "<M‴+130s>"` → espera-se `ok`, `received_late=false`.
- **Ação 2 (tardio):** `lab-c9b` idêntico na MESMA ocorrência; POST
  success APÓS M‴+tolerância, com `started_at_utc` ainda dentro da
  janela da ocorrência (ex.: M‴+10s) → espera-se `ok` +
  `received_late=true` e reason com sufixo `-late`.
- **Evidência:** JSONs das duas tarefas + horário real dos POSTs.
- **Critério:** tardio sem `received_late` → reprovado; fixture com
  início fora da janela da ocorrência não satisfaz a cobrança —
  corrija a fixture, não o esperado.

### C10 — Suspensão pendente e confirmada
- **Prep:** `lab-c10` automática aplicada (gen 1). `suspend`
  (`expected_generation=1`) → gen 2 pendente.
- **Ação 1:** explicação "continua ativa até confirmar".
- **Ação 2:** `confirm lab-c10 2 null` → `suspended`; nenhuma
  ocorrência nova esperada; sem `missing` após prazo.
- **Evidência:** JSON antes/depois.

### C11 — Republicação sobre suspensa, sem cobrança antes da retomada
- **Prep:** `lab-c10` suspensa (gen 2). `suspend` de novo
  (`expected_generation=2`) OU `publish` (`expected_generation=2`) —
  exercitar as duas formas (gen 3 pendente).
- **Esperado:** `pending_suspend`/`pending_apply` com explicação
  "permanece/já estava suspensa"; `no_history`, jamais `missing`.
- **Critério:** "agenda continua ativa" ou cobrança → reprovado.

### C12 — Retomada confirmada reancora
- **Prep:** `publish` rev automática em `lab-c10`
  (`expected_generation` atual) → `confirm T <gen> <rev>`.
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
- **Prep:** detalhe de `lab-c7` aberto e carregado.
- **Ação:** `npx playwright test e2e/monitoring-fault.spec.ts` com
  `MACIP_HOMOLOG_*` — a spec injeta, no navegador, aborto SELETIVO e
  SIMULADO apenas em `/api/v1/monitoring/*` (simulação de falha de
  rede, NÃO indisponibilidade real da API) e recarrega.
- **Esperado:** tarefa/revisões continuam exibidas; a seção mostra
  "Não foi possível carregar o acompanhamento"; nenhum selo de
  falha/`missing` aparece; removida a interceptação, nova consulta
  restaura a seção.
- **Critério:** selo de backup derivado de erro de rede → reprovado;
  spec skipada (sem envs) → inconclusivo, não aprovado.

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
