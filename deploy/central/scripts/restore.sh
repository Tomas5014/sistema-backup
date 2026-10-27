#!/usr/bin/env bash
# Restaura um dump pg_dump -Fc no banco da stack. Uso: restore.sh <dump>
set -euo pipefail
source "$(dirname "$0")/_lib.sh"
cd "$CENTRAL_DIR"
DUMP="${1:?uso: restore.sh <arquivo.pgdump>}"
require_file "$DUMP"
source secrets/postgres.env
echo "ATENÇÃO: substitui o conteúdo do banco $POSTGRES_DB desta stack"
read -r -p "confirmar (sim/não): " ok
[[ "$ok" == "sim" ]] || { echo "abortado"; exit 1; }
cat "$DUMP" | compose exec -T db pg_restore -U "$POSTGRES_USER" \
  -d "$POSTGRES_DB" --clean --if-exists --no-owner
echo "restore concluído de $DUMP"
