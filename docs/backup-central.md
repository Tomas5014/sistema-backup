# Backup diário da central (operação)

Rotina `deploy/central/scripts/central-backup.sh`: dump PostgreSQL
(reusa `backup.sh`, atômico) + `.env` + `secrets/*.env` +
certificado e chave TLS **nos caminhos efetivamente usados**
(`CENTRAL_TLS_CERT`/`CENTRAL_TLS_KEY`, relativos ou absolutos,
inclusive com espaços) + CA de laboratório (`tls/lab-ca.*`) +
manifesto → staging privado (`700`) → snapshot **Restic**
(criptografia nativa do repositório, sem criptografia própria).

- **Sem `forget`/`prune`** neste incremento: a cópia válida anterior
  permanece mesmo se a geração ou o envio falhar.
- Lock por `flock`: nunca há duas execuções simultâneas.
- `docker info` é testado **no contexto do serviço** — `newgrp
  docker` no terminal não comprova acesso via `systemd --user`.
- Nada de senha em argv, JSON ou log: a senha do repositório é
  resolvida pelo mecanismo de credenciais existente (variável de
  ambiente ou kernel keyring `macip.<nome>`).

## Ativação (executar na VM, usuário `estagio01`)

```bash
cd ~/macip-central/deploy/central

# 1) configuração do destino (não versionar!)
cp secrets/central-backup.env.example secrets/central-backup.env
chmod 600 secrets/central-backup.env
# edite CENTRAL_BACKUP_REPOSITORY (local:/..., sftp:user@host:/...)

# 2) senha do repositório restic — sem argv/histórico
echo -n '<senha forte gerada>' | \
  keyctl padd user macip.MACIP_CENTRAL_BACKUP_PASSWORD @s
# IMPORTANTE: keyring @s/@u não sobrevive a reboot — regrave após
# reiniciar OU defina EnvironmentFile na unit. Guarde a senha em
# custódia INDEPENDENTE da VM (cofre do operador) — sem ela o
# repositório não abre.

# 3) dry-run manual — comprova docker+staging+restic neste contexto
./scripts/central-backup.sh

# 4) instala o par service+timer (02h15 local, Persistent)
./scripts/install-central-backup-timer.sh "*-*-* 02:15:00" --enable
# sem --enable apenas escreve as units e mostra o comando de ativação

systemctl --user list-timers macip-central-backup.timer
journalctl --user -u macip-central-backup.service -e
```

Se `docker info` falhar dentro do serviço: `loginctl`/`id` mostram se
o usuário está no grupo `docker`; grupos recém-adicionados exigem
nova sessão/systemd-user-runtime — ou use `DOCKER_HOST` exportado no
`.env` do serviço (socket compatível), sem alterar o socket nem
derrubar serviços do piloto.

## Destino externo SFTP (quando houver servidor)

```bash
# known_hosts verificado ANTES (fora de banda):
ssh-keyscan -t ed25519 <host> >> ~/.ssh/known_hosts.central-backup
ssh-keygen -F <host> -f ~/.ssh/known_hosts.central-backup   # confira fingerprint

# chave dedicada, só leitura escrita no destino:
ssh-keygen -t ed25519 -f ~/.ssh/central-backup-key -N ''
# autorize no servidor de backup; depois em secrets/central-backup.env:
#   CENTRAL_BACKUP_REPOSITORY=sftp:<user>@<host>:/caminho/repo
#   CENTRAL_BACKUP_SFTP_KEY=/home/estagio01/.ssh/central-backup-key
#   CENTRAL_BACKUP_SFTP_KNOWN_HOSTS=/home/estagio01/.ssh/known_hosts.central-backup
```

`StrictHostKeyChecking=yes` é sempre usado — host desconhecido ou
fingerprint divergente faz o envio falhar (e a cópia anterior é
preservada). **Ausência de destino externo configurado nunca é
reportada como envio bem-sucedido** — sem `CENTRAL_BACKUP_REPOSITORY`
o script falha na validação.

## Recuperação em ambiente novo (runbook)

Custódia necessária, guardada FORA da VM:
1. senha do repositório restic (`MACIP_CENTRAL_BACKUP_PASSWORD`);
2. endereço do repositório (e chave SFTP se externo).

```bash
# 1) código: clone do repositório no commit do manifesto
git clone <repo> ~/macip-central && cd ~/macip-central
export RESTIC_REPOSITORY=<mesmo de central-backup.env>
export RESTIC_PASSWORD='<senha sob custódia>'

# 2) lista e restaura o último snapshot
restic snapshots
restic restore latest --target /tmp/macip-recovery
cd /tmp/macip-recovery/*/config   # árvore do staging

# 3) reconstitui deploy/central:
#    .env, compose.yaml, nginx.conf → deploy/central/
#    secrets/*.env → deploy/central/secrets/ (chmod 600)
#    tls/* → caminhos indicados em MANIFEST.txt
#      (tls_cert_source / tls_key_source — podem ser fora do projeto)
#    lab-ca.* → tls/ (usada pelos clientes para confiar na central)

# 4) reinstala segredos de operação:
./deploy/central/scripts/gen-secrets.sh   # SÓ se secrets perdidos;
   # senão use os restaurados (mantêm senhas/tokens existentes)

# 5) sobe a stack e restaura o banco
./deploy/central/scripts/up.sh
./deploy/central/scripts/restore.sh /tmp/macip-recovery/*/postgres.pgdump

# 6) operador admin: existe no dump; se a senha for desconhecida,
./deploy/central/scripts/bootstrap-operator.sh   # redefine via CLI
```

## Alcance

A central armazena **histórico operacional** (clientes, agentes,
execuções, operadores) — este backup protege isso. Os **dados de
backup dos agentes** (repositórios Restic locais/SFTP/S3/SMB) têm
recuperação própria documentada no runbook do agente: para recuperar
dados de cliente, usa-se o repositório do agente + senha da credencial,
independente deste procedimento.
