#!/usr/bin/env bash
# Instalação/atualização do agente MAC IP Backup a partir de wheel.
#
#   deploy/install.sh <wheel> [--commit SHA]
#
# Layout por usuário (todos sobrescrevíveis por env):
#   programa   $MACIP_HOME/releases/<versão>-<sha12>/ + current -> release
#   binário    $MACIP_BIN/macip-backup  (launcher, shebang /bin/sh)
#   config     $MACIP_CONFIG   (0700, agent.env 0600)
#   histórico  $MACIP_STATE/agent.db + logs/ + locks/ + db-backups/
#   cache      $MACIP_CACHE
#
# Releases são IMUTÁVEIS: cada artefato ganha diretório próprio
# identificado por versão+SHA-256 — current nunca é reescrito.
# Reinstalar o mesmo artefato é idempotente (marca .ok reutilizada).
# Lock de manutenção: exclusividade total durante a operação —
# executores (run/agent/service) seguram SH e são recusados aqui.
set -euo pipefail

die() { echo "erro: $*" >&2; exit 1; }
warn() { echo "aviso: $*" >&2; }

WHEEL=""
COMMIT=""
while [ $# -gt 0 ]; do
  case "$1" in
    --commit) COMMIT="${2:?--commit requer SHA}"; shift 2 ;;
    -h|--help) sed -n '1,20p' "$0"; exit 0 ;;
    *) WHEEL="$1"; shift ;;
  esac
done
[ -n "$WHEEL" ] || die "uso: install.sh <wheel> [--commit SHA]"
[ -f "$WHEEL" ] || die "wheel inexistente: $WHEEL"
WHEEL="$(readlink -f "$WHEEL")"

MACIP_HOME="${MACIP_HOME:-$HOME/.local/share/macip-backup}"
MACIP_CONFIG="${MACIP_CONFIG:-$HOME/.config/macip-backup}"
MACIP_STATE="${MACIP_STATE:-$HOME/.local/state/macip-backup}"
MACIP_BIN="${MACIP_BIN:-$HOME/.local/bin}"
MACIP_CACHE="${MACIP_CACHE:-$HOME/.cache/macip-backup}"
LOCKS="$MACIP_STATE/locks"
LOGS="$MACIP_STATE/logs"
BACKUPS="$MACIP_STATE/db-backups"
DB="$MACIP_STATE/agent.db"
RELEASES="$MACIP_HOME/releases"
CURRENT="$MACIP_HOME/current"

# ---- requisitos ------------------------------------------------------
missing=""
for tool in python3 restic ssh flock sha256sum; do
  command -v "$tool" >/dev/null || missing="$missing $tool"
done
[ -z "$missing" ] || die "requisitos básicos ausentes:$missing"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
  || die "python3 >= 3.11 exigido"
python3 -c 'import venv' 2>/dev/null || die "módulo venv ausente (instale python3-venv)"
command -v keyctl >/dev/null || warn "keyctl ausente — keyring do kernel indisponível; use agent.env"
command -v systemd-analyze >/dev/null || warn "systemd-analyze ausente — validação de OnCalendar falhará"
systemctl --user status >/dev/null 2>&1 || warn "systemd --user inativo — agendamento exigirá sessão systemd do usuário"
command -v mount.cifs >/dev/null || warn "mount.cifs ausente — necessário apenas para destino SMB"

# ---- lock de manutenção: exclusividade de ponta a ponta --------------
mkdir -p "$LOCKS"
exec 9>"$LOCKS/maintenance.lock"
flock -n 9 || die "execução ou outra manutenção em andamento — tente mais tarde"

# ---- identidade do artefato ------------------------------------------
base="$(basename "$WHEEL")"
case "$base" in
  macip_backup_poc-*) ;;
  *) die "nome de wheel inesperado: $base" ;;
esac
VERSION="${base#macip_backup_poc-}"
VERSION="${VERSION%%-*}"
SHA256="$(sha256sum "$WHEEL" | awk '{print $1}')"
KEY="${VERSION}-${SHA256:0:12}"
if [ -z "$COMMIT" ]; then
  COMMIT="$(git -C "$(dirname "$0")/.." rev-parse --short=12 HEAD 2>/dev/null || true)"
  COMMIT="${COMMIT:-desconhecido}"
fi

RELEASE="$RELEASES/$KEY"
CANDIDATE="$RELEASES/.candidate-$KEY"

# ---- atualização: backup consistente do histórico --------------------
if [ -e "$CURRENT" ] && [ -f "$DB" ]; then
  mkdir -p "$BACKUPS"
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  python3 - "$DB" "$BACKUPS/agent.db-$stamp" <<'PY'
import sqlite3, sys
source, target = sys.argv[1:3]
src = sqlite3.connect(source)
dst = sqlite3.connect(target)
src.backup(dst)
dst.close()
src.close()
PY
  echo "backup do histórico: $BACKUPS/agent.db-$stamp"
fi

# ---- release candidata (falha aqui NÃO toca a instalação atual) ------
mkdir -p "$RELEASES"
if [ -f "$RELEASE/.ok" ]; then
  echo "mesmo artefato já instalado em $RELEASE — reutilizando"
else
  if [ -e "$RELEASE" ] || [ -L "$RELEASE" ]; then
    [ "$(readlink -f "$CURRENT" 2>/dev/null || true)" = "$(readlink -f "$RELEASE")" ] \
      && die "release $RELEASE sem marca .ok está ativa — inspeção manual"
    rm -rf "$RELEASE"  # incompleta e não apontada por current
  fi
  rm -rf "$CANDIDATE"
  python3 -m venv "$CANDIDATE"
  "$CANDIDATE/bin/pip" install --quiet --no-deps "$WHEEL"
  "$CANDIDATE/bin/python" -c 'import macip_backup; import macip_backup.cli' \
    || die "smoke do wheel falhou em $CANDIDATE"
  touch "$CANDIDATE/.ok"
  mv -T "$CANDIDATE" "$RELEASE"  # candidato validado vira release atômica
fi

# ---- estado do usuário (nunca apagado em reinstalação) ---------------
mkdir -p "$MACIP_CONFIG" "$LOCKS" "$LOGS" "$BACKUPS" "$MACIP_CACHE" "$MACIP_BIN"
chmod 700 "$MACIP_CONFIG"
if [ ! -e "$MACIP_CONFIG/agent.env" ]; then
  cat > "$MACIP_CONFIG/agent.env" <<'EOF'
# Segredos do agente para o service systemd --user (EnvironmentFile).
# Formato KEY=value, um por linha — sem aspas nem export.
# Ex.: MACIP_RESTIC_PASSWORD_LOCAL=senha-do-repositorio
# Permissões: este arquivo deve permanecer 0600.
EOF
  chmod 600 "$MACIP_CONFIG/agent.env"
  echo "criado $MACIP_CONFIG/agent.env (0600) — preencha os segredos da tarefa"
fi

# ---- ativação atômica por último -------------------------------------
ln -sfn "$RELEASE" "$MACIP_HOME/.current.tmp"
mv -T "$MACIP_HOME/.current.tmp" "$CURRENT"
cat > "$MACIP_BIN/macip-backup" <<EOF
#!/bin/sh
# macip-backup — launcher instalado por deploy/install.sh
exec "$CURRENT/bin/python" -m macip_backup.cli "\$@"
EOF
chmod 755 "$MACIP_BIN/macip-backup"

# ---- manifesto com identidade completa do artefato -------------------
"$RELEASE/bin/python" - "$WHEEL" "$VERSION" "$SHA256" "$COMMIT" "$MACIP_HOME/install.json" <<'PY'
import json, sys
from datetime import UTC, datetime
from pathlib import Path

wheel, version, sha256, commit, manifest_path = sys.argv[1:6]
Path(manifest_path).write_text(
    json.dumps(
        {
            "version": version,
            "wheel": Path(wheel).name,
            "sha256": sha256,
            "commit": commit,
            "installed_at_utc": datetime.now(UTC).isoformat(),
        },
        indent=2,
    )
    + "\n"
)
PY

cat <<EOF

Instalação concluída:
  versão   $VERSION (commit $COMMIT, sha256 ${SHA256:0:16}…)
  release  $RELEASE
  binário  $MACIP_BIN/macip-backup
  config   $MACIP_CONFIG
  histórico $MACIP_STATE
  logs     $LOGS
  locks    $LOCKS
  cache    $MACIP_CACHE

Próximos passos: docs/piloto-operacao.md (credenciais, schedule, linger).
EOF
