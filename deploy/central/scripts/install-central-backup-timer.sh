#!/usr/bin/env bash
# Gera (ou reinstala) o par service+timer systemd --user do backup
# diário da central. Padrão: 02h15 horário local da máquina,
# Persistent=true (execução perdida é recuperada). Só habilita com
# --enable explícito. Nunca sobrescreve unit sem o marcador desta
# instalação. Uso: install-central-backup-timer.sh [OnCalendar] [--enable]
set -euo pipefail
source "$(dirname "$0")/_lib.sh"

CAL="${1:-*-*-* 02:15:00}"
ENABLE=0
[[ "${2:-}" == "--enable" || "${1:-}" == "--enable" ]] && ENABLE=1
[[ "$CAL" == "--enable" ]] && CAL="*-*-* 02:15:00"

systemd-analyze calendar "$CAL" >/dev/null || {
  echo "ERRO: OnCalendar inválido '$CAL'" >&2; exit 1; }

UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p "$UNIT_DIR"
NAME="macip-central-backup"
MARKER="# macip-backup: central-backup=$CENTRAL_DIR"

for suffix in service timer; do
  f="$UNIT_DIR/$NAME.$suffix"
  if [[ -f "$f" ]] && ! grep -qxF "$MARKER" "$f"; then
    echo "ERRO: $f existe e não é desta instalação — conteúdo preservado" >&2
    exit 1
  fi
done

# aspas no ExecStart cobrem caminhos com espaço
DOCKER_HOST_LINE=""
[[ -n "${DOCKER_HOST:-}" ]] && \
  DOCKER_HOST_LINE="Environment=\"DOCKER_HOST=$DOCKER_HOST\"
"

cat > "$UNIT_DIR/$NAME.service" <<EOF
$MARKER
[Unit]
Description=MAC IP Backup — backup diário da central (restic)

[Service]
Type=oneshot
${DOCKER_HOST_LINE}ExecStart="$CENTRAL_DIR/scripts/central-backup.sh"
EOF

cat > "$UNIT_DIR/$NAME.timer" <<EOF
$MARKER
[Unit]
Description=Agendamento: backup diário da central

[Timer]
OnCalendar=$CAL
Persistent=true
AccuracySec=1min

[Install]
WantedBy=timers.target
EOF

if (( ENABLE )); then
  systemctl --user daemon-reload
  systemctl --user enable --now "$NAME.timer"
  echo "timer $NAME.timer habilitado — próxima execução conforme '$CAL'"
else
  echo "units escritas em $UNIT_DIR — habilite com:"
  echo "  systemctl --user daemon-reload && systemctl --user enable --now $NAME.timer"
fi
