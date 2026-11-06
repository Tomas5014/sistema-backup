# Plano de homologação — monitoramento de tarefas (0.4.0dev2)

Execução posterior pelo operador, no laboratório isolado instalado por
[instalacao-monitoramento.md](instalacao-monitoramento.md). Cada
cenário declara preparação, ação, esperado, evidência e critério
(aprovado / reprovado / inconclusivo). Registre por cenário: data,
SHA em execução (`deploy/central/VERSION`), respostas e capturas.

Esta homologação é do MONITORAMENTO — não confundir com os sete dias
do piloto SFTP, que segue congelado em `ea74d5d` e não usa este código.

## Classificação das evidências

- **Fixture REST:** execuções/confirmações postadas via API com tokens
  do laboratório (admin/agente) — simulam sinais, NÃO são prova de
  backup Restic real nem de agente instalado.
- **Evidência de navegador:** telas do portal observadas pelo operador.
- **Evidência operacional:** comandos executados e suas saídas.
- **Tempo real:** cenários `pending`→`missing` exigem espera real de
  minutos (tolerância configurável via `MACIP_API_MONITOR_TOLERANCE_SECONDS`
  em `secrets/api.env` — anote o valor usado; não há controle de
  relógio na API de produção).
- **Atualização do portal:** lista e detalhe refletem o último GET —
  após alterar dados, RECARREGUE a página/rota; não há atualização
  automática declarada.

Pré-requisito comum: laboratório no ar (`check.sh` verde), operador
admin `lab-operador` e viewer `lab-viewer` criados, cliente/agente
`lab-*` com credencial (token `AGENT_TOKEN` guardado).

Bloco de apoio (fixture — ajuste porta/hostname ao laboratório):

```bash
BASE='https://localhost:19443/api/v1'
CA='--cacert tls/lab-ca.pem'
ADMIN="Authorization: Bearer $ADMIN_TOKEN"   # Bearer admin de api.env
AG="Authorization: Bearer $AGENT_TOKEN"
# confirmar geração G da tarefa T como aplicada (applied_revision pode
# ser null = suspensão instalada)
confirm() { curl -sS $CA -X POST "$BASE/agent/tasks/$1/confirmations" \
  -H "$AG" -H 'Content-Type: application/json' \
  -d "{\"generation\":$2,\"attempt\":1,\"status\":\"applied\",\"applied_revision\":$3}"; }
# postar execução-figura (fixture, não Restic)
exec_fig() { curl -sS $CA -X POST "$BASE/agent/executions" \
  -H "$AG" -H 'Content-Type: application/json' -d "$2"; }
```

## Cenários

### C1 — Tarefa não publicada
- **Prep:** tarefa `lab-c1` criada, nunca publicada.
- **Ação:** lista `/tasks` + detalhe.
- **Esperado:** operacional `unpublished`; sem selo de agenda ativa;
  explicação "nunca publicada".
- **Evidência:** captura da linha/detalhe. **Critério:** reprovado se
  anunciar agenda instalada.

### C2 — Manual aplicada
- **Prep:** `lab-c2` agenda `manual`; publicar rev 1; `confirm` gen 1
  rev 1. Nova publicação pendente (gen 2, sem confirmar).
- **Ação:** detalhe.
- **Esperado:** `pending_apply`; explicação de continuidade MANUAL
  ("configuração aplicada permanece manual"), sem "agenda ativa";
  `next_occurrence_utc` nulo.
- **Evidência:** JSON `GET /monitoring/tasks/{id}` + captura.
  **Critério:** qualquer menção a timer automático ativo reprova.

### C3 — Publicação pendente preserva configuração anterior
- **Prep:** `lab-c3` diária; publicar+confirmar gen 1; publicar rev
  nova sem confirmar (gen 2 pendente).
- **Ação:** detalhe após ocorrência vencida.
- **Esperado:** `pending_apply`; explicação "agenda automática anterior
  continua ativa"; cobrança segue pela agenda ANTERIOR.
- **Evidência:** JSON + captura. **Critério:** cobrança pausada ou
  agenda nova assumida antes da confirmação reprova.

### C4 — Agenda automática confirmada
- **Prep:** `lab-c4` horária/diária; publicar+confirmar.
- **Esperado:** `scheduled` + "agendamento confirmado como ativo";
  `next_occurrence_utc` coerente com fuso do contrato.
- **Evidência:** JSON + captura.

### C5 — Ocorrência dentro da tolerância
- **Prep:** `lab-c5` horária com minuto próximo; confirmar aplicação
  ANTES da ocorrência (âncora anterior a ela).
- **Ação:** consultar entre ocorrência e ocorrência+tolerância.
- **Esperado:** `pending`/`within-tolerance`, `expected_occurrence_utc`
  e `result_deadline_utc` preenchidos.
- **Evidência:** JSON com horário do servidor. **Critério:** exige
  espera real curta; janela perdida → repetir com próxima hora.

### C6 — Prazo vencido sem resultado → `missing` ≠ falha
- **Prep:** mesma tarefa de C5, sem execução postada.
- **Ação:** consultar APÓS o prazo (tolerância anotada).
- **Esperado:** `missing`/`overdue`, selo âmbar "sem resultado",
  texto "não prova falha do backup". NÃO vermelho, NÃO "agente
  offline".
- **Evidência:** JSON + captura do selo âmbar.

### C7 — Falha recebida ≠ ausência
- **Prep:** `lab-c7` horária; fixture `exec_fig` status `failed`,
  `started_at_utc` posterior à ocorrência.
- **Esperado:** `failed`, selo vermelho "falha", last_execution com o
  status recebido.
- **Evidência:** JSON + captura — contraste com C6 (âmbar vs vermelho).

### C8 — Resultado parcial
- **Prep:** `lab-c8`; fixture `partial`.
- **Esperado:** `partial`, selo âmbar "parcial" — NUNCA "em dia".
- **Critério:** verde ou "em dia" reprova.

### C9 — Sucesso e recebimento tardio
- **Prep:** `lab-c9`; fixture `success` iniciado após a ocorrência
  (chegará marcado como recebido após o prazo se postado tarde).
- **Esperado:** `ok` + `received_late=true` e sufixo `-late` no
  reason_code quando recebido após o prazo.
- **Evidência:** JSON com `received_late`.

### C10 — Suspensão pendente e confirmada
- **Prep:** `lab-c10` automática aplicada. `suspend` admin (gen 2).
- **Ação 1 (pendente):** explicação "continua ativa até confirmar";
  cobrança da agenda anterior segue.
- **Ação 2:** `confirm` gen 2 `applied_revision=null`.
- **Esperado 2:** `suspended` — "nenhuma ocorrência nova é esperada";
  sem `missing` após prazo.
- **Evidência:** JSON antes/depois + capturas.

### C11 — Republicação sobre suspensa, sem cobrança antes da retomada
- **Prep:** `lab-c10` suspensa. `suspend` de novo (gen 3 pendente) OU
  `publish` rev (gen 3 pendente) — testar as duas formas.
- **Esperado:** `pending_suspend`/`pending_apply` com explicação
  coerente ("já estava suspensa"/"permanece suspensa"); `no_history`,
  jamais `missing` durante suspensão.
- **Evidência:** JSON — reason NÃO pode conter "continua ativa".

### C12 — Retomada confirmada reancora
- **Prep:** `publish` em `lab-c10` (gen N) e `confirm` rev aplicada.
- **Esperado:** `scheduled`; cobrança só para ocorrências APÓS a nova
  âncora — intervalo suspenso nunca cobrado retroativamente.
- **Evidência:** JSON com `expected_occurrence_utc` posterior à âncora.

### C13 — Viewer lê; escrita recusada por AUTORIZAÇÃO (CSRF válido)
- **Prep:** login UI como `lab-viewer`.
- **Ação 1:** lista/detalhe carregam acompanhamento.
- **Ação 2 (prova de CSRF):** `curl` com cookie de sessão SEM
  `x-csrf-token` → 403 detail contendo "CSRF".
- **Ação 3 (prova RBAC):** mesmo POST com `x-csrf-token` igual ao
  cookie `macip_csrf` → 403 detail = "perfil sem permissão de escrita";
  geração da distribuição inalterada antes/depois.
- **Evidência:** respostas com detail distintos + GET da geração.
  **Critério:** recusa com detail CSRF NÃO comprova papel — exige a
  segunda resposta.

### C14 — Admin escreve pela mesma via (controle positivo)
- **Prep:** login UI como `lab-operador`.
- **Ação:** POST `suspend`/`publish` com sessão+CSRF válidos.
- **Esperado:** 200 e geração incrementada — prova de que a via de
  escrita funciona e a recusa de C13 foi papel.
- **Evidência:** resposta + geração antes/depois.

### C15 — Falha de carregamento ≠ falha de backup
- **Prep:** detalhe de tarefa aberto; derrubar `api` (`docker compose
  stop api`) simulando indisponibilidade da consulta.
- **Esperado:** seção "Acompanhamento" apresenta erro de CARGA
  (mensagem/retry), nunca selo de falha do backup; demais seções
  mantidas.
- **Evidência:** captura + log de erro. Restaure `api` após.
  **Critério:** qualquer selo de falha/ausência derivado de erro de
  rede reprova.

## Fechamento

Consolide: tabela cenário × critério, anexos (JSON, capturas, saídas),
valor de `MACIP_API_MONITOR_TOLERANCE_SECONDS` usado, SHA em execução
e divergências. Resultado global só é "aprovado" com TODOS os cenários
aprovados ou inconclusivos justificados. A homologação deste plano NÃO
aprova produção nem o piloto — é evidência para a decisão seguinte.
