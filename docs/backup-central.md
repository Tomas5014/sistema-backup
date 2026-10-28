# Backup diário da central (operação)

Rotina `deploy/central/scripts/central-backup.sh`: dump PostgreSQL
(reusa `backup.sh`, atômico) + `.env` + `secrets/*.env` +
certificado e chave TLS **nos caminhos efetivamente usados**
(`CENTRAL_TLS_CERT`/`CENTRAL_TLS_KEY`, relativos ou absolutos,
inclusive com espaços; links são desreferenciados e gravados por
função — `config/tls/cert.pem`, `config/tls/key.pem` — para que
basenames iguais não colidam) + CA de laboratório (`tls/lab-ca.*`) +
manifesto → staging privado (`700`) → snapshot **Restic**
(criptografia nativa do repositório, sem criptografia própria).

- **Sem `forget`/`prune`** neste incremento: a cópia válida anterior
  permanece mesmo se a geração ou o envio falhar.
- Lock por `flock`: nunca há duas execuções simultâneas.
- `docker info` é testado **no contexto do serviço** — `newgrp
  docker` no terminal não comprova acesso via `systemd --user`.
- A senha do repositório vai do resolvedor de credenciais direto para
  o ambiente do processo restic — **byte a byte** (espaços e quebras
  de linha preservados; nada passa por `$(...)` que apara `\n`),
  nunca em argv, JSON ou log.
- `init` só roda quando `restic cat config` sai com **código 10**
  (repositório inexistente). Senha errada (12), permissão negada,
  bloqueio ou indisponibilidade falham sem tentar inicializar.
- Argumentos SSH para SFTP são montados com `shlex.join` (mesma
  regra do adaptador do agente): `BatchMode`, `StrictHostKeyChecking`
  + `UserKnownHostsFile`, `IdentitiesOnly`, publickey-only,
  `ConnectTimeout`/`ServerAlive*` — espaços em caminhos não quebram.

## Ativação (executar na VM, usuário `estagio01`)

```bash
cd ~/macip-central/deploy/central

# 1) configuração do destino (não versionar!)
cp secrets/central-backup.env.example secrets/central-backup.env
chmod 600 secrets/central-backup.env
# edite CENTRAL_BACKUP_REPOSITORY (local:/..., sftp:user@host:/...)
# — valores com espaços precisam de aspas, ex.: KEY="./meus arquivos/k"
```

### 2) Senha do repositório — escolha UMA opção

**Opção A — EnvironmentFile 0600 (recomendada para o timer):**
persiste a reboot e não depende de nenhuma sessão/keyring.

```bash
# crie SOMENTE se não existir, já com permissão 0600 — depois edite:
[[ -f secrets/central-backup-runtime.env ]] || \
  install -m 600 /dev/null secrets/central-backup-runtime.env
${EDITOR:-nano} secrets/central-backup-runtime.env
#   conteúdo da linha:  MACIP_CENTRAL_BACKUP_PASSWORD=<senha forte>
# (nunca: echo 'senha' > arquivo — fica no histórico do shell)
```

A unit gerada já inclui
`EnvironmentFile=-<central>/secrets/central-backup-runtime.env`
(opcional) — crie o arquivo 0600 acima e rode
`systemctl --user daemon-reload`; o serviço passa a funcionar após
reboot sem sessão interativa.

**Opção B — kernel keyring (sem arquivo extra; expira no reboot):**

```bash
# keyctl lê o segredo de stdin até Ctrl-D — digite/cole no prompt,
# sem deixar o valor no histórico do shell:
keyctl padd user macip.MACIP_CENTRAL_BACKUP_PASSWORD @u
# @u = keyring do usuário: visível para os processos do usuário
# (incl. systemd --user), mas expira no reboot — após reiniciar,
# regrave antes da próxima execução agendada. @s NÃO basta: é o
# keyring da sessão do terminal, invisível para o serviço.
```

Em ambas: guarde a senha também em custódia **independente** da VM
(cofre do operador) — sem ela o repositório não abre.

```bash
# 3) gera as units SEM habilitar (mostra o comando de ativação)
./scripts/install-central-backup-timer.sh "*-*-* 02:15:00"

# 4) prova NO CONTEXTO DO SERVIÇO — é a unit que carrega
#    central-backup-runtime.env; executar central-backup.sh na mão
#    NÃO carrega esse arquivo (reproduziria 'credencial ausente').
#    Também comprova o acesso ao Docker sob systemd --user.
systemctl --user daemon-reload
systemctl --user start macip-central-backup.service
systemctl --user status macip-central-backup.service --no-pager
journalctl --user -u macip-central-backup.service -e
#    → "central-backup concluído — snapshot <id>"; confira no repo:
#      restic -r <repositório> snapshots

# 5) comprovado → habilita o agendamento diário
systemctl --user enable --now macip-central-backup.timer
systemctl --user list-timers macip-central-backup.timer
```

## Destino externo SFTP (quando houver servidor)

```bash
# known_hosts verificado ANTES (fora de banda):
ssh-keyscan -t ed25519 <host> >> ~/.ssh/known_hosts.central-backup
ssh-keygen -F <host> -f ~/.ssh/known_hosts.central-backup   # confira fingerprint

# chave dedicada, sem senha na própria chave (o repo já é cifrado):
ssh-keygen -t ed25519 -f ~/.ssh/central-backup-key -N ''
# autorize a pública no servidor; depois em secrets/central-backup.env
# (valores com espaço precisam de aspas):
#   CENTRAL_BACKUP_REPOSITORY=sftp:<user>@<host>:/caminho/repo
#   CENTRAL_BACKUP_SFTP_KEY="/home/estagio01/.ssh/central-backup-key"
#   CENTRAL_BACKUP_SFTP_KNOWN_HOSTS="/home/estagio01/.ssh/known_hosts.central-backup"
#   CENTRAL_BACKUP_SFTP_PORT=2222            # se não for 22
```

`BatchMode`/`StrictHostKeyChecking`/`ConnectTimeout`/
`ServerAliveInterval` são sempre usados — host desconhecido,
fingerprint divergente ou destino travado falham com limite de tempo
(e a cópia anterior é preservada). **Ausência de destino externo
nunca é reportada como envio bem-sucedido** — sem
`CENTRAL_BACKUP_REPOSITORY` o script falha na validação.

## Recuperação em ambiente novo (runbook)

Custódia necessária, guardada FORA da VM:
1. senha do repositório restic;
2. endereço do repositório (e chave SFTP + known_hosts se externo).

```bash
# 1) código: clone e faça checkout da versão gravada no manifesto
#    (version= em MANIFEST.txt — commit+data embutidos no artefato
#    por git archive, ou rev-parse num checkout normal).
git clone <repo> ~/macip-central && cd ~/macip-central

# 2) senha: por prompt, sem deixar valor no histórico —
read -s -p "senha do repositório: " RESTIC_PASSWORD; export RESTIC_PASSWORD; echo
#    ou aponte um arquivo 0600 criado pelo seu gerenciador de senhas:
#      restic --password-file ~/senha-repo.txt ...

# 3) opções reutilizáveis — o mesmo array entra em TODAS as chamadas
#    restic da recuperação (snapshots, restore, etc.), mantendo
#    identidade explícita e host key verificado em cada uma:
export RESTIC_REPOSITORY='<o repositório>'   # local:/... ou sftp:...
RESTIC_OPTS=()
# se o repositório for sftp:, gere sftp.args com shlex.join a partir
# de uma LISTA de argumentos (mesma regra do wrapper da rotina) —
# nunca aspas escritas à mão dentro do texto:
SSH_ARGS="$(python3 - <<'PY'
import os, shlex
home = os.path.expanduser("~")
print(shlex.join([
    "-oBatchMode=yes", "-oStrictHostKeyChecking=yes",
    "-oUserKnownHostsFile=" + home + "/.ssh/known_hosts.central-backup",
    "-oIdentitiesOnly=yes", "-oPasswordAuthentication=no",
    "-oKbdInteractiveAuthentication=no",
    "-oPreferredAuthentications=publickey",
    "-i", home + "/.ssh/central-backup-key",
    "-p", "22",
    "-oConnectTimeout=15", "-oServerAliveInterval=30",
    "-oServerAliveCountMax=3",
]))
PY
)"
RESTIC_OPTS=(-o "sftp.args=$SSH_ARGS")

restic "${RESTIC_OPTS[@]}" snapshots

# 4) recupere o manifesto ANTES — ele diz a versão do artefato:
restic "${RESTIC_OPTS[@]}" restore latest \
  --target /tmp/macip-recovery --include MANIFEST.txt
MAN="$(find /tmp/macip-recovery -name MANIFEST.txt -print -quit)"
VERSION_SHA="$(sed -n 's/^version=\([0-9a-f]\{7,\}\).*/\1/p' "$MAN")"
#    só o SHA → checkout (data ao lado do hash não entra no comando):
git checkout "$VERSION_SHA"   # se 'desconhecido', use o HEAD do clone

# 5) restaura o staging inteiro:
restic "${RESTIC_OPTS[@]}" restore latest --target /tmp/macip-recovery

# 6) o Restic restaura o caminho ABSOLUTO do staging
#    (…/deploy/central/backups/.stage.XXXXXX/) — localize-o:
STAGED="$(dirname "$(find /tmp/macip-recovery -name postgres.pgdump -print -quit)")"
CFG_DIR="$STAGED/config"   # .env, compose.yaml, nginx.conf, secrets/, tls/

# 7) reconstitui deploy/central:
#    $CFG_DIR/.env, compose.yaml, nginx.conf → deploy/central/
#    $CFG_DIR/secrets/*.env → deploy/central/secrets/ (chmod 600)
#    $CFG_DIR/tls/{cert,key}.pem → os caminhos tls_cert_source /
#      tls_key_source do MANIFEST.txt (podem ser fora do projeto)
#    lab-ca.* → deploy/central/tls/ (clientes confiam nela)

# 8) sobe a stack e restaura o banco (confirmação 'sim' é pedida):
cd ~/macip-central
./deploy/central/scripts/up.sh
./deploy/central/scripts/restore.sh "$STAGED/postgres.pgdump"

# 9) operador: para redefinir a senha de um operador existente use o
#    reset-password da CLI (senha por getpass no TTY ou por stdin —
#    nunca em argv):
docker compose -p macip-central -f deploy/central/compose.yaml exec api \
  python -m macip_backup_api.operator_cli reset-password --login <login>
#    (sem -T, o exec aloca TTY → getpass pergunta a senha duas vezes;
#     ou por pipe: printf '%s\n' "$SENHA" | docker compose -p macip-central
#     exec -T api python -m macip_backup_api.operator_cli \
#       reset-password --login <login> --password-stdin)
#    Para criar um operador NOVO:
./deploy/central/scripts/bootstrap-operator.sh <login> "<nome>" admin
```

## Alcance

A central armazena **histórico operacional** (clientes, agentes,
execuções, operadores) — este backup protege isso. Os **dados de
backup dos agentes** (repositórios Restic locais/SFTP/S3/SMB) têm
recuperação própria documentada no runbook do agente: para recuperar
dados de cliente, usa-se o repositório do agente + senha da credencial,
independente deste procedimento.
