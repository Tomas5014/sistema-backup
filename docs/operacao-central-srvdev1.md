# Operação da central em srvdev1

Estado em 28/10/2026 — central do commit `62327ff` implantada e
validada manualmente pelo operador em `estagio01@srvdev1`
(`10.10.20.239`, Ubuntu 26.04.1, Docker 29.1.3, Compose 2.40.3).

- Código: `~/macip-central`; Compose: `~/macip-central/deploy/central`,
  projeto `macip-central` (PG17 + API + nginx, só `127.0.0.1:8443`).
- Portal acessado do PC por túnel SSH → `https://localhost:18443`.
- Agente 0.2.1 e piloto `piloto-local` **ativos** — acompanhamento de
  7 dias termina em 02/11/2026; o piloto NÃO está concluído.

## Registro de homologação (evidências do operador)

Executadas manualmente na VM pelo operador — distintas das provas
automatizadas do repositório:

- Login no portal e HTTPS com CA de laboratório.
- Backup local, `check --read-data`, restauração, comparação de
  conteúdo e exclusões.
- Backup agendado e sincronização automática.
- **API parada**: backups continuaram `success`; resultados ficaram
  `pending`, com HTTP 502 e backoff — comportamento correto de fila.
- **API reiniciada**: os mesmos três itens passaram a `sent` sem erro:
  - `76bc6625-3569-496d-bd80-70871801baec`
  - `37518939-421c-4514-8e47-0db1df126b0e`
  - `d6e37269-71c9-4959-8853-7cfbc756e2ad`
- Dump PostgreSQL de 25.185 bytes copiado ao PC e conferido por
  SHA-256.
- Restauração desse dump em PostgreSQL temporário e isolado:
  1 cliente, 1 agente, 8 execuções, 8 destinos, 8 tentativas,
  1 operador e migrações 1 e 2.
- Após os testes: central saudável, TLS válido e piloto ativo.

## Tarefa de homologação `homologacao-central-srvdev1`

- Configurações: `~/macip-validacao-central`
- Histórico: `~/macip-validacao-central/estado/agent.db`
- Credenciais/ambiente: `~/macip-validacao-central/agent.env` (0600)
- Código executado **diretamente de `~/macip-central/src`** — os
  timers devem continuar apontando para esse código, não para o
  launcher 0.2.1 de `~/.local/bin/macip-backup`.

## Transição dos timers (comandos — executar na VM)

Ajusta SOMENTE a tarefa de homologação. `piloto-local` e sua
instalação/histórico/agendamento não são tocados.

```bash
cd ~/macip-central

# Força código do checkout (launcher 0.2.1 ficaria fora do caminho):
export MACIP_BIN="$HOME/macip-validacao-central/no-launcher"
export MACIP_HOME="$HOME/macip-central"
export MACIP_CONFIG="$HOME/macip-validacao-central"
export PYTHONPATH="$HOME/macip-central/src"

# 1) backup da tarefa a cada hora (vínculo com a central preservado)
python3 -m macip_backup.cli schedule \
  "$HOME/macip-validacao-central/<arquivo-da-tarefa>.json" \
  --store "$HOME/macip-validacao-central/estado/agent.db" \
  --central "$HOME/macip-validacao-central/central.json" \
  --calendar "hourly" --enable

# 2) sincronização da outbox a cada 5 minutos
python3 -m macip_backup.cli schedule-sync \
  --store "$HOME/macip-validacao-central/estado/agent.db" \
  --central "$HOME/macip-validacao-central/central.json" \
  --calendar "*:0/5" --enable

# verificação — só as units da homologação devem aparecer/ mudar:
systemctl --user list-timers 'macip-backup-*'
```

`MACIP_CONFIG` faz o EnvironmentFile apontar para
`~/macip-validacao-central/agent.env` (credenciais preservadas, 0600).
`MACIP_BIN` inexistente garante PYTHONPATH de `~/macip-central/src` na
unit; `MACIP_HOME` fixa o marcador de propriedade da instalação —
units de outras instalações (0.2.1/piloto) não são sobrescritas.

## Backup diário da central — ver docs/backup-central.md

Rotina `central-backup.sh` + par `macip-central-backup.{service,timer}`
(02h15, Persistent, lock anti-simultâneo). Pendentes para ativar a
cópia externa: **host SFTP, usuário, chave autorizada e
known_hosts verificado** — não fornecidos; exemplo e preparação em
`secrets/central-backup.env.example`.
