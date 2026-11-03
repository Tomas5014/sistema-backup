# Homologação — distribuição gerenciada de tarefas

Roteiro para validar o incremento de distribuição num agente e tarefa
**descartáveis**. NÃO aplicar aos pilotos existentes — nenhuma tarefa
operacional deve migrar sem decisão explícita.

Escopo desta etapa: publicação de revisão, aplicação de
configuração+agenda e confirmação. Windows, execução sob demanda,
restauração pelo portal, distribuição de segredos e autoatualização do
binário ficam de fora.

## Evidência de laboratório — 03/11/2026

Esta seção registra evidências fornecidas pelo operador. Elas não foram
reexecutadas nesta revisão. O ambiente usou central e agente no commit
`469b356`, com componentes isolados do piloto 0.2.1:

- central na VM `srvdev1`, HTTPS em `127.0.0.1:8444`, acessada pelo PC
  pelo túnel `https://localhost:18444`;
- agente Linux no PC de desenvolvimento, com configuração, estado,
  cache, locks e repositório próprios;
- agente `381143c2-d36e-4933-82cb-edafe49a0070`, tarefa
  `d96b57f2-740a-46ec-83e5-fb47b3fb3097` e destino local
  `local-homologacao`.

### Ciclo observado pelo operador

1. `pull-tasks --no-activate` preparou a geração 1. Configuração e
   units ficaram em staging. Nenhuma unit operacional, repositório ou
   confirmação de aplicação foi criada nessa preparação.
2. A revisão 1 foi aplicada e confirmada. O timer disparou o backup às
   `15:04:01 BRT`. A execução do agente foi
   `e2bfa774-0f65-4a33-a7a1-1d85afd6c2a9`; o registro central exibido
   no portal foi `f8fbddab-5e63-4dd8-9d73-3eb4f0eb9479`. Resultado:
   sucesso, 2 arquivos, outbox `sent`, sem erro. O portal recebeu o
   resultado às `15:06:31 BRT`.
3. O snapshot criado foi
   `955c28da45e3ebbb8ec575133958db300f9ba3289ba277d84b31d03866c60686`.
   `check --read-data` passou. A restauração para diretório novo e a
   comparação por `diff` passaram; `ignorar.tmp` permaneceu excluído.
4. A revisão 2 foi publicada como geração 2. O timer de consulta aplicou
   a revisão às `15:19:15 BRT`, sem execução manual de `pull-tasks`.
   A confirmação ficou `sent`; após recarregar, o portal mostrou revisão
   cadastrada, publicada e aplicada 2, com geração 2.
5. A suspensão foi publicada como geração 3. A confirmação foi enviada
   às `15:23:12 BRT`. O timer de backup ficou
   `UnitFileState=disabled` e `ActiveState=inactive`; o snapshot foi
   preservado. Nova restauração após a suspensão passou, com conteúdo
   igual e exclusão preservada. O timer permaneceu desabilitado e
   inativo após a recuperação.

### Ressalvas e alcance

- No ciclo original, o agendamento da consulta foi cadastrado pela
  função interna `schedule_pull()`. O comando público `schedule-pull`
  falhava com `UnboundLocalError`; portanto, ele não foi aprovado por
  essa prova original.
- A revisão posterior corrigiu `schedule-pull` e `unschedule-pull` e
  adicionou regressões da entrada pública. Isso comprova o despacho do
  CLI em ambiente simulado e isolado; não reexecuta o ciclo acima. O
  gate pertinente desta revisão aprovou 76 de 76 testes de CLI, geração
  de units e distribuição.
- O operador confirmou posteriormente a parada dos timers de consulta e
  sincronização; detalhes no encerramento abaixo.
- A prova usou somente destino local em laboratório. Ela não homologa
  produção, SMB, S3 ou Windows.

## Validação complementar do CLI — 03/11/2026

Esta validação avaliou o commit `ef3ff75` em Linux descartável. Ela não
reexecutou a homologação histórica do commit `469b356` nem acessou a VM
`srvdev1`.

### Investigação do cancelamento

O código `src/macip_backup/restic.py` da base `469b356` e do candidato
`ef3ff75` apresentou o mesmo SHA-256:
`7aa54545f5ec60f765d4434efe08517a2af38a878a6e4844036585193a9cc224`.
Dois worktrees isolados em `/tmp` confirmaram a comparação e foram
removidos ao final.

- Execução inicial de `AdapterCancellationTests`: 3/3 aprovados em
  5,477 s. O encerramento limpo medido separadamente levou 0,076 s; o
  caso que ignora SIGTERM levou 5,451 s e escalou para SIGKILL.
- Base e candidato: três rodadas completas em cada checkout, 18/18
  aprovadas. Cada rodada levou entre 5,424 s e 5,530 s dentro do runner.
- Prova fortalecida: 100 repetições do encerramento limpo e 10 do
  descendente resistente, 110/110 aprovadas em 64,340 s.
- Regressões próximas do adaptador Restic: 9/9 aprovadas em 5,149 s.
- Gate final de CLI, units simuladas, cancelamento e distribuição:
  79/79 aprovados em 39,226 s.

As duas falhas relatadas não foram reproduzidas como defeito funcional.
A prova antiga iniciava a medição assim que o objeto `Popen` era
publicado, sem confirmar que o binário falso já havia iniciado. Também
usava `pgrep -f`, que não preserva a identidade do processo diante de
reuso de PID e diferenças de namespace, nem expressa a regra adotada
para estados zumbi. O teste passou a:

- aguardar um arquivo de prontidão antes do cancelamento;
- registrar `PID:starttime` do descendente em `/proc`;
- considerar `Z`/`X` encerrado e detectar reuso de PID pelo `starttime`;
- limpar processos registrados no `tearDown`, inclusive após falha.

Nenhuma linha do adaptador Restic foi alterada. A asserção de duração
`< 10 s` e a exigência de encerramento do descendente foram preservadas.

### Prova com systemd --user real

O `systemctl --user` estava disponível, em estado `degraded`, mas apto a
carregar e operar o timer descartável. A entrada pública foi usada em
todos os passos:

```bash
python3 -m macip_backup.cli schedule-pull --store STORE \
  --central CENTRAL --calendar '2099-12-31 23:59:59'
python3 -m macip_backup.cli schedule-pull --store STORE \
  --central CENTRAL --calendar '2099-12-31 23:59:59' --enable
python3 -m macip_backup.cli unschedule-pull --store STORE --central CENTRAL
```

Resultado:

- identidade exclusiva `macip-backup-pull-79b2a94fe0cb4308`;
- antes da prova, service e timer não existiam;
- sem `--enable`: JSON válido, dois arquivos criados,
  `is-enabled=disabled` e `is-active=inactive`;
- com `--enable`: JSON válido, `enabled` e `active`; próximo disparo em
  `Thu 2099-12-31 23:59:59 -03`, sem consulta executada;
- primeira remoção: somente os dois caminhos dessa identidade em
  `removed`; segunda remoção: `removed: []`;
- ao final, nenhuma unit carregada, arquivo ou processo dessa identidade.

Configuração, estado, cache, locks e binários ficaram sob
`/tmp/macip-pull-systemd-Gp6AJ6`. `central.json` era sintético, apontava
para `127.0.0.1:65534` e continha somente o nome de uma variável de token.
Não houve comunicação com central. O trap executou remoção idempotente;
as units, os dois worktrees e os diretórios temporários foram removidos.

Esta prova aprova os comandos públicos corrigidos no Linux testado. Não
aprova produção, Windows, SMB ou S3.

Limitação da revisão independente: o sandbox apresentava PIDs diferentes
entre Python e `/proc`. As falhas originais não foram reproduzidas no
ambiente da validação complementar. A prontidão e identidade fortalecem a
prova, mas não garantem compatibilidade com qualquer combinação de
namespaces. O adaptador não foi alterado para acomodar esse sandbox.

## Encerramento dos timers do laboratório — confirmação do operador

O operador confirmou ambos os timers com `ActiveState=inactive` e
`UnitFileState=disabled`:

- `macip-backup-pull-a985252fb52df2de.timer`;
- `macip-backup-sync-67ee9fbc7ff2b5da.timer`.

Essa pendência específica está encerrada por evidência fornecida, sem nova
consulta ao laboratório nesta consolidação. O backup gerenciado já estava
suspenso pela geração 3. Snapshot e recuperação após suspensão permanecem
confirmados. Isso não significa remoção do laboratório, SQLite, repositório
ou arquivos recuperados.

## Pré-requisitos

- Central saudável com migrações 5 e 6 aplicadas (`task_distributions`
  com `last_applied_*`/`confirmed_attempt`, `task_confirmations` com
  `attempt`).
- Máquina Linux de teste com o pacote do agente instalado (ou
  `PYTHONPATH=src`), systemd --user ativo, `systemd-analyze`, restic
  e `keyctl`.
- Usuário dedicado de teste (units `--user`, sem root).

## Preparação do agente descartável

1. Criar cliente + agente + credencial no portal (aba do agente —
   cadastro já existente). Guardar o **UUID central do agente** e o
   token exibido uma única vez.
2. Na máquina de teste:

```bash
mkdir -p ~/.config/macip-backup
cat > ~/.config/macip-backup/central.json <<'EOF'
{
  "url": "https://central.exemplo.local",
  "agent_id": "UUID-CENTRAL-DO-AGENTE",
  "token_environment": "MACIP_API_TOKEN"
}
EOF
chmod 600 ~/.config/macip-backup/central.json

# token entra pela linha de EnvironmentFile das units — ou por keyring
printf 'MACIP_API_TOKEN=token-recebido\n' > ~/.config/macip-backup/agent.env
chmod 600 ~/.config/macip-backup/agent.env
# alternativa sem arquivo: keyctl padd user macip.MACIP_API_TOKEN @u
```

3. Criar a tarefa de teste na central (portal → tarefa nova): origem
   e repositório **descartáveis** (`/tmp/origem`, `/tmp/repositorio`),
   `password_environment` apontando uma variável provisionada no
   agente (`agent.env` ou keyring), agenda `daily`.

## Validação

| Passo | Comando / ação | Esperado |
|---|---|---|
| 1 | `macip-backup pull-check --central ~/.config/macip-backup/central.json --store ~/.local/state/macip-backup/agent.db` | `desired: []`, `pending` — **sem** efeitos (sem `managed/`, sem units) |
| 2 | Publicar rev 1 pelo portal | Portal mostra "aguardando aplicação" — NÃO anuncia aplicado |
| 3 | `macip-backup pull-tasks --central … --store … --no-activate` | `prepared`: `config-1.json` + units escritas, `current.json` AUSENTE, journal com `prepared`, **nenhuma** confirmação na central |
| 4 | `macip-backup pull-tasks --central … --store …` (mesma geração) | `applied`; `current.json` publicado, journal termina em `applied`, confirmação `applied` tentativa 1 na central |
| 5 | `systemctl --user list-timers` | timer gerenciado habilitado com `OnCalendar` da agenda e fuso IANA |
| 6 | Recarregar a página da tarefa | "revisão aplicada" = 1, geração 1, tentativa 1, status `applied`; "último sucesso aplicado" = rev 1 |
| 7 | Editar → rev 2 (agenda diferente) → Publicar | `generation: 2`; após `pull-tasks`, `current.json` novo, timer com horário novo; `gen-1.json`/`config-1.json` preservados |
| 8 | Backup pelo timer (aguardar o `OnCalendar` ou `systemctl --user start macip-backup-managed-<id>.service`) | backup Restic real; resultado sai pela outbox → `sync` → execução visível na central |
| 9 | Suspender agendamento no portal | próximo `pull-tasks` → `suspended`; `is-enabled` = `disabled`; `current.json` e snapshots **preservados** |
| 10 | Derrubar a central (`systemctl --user stop …` ou firewall) e rodar `pull-tasks` | `communication_error` no resumo; units/config intocadas; confirmações pendentes ficam em retry com backoff — **não** bloqueiam |
| 11 | Revogar a credencial no portal | `pull-tasks` → falha de autenticação (`403`); confirmação nova fica `blocked` até credencial reemitida + `pull-unblock` |

## Falhas e recuperação

| Cenário | Comportamento esperado |
|---|---|
| Indisponibilidade longa da central | Confirmações transitórias (rede/5xx) continuam em retry com backoff capado em 1h — sem intervenção, entregam quando a API volta |
| Credencial revogada | `pull-tasks` aborta com diagnóstico; confirmações novas → `blocked`; **recuperação**: emitir credencial nova, provisionar no agente, `pull-unblock` |
| Falha de efeito na aplicação (ex.: systemd fora) | rollback: units + `current.json` + timer voltam à última geração aplicada (ou são removidos se era a primeira); journal `rolled_back`; tentativa seguinte recomeça |
| Pré-requisito local ausente (origem, montagem) | `failed_validation` → confirmação `failed` tentativa 1; corrigido o pré-requisito, a MESMA geração aplica na tentativa 2 e a central mostra evolução |
| Troca de central (`--rebind`) | só com flag explícita: timers do vínculo anterior são desativados, `active.json` aponta o novo vínculo, journals do vínculo antigo ficam preservados sob `managed/<bond-antigo>/` — confirmações antigas nunca migram de vínculo |
| `pull-tasks` sem `--no-activate` e sem `systemctl --user` funcional | falha transitória + rollback — a preparação de arquivos não é confundida com aplicação |

## Desativação apenas do gerenciamento

```bash
macip-backup unschedule-pull --central ~/.config/macip-backup/central.json \
  --store ~/.local/state/macip-backup/agent.db
# para cada tarefa gerenciada: suspender pela central + pull-tasks, ou
systemctl --user disable --now macip-backup-managed-<id>.timer
rm ~/.config/systemd/user/macip-backup-managed-<id>.{service,timer}
rm -rf ~/.local/state/macip-backup/managed   # vínculo + journals
rm ~/.config/macip-backup/central.json
```

As tarefas locais e o histórico de execuções (`agent.db`) **não** são
removidos — só o vínculo central desaparece.
