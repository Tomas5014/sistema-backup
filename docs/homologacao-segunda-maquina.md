# Homologação na segunda máquina — runbook executável

Status de cada item do ciclo operacional. **Comprovado em sessão real**
(systemd --user verdadeiro nesta máquina, não laboratório isolado):

| Item | Status | Evidência |
|---|---|---|
| 1. Wheel fora do checkout | ✅ real | `install.sh` em `/tmp/macip-homolog` |
| 2. Backup/check/restore | ✅ real | `check --read-data` ok, `diff -r` idêntico |
| 3. Timer após reboot sem terminal | ⏳ pendente | exige reboot real + `enable-linger` |
| 4. Update → service executa código novo | ✅ real | unit de 0.2.0 rodou 0.2.1 via systemd |
| 5. Piloto 7 dias | ⏳ pendente | aguardando máquina de destino |

## Homologado nesta sessão (systemd real, máquina local)

```bash
MACIP_HOME=/tmp/macip-homolog/app MACIP_CONFIG=/tmp/macip-homolog/config \
MACIP_STATE=/tmp/macip-homolog/state MACIP_CACHE=/tmp/macip-homolog/cache \
MACIP_BIN=/tmp/macip-homolog/bin \
bash deploy/install.sh dist/macip_backup_poc-0.2.0-py3-none-any.whl --commit f4b624c
# → releases/0.2.0-079bd01c4955 + launcher + install.json

printf 'pw' | keyctl padd user macip.MACIP_HOMOLOG_PW @u   # segredo por stdin

macip-backup agent tarefa.json --store state/agent.db    # success
macip-backup schedule tarefa.json --store state/agent.db --calendar '*:0/30'
# unit real em ~/.config/systemd/user com ExecStart=launcher + marker install=

systemctl --user daemon-reload
systemctl --user start macip-backup-agent-task-homolog.service
# Result=success — execução 2 gravada

bash deploy/install.sh macip_backup_poc-0.2.1-py3-none-any.whl --commit local-021
macip-backup --version        # → 0.2.1
systemctl --user start macip-backup-agent-task-homolog.service
# Result=success — execução 3 rodou o CÓDIGO NOVO

macip-backup check tarefa.json local --read-data          # success
macip-backup restore tarefa.json local <snap> /tmp/...    # diff -r idêntico
macip-backup unschedule task-homolog                      # units removidas
```

`loginctl show-user $USER -p Linger` → **`Linger=no`** — necessário para
o item 3 (administrativo; não alterado automaticamente).

## Comandos para a segunda máquina (quando disponibilizada)

```bash
# copiar: dist/macip_backup_poc-0.2.0-py3-none-any.whl + deploy/ + tarefa
scp dist/macip_backup_poc-*.whl deploy/install.sh deploy/uninstall.sh \
    user@maquina2:/tmp/

ssh user@maquina2
bash /tmp/install.sh /tmp/macip_backup_poc-0.2.0-py3-none-any.whl --commit f4b624c
printf '<segredo>' | keyctl padd user macip.MACIP_RESTIC_PASSWORD @u
# ou ~/.config/macip-backup/agent.env (0600)

macip-backup agent tarefa.json --store ~/.local/state/macip-backup/agent.db
macip-backup schedule tarefa.json --store ~/.local/state/macip-backup/agent.db \
  --calendar 'Mon..Fri 02:00' --enable

# administrativo (uma vez, fora do instalador):
# sudo loginctl enable-linger <usuario>   → verifica: loginctl show-user $USER -p Linger

# reboot → sem abrir terminal:
systemctl --user status macip-backup-agent-task-homolog.service
macip-backup history --store ~/.local/state/macip-backup/agent.db
```

## Piloto de 7 dias (segue o checklist de docs/piloto-operacao.md §7)

Não iniciado — aguarda a máquina de destino. Não declarar concluído.

## Correção 0.2.1 — EnvironmentFile (defeito encontrado na VM)

Bug real em 0.2.0: `EnvironmentFile=-"/path/agent.env"` era ignorado
pelo systemd (`path is not absolute`) → serviço sem credencial.
Em 0.2.1 a diretiva sai literal: `EnvironmentFile=-/path` — incluindo
caminhos com espaço (a linha inteira é o filename; aspas não são
aceitas nessa diretiva).

### Atualizar a VM (a unit já corrigida manualmente segue funcionando)

```bash
scp dist/macip_backup_poc-0.2.1-py3-none-any.whl deploy/install.sh \
    estagio01@<vm>:/tmp/

# na VM — sem execução ativa:
sha256sum /tmp/macip_backup_poc-0.2.1-py3-none-any.whl
# conferir: ac7ba45455453bf2312762ccf613ac983ec898e55e8cab5a3b2f40134a1a2136
bash /tmp/install.sh /tmp/macip_backup_poc-0.2.1-py3-none-any.whl --commit <sha>
macip-backup --version   # → 0.2.1

# opcional: regenerar units já agendadas com a sintaxe corrigida
macip-backup schedule tarefa.json --store ~/.local/state/macip-backup/agent.db \
  --calendar '<mesmo-calendário>'
```

A unit corrigida manualmente na VM permanece válida e reconhecida
pelo marcador `install=` — a atualização não a toca.
