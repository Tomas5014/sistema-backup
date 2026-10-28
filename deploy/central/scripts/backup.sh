#!/usr/bin/env bash
# Backup nativo do PostgreSQL (pg_dump -Fc) para backups/.
#
# Atomicidade: grava em temporário restrito NO DIRETÓRIO DE DESTINO
# e só publica por rename após sucesso. Nunca sobrescreve um dump
# existente. Em falha/interrupção remove APENAS o temporário desta
# execução e preserva o código de erro original. Dump final fica 600.
# Uso: backup.sh [arquivo-saída]
set -euo pipefail
source "$(dirname "$0")/_lib.sh"
cd "$CENTRAL_DIR"
source secrets/postgres.env

OUT="${1:-$CENTRAL_DIR/backups/macip-central-$(date +%Y%m%d-%H%M%S).pgdump}"
DEST_DIR="$(dirname "$OUT")"
mkdir -p "$DEST_DIR"

[[ -e "$OUT" ]] && {
  echo "ERRO: $OUT já existe — não sobrescrevo dumps; escolha outro nome" >&2
  exit 1; }

# temporário com permissão restrita no diretório de destino —
# mesmo filesystem, então o rename final é atômico
umask 077
TMP="$(mktemp "$DEST_DIR/.pgdump.XXXXXX")"
cleanup() {
  rc=$?
  rm -f "$TMP"    # só o temporário desta execução
  exit "$rc"      # preserva o código original
}
trap cleanup EXIT INT TERM HUP

rc=0
compose exec -T db pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc \
  > "$TMP" || rc=$?
(( rc )) && { echo "FALHA: pg_dump terminou com código $rc" >&2; exit "$rc"; }

[[ -s "$TMP" ]] || { echo "FALHA: pg_dump não produziu dados" >&2; exit 1; }

mv -n "$TMP" "$OUT" || { echo "FALHA: não consegui publicar $OUT" >&2; exit 1; }
chmod 600 "$OUT"
echo "backup gravado em $OUT ($(stat -c %s "$OUT") bytes)"
