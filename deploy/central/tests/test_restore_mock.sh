#!/usr/bin/env bash
# restore.sh com Docker SIMULADO: validação pelo contêiner (sem
# pg_restore no host), confirmação, isolamento de escritas e as 4
# combinações restauração×retomada — sem pg_restore instalado no host
# (o stub docker absorve TODAS as chamadas).
set -u
TESTS_DIR="$(cd "$(dirname "$0")" && pwd)"
CENTRAL_REAL="$(cd "$TESTS_DIR/.." && pwd)"
source "$TESTS_DIR/_harness.sh"

CENTRAL="$WORK/central"
stage_central "$CENTRAL"
write_secrets "$CENTRAL" "" "" "pw-a" "pw-a"
write_tls "$CENTRAL"
write_env "$CENTRAL" ""

STUB_DIR="$WORK/stubbin"; mkdir -p "$STUB_DIR"; stub_docker "$STUB_DIR"
export STUB_LOG="$WORK/docker.log"; : > "$STUB_LOG"
export PATH="$STUB_DIR:$PATH"   # sem pg_restore no host
export STUB_RUNNING_SERVICES="api web db"
export STUB_L_RC=0 STUB_PGRESTORE_RC=0 STUB_START_RC=0

echo "== restore.sh (docker simulado) =="
echo "FAKE-DUMP" > "$WORK/dump.pgdump"

# 1) confirmação obrigatória
: > "$STUB_LOG"
set +e
printf 'não\n' | "$CENTRAL/scripts/restore.sh" "$WORK/dump.pgdump" >/dev/null 2>&1
rc=$?
set -e
expect_eq "confirmação negada aborta" "$rc" "1"
expect_eq "sem restauração no banco" \
  "$(grep 'pg_restore' "$STUB_LOG" | grep -vc 'pg_restore -l')" "0"

# 2) arquivo ausente/vazio recusados ANTES de qualquer validação/docker
set +e
"$CENTRAL/scripts/restore.sh" "$WORK/nao-existe.pgdump" >/dev/null 2>&1
rc1=$?
: > "$WORK/vazio.pgdump"
printf 'sim\n' | "$CENTRAL/scripts/restore.sh" "$WORK/vazio.pgdump" >/dev/null 2>&1
rc2=$?
set -e
[[ $rc1 -ne 0 ]] && ok "arquivo ausente recusado" || bad "arquivo ausente recusado"
[[ $rc2 -ne 0 ]] && ok "arquivo vazio recusado" || bad "arquivo vazio recusado"

# 3) dump inválido (pg_restore -l do CONTÊINER rejeita) → não para serviços
: > "$STUB_LOG"
export STUB_L_RC=3
set +e
out="$(printf 'sim\n' | "$CENTRAL/scripts/restore.sh" "$WORK/dump.pgdump" 2>&1)"
rc=$?
set -e
expect_eq "dump inválido recusado" "$rc" "1"
expect_contains "diagnóstico cita validação" "$out" "validação"
expect_eq "serviços NÃO parados" "$(grep -c 'stop ' "$STUB_LOG" || true)" "0"
export STUB_L_RC=0

# 4) contêiner indisponível ≠ dump corrompido
: > "$STUB_LOG"
export STUB_RUNNING_SERVICES="api web"   # db fora do ar
set +e
out="$(printf 'sim\n' | "$CENTRAL/scripts/restore.sh" "$WORK/dump.pgdump" 2>&1)"
rc=$?
set -e
expect_eq "db indisponível aborta" "$rc" "1"
expect_contains "diagnóstico cita infraestrutura" "$out" "infraestrutura"
expect_eq "sem chamada de restauração" \
  "$(grep -c 'pg_restore' "$STUB_LOG" || true)" "0"
export STUB_RUNNING_SERVICES="api web db"

# 5) sucesso + retomada ok → 0, na ordem stop→restore→start
: > "$STUB_LOG"
printf 'sim\n' | "$CENTRAL/scripts/restore.sh" "$WORK/dump.pgdump" >/dev/null 2>&1
rc=$?
expect_eq "restauração+retomada ok = 0" "$rc" "0"
expect_contains "api+web parados" "$(cat "$STUB_LOG")" "stop api web"
expect_contains "transação única" "$(cat "$STUB_LOG")" "single-transaction"
expect_contains "aborta na 1ª falha" "$(cat "$STUB_LOG")" "exit-on-error"
expect_contains "serviços religados" "$(cat "$STUB_LOG")" "start api web"
stop_line="$(grep -n 'stop api web' "$STUB_LOG" | cut -d: -f1)"
rst_line="$(grep -n 'pg_restore' "$STUB_LOG" | tail -1 | cut -d: -f1)"
start_line="$(grep -n 'start api web' "$STUB_LOG" | cut -d: -f1)"
[[ $stop_line -lt $rst_line && $rst_line -lt $start_line ]] \
  && ok "ordem stop→restore→start" || bad "ordem stop→restore→start"
# validação -l ANTES da confirmação/parada
val_line="$(grep -n 'pg_restore -l' "$STUB_LOG" | cut -d: -f1)"
[[ $val_line -lt $stop_line ]] && ok "validação antes de parar" \
  || bad "validação antes de parar"

# 6) restauração ok + retomada FALHA → não zero, diagnóstico explícito
: > "$STUB_LOG"
export STUB_START_RC=7
set +e
out="$(printf 'sim\n' | "$CENTRAL/scripts/restore.sh" "$WORK/dump.pgdump" 2>&1)"
rc=$?
set -e
expect_eq "retomada falha → retorno não zero" "$rc" "7"
expect_contains "distingue banco restaurado de retomada" "$out" "banco restaurado"
case "$out" in *"concluído"*) bad "não anuncia recuperação completa";; \
  *) ok "não anuncia recuperação completa";; esac
export STUB_START_RC=0

# 7) restauração falha + retomada ok → código original preservado
: > "$STUB_LOG"
export STUB_PGRESTORE_RC=4
set +e
out="$(printf 'sim\n' | "$CENTRAL/scripts/restore.sh" "$WORK/dump.pgdump" 2>&1)"
rc=$?
set -e
expect_eq "código da restauração preservado" "$rc" "4"
expect_contains "serviços religados após falha" "$(cat "$STUB_LOG")" "start api web"

# 8) AMBOS falham → erro principal preservado + falha de retomada citada
: > "$STUB_LOG"
export STUB_PGRESTORE_RC=4 STUB_START_RC=9
set +e
out="$(printf 'sim\n' | "$CENTRAL/scripts/restore.sh" "$WORK/dump.pgdump" 2>&1)"
rc=$?
set -e
expect_eq "ambos falham → código da restauração" "$rc" "4"
expect_contains "retomada também citada" "$out" "retomada"
export STUB_PGRESTORE_RC=0 STUB_START_RC=0

# 9) serviço parado fica parado
: > "$STUB_LOG"
export STUB_RUNNING_SERVICES="api db"   # web já parado
printf 'sim\n' | "$CENTRAL/scripts/restore.sh" "$WORK/dump.pgdump" >/dev/null 2>&1
expect_contains "só api estava de pé → stop api" "$(cat "$STUB_LOG")" "stop api"
grep -q "stop api web" "$STUB_LOG" && bad "web parada parada de novo" \
  || ok "web parada não parada de novo"
grep -q "start api web" "$STUB_LOG" && bad "web parada não religada" \
  || ok "web parada não religada"
export STUB_RUNNING_SERVICES="api web db"

# 10) falha na identificação do estado aborta ANTES da aplicação
: > "$STUB_LOG"
export STUB_PS_RC=2
set +e
out="$(printf 'sim\n' | "$CENTRAL/scripts/restore.sh" "$WORK/dump.pgdump" 2>&1)"
rc=$?
set -e
expect_eq "estado indetectável aborta" "$rc" "1"
expect_contains "diagnóstico cita estado" "$out" "estado"
expect_eq "sem chamada de restauração" \
  "$(grep -c 'pg_restore' "$STUB_LOG" || true)" "0"
unset STUB_PS_RC

echo "-- restore mock: $PASS ok, $FAIL falhas"
(( FAIL == 0 ))
