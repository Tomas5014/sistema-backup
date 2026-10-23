#!/usr/bin/env bash
# Desinstala o agente MAC IP Backup.
#
#   deploy/uninstall.sh [--purge]
#
# Sempre: remove units macip-backup-*, symlink do binário, releases e o
# manifesto install.json. Recusa com execução ativa (lock ocupado).
# Padrão: PRESERVA configurações, credenciais, SQLite, logs, locks,
# origens e repositórios de backup. --purge remove também
# $MACIP_CONFIG, $MACIP_STATE e $MACIP_CACHE — informe cada item.
set -euo pipefail

die() { echo "erro: $*" >&2; exit 1; }
warn() { echo "aviso: $*" >&2; }

PURGE=0
[ "${1:-}" = "--purge" ] && PURGE=1

MACIP_HOME="${MACIP_HOME:-$HOME/.local/share/macip-backup}"
MACIP_CONFIG="${MACIP_CONFIG:-$HOME/.config/macip-backup}"
MACIP_STATE="${MACIP_STATE:-$HOME/.local/state/macip-backup}"
MACIP_BIN="${MACIP_BIN:-$HOME/.local/bin}"
MACIP_CACHE="${MACIP_CACHE:-$HOME/.cache/macip-backup}"
LOCKS="$MACIP_STATE/locks"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

REMOVED=()
PRESERVED=()

# ---- recusa com execução ativa ---------------------------------------
for lock in "$LOCKS"/*.lock; do
  [ -e "$lock" ] || continue
  if ! flock -n "$lock" -c 'true' 2>/dev/null; then
    die "execução ativa detectada ($lock) — aguarde o término ou encerre pelo mecanismo controlado"
  fi
done

# ---- units desta instalação ------------------------------------------
for unit in "$UNIT_DIR"/macip-backup-*.timer "$UNIT_DIR"/macip-backup-*.service; do
  [ -e "$unit" ] || continue
  name="$(basename "$unit")"
  systemctl --user disable --now "$name" >/dev/null 2>&1 \
    || warn "não consegui desativar $name (já inexistente?)"
  rm -f "$unit"
  REMOVED+=("unit $name")
done
systemctl --user daemon-reload >/dev/null 2>&1 || true

# ---- binário: só remove se for launcher nosso -------------------------
BIN="$MACIP_BIN/macip-backup"
if [ -f "$BIN" ] && grep -q "macip_backup.cli" "$BIN" 2>/dev/null \
   && grep -q "$MACIP_HOME" "$BIN" 2>/dev/null; then
  rm -f "$BIN"
  REMOVED+=("binário $BIN")
elif [ -e "$BIN" ]; then
  warn "$BIN não parece ser o launcher desta instalação — preservado"
  PRESERVED+=("$BIN")
fi

# ---- programa: só o que está dentro de $MACIP_HOME --------------------
for path in "$MACIP_HOME/releases" "$MACIP_HOME/current" "$MACIP_HOME/install.json"; do
  [ -e "$path" ] || [ -L "$path" ] || continue
  resolved="$(readlink -f "$path" 2>/dev/null || true)"
  case "$resolved" in
    "$MACIP_HOME"*|"$MACIP_HOME" ) rm -rf "$path"; REMOVED+=("$path") ;;
    *) warn "$path não pertence à instalação — preservado"; PRESERVED+=("$path") ;;
  esac
done
rmdir "$MACIP_HOME" 2>/dev/null || true

# ---- dados operacionais ----------------------------------------------
if [ "$PURGE" = 1 ]; then
  for path in "$MACIP_CONFIG" "$MACIP_STATE" "$MACIP_CACHE"; do
    [ -e "$path" ] || continue
    rm -rf "$path"
    REMOVED+=("$path (--purge)")
  done
else
  for path in "$MACIP_CONFIG" "$MACIP_STATE" "$MACIP_CACHE"; do
    [ -e "$path" ] && PRESERVED+=("$path")
  done
fi

echo "Removido:"
if [ ${#REMOVED[@]} -eq 0 ]; then echo "  nada"; else printf '  %s\n' "${REMOVED[@]}"; fi
echo "Preservado:"
if [ ${#PRESERVED[@]} -eq 0 ]; then echo "  nada"; else printf '  %s\n' "${PRESERVED[@]}"; fi
