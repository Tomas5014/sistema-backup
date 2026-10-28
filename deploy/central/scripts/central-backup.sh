#!/usr/bin/env bash
# Backup operacional da PRÓPRIA central (uso diário via timer):
#   dump PostgreSQL (backup.sh) + .env + secrets/*.env + TLS efetivo +
#   CA de laboratório + manifesto → staging privado → restic
#   (criptografia nativa) no repositório de secrets/central-backup.env.
#
# Sem criptografia própria, sem fila própria e SEM forget/prune — a
# cópia válida anterior sempre permanece: falha de geração/envio não
# toca snapshots existentes. Nada imprime senhas, tokens ou conteúdo
# de segredos.
set -euo pipefail
source "$(dirname "$0")/_lib.sh"
cd "$CENTRAL_DIR"
umask 077

REPO_SRC="$(cd "$CENTRAL_DIR/../.." && pwd)/src"

# --- exclusão de execuções simultâneas -------------------------------
mkdir -p "$CENTRAL_DIR/backups"
exec 9>"$CENTRAL_DIR/backups/.central-backup.lock"
flock -n 9 || {
  echo "ERRO: outra execução de central-backup em curso (etapa: lock)" >&2
  exit 1; }

# --- arquivos exigidos ANTES de qualquer chamada ao docker ---------
require_file .env
require_file secrets/postgres.env
require_file secrets/api.env
ENV_CFG="secrets/central-backup.env"
require_file "$ENV_CFG"
check_secret_perms "$ENV_CFG"

set -a; source .env; set +a
source "$ENV_CFG"

# --- pré-requisitos NESTE contexto (systemd ≠ terminal com newgrp) ---
docker info >/dev/null 2>&1 || {
  echo "ERRO: docker não responde neste contexto — 'newgrp docker' só vale para o terminal; verifique grupo do usuário e permissões do socket (etapa: pré-requisito)" >&2
  exit 1; }
require_var CENTRAL_BACKUP_REPOSITORY "$ENV_CFG"
PASSWORD_NAME="${CENTRAL_BACKUP_PASSWORD_NAME:-MACIP_CENTRAL_BACKUP_PASSWORD}"

# senha do repositório resolvida pelo mecanismo existente
# (variável de ambiente primeiro, kernel keyring `macip.` depois) —
# nunca em argumento nem em arquivo JSON.
[[ -d "$REPO_SRC/macip_backup" ]] || {
  echo "ERRO: código do agente não encontrado em $REPO_SRC (etapa: credencial)" >&2
  exit 1; }
RESTIC_PASSWORD="$(PYTHONPATH="$REPO_SRC" python3 -c \
'import sys
try:
    from macip_backup.credentials import credential_from_environment_or_os
    sys.stdout.write(credential_from_environment_or_os(sys.argv[1]))
except Exception:
    sys.exit(1)' "$PASSWORD_NAME" 2>/dev/null || true)"
[[ -n "$RESTIC_PASSWORD" ]] || {
  echo "ERRO: senha do repositório ausente — defina $PASSWORD_NAME no ambiente ou 'keyctl padd user macip.$PASSWORD_NAME @s' (etapa: credencial)" >&2
  exit 1; }
export RESTIC_PASSWORD RESTIC_REPOSITORY="$CENTRAL_BACKUP_REPOSITORY"

# --- destino SFTP opcional: chave explícita + known_hosts verificado -
RESTIC_EXTRA=()
case "$CENTRAL_BACKUP_REPOSITORY" in
  sftp:*)
    TARGET="${CENTRAL_BACKUP_REPOSITORY#sftp:}"
    TARGET="${TARGET%%:*}"                  # user@host (ou host)
    [[ "$TARGET" == *"@"* ]] || {
      echo "ERRO: repositório sftp precisa de user@host (etapa: configuração)" >&2; exit 1; }
    require_var CENTRAL_BACKUP_SFTP_KEY "$ENV_CFG"
    require_var CENTRAL_BACKUP_SFTP_KNOWN_HOSTS "$ENV_CFG"
    require_file "$CENTRAL_BACKUP_SFTP_KEY"
    check_secret_perms "$CENTRAL_BACKUP_SFTP_KEY"
    require_file "$CENTRAL_BACKUP_SFTP_KNOWN_HOSTS"
    PORT_OPT=()
    [[ -n "${CENTRAL_BACKUP_SFTP_PORT:-}" ]] && PORT_OPT=(-p "$CENTRAL_BACKUP_SFTP_PORT")
    RESTIC_EXTRA=(-o "sftp.command=ssh $TARGET${PORT_OPT:+ ${PORT_OPT[*]}} -i $CENTRAL_BACKUP_SFTP_KEY -o IdentitiesOnly=yes -o UserKnownHostsFile=$CENTRAL_BACKUP_SFTP_KNOWN_HOSTS -o StrictHostKeyChecking=yes -s sftp")
    ;;
  s3:*|local:*|/*) ;;                     # suportados direto
  *) echo "ERRO: backend '$CENTRAL_BACKUP_REPOSITORY' não suportado (etapa: configuração)" >&2; exit 1 ;;
esac

# --- staging privado no mesmo filesystem ------------------------------
STAGE="$(mktemp -d "$CENTRAL_DIR/backups/.stage.XXXXXX")"
cleanup() { rc=$?; rm -rf "$STAGE"; exit "$rc"; }
trap cleanup EXIT INT TERM HUP

# 1) dump lógico atômico do PostgreSQL (reusa backup.sh — validado)
"$CENTRAL_DIR/scripts/backup.sh" "$STAGE/postgres.pgdump" >/dev/null

# 2) configuração e TLS efetivamente usados (caminhos personalizados
#    resolvidos como no up.sh; copia preserva modo/permissões)
mkdir -p "$STAGE/config/secrets" "$STAGE/config/tls"
cp -a .env compose.yaml nginx.conf "$STAGE/config/" 2>/dev/null || true
cp -a secrets/postgres.env secrets/api.env "$ENV_CFG" "$STAGE/config/secrets/"
TLS_CERT="$(resolve_path "${CENTRAL_TLS_CERT:-./tls/cert.pem}")"
TLS_KEY="$(resolve_path "${CENTRAL_TLS_KEY:-./tls/key.pem}")"
cp -a "$TLS_CERT" "$STAGE/config/tls/$(basename "$TLS_CERT")"
cp -a "$TLS_KEY" "$STAGE/config/tls/$(basename "$TLS_KEY")"
for extra in lab-ca.pem lab-ca.key lab-ca.srl; do
  [[ -f "$CENTRAL_DIR/tls/$extra" ]] && cp -a "$CENTRAL_DIR/tls/$extra" "$STAGE/config/tls/$extra"
done

# 3) manifesto — informação de recuperação, ZERO valores secretos:
#    arquivos secretos entram só com nome/tamanho/modo
COMMIT="$(git -C "$CENTRAL_DIR/.." rev-parse HEAD 2>/dev/null || echo desconhecido)"
{
  echo "macip-central backup manifest"
  echo "created_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "git_commit=$COMMIT"
  echo "compose_project=$PROJECT"
  echo "tls_cert_source=$TLS_CERT"
  echo "tls_key_source=$TLS_KEY"
  echo "-- arquivos (caminho modo bytes) --"
  ( cd "$STAGE" && find . -type f -printf '%P %m %s\n' | sort )
  echo "-- sha256 (apenas arquivos NÃO secretos) --"
  ( cd "$STAGE" && find . -type f \
      ! -path './config/secrets/*' ! -name '*.key' \
      -exec sha256sum {} + | sort )
} > "$STAGE/MANIFEST.txt"

# 4) restic: init se necessário, snapshot criptografado
if ! restic snapshots "${RESTIC_EXTRA[@]}" >/dev/null 2>&1; then
  restic init "${RESTIC_EXTRA[@]}" >/dev/null 2>&1 || {
    echo "ERRO: não consegui abrir nem inicializar o repositório — destino indisponível ou credenciais (etapa: repositório)" >&2
    exit 1; }
fi
rc=0
restic backup "$STAGE" --tag central-backup "${RESTIC_EXTRA[@]}" \
  >/dev/null 2>&1 || rc=$?
(( rc )) && {
  echo "FALHA ($rc): envio ao repositório falhou — a cópia válida anterior continua intacta (etapa: envio)" >&2
  exit "$rc"; }
SNAP="$(restic snapshots --last --json "${RESTIC_EXTRA[@]}" 2>/dev/null \
        | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d[-1]["short_id"] if d else "?")' 2>/dev/null || echo '?')"
echo "central-backup concluído — snapshot $SNAP ($(stat -c %s "$STAGE/postgres.pgdump") bytes de dump)"
