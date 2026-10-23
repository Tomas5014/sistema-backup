#!/usr/bin/env bash
# Desinstala o agente MAC IP Backup.
#
#   deploy/uninstall.sh
#
# Remove SOMENTE o que é comprovadamente da instalação:
#   - units com marcador `# macip-backup:` no conteúdo;
#   - launcher $MACIP_BIN/macip-backup com referência à instalação;
#   - releases/, current e install.json dentro de $MACIP_HOME.
# Configurações, credenciais, SQLite, logs, locks, origens e
# repositórios são sempre preservados. Recusa com execução ou
# manutenção em andamento (flock de manutenção).
set -euo pipefail

die() { echo "erro: $*" >&2; exit 1; }
warn() { echo "aviso: $*" >&2; }

MACIP_HOME="${MACIP_HOME:-$HOME/.local/share/macip-backup}"
MACIP_CONFIG="${MACIP_CONFIG:-$HOME/.config/macip-backup}"
MACIP_STATE="${MACIP_STATE:-$HOME/.local/state/macip-backup}"
MACIP_BIN="${MACIP_BIN:-$HOME/.local/bin}"
MACIP_CACHE="${MACIP_CACHE:-$HOME/.cache/macip-backup}"
LOCKS="$MACIP_STATE/locks"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
SYSTEMCTL="${MACIP_SYSTEMCTL:-systemctl}"

REMOVED=()
PRESERVED=()

# ---- lock de manutenção: nada executa enquanto removemos -------------
mkdir -p "$LOCKS"
exec 9>"$LOCKS/maintenance.lock"
flock -n 9 || die "execução ou manutenção em andamento — tente mais tarde"

# ---- units: só com identidade DESTA instalação; falha ABORTA --------
marker="# macip-backup: install=$MACIP_HOME"
declare -a OUR_UNITS=()
for unit in "$UNIT_DIR"/macip-backup-*.timer "$UNIT_DIR"/macip-backup-*.service; do
  [ -e "$unit" ] || continue
  if grep -Fxq -- "$marker" "$unit" 2>/dev/null; then
    OUR_UNITS+=("$unit")
  else
    PRESERVED+=("unit de outra instalação: $unit")
  fi
done
for unit in "${OUR_UNITS[@]}"; do
  name="$(basename "$unit")"
  "$SYSTEMCTL" --user disable --now "$name" >/dev/null 2>&1 \
    || die "não consegui desativar $name — desinstalação abortada; programa preservado"
done
for unit in "${OUR_UNITS[@]}"; do
  rm -f "$unit"
  REMOVED+=("unit $(basename "$unit")")
done
"$SYSTEMCTL" --user daemon-reload >/dev/null 2>&1 || true

# ---- binário: só launcher com marcador EXATO desta instalação -------
BIN="$MACIP_BIN/macip-backup"
if [ -f "$BIN" ] && grep -Fxq -- "$marker" "$BIN" 2>/dev/null; then
  rm -f "$BIN"
  REMOVED+=("binário $BIN")
elif [ -e "$BIN" ]; then
  warn "$BIN sem marcador desta instalação — preservado"
  PRESERVED+=("$BIN")
fi

# ---- programa: só dentro de $MACIP_HOME -------------------------------
for path in "$MACIP_HOME/releases" "$MACIP_HOME/current" "$MACIP_HOME/install.json"; do
  [ -e "$path" ] || [ -L "$path" ] || continue
  resolved="$(readlink -f "$path" 2>/dev/null || true)"
  case "$resolved" in
    "$MACIP_HOME"*) rm -rf "$path"; REMOVED+=("$path") ;;
    *) warn "$path fora da instalação — preservado"; PRESERVED+=("$path") ;;
  esac
done
rmdir "$MACIP_HOME" 2>/dev/null || true

# ---- dados operacionais sempre preservados ----------------------------
for path in "$MACIP_CONFIG" "$MACIP_STATE" "$MACIP_CACHE"; do
  [ -e "$path" ] && PRESERVED+=("$path")
done

echo "Removido:"
if [ ${#REMOVED[@]} -eq 0 ]; then echo "  nada"; else printf '  %s\n' "${REMOVED[@]}"; fi
echo "Preservado:"
if [ ${#PRESERVED[@]} -eq 0 ]; then echo "  nada"; else printf '  %s\n' "${PRESERVED[@]}"; fi
