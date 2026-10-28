#!/usr/bin/env bash
# backup.sh com Docker SIMULADO: atomicidade, sobrescrita, limpeza.
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
export STUB_LOG="$WORK/docker.log"
export PATH="$STUB_DIR:$PATH"

echo "== backup.sh (docker simulado) =="

# 1) sucesso: publica, conteúdo do dump, modo 600
STUB_PGDUMP_MODE=ok "$CENTRAL/scripts/backup.sh" \
  "$CENTRAL/backups/ok.pgdump" >/dev/null
rc=$?
expect_eq "backup sucesso retorna 0" "$rc" "0"
expect_file "dump publicado" "$CENTRAL/backups/ok.pgdump"
expect_eq "conteúdo é o do pg_dump" \
  "$(cat "$CENTRAL/backups/ok.pgdump")" "PGDUMP-BYTES-9f8e7d6c"
expect_eq "dump final é 600" "$(stat -c %a "$CENTRAL/backups/ok.pgdump")" "600"

# 2) saída vazia: falha, sem arquivo, sem temporário
set +e
STUB_PGDUMP_MODE=empty "$CENTRAL/scripts/backup.sh" \
  "$CENTRAL/backups/empty.pgdump" >/dev/null 2>&1
rc=$?
set -e
[[ $rc -ne 0 ]] && ok "dump vazio falha" || bad "dump vazio falha"
expect_no_file "vazio não publica" "$CENTRAL/backups/empty.pgdump"
expect_eq "sem temporário após vazio" \
  "$(find "$CENTRAL/backups" -name '.pgdump.*' | wc -l)" "0"

# 3) falha após escrita parcial: código original, sem arquivo, limpa tmp
set +e
STUB_PGDUMP_MODE=partial "$CENTRAL/scripts/backup.sh" \
  "$CENTRAL/backups/parcial.pgdump" >/dev/null 2>&1
rc=$?
set -e
expect_eq "código de erro preservado" "$rc" "3"
expect_no_file "parcial não publica" "$CENTRAL/backups/parcial.pgdump"
expect_eq "sem temporário após falha" \
  "$(find "$CENTRAL/backups" -name '.pgdump.*' | wc -l)" "0"

# 4) destino preexistente: recusa SEM chamar docker, bytes preservados
echo "DUMP-ANTIGO-BYTES" > "$CENTRAL/backups/antigo.pgdump"
chmod 600 "$CENTRAL/backups/antigo.pgdump"
md5_before="$(md5sum "$CENTRAL/backups/antigo.pgdump" | cut -d' ' -f1)"
: > "$STUB_LOG"
set +e
"$CENTRAL/scripts/backup.sh" "$CENTRAL/backups/antigo.pgdump" >/dev/null 2>&1
rc=$?
set -e
expect_eq "preexistente falha" "$rc" "1"
expect_eq "docker NÃO foi chamado" "$(cat "$STUB_LOG" | wc -l)" "0"
expect_eq "dump anterior byte a byte idêntico" \
  "$(md5sum "$CENTRAL/backups/antigo.pgdump" | cut -d' ' -f1)" "$md5_before"

echo "-- backup: $PASS ok, $FAIL falhas"
(( FAIL == 0 ))
