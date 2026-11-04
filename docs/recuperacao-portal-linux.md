# Recuperação Linux pelo portal — incremento experimental

Esta funcionalidade parte da correção 11a589c, mas **não pertence ao pacote
0.3.0rc1 corrigido**. Não foi homologada em srvdev1 ou no piloto operacional.
Windows, seleção de arquivos, sobrescrita e recuperação entre agentes ficam fora.

## Contrato e migração

Migração PostgreSQL 7 adiciona recovery_operations sem remover tabelas/dados.
API/portal anteriores não devem operar esse esquema: código antigo rejeita
migrações futuras. Faça backup e ensaio antes da atualização; retorno exige
compatibilidade ou restauração/reconciliação de escritas, conforme runbook.
SQLite principal continua v7. A fila separada de recuperação usa esquema v2
(migração transacional v1→v2 adiciona `deferred_until INTEGER NOT NULL DEFAULT 0`;
registros e eventos existentes são preservados).

POST /api/v1/tasks/{task}/recovery registra intenção e retorna 202 e ID.
Tipos exclusivos: snapshots e restore. Não executa Restic em HTTP. Admin
solicita (sessão exige CSRF), viewer consulta. Cliente/agente/configuração
são derivados no servidor de uma revisão imutável, não de IDs do navegador.
Consulta exige revisão e destino. Restore exige query_id bem-sucedido e
snapshot_id completo dessa consulta; herda configuração/revisão dela mesmo
após edição da tarefa. Nenhuma senha/chave entra no contrato.

GET /tasks/{task}/recovery mostra estados e resultados. GET /agent/recovery
entrega apenas operações do agente autenticado, listando pendentes antes de
running para que um claim ocupado não monopolize a janela de descoberta e
esconda operações novas (limite 100). POST /agent/recovery/{id}/claim
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
adiadas (tarefa ocupada, falha transitória) são excluídas da seleção por
`deferred_until`, então um item inelegível nunca monopoliza rodadas seguidas
— inclusive com `--limit 1`, a próxima rodada seleciona outra operação
elegível. Não há loop infinito nem varredura ilimitada: a rodada processa
apenas os itens selecionados e termina.

Causa do bloqueio corrigido: `acquire_task_lock()` era chamado fora do
tratamento por operação, então uma tarefa ocupada abortava todo o lote; e um
claim recusado (409) deixava a operação na primeira posição elegível, impedindo
que outras operações recebessem claim. Correção: adiamento persistente por
`deferred_until` e classificação local de bloqueio.

Política de adiamento/conflito:
- **Tarefa temporariamente ocupada** (lock EX de outra execução): adia por
  `DEFERRED_SECONDS` (30s). Permanece `pending`, intenção preservada. Após
  liberar o lock e expirar o adiamento, volta a executar.
- **Falha transitória de comunicação** (status 0/429/5xx): adia por
  `DEFERRED_SECONDS`. Operação continua recuperável, sem sucesso falso.
- **Conflito definitivo de claim** (409): bloqueio local permanente
  (`deferred_until = _BLOCKED`). Não toma o claim de outro executor, não
  repete restauração. Permanece diagnosticável no banco local.
- **Resposta ambígua/incompatível** (ack malformado, 4xx restante): bloqueio
  local permanente. Nenhum efeito Restic é iniciado.
- **Contrato inválido** (ConfigurationError na validação): bloqueio local
  permanente, sem tentar claim.
- **Manutenção global ativa**: recusa o worker inteiro (lock SH no início),
  sem efeito operacional, fila preservada.

Persistência e retomada: `deferred_until` é gravado no SQLite antes de
continuar, então reiniciar o processo não desfaz a classificação. Running
órfão continua virando `interrupted/abandoned` e nunca é retomado. Resultados
concluídos continuam sendo reenviados pelo poll sem repetir o efeito.

Diagnóstico de operações bloqueadas: consulte a fila local
(`SELECT id,state,deferred_until,event FROM recovery_queue WHERE deferred_until>0`).
`deferred_until = _BLOCKED` (2^62) indica bloqueio definitivo; valor menor
indica adiamento temporário e a operação voltará a ser selecionada após
expirar. Não altere manualmente `deferred_until` para repetir restore cujo
efeito pode já ter ocorrido.

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
