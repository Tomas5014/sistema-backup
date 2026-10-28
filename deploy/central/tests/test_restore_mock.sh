#!/usr/bin/env bash
# restore.sh com Docker + pg_restore SIMULADOS: confirmação,
# validação, isolamento de escritas, restauração de estado.
set -u
TESTS_DIR="$(cd "$(dirname "$0")" && pwd)"
CENTRAL_REAL="$(cd "$TESTS_DIR/.." && pwd)"
source "$TESTS_DIR/_harness.sh"

CENTRAL="$WORK/central"
stage_central "$CENTRAL"
write_secrets "$CENTRAL" "" "" "pw-a" "pw-a"
write_tls "$CENTRAL"
write_env "$CENTRAL" ""

STUB_DIR="$WORK/stubbin"; mkdir -p "$STUB_DIR"
stub_docker "$STUB_DIR"; stub_pg_restore "$STUB_DIR"
export STUB_LOG="$WORK/docker.log"; : > "$STUB_LOG"
export PATH="$STUB_DIR:$PATH"

echo "== restore.sh (docker simulado) =="
echo "FAKE-DUMP" > "$WORK/dump.pgdump"

# 1) confirmação exigida: "não" aborta sem chamar pg_restore no banco
: > "$STUB_LOG"
set +e
printf 'não\n' | "$CENTRAL/scripts/restore.sh" "$WORK/dump.pgdump" >/dev/null 2>&1
rc=$?
set -e
expect_eq "confirmação negada aborta" "$rc" "1"
expect_eq "sem pg_restore no banco" \
  "$(grep -c 'pg_restore' "$STUB_LOG" || true)" "0"

# 2) arquivo inexistente e vazio recusados
set +e
"$CENTRAL/scripts/restore.sh" "$WORK/nao-existe.pgdump" >/dev/null 2>&1
rc1=$?
: > "$WORK/vazio.pgdump"
printf 'sim\n' | "$CENTRAL/scripts/restore.sh" "$WORK/vazio.pgdump" >/dev/null 2>&1
rc2=$?
set -e
[[ $rc1 -ne 0 ]] && ok "arquivo ausente recusado" || bad "arquivo ausente recusado"
[[ $rc2 -ne 0 ]] && ok "arquivo vazio recusado" || bad "arquivo vazio recusado"

# 3) sucesso: api+web parados, flags atômicas, serviços reiniciados
: > "$STUB_LOG"
export STUB_RUNNING_SERVICES="api web db"
export STUB_PGRESTORE_RC=0
printf 'sim\n' | "$CENTRAL/scripts/restore.sh" \
  "$WORK/dump.pgdump" >/dev/null 2>&1
rc=$?
expect_eq "restore sucesso retorna 0" "$rc" "0"
expect_contains "api+web parados antes" "$(cat "$STUB_LOG")" "stop api web"
expect_contains "transação única" "$(cat "$STUB_LOG")" "single-transaction"
expect_contains "aborta na 1ª falha" "$(cat "$STUB_LOG")" "exit-on-error"
expect_contains "serviços religados" "$(cat "$STUB_LOG")" "start api web"
# ordem: stop ANTES de pg_restore, start DEPOIS
stop_line="$(grep -n 'stop api web' "$STUB_LOG" | cut -d: -f1)"
rst_line="$(grep -n 'pg_restore' "$STUB_LOG" | tail -1 | cut -d: -f1)"
start_line="$(grep -n 'start api web' "$STUB_LOG" | cut -d: -f1)"
[[ $stop_line -lt $rst_line && $rst_line -lt $start_line ]] \
  && ok "ordem stop→restore→start" || bad "ordem stop→restore→start"

# 4) falha: código preservado, serviços religados mesmo assim
: > "$STUB_LOG"
export STUB_RUNNING_SERVICES="api web db"
set +e
export STUB_PGRESTORE_RC=4
printf 'sim\n' | "$CENTRAL/scripts/restore.sh" \
  "$WORK/dump.pgdump" >/dev/null 2>&1
rc=$?
set -e
expect_eq "código de falha preservado" "$rc" "4"
expect_contains "serviços religados após falha" "$(cat "$STUB_LOG")" "start api web"

# 5) serviço parado não é religado: web já estava down
: > "$STUB_LOG"
export STUB_RUNNING_SERVICES="api db"   # web já parado
export STUB_PGRESTORE_RC=0
printf 'sim\n' | "$CENTRAL/scripts/restore.sh" \
  "$WORK/dump.pgdump" >/dev/null 2>&1
expect_contains "só api estava de pé → stop api" "$(cat "$STUB_LOG")" "stop api"
grep -q "stop api web" "$STUB_LOG" && bad "web parada não parada de novo" \
  || ok "web parada não parada de novo"
expect_contains "só api religada" "$(cat "$STUB_LOG")" "start api"
grep -q "start api web" "$STUB_LOG" && bad "web parada não religada" \
  || ok "web parada não religada"

echo "-- restore mock: $PASS ok, $FAIL falhas"
(( FAIL == 0 ))
