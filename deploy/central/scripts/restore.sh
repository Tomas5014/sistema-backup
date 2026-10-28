#!/usr/bin/env bash
# Restaura um dump pg_dump -Fc no banco desta stack.
#
# ATÔMICA: --single-transaction + --exit-on-error — uma falha
# qualquer desfaz TODAS as alterações, inclusive as já aplicadas; o
# banco anterior é preservado. Escritas concorrentes são impedidas
# parando api+web (somente esta stack); ao final, são reiniciados
# apenas os serviços que estavam de pé. Uso: restore.sh <dump>
set -euo pipefail
source "$(dirname "$0")/_lib.sh"
cd "$CENTRAL_DIR"
DUMP="${1:?uso: restore.sh <arquivo.pgdump>}"
require_file "$DUMP"
[[ -s "$DUMP" ]] || { echo "ERRO: $DUMP está vazio" >&2; exit 1; }
# valida o arquivo ANTES de qualquer alteração no banco
pg_restore -l "$DUMP" > /dev/null 2>&1 || {
  echo "ERRO: $DUMP não é um dump pg_dump -Fc válido" >&2; exit 1; }
source secrets/postgres.env

echo "ATENÇÃO: substitui o conteúdo do banco $POSTGRES_DB desta stack"
read -r -p "confirmar (sim/não): " ok
[[ "$ok" == "sim" ]] || { echo "abortado"; exit 1; }

# impede escritas concorrentes: para SÓ os serviços desta stack que
# estão de pé; nada fora do projeto é tocado. Se não dá para listar
# o estado, não restaura — um escritor solto poderia ganhar a corrida.
RUNNING="$(compose ps --status running --services 2>/dev/null)" || {
  echo "ERRO: não consegui listar serviços da stack" >&2; exit 1; }
STOPPED=""
for svc in api web; do
  grep -qx "$svc" <<< "$RUNNING" && STOPPED="$STOPPED $svc"
done
restore_state() {
  rc=$?
  if [[ -n "$STOPPED" ]]; then
    compose start $STOPPED >/dev/null 2>&1 || true
  fi
  exit "$rc"
}
# shellcheck disable=SC2064
trap "restore_state" EXIT

[[ -n "$STOPPED" ]] && compose stop $STOPPED

rc=0
cat "$DUMP" | compose exec -T db pg_restore -U "$POSTGRES_USER" \
  -d "$POSTGRES_DB" --clean --if-exists --no-owner \
  --single-transaction --exit-on-error || rc=$?
(( rc )) && {
  echo "FALHA ($rc): restauração abortada — transação única desfez" \
       "tudo; o banco anterior está preservado" >&2
  exit "$rc"; }
echo "restore concluído de $DUMP"
