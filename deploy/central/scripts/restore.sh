#!/usr/bin/env bash
# Restaura um dump pg_dump -Fc no banco desta stack.
#
# ATÔMICA: --single-transaction + --exit-on-error — uma falha
# qualquer desfaz TODAS as alterações, inclusive as já aplicadas; o
# banco anterior é preservado.
#
# Validação pelo CLIENTE DO CONTÊINER db — pg_restore no host não é
# requisito. Arquivo ausente/vazio/inválido é recusado ANTES de parar
# serviços ou tocar no banco; contêiner indisponível é diagnosticado
# como falha de infraestrutura, nunca como dump corrompido.
#
# Códigos de saída: 0 = banco restaurado E serviços retomados; o
# código do pg_restore é preservado quando a restauração falha;
# retomada que falha depois de restauração OK também gera erro
# (nunca anuncia recuperação completa). Uso: restore.sh <dump>
set -euo pipefail
source "$(dirname "$0")/_lib.sh"
cd "$CENTRAL_DIR"
DUMP="${1:?uso: restore.sh <arquivo.pgdump>}"
require_file "$DUMP"
[[ -s "$DUMP" ]] || { echo "ERRO: $DUMP está vazio" >&2; exit 1; }
source secrets/postgres.env

# valida pelo cliente do contêiner — sem pg_restore no host
RUNNING="$(compose ps --status running --services 2>/dev/null)" || {
  echo "ERRO: não consegui listar serviços da stack (etapa: estado)" >&2
  exit 1; }
grep -qx "db" <<< "$RUNNING" || {
  echo "ERRO: contêiner do banco não está de pé (etapa: infraestrutura)" >&2
  exit 1; }
if ! cat "$DUMP" | compose exec -T db pg_restore -l >/dev/null 2>&1; then
  echo "ERRO: $DUMP não é um dump pg_dump -Fc válido (etapa: validação)" >&2
  exit 1; fi

echo "ATENÇÃO: substitui o conteúdo do banco $POSTGRES_DB desta stack"
read -r -p "confirmar (sim/não): " ok
[[ "$ok" == "sim" ]] || { echo "abortado"; exit 1; }

# impede escritas concorrentes: para SÓ os serviços desta stack que
# estão de pé; nada fora do projeto é tocado
STOPPED=""
for svc in api web; do
  grep -qx "$svc" <<< "$RUNNING" && STOPPED="$STOPPED $svc"
done

# em sinal, tenta retomar os serviços parados e sai com 130
on_signal() {
  [[ -n "${STOPPED:-}" ]] && compose start $STOPPED >/dev/null 2>&1
  exit 130
}
trap 'on_signal' INT TERM HUP

[[ -n "$STOPPED" ]] && compose stop $STOPPED

restore_rc=0
cat "$DUMP" | compose exec -T db pg_restore -U "$POSTGRES_USER" \
  -d "$POSTGRES_DB" --clean --if-exists --no-owner \
  --single-transaction --exit-on-error || restore_rc=$?

# retoma SOMENTE o que estava de pé — falha aqui é diagnóstico à
# parte, nunca rebaixada a `|| true`
start_rc=0
[[ -n "$STOPPED" ]] && { compose start $STOPPED || start_rc=$?; }
trap - INT TERM HUP

if (( restore_rc )); then
  msg="FALHA ($restore_rc): restauração abortada — transação única desfez tudo; o banco anterior está preservado"
  (( start_rc )) && msg+="; a retomada dos serviços também falhou ($start_rc)"
  echo "$msg" >&2
  exit "$restore_rc"
fi
if (( start_rc )); then
  echo "ERRO: banco restaurado, mas a retomada de ($STOPPED) falhou ($start_rc) — verifique 'compose ps' antes de usar" >&2
  exit "$start_rc"
fi
echo "restore concluído de $DUMP — banco restaurado e serviços retomados"
