#!/usr/bin/env bash
# Instalação/atualização do agente MAC IP Backup a partir de wheel.
#
#   deploy/install.sh <wheel> [--commit SHA]
#
# Layout por usuário (todos sobrescrevíveis por env):
#   programa   $MACIP_HOME/releases/<versão>/ + current -> release
#   binário    $MACIP_BIN/macip-backup
#   config     $MACIP_CONFIG   (0700, agent.env 0600)
#   histórico  $MACIP_STATE/agent.db + logs/ + locks/ + db-backups/
#   cache      $MACIP_CACHE
#
# A mesma tarefa compartilha <MACIP_STATE>/locks/<task_id>.lock entre
# run, agent e o service systemd (identidade preservada por padrão).
#
# Reinstalar é seguro: configurações, credenciais, histórico, locks e
# logs nunca são apagados. Atualizar NUNCA roda com execução ativa.
set -euo pipefail

die() { echo "erro: $*" >&2; exit 1; }
warn() { echo "aviso: $*" >&2; }

WHEEL=""
COMMIT=""
while [ $# -gt 0 ]; do
  case "$1" in
    --commit) COMMIT="${2:?--commit requer SHA}"; shift 2 ;;
    -h|--help) sed -n '1,18p' "$0"; exit 0 ;;
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

# ---- requisitos ------------------------------------------------------
missing=""
for tool in python3 restic ssh flock; do
  command -v "$tool" >/dev/null || missing="$missing $tool"
done
[ -z "$missing" ] || die "requisitos básicos ausentes:$missing"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
  || die "python3 >= 3.11 exigido"
python3 -c 'import venv' 2>/dev/null || die "módulo venv ausente (instale python3-venv)"
# Opcionais — exigidos só por backend/operação específica:
command -v keyctl >/dev/null || warn "keyctl ausente — keyring do kernel indisponível; use agent.env"
command -v systemd-analyze >/dev/null || warn "systemd-analyze ausente — validação de OnCalendar falhará"
systemctl --user status >/dev/null 2>&1 || warn "systemd --user inativo — agendamento exigirá sessão systemd do usuário"
command -v mount.cifs >/dev/null || warn "mount.cifs ausente — necessário apenas para destino SMB"

# ---- identidade do artefato ------------------------------------------
base="$(basename "$WHEEL")"
case "$base" in
  macip_backup_poc-*) ;;
  *) die "nome de wheel inesperado: $base" ;;
esac
VERSION="${base#macip_backup_poc-}"
VERSION="${VERSION%%-*}"
if [ -z "$COMMIT" ]; then
  COMMIT="$(git -C "$(dirname "$0")/.." rev-parse --short=12 HEAD 2>/dev/null || true)"
  COMMIT="${COMMIT:-desconhecido}"
fi

RELEASES="$MACIP_HOME/releases"
RELEASE="$RELEASES/$VERSION"
CURRENT="$MACIP_HOME/current"

# ---- não instalar/atualizar durante execução ativa -------------------
for lock in "$LOCKS"/*.lock; do
  [ -e "$lock" ] || continue
  if ! flock -n "$lock" -c 'true' 2>/dev/null; then
    die "execução ativa detectada ($lock) — aguarde o término"
  fi
done

# ---- atualização: backup consistente do histórico --------------------
if [ -e "$CURRENT" ] && [ -f "$DB" ]; then
  mkdir -p "$BACKUPS"
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  if command -v sqlite3 >/dev/null; then
    sqlite3 "$DB" ".backup '$BACKUPS/agent.db-$stamp'"
  else
    cp -p "$DB" "$BACKUPS/agent.db-$stamp"
  fi
  echo "backup do histórico: $BACKUPS/agent.db-$stamp"
fi

# ---- nova release (falha aqui NÃO toca a instalação atual) -----------
mkdir -p "$RELEASES"
if [ -d "$RELEASE" ] || [ -L "$RELEASE" ]; then
  resolved="$(readlink -f "$RELEASE" 2>/dev/null || true)"
  case "$resolved" in
    "$MACIP_HOME"/releases/*) rm -rf "$RELEASE" ;;
    *) die "caminho de release não pertence à instalação: $RELEASE" ;;
  esac
fi
python3 -m venv "$RELEASE"
"$RELEASE/bin/pip" install --quiet --no-deps "$WHEEL"
"$RELEASE/bin/python" -c 'import macip_backup; import macip_backup.cli' \
  || die "smoke do wheel falhou em $RELEASE"

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

# ---- ativação: flip por último — falha anterior mantém instalação ----
ln -sfnT "$RELEASE" "$CURRENT"
# Launcher próprio: shebang de script do venv quebra com espaços no
# caminho. /bin/sh + exec do python da release funciona sempre.
cat > "$MACIP_BIN/macip-backup" <<EOF
#!/bin/sh
# macip-backup — launcher instalado por deploy/install.sh
exec "$CURRENT/bin/python" -m macip_backup.cli "\$@"
EOF
chmod 755 "$MACIP_BIN/macip-backup"

# ---- manifesto --------------------------------------------------------
"$RELEASE/bin/python" - "$WHEEL" "$VERSION" "$COMMIT" "$MACIP_HOME/install.json" <<'PY'
import json, sys
from datetime import UTC, datetime
from pathlib import Path

wheel, version, commit, manifest_path = sys.argv[1:5]
Path(manifest_path).write_text(
    json.dumps(
        {
            "version": version,
            "wheel": Path(wheel).name,
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
  versão   $VERSION (commit $COMMIT)
  programa $RELEASE
  binário  $MACIP_BIN/macip-backup
  config   $MACIP_CONFIG
  histórico $MACIP_STATE
  logs     $LOGS
  locks    $LOCKS
  cache    $MACIP_CACHE

Próximos passos: docs/piloto-operacao.md (credenciais, schedule, linger).
EOF
