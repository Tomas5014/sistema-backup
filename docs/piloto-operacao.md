# Operação do agente Linux — instalação, atualização, piloto de 7 dias

Guia executável para instalar o agente numa máquina Linux dedicada —
sem checkout de desenvolvimento e sem terminal aberto.

> **Status**: instalação/atualização/desinstalação homologadas em
> laboratório (`tests/test_install.py`). O piloto de 7 dias e a
> validação em segunda máquina real estão **pendentes** — esta entrega
> só prepara artefatos e comandos.

## 1. Artefato

```bash
python3 -c "from setuptools.build_meta import build_wheel; build_wheel('dist')"
# → dist/macip_backup_poc-<versão>-py3-none-any.whl
# (requer setuptools>=68; python3-pip NÃO é necessário para construir)
```

Copie `dist/macip_backup_poc-*.whl` e `deploy/install.sh` para a máquina.

## 2. Instalação

```bash
bash install.sh macip_backup_poc-<versão>-py3-none-any.whl --commit <sha>
```

Layout criado (por usuário, sem root):

| Item | Caminho |
|---|---|
| Programa | `~/.local/share/macip-backup/releases/<versão>/` + `current` |
| Binário | `~/.local/bin/macip-backup` |
| Config | `~/.config/macip-backup/` (0700) |
| Credenciais | `~/.config/macip-backup/agent.env` (0600) |
| SQLite | `~/.local/state/macip-backup/agent.db` |
| Logs | `~/.local/state/macip-backup/logs/` |
| Locks | `~/.local/state/macip-backup/locks/<task_id>.lock` |
| Cache | `~/.cache/macip-backup/` |
| Units | `~/.config/systemd/user/macip-backup-<task>.{service,timer}` |

Requisitos **básicos**: python3 ≥ 3.11 + `venv`, restic, ssh, flock.
Opcionais por backend: `keyctl` (keyring), `systemd-analyze`/`systemctl
--user` (agenda), `mount.cifs` (destino SMB). Docker **não** é requisito.

Reexecutar o instalador preserva tudo que é do operador.

## 3. Credenciais que sobrevivem a reboot

O service carrega `EnvironmentFile=-%h/.config/macip-backup/agent.env`
— segredos em arquivo 0600 sobrevivem a reboot (o keyring `%user` é
temporário; `@s` depende de sessão):

```bash
install -m 600 /dev/null ~/.config/macip-backup/agent.env
printf 'MACIP_RESTIC_PASSWORD_LOCAL=<segredo>\n' >> ~/.config/macip-backup/agent.env
```

Sem imprimir o segredo: `read -rs MACIP_RESTIC_PASSWORD_LOCAL &&
keyctl padd user macip.MACIP_RESTIC_PASSWORD_LOCAL @u; unset
MACIP_RESTIC_PASSWORD_LOCAL` (complemento para sessão atual).

Host key estrita SFTP: `known_hosts_file` obrigatório — o restic/ssh
recusa host desconhecido.

## 4. Agendamento e funcionamento sem login

```bash
~/.local/bin/macip-backup schedule tarefa.json \
  --store ~/.local/state/macip-backup/agent.db \
  --calendar 'Mon..Fri 02:00' --enable
```

**Administrativo necessário** (não automatizado): para o timer disparar
sem sessão aberta, habilite lingering do usuário:

```bash
loginctl enable-linger <usuário>   # pode exigir privilégio admin
systemctl --user enable --now macip-backup-<tarefa>.timer
```

## 5. Atualização

```bash
bash install.sh macip_backup_poc-<nova-versão>.whl --commit <sha>
```

- Recusa com execução ativa (lock ocupado) — tente depois.
- Copia `agent.db` para `db-backups/` antes de tocar o banco.
- Nova release ativada só no final — falha mantém a anterior utilizável.
- **Reversão**: se o esquema SQLite migrou, voltar só o código não basta —
  restaure também `db-backups/agent.db-<stamp>` para `agent.db`, depois
  rode `install.sh` com o wheel anterior.

## 6. Desinstalação

```bash
bash uninstall.sh            # preserva config/creds/DB/logs/repos
bash uninstall.sh --purge    # remove também os dados operacionais
```

Recusa com execução ativa; desativa timers/units próprios; lista cada
item removido e cada item preservado.

## 7. Piloto de 7 dias (checklist executável)

Pré-requisito: agente instalado + `agent.env` preenchido + tarefa com
origem pequena e sintética + destino local (repo exclusivo do piloto).

- [ ] **Dia 0** — `macip-backup run tarefa.json` manual; `history`
      mostra execução; `schedule ... --enable`; timer listado em
      `systemctl --user list-timers`.
- [ ] **Dias 1–6** — conferir `history` e `logs/*.jsonl` diariamente;
      uma `check --read-data` por semana.
- [ ] **Reboot** — após reboot SEM abrir terminal: `systemctl --user
      status macip-backup-<task>.service` mostra execução agendada
      (linger ativo); histórico acumula nova linha.
- [ ] **Cancelamento** — `systemctl --user stop macip-backup-<task>.service`
      durante uma execução → `history` registra `interrupted` com motivo;
      executar manualmente depois → sucesso.
- [ ] **Restauração** — `restore` de snapshot em diretório novo +
      comparação de conteúdo com a origem (`diff -r`).
- [ ] **Falhas** — registrar em `registro-trabalho.md`: cada falha,
      causa e recuperação.

Não declarar o piloto concluído antes dos 7 dias reais.

## 8. Limitações conhecidas

- `EnvironmentFile` lê `KEY=value` simples — segredos com `\n` exigem
  escape manual ou keyring de sessão.
- Sem portal/API: o histórico fica local no SQLite da máquina.
- Linger exige configuração administrativa — fora do instalador.
