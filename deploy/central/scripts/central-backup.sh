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
require_var CENTRAL_BACKUP_REPOSITORY "$ENV_CFG"

# --- pré-requisitos NESTE contexto (systemd ≠ terminal com newgrp) ---
docker info >/dev/null 2>&1 || {
  echo "ERRO: docker não responde neste contexto — 'newgrp docker' só vale para o terminal; verifique grupo do usuário e permissões do socket (etapa: pré-requisito)" >&2
  exit 1; }

# --- destino SFTP opcional: validação de arquivos (argumentos são
#     montados no wrapper Python com shlex.join — espaços seguros) ---
export CBK_SFTP="" CBK_SFTP_KEY="" CBK_SFTP_KNOWN_HOSTS="" CBK_SFTP_PORT=""
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
    CBK_SFTP=1
    CBK_SFTP_KEY="$CENTRAL_BACKUP_SFTP_KEY"
    CBK_SFTP_KNOWN_HOSTS="$CENTRAL_BACKUP_SFTP_KNOWN_HOSTS"
    CBK_SFTP_PORT="${CENTRAL_BACKUP_SFTP_PORT:-22}"
    ;;
  s3:*|local:*|/*) ;;                     # suportados direto
  *) echo "ERRO: backend '$CENTRAL_BACKUP_REPOSITORY' não suportado (etapa: configuração)" >&2; exit 1 ;;
esac

# wrapper: preserva a credencial BYTE A BYTE (env do filho — nunca
# argv) e monta sftp.args com shlex.join, como o adaptador restic.py.
[[ -d "$REPO_SRC/macip_backup" ]] || {
  echo "ERRO: código do agente não encontrado em $REPO_SRC (etapa: credencial)" >&2
  exit 1; }
export RESTIC_REPOSITORY="$CENTRAL_BACKUP_REPOSITORY"
export CBK_PASSWORD_NAME="${CENTRAL_BACKUP_PASSWORD_NAME:-MACIP_CENTRAL_BACKUP_PASSWORD}"
restic_run() {
  PYTHONPATH="$REPO_SRC" python3 - "$@" <<'PY'
import os, shlex, sys
from macip_backup.credentials import credential_from_environment_or_os
try:
    pw = credential_from_environment_or_os(os.environ["CBK_PASSWORD_NAME"])
except Exception:
    sys.stderr.write("credencial do repositório ausente\n")
    sys.exit(96)
args = list(sys.argv[1:])
if os.environ.get("CBK_SFTP"):
    ssh = ["-oBatchMode=yes", "-oStrictHostKeyChecking=yes",
           "-oUserKnownHostsFile=" + os.environ["CBK_SFTP_KNOWN_HOSTS"],
           "-oIdentitiesOnly=yes", "-oPasswordAuthentication=no",
           "-oKbdInteractiveAuthentication=no",
           "-oPreferredAuthentications=publickey",
           "-i", os.environ["CBK_SFTP_KEY"],
           "-p", os.environ["CBK_SFTP_PORT"],
           "-oConnectTimeout=15", "-oServerAliveInterval=30",
           "-oServerAliveCountMax=3"]
    args += ["-o", "sftp.args=" + shlex.join(ssh)]
os.environ["RESTIC_PASSWORD"] = pw        # bytes exatos — sem aparar
os.execvp("restic", ["restic"] + args)
PY
}
# pré-checagem de credencial sem executar restic
PYTHONPATH="$REPO_SRC" python3 -c "
import os, sys
from macip_backup.credentials import credential_from_environment_or_os
try:
    credential_from_environment_or_os(os.environ['CBK_PASSWORD_NAME'])
except Exception:
    sys.exit(1)" || {
  echo "ERRO: senha do repositório ausente — defina $CBK_PASSWORD_NAME no ambiente, EnvironmentFile ou 'keyctl padd user macip.$CBK_PASSWORD_NAME @s' (etapa: credencial)" >&2
  exit 1; }

# --- staging privado no mesmo filesystem ------------------------------
STAGE="$(mktemp -d "$CENTRAL_DIR/backups/.stage.XXXXXX")"
cleanup() { rc=$?; rm -rf "$STAGE"; exit "$rc"; }
trap cleanup EXIT INT TERM HUP

# 1) dump lógico atômico do PostgreSQL (reusa backup.sh — validado)
"$CENTRAL_DIR/scripts/backup.sh" "$STAGE/postgres.pgdump" >/dev/null

# 2) configuração e TLS efetivamente usados — CONTEÚDO (não link)
#    em destinos por FUNÇÃO: mesmo basename/diretório não colide
mkdir -p "$STAGE/config/secrets" "$STAGE/config/tls"
cp -a .env compose.yaml nginx.conf "$STAGE/config/" 2>/dev/null || true
cp -a secrets/postgres.env secrets/api.env "$ENV_CFG" "$STAGE/config/secrets/"
TLS_CERT="$(resolve_path "${CENTRAL_TLS_CERT:-./tls/cert.pem}")"
TLS_KEY="$(resolve_path "${CENTRAL_TLS_KEY:-./tls/key.pem}")"
require_file "$TLS_CERT"
require_file "$TLS_KEY"
cp -aL "$TLS_CERT" "$STAGE/config/tls/cert.pem"
cp -aL "$TLS_KEY" "$STAGE/config/tls/key.pem"
chmod 600 "$STAGE/config/tls/key.pem"
for extra in lab-ca.pem lab-ca.key lab-ca.srl; do
  [[ -f "$CENTRAL_DIR/tls/$extra" ]] && cp -aL "$CENTRAL_DIR/tls/$extra" "$STAGE/config/tls/$extra"
done

# 3) manifesto — versão do artefato (git archive substitui $Format$;
#    checkout usa rev-parse; ausência de ambos → "desconhecido"),
#    checksums verificáveis ANTES do manifesto existir, segredos
#    identificados por FUNÇÃO (sem depender de extensão)
VERSION="$(cat "$CENTRAL_DIR/VERSION" 2>/dev/null || true)"
case "$VERSION" in *Format*|"")
  VERSION="$(git -C "$CENTRAL_DIR/.." rev-parse HEAD 2>/dev/null || echo desconhecido)" ;;
esac
sha_of() {
  case "$1" in
    *secrets/*|*key.pem|*lab-ca.key) printf 'role=secret\n' ;;
    *) ( cd "$STAGE" && sha256sum "$1" | cut -d' ' -f1 ) ;;
  esac
}
{
  echo "macip-central backup manifest"
  echo "created_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "version=$VERSION"
  echo "compose_project=$PROJECT"
  echo "tls_cert_source=$TLS_CERT"
  echo "tls_key_source=$TLS_KEY"
  echo "-- arquivos (caminho modo bytes) --"
  ( cd "$STAGE" && find . -type f ! -name 'MANIFEST.txt' -printf '%P %m %s\n' | sort )
  echo "-- sha256 (arquivos NÃO secretos; secretos = role=secret) --"
  ( cd "$STAGE" && find . -type f ! -name 'MANIFEST.txt' -printf '%P\n' | sort ) \
    | while IFS= read -r f; do printf '%s %s\n' "$f" "$(sha_of "$f")"; done
} > "$STAGE/MANIFEST.txt"

# 4) restic: `cat config` decide — init SÓ com código 10 (repo
#    inexistente); senha errada/acesso/bloqueio/indisponível falham
cat_rc=0
restic_run cat config >/dev/null 2>&1 || cat_rc=$?
case $cat_rc in
  0) ;;
  10) restic_run init >/dev/null 2>&1 || {
        echo "ERRO: repositório inexistente e init falhou (etapa: repositório)" >&2
        exit 1; } ;;
  96) echo "ERRO: credencial do repositório ausente (etapa: credencial)" >&2; exit 1 ;;
  *)  echo "ERRO ($cat_rc): repositório inacessível — senha, permissão, bloqueio ou rede (etapa: repositório)" >&2
      exit 1 ;;
esac

rc=0
restic_run backup "$STAGE" --tag central-backup >/dev/null 2>&1 || rc=$?
(( rc )) && {
  echo "FALHA ($rc): envio ao repositório falhou — a cópia válida anterior continua intacta (etapa: envio)" >&2
  exit "$rc"; }
SNAP="$(restic_run snapshots --last --json 2>/dev/null \
        | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d[-1]["short_id"] if d else "?")' 2>/dev/null || echo '?')"
echo "central-backup concluído — snapshot $SNAP ($(stat -c %s "$STAGE/postgres.pgdump") bytes de dump)"
