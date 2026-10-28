#!/usr/bin/env bash
# Restaura um dump pg_dump -Fc no banco desta stack.
#
# ATÔMICA: --single-transaction + --exit-on-error — uma falha
# qualquer desfaz TODAS as alterações; o banco anterior é preservado.
#
# O estado dos serviços é consultado DE NOVO depois da confirmação —
# serviços ligados enquanto o operador espera para digitar `sim` são
# detectados e parados. A parada é CONFIRMADA (com prazo) antes da
# aplicação; escritor ativo ou estado não verificável aborta o dump.
# A recuperação (retomada) fica armada ANTES do primeiro `stop` —
# falha parcial, erro de preparação, falha de restore ou interrupção
# sempre tentam religar somente o que estava de pé após o `sim`.
#
# Interrupções: SIGHUP=129, SIGINT=130, SIGTERM=143 — o código do
# sinal é preservado e a retomada ainda é tentada uma única vez.
# "concluído" e código 0 SÓ saem quando a restauração E a retomada
# terminam bem-sucedidas, sem interrupção.
set -euo pipefail
source "$(dirname "$0")/_lib.sh"
cd "$CENTRAL_DIR"
DUMP="${1:?uso: restore.sh <arquivo.pgdump>}"
require_file "$DUMP"
[[ -s "$DUMP" ]] || { echo "ERRO: $DUMP está vazio" >&2; exit 1; }
source secrets/postgres.env

STAGE="preparação"          # etapa atual, para diagnóstico de sinal
DUMP_APPLIED=0              # 1 só quando pg_restore termina com sucesso
STOPPED=""

running_services() {
  compose ps --status running --services 2>/dev/null
}

# --- validação pelo cliente do contêiner (antes do destrutivo) -----
STAGE="validação"
RUNNING="$(running_services)" || {
  echo "ERRO: não consegui listar serviços da stack (etapa: $STAGE)" >&2
  exit 1; }
grep -qx "db" <<< "$RUNNING" || {
  echo "ERRO: contêiner do banco não está de pé (etapa: infraestrutura)" >&2
  exit 1; }
if ! cat "$DUMP" | compose exec -T db pg_restore -l >/dev/null 2>&1; then
  echo "ERRO: $DUMP não é um dump pg_dump -Fc válido (etapa: validação)" >&2
  exit 1; fi

STAGE="confirmação"
echo "ATENÇÃO: substitui o conteúdo do banco $POSTGRES_DB desta stack"
read -r -p "confirmar (sim/não): " ok
[[ "$ok" == "sim" ]] || { echo "abortado"; exit 1; }

# --- estado REAL depois do `sim` — nada de captura antiga ----------
STAGE="estado pós-confirmação"
RUNNING="$(running_services)" || {
  echo "ERRO: não consegui listar serviços após confirmação (etapa: $STAGE)" >&2
  exit 1; }
for svc in api web; do
  grep -qx "$svc" <<< "$RUNNING" && STOPPED="$STOPPED $svc"
done

# --- recuperação armada ANTES de qualquer parada --------------------
# exit 0 só se aplicação E retomada terminaram bem — nunca por sinal
cleanup() {
  rc=$?
  trap - EXIT INT TERM HUP
  start_rc=0
  if [[ -n "${STOPPED:-}" ]]; then
    compose start $STOPPED || start_rc=$?
  fi
  if (( rc )); then
    (( start_rc )) && echo "AVISO: retomada dos serviços também falhou ($start_rc)" >&2
    exit "$rc"
  fi
  if (( start_rc )); then
    echo "ERRO: banco restaurado, mas a retomada de ($STOPPED) falhou ($start_rc) — verifique 'compose ps' antes de usar" >&2
    exit "$start_rc"
  fi
  if (( DUMP_APPLIED )); then
    echo "restore concluído de $DUMP — banco restaurado e serviços retomados"
  fi
  exit 0
}
trap cleanup EXIT
on_signal() {
  echo "interrompido por $1 (etapa: $STAGE)" >&2
  exit "$2"   # dispara o cleanup do EXIT com o código do sinal
}
trap 'on_signal SIGHUP 129' HUP
trap 'on_signal SIGINT 130' INT
trap 'on_signal SIGTERM 143' TERM

# --- parada + confirmação com prazo ---------------------------------
STAGE="parada"
if [[ -n "$STOPPED" ]]; then
  compose stop $STOPPED || {
    rc=$?
    echo "FALHA ($rc): não consegui parar escritores (etapa: $STAGE)" >&2
    exit "$rc"
  }
fi

STAGE="verificação"
writers_down() {
  local now
  now="$(running_services)" || return 2
  ! grep -Eqx 'api|web' <<< "$now"
}
deadline=$((SECONDS + 15))
verify_rc=1
while (( SECONDS <= deadline )); do
  wrc=0; writers_down || wrc=$?
  (( wrc == 0 )) && { verify_rc=0; break; }
  (( wrc == 2 )) && { verify_rc=2; break; }
  sleep 1
done
case $verify_rc in
  0) ;;
  2) echo "FALHA: não consegui confirmar estado dos escritores (etapa: $STAGE)" >&2; exit 1 ;;
  *) echo "FALHA: escritores ainda ativos após parada (etapa: $STAGE) — dump não aplicado" >&2; exit 1 ;;
esac

# --- aplicação ------------------------------------------------------
STAGE="aplicação"
restore_rc=0
cat "$DUMP" | compose exec -T db pg_restore -U "$POSTGRES_USER" \
  -d "$POSTGRES_DB" --clean --if-exists --no-owner \
  --single-transaction --exit-on-error || restore_rc=$?
(( restore_rc )) && {
  echo "FALHA ($restore_rc): restauração abortada — transação única desfez tudo; o banco anterior está preservado" >&2
  exit "$restore_rc"; }
DUMP_APPLIED=1
exit 0
