# Recuperação Linux pelo portal — incremento experimental

Esta funcionalidade parte da correção 11a589c, mas **não pertence ao pacote
0.3.0rc1 corrigido**. Não foi homologada em srvdev1 ou no piloto operacional.
Windows, seleção de arquivos, sobrescrita e recuperação entre agentes ficam fora.

## Contrato e migração

Migração PostgreSQL 7 adiciona recovery_operations sem remover tabelas/dados.
API/portal anteriores não devem operar esse esquema: código antigo rejeita
migrações futuras. Faça backup e ensaio antes da atualização; retorno exige
compatibilidade ou restauração/reconciliação de escritas, conforme runbook.
SQLite principal continua v7. A fila separada de recuperação usa esquema v3
(migração transacional v1→v2→v3 adiciona `deferred_until INTEGER NOT NULL DEFAULT 0`,
`evaluated_at INTEGER NOT NULL DEFAULT 0` e tabela `discovery_cursor`;
registros, claims, eventos e resultados existentes são preservados). A
migração é atômica: `BEGIN IMMEDIATE` serializa poll/worker, rollback desfaz
esquema, versão (`PRAGMA user_version` é transacional) e dados em falha, e a
exceção original é propagada — nunca substituída por erro de limpeza.
Bancos parcialmente migrados pela versão anterior são detectados e concluídos
sem perda de dados.

POST /api/v1/tasks/{task}/recovery registra intenção e retorna 202 e ID.
Tipos exclusivos: snapshots e restore. Não executa Restic em HTTP. Admin
solicita (sessão exige CSRF), viewer consulta. Cliente/agente/configuração
são derivados no servidor de uma revisão imutável, não de IDs do navegador.
Consulta exige revisão e destino. Restore exige query_id bem-sucedido e
snapshot_id completo dessa consulta; herda configuração/revisão dela mesmo
após edição da tarefa. Nenhuma senha/chave entra no contrato.

GET /tasks/{task}/recovery mostra estados e resultados. GET /agent/recovery
entrega apenas operações do agente autenticado, com paginação por cursor
(after/limit/next_cursor) ordenada por campos imutáveis (created_at, id),
para descoberta além de 100 operações. POST /agent/recovery/{id}/claim
grava claim_id exclusivo; POST /agent/recovery/{id}/result confirma resultado.
Identidades autenticadas e locks de linha protegem transições. Reenvio idêntico
é idempotente; outro claim ou regressão de estado terminal é recusado.
Estado: pending → running → succeeded | failed | interrupted. Sem expiração
automática de claims: agente indisponível não deve provocar outra restauração.

Cada solicitação do navegador tem UUID persistido na sessão. Cliques/reenvios
do mesmo query_id/snapshot_id retornam a mesma restauração inclusive depois
do término. Para outra recuperação deliberada, faça uma **nova consulta**.
O catálogo vem de snapshots Restic atuais com tag task:<UUID> no destino
selecionado; histórico de execuções não é usado como catálogo. Limite explícito
de 1000 pontos: excesso falha, não retorna catálogo truncado silenciosamente.

## Configuração local e execução independente

Use instalação de teste desta branch e os diretórios/lock registrados. Não
misture seu wheel com a candidata anterior. Provisione credenciais localmente;
o fluxo manual `macip_backup.manual` também carrega SSL_CERT_FILE.

Crie uma raiz de recuperação **existente**, absoluta, de propriedade do usuário
executor, modo 0700, sem links ou componentes relativos. Em recovery.json
privado, grave somente {"restore_root":"/caminho autorizado/recuperações"}.
O portal não fornece TARGET: cada operação cria raiz/<UUID-da-operação>.
Pasta existente, links e sobreposição com origens ou repositórios são recusados.

```bash
# Função manual do runbook; diretórios MACIP_* já exportados.
manual recovery-poll --central "$MACIP_CONFIG/central.json" \
  --queue "$MACIP_STATE/recovery.db"
manual recovery-work --central "$MACIP_CONFIG/central.json" \
  --queue "$MACIP_STATE/recovery.db" --policy "$MACIP_CONFIG/recovery.json" --limit 1
```

São comandos públicos de uma rodada, para processos independentes. Poll
consulta solicitações e envia resultados duráveis; work executa Restic. Uma
restauração longa não roda em pull-tasks/sync nem bloqueia o poll de resultados.
Para piloto automatizado configure dois serviços/timers próprios por decisão
do operador, usando esses comandos e EnvironmentFile; esta etapa não instala
timers novos automaticamente. Use nomes exclusivos e remova somente esses
recursos ao encerrar. Para validação inicial, mantenha dois processos de
supervisão separados chamando as rodadas; não execute work dentro de pull-tasks.

Worker é exclusivo por agente e usa maintenance SH e lock da tarefa; manutenção
EX ou backup concorrente recusa execução sem consumir a intenção. Outra tarefa
ou reconciliação pode progredir. A configuração fixada não depende de agenda
ativa. Suspender uma tarefa não impede recuperação e restore não toca timers.

## Seleção, adiamento e significado de --limit

`--limit` = número máximo de operações selecionadas por rodada para avaliação
(lock de tarefa + claim). Não é trabalho executado nem operações iniciadas:
uma operação pode ser adiada sem consumir o limite de efeitos. Operações
adiadas (tarefa ocupada, falha transitória, resposta ambígua) são excluídas
da seleção por `deferred_until`, então um item inelegível nunca monopoliza
rodadas seguidas — inclusive com `--limit 1`, a próxima rodada seleciona
outra operação elegível. Não há loop infinito nem varredura ilimitada: a rodada
processa apenas os itens selecionados e termina.

### Seleção justa

A seleção ordena por `(evaluated_at, rowid)`: operações nunca avaliadas
(`evaluated_at=0`) ou avaliadas há mais tempo são selecionadas primeiro,
independentemente do intervalo entre rodadas. Assim, mesmo que o worker
rode a cada 60s e o adiamento de 30s expire, a operação adiada não volta à
primeira posição se outra operação elegível ainda não foi avaliada. Após
liberar o lock da tarefa, a operação volta a executar quando for a menos
recentemente avaliada entre as elegíveis. Reinícios preservam
`evaluated_at` (persistido no SQLite).

Causa do bloqueio corrigido: `acquire_task_lock()` era chamado fora do
tratamento por operação, então uma tarefa ocupada abortava todo o lote; e um
claim recusado (409) deixava a operação na primeira posição elegível, impedindo
que outras operações recebessem claim. Correção: adiamento persistente por
`deferred_until`, seleção justa por `evaluated_at` e classificação local de
bloqueio.

### Política de adiamento/conflito

- **Tarefa temporariamente ocupada** (lock EX de outra execução): adia por
  `DEFERRED_SECONDS` (30s). Permanece `pending`, intenção preservada. Após
  liberar o lock e ser a menos recentemente avaliada, volta a executar.
- **Falha transitória de comunicação** (status 0/429/5xx): adia por
  `DEFERRED_SECONDS`. Operação continua recuperável, sem sucesso falso.
- **Resposta ambígua de claim** (200 com ack malformado/incompleto): adia por
  `DEFERRED_SECONDS` e reenvia o MESMO `claim_id` na próxima tentativa. Uma
  resposta ambígua não comprova conflito: a API pode ter registrado o claim
  corretamente antes de a resposta ser perdida ou alterada no transporte. A
  idempotência da API aceita o reenvio do mesmo claim. Nenhum efeito Restic é
  iniciado antes de um ack válido e compatível.
- **Conflito definitivo de claim** (409): bloqueio local permanente
  (`deferred_until = _BLOCKED`). Não toma o claim de outro executor, não
  repete restauração. Permanece diagnosticável no banco local.
- **Falha de autorização** (401/403): bloqueio local permanente.
- **Contrato inválido** (ConfigurationError na validação): bloqueio local
  permanente, sem tentar claim.
- **Manutenção global ativa**: recusa o worker inteiro (lock SH no início),
  sem efeito operacional, fila preservada.

Persistência e retomada: `deferred_until` e `evaluated_at` são gravados no
SQLite antes de continuar, então reiniciar o processo não desfaz a
classificação. Running órfão continua virando `interrupted/abandoned` e nunca
é retomado. Resultados concluídos continuam sendo reenviados pelo poll sem
repetir o efeito.

Diagnóstico de operações bloqueadas: consulte a fila local
(`SELECT id,state,deferred_until,evaluated_at,event FROM recovery_queue WHERE deferred_until>0`).
`deferred_until = _BLOCKED` (2^62) indica bloqueio definitivo; valor menor
indica adiamento temporário e a operação voltará a ser selecionada após
expirar. Não altere manualmente `deferred_until` para repetir restore cujo
efeito pode já ter ocorrido.

## Descoberta paginada

A API limita cada listagem a 100 operações. Para descobrir operações além
dessa janela, o poll usa paginação por cursor: `GET /api/v1/agent/recovery`
aceita `after=<id>` e `limit=1..100`, retornando `next_cursor`. A ordenação,
o cursor e o predicado de continuação representam a MESMA ordem total
imutável — `(created_at, id)`. Campos imutáveis garantem estabilidade quando
o estado muda entre páginas (pending→running não move a operação na ordem,
sem omissão nem duplicação). O agente persiste o cursor no SQLite
(`discovery_cursor`) e continua a descoberta entre rodadas com orçamento de
`DISCOVERY_BUDGET` (5) requisições por poll. Ao concluir uma passagem
(`next_cursor=None`), o cursor retorna ao início. Deduplicação local via
`INSERT OR IGNORE` preserva operações conhecidas. Isolamento pelo agente
autenticado. Cursor de outro agente ou inexistente = 404.

## Efeitos, interrupções e resultados

A fila grava intenção, claim e running antes do efeito. Restic usa credenciais
locais, timeouts por operação e término do grupo já implementados. Nunca init.
Restore usa --verify do motor; resultado só é succeeded após retorno confirmado.
O resultado terminal é persistido antes do envio e reenviado sem repetir efeito.
Sem rede: retenha fila e aguarde nova rodada. Rejeição definitiva é marcada
blocked localmente (sent=-1), preservando evento para diagnóstico; não altere
manualmente estado para repetir restore. Corrija vínculo/contrato e investigue
o evento antes de qualquer procedimento explícito de reenfileiramento.

SIGTERM/SIGINT durante Restic resulta em interrupted. Após queda/reboot, running
local vira interrupted/abandoned e nunca é retomado. Conteúdo parcial permanece
na pasta exclusiva e não é considerado válido. Inspecione-o, confirme processos
encerrados e solicite nova consulta/recuperação; não reutilize a pasta parcial.
SIGKILL não permite limpeza graciosa: antes de outra operação, o responsável
deve conferir descendentes órfãos. Em serviço, use KillMode=control-group.
Não apague fila/banco para contornar idempotência: perda da fila exige diagnóstico
de claims antes de qualquer nova execução.

## Homologação descartável e limites

Valide admin/viewer/CSRF, isolamento de agente, consulta vazia/falha, snapshot
removido, manutenção, duplicidade, perda de resposta e callbacks antigos.
Na prova real: API+PG17+Chrome, backup local, consulta pelo portal, restauração
por worker, confirmação, comparação de conteúdo e exclusão preservada. Ver
portal/e2e/recovery.spec.ts; dados sintéticos em diretórios temporários.

Antes de piloto operacional, ensaie instalação dos dois serviços, TLS real,
parada durante volume representativo, reboot, custódia da fila e recuperação
de claims abandonados. A confirmação histórica dos timers antigos como
inactive/disabled permanece válida; nenhum desses timers deve ser reativado.

## Evidências de desenvolvimento — 04/11/2026

- API: 180 testes aprovados em PG17 real, 234,52 s; incluem seis novos testes
  de recuperação, autorização, CSRF, configuração fixada e idempotência.
- Portal: 88 testes aprovados, incluindo dez de recuperação/rotas atrasadas;
  typecheck e build passaram. Estados de servidor desses testes são simulados.
- Worker/CLI/identidade/adaptador: gate de 34 testes, incluindo nove da fila
  com SQLite real e API/Restic simulados. Poll progride enquanto restore está
  bloqueado no teste controlado; falhas não exigem espera longa.
- Playwright: um teste integrado aprovado (23 s), Chrome + API + PG17 +
  Restic local reais. Comandos públicos recovery-poll/work foram executados
  como subprocessos. Pasta nova, conteúdo e exclusão comparados. A suspensão
  verificada é estado real da API, não desativação de timer systemd real.
- Warnings: depreciação Starlette/httpx, avisos React Router e cores de terminal;
  não houve skips nesses gates. Não há nova homologação SFTP/SMB/S3/Windows.

Comandos: na API, `PYTHONPATH=src ../.venv-api/bin/python -m pytest tests -q`
com MACIP_API_TEST_DATABASE_URL descartável; no portal, `npm test`,
`npm run typecheck`, `npm run build` e `npx playwright test e2e/recovery.spec.ts`.
O E2E exige essa URL e PYTHONPATH absoluto de api/src. Nunca use banco principal.
