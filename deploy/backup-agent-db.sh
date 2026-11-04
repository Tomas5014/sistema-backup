#!/usr/bin/env bash
# Backup SQLite online sob o mesmo lock EX do instalador e executor.
set -euo pipefail
umask 077
MACIP_STATE="${MACIP_STATE:-$HOME/.local/state/macip-backup}"
MACIP_HOME="${MACIP_HOME:-$HOME/.local/share/macip-backup}"
LOCKS="${MACIP_LOCK_DIR:-$MACIP_STATE/locks}"
[[ "$LOCKS" = /* ]] || { echo 'erro: diretório de locks deve ser absoluto' >&2; exit 1; }
[[ -f "$MACIP_STATE/agent.db" ]] || { echo 'erro: banco do agente ausente' >&2; exit 1; }
TARGET="${1:?uso: backup-agent-db.sh arquivo-novo}"
export MACIP_HOME MACIP_STATE
resolver="$(dirname "$0")/../src/macip_backup/lock_identity.py"
if [ -f "$resolver" ]; then
  LOCKS="$(python3 "$resolver" --maintenance)"
else
  LOCKS="$("$MACIP_HOME/current/bin/python" -m macip_backup.lock_identity --maintenance)"
fi
mkdir -p "$LOCKS"
exec 9>"$LOCKS/maintenance.lock"
flock -n 9 || { echo 'erro: execução ou manutenção em andamento' >&2; exit 1; }
python3 - "$MACIP_STATE/agent.db" "$TARGET" <<'PY'
import os, sqlite3, sys
from pathlib import Path
source, target = map(Path, sys.argv[1:])
fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
os.close(fd)
try:
    with sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True) as src:
        with sqlite3.connect(target) as dst:
            src.backup(dst)
            if dst.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise RuntimeError('integridade SQLite inválida')
except BaseException:
    target.unlink(missing_ok=True)
    raise
print('backup SQLite concluído')
PY
