#!/usr/bin/env bash
# Backup nativo do PostgreSQL (pg_dump -Fc) para backups/ — só lê,
# nada é apagado. Uso: backup.sh [arquivo-saída]
set -euo pipefail
source "$(dirname "$0")/_lib.sh"
cd "$CENTRAL_DIR"
source secrets/postgres.env
OUT="${1:-$CENTRAL_DIR/backups/macip-central-$(date +%Y%m%d-%H%M%S).pgdump}"
mkdir -p "$(dirname "$OUT")"
compose exec -T db pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc > "$OUT"
[[ -s "$OUT" ]] || { rm -f "$OUT"; echo "FALHA: dump vazio" >&2; exit 1; }
echo "backup gravado em $OUT ($(stat -c %s "$OUT") bytes)"
