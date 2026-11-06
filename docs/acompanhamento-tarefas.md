# Acompanhamento de tarefas (monitoramento)

Primeira versão de leitura operacional: identifica **backup em dia**,
**próxima execução aguardada**, **resultado com falha ou parcial** e
**ausência de resultado recebido dentro do prazo esperado**.

Não implementa notificações (e-mail/WhatsApp) nem declara "agente
offline" — os dados persistidos não sustentam essa conclusão.

## O que NÃO é

- **Ausência de resultado ≠ falha de backup.** O agente pode ter
  executado e o resultado pode estar pendente na outbox local. O estado
  `missing` diz "não recebemos nada no prazo" — nunca "o backup falhou".
- **Ausência de resultado ≠ agente offline.** `agent_last_seen_at` é
  exibido como sinal de comunicação informativo, sem veredito.
- **O contrato não distingue disparo agendado de manual.** Qualquer
  resultado recebido satisfaz a ocorrência esperada; a UI não afirma
  que um resultado veio do timer.

## Dimensão 1 — situação operacional

Derivada SOMENTE da distribuição confirmada pelo agente — o POST
administrativo bem-sucedido nunca anuncia efeito instalado:

| Estado | Significado |
|---|---|
| `unpublished` | Nunca publicada (`desired_action=none`) |
| `pending_apply` | Publicada; aplicação ainda não confirmada (ou a tentativa vigente falhou) |
| `manual` | Aplicada; a agenda confirmada é `manual` |
| `scheduled` | Agenda automática confirmada como instalada |
| `pending_suspend` | Suspensão pedida; a agenda ANTERIOR continua ativa |
| `suspended` | Suspensão confirmada — nenhuma ocorrência nova é esperada |

## Dimensão 2 — situação dos resultados

| Estado | Significado |
|---|---|
| `no_history` | Nenhum resultado recebido (ou monitoramento informativo: manual/suspenso/não publicado sem execuções) |
| `awaiting` | Agenda ativa; a próxima ocorrência ainda não venceu |
| `pending` | Ocorrência vencida dentro da tolerância de recebimento |
| `ok` | Resultado `success` recebido para a ocorrência |
| `partial` | Resultado `partial` — **não** conta como "em dia" |
| `failed` | Resultado `failed`/`interrupted` recebido |
| `missing` | Vencida e **sem resultado recebido** após o prazo |

`reason_code` (estável) e `detail` (explicação) acompanham cada estado;
a lógica do frontend usa os códigos, não o texto livre.

## Regras de cálculo

- **Marco inicial (âncora):** `task_distributions.last_applied_at` —
  timestamp atribuído pelo SERVIDOR ao receber a confirmação de
  aplicação. Ocorrências anteriores à âncora nunca são cobradas:
  tarefa recém-publicada/aplicada não herda histórico, e mudança de
  agenda/revisão reancora sem reinterpretar o passado pela agenda nova.
- **Agenda avaliada:** a da revisão `last_applied_revision` — a que o
  agente confirmou ter instalado (não a revisão mais recente do cadastro).
- **Ocorrências:** contrato `schedule` (`manual`/`hourly`/`daily`/
  `weekly`) interpretado no fuso IANA do contrato; comparações sempre
  em UTC. Horário inexistente no salto de verão é pulado (como
  `OnCalendar` do systemd); horário ambíguo usa a primeira ocorrência
  (`fold=0`). Nada usa o relógio do navegador nem do agente.
- **Qual execução satisfaz a ocorrência O:** `started_at_utc >= O`.
  Execução iniciada antes da janela não conta.
- **Várias execuções na mesma janela:** vence a de maior
  `(started_at_utc, received_at)` — um sucesso posterior corrige um
  estado de falha; um resultado antigo recebido fora de ordem não
  regride um estado já satisfeito.
- **Recebimento tardio:** resultado que satisfaz a ocorrência mas chegou
  depois do prazo aparece como `ok`/`partial`/`failed` com
  `received_late=true` e sufixo `-late` no `reason_code`.
- **Tolerância:** `MACIP_API_MONITOR_TOLERANCE_SECONDS` (padrão 1800 s,
  válido 1..86400; valor inválido falha explicitamente). O prazo cobre
  a expectativa de RECEBIMENTO (início + execução + envio), não só o
  início do backup — é critério de ausência, não SLA.
- **O que os dados NÃO separam:** atraso de início, tempo de execução,
  atraso de envio e falta de comunicação — todos caem em `missing`.
- **Arquivada:** a tarefa arquivada permanece calculável; arquivar não
  desliga agenda já instalada no agente (semântica do produto).

## API

Leitura pura, sem efeitos colaterais (`GET` apenas — nada publica,
suspende ou executa):

- `GET /api/v1/monitoring/tasks?client_id=&agent_id=&archived=&limit=&offset=`
- `GET /api/v1/monitoring/tasks/{task_id}`

Autorização: `require_viewer` (Bearer de automação ou qualquer sessão de
operador ativa). Token de **agente** não lê acompanhamento (403).

Consultas em lote (sem N+1): distribuições, revisões aplicadas,
agentes e execuções são buscados uma vez por página; execuções por
tarefa limitadas às 64 mais recentes (a janela que decide o estado está
sempre no topo); `last_success` usa janela própria `status='success'`.

Sem migração: todos os sinais já existiam (`task_distributions`,
`task_confirmations`, `task_revisions`, `executions`, `agents.last_seen_at`).

## Portal

- Lista (`/tasks`): selo por linha + informação temporal essencial
  (ocorrência esperada + prazo quando `missing`; próxima ocorrência
  quando aguardando). O selo resume a página atual — não é contagem
  global. Tons: `missing` âmbar (não o vermelho de falha), `partial`
  âmbar, estados sem dados neutros — nada verde sem conclusão.
- Detalhe (`/tasks/:id`): seção "Acompanhamento" com situação
  operacional, resultado, última execução, último sucesso, ocorrência
  esperada + prazo, próxima ocorrência, último contato do agente
  (informativo), instante do cálculo e tolerância. Falha da consulta
  aparece como erro de carga da seção — nunca como falha do backup.

## Validação em ambiente descartável

```bash
# PostgreSQL descartável (obrigatório para a camada de integração)
docker run --rm -d --name pgtest-mon \
  -e POSTGRES_PASSWORD=test -e POSTGRES_DB=macip_test \
  -p 127.0.0.1:15432:5432 postgres:17-alpine

cd api
MACIP_API_TEST_DATABASE_URL='postgresql+psycopg://postgres:test@127.0.0.1:15432/macip_test' \
  PYTHONPATH=src:. python -m pytest tests/test_monitoring.py -q

cd ../portal && npx vitest run tests/monitoring.test.tsx
```

Os testes controlam o relógio — unidade via `now=` injetável e
integração via `app.datetime` congelado; nenhum teste espera 30 minutos
nem toca systemd/timers.
