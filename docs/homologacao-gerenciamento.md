# Homologação — distribuição gerenciada de tarefas

Roteiro para validar o incremento de distribuição num agente e tarefa
**descartáveis**. NÃO aplicar aos pilotos existentes — nenhuma tarefa
operacional deve migrar sem decisão explícita.

Escopo desta etapa: publicação de revisão, aplicação de
configuração+agenda e confirmação. Windows, execução sob demanda,
restauração pelo portal, distribuição de segredos e autoatualização do
binário ficam de fora.

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
