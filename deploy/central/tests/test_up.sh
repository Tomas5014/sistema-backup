#!/usr/bin/env bash
# up.sh com Docker SIMULADO: senha divergente bloqueia antes do Docker,
# caminhos TLS configurados são respeitados.
set -u
TESTS_DIR="$(cd "$(dirname "$0")" && pwd)"
CENTRAL_REAL="$(cd "$TESTS_DIR/.." && pwd)"
source "$TESTS_DIR/_harness.sh"

STUB_DIR="$WORK/stubbin"; mkdir -p "$STUB_DIR"; stub_docker "$STUB_DIR"
export STUB_LOG="$WORK/docker.log"; : > "$STUB_LOG"
export PATH="$STUB_DIR:$PATH"

new_stage() { # cria projeto espelhado configurável
  local d="$WORK/$1"
  stage_central "$d"
  printf '%s' "$d"
}

echo "== up.sh: divergência de senha bloqueia antes do Docker =="

C1="$(new_stage c1)"
write_env "$C1" ""; write_secrets "$C1" "" "" "pw-real" "pw-real"; write_tls "$C1"
set +e; "$C1/scripts/up.sh" >/dev/null 2>&1; rc=$?; set -e
expect_eq "configuração coerente passa" "$rc" "0"
expect_contains "docker foi chamado (build)" "$(cat "$STUB_LOG")" "build"
expect_contains "docker foi chamado (up)" "$(cat "$STUB_LOG")" "up -d"

C2="$(new_stage c2)"
write_env "$C2" ""; write_secrets "$C2" "" "" "pw-real" "pw-ERRADA"; write_tls "$C2"
: > "$STUB_LOG"
set +e; "$C2/scripts/up.sh" >/dev/null 2>&1; rc=$?; set -e
[[ $rc -ne 0 ]] && ok "senha divergente recusada" || bad "senha divergente recusada"
expect_eq "docker NÃO foi chamado" "$(wc -l < "$STUB_LOG")" "0"

# senha esperada aparecendo em OUTRA parte da URL → ainda deve falhar
C3="$(new_stage c3)"
write_env "$C3" ""
cat > "$C3/secrets/postgres.env" <<'EOF'
POSTGRES_USER=macip_central
POSTGRES_DB=macip_central
POSTGRES_PASSWORD=pw-real
EOF
cat > "$C3/secrets/api.env" <<'EOF'
MACIP_API_DATABASE_URL=postgresql+psycopg://pw-real:outra@pw-real.host:5432/pw-real
MACIP_API_ADMIN_TOKEN=fake
EOF
chmod 600 "$C3/secrets/postgres.env" "$C3/secrets/api.env"
write_tls "$C3"
: > "$STUB_LOG"
set +e; out="$("$C3/scripts/up.sh" 2>&1)"; rc=$?; set -e
[[ $rc -ne 0 ]] && ok "substring na URL não é falso positivo" \
  || bad "substring na URL não é falso positivo"
expect_eq "docker NÃO foi chamado" "$(wc -l < "$STUB_LOG")" "0"
case "$out" in *"pw-real"*|*"outra"*) bad "diagnóstico não imprime senha";; *) ok "diagnóstico não imprime senha";; esac

echo "== up.sh: caminhos TLS configurados =="

# só caminhos PERSONALIZADOS presentes (padrões ausentes)
C4="$(new_stage c4)"
write_env "$C4" 'CENTRAL_TLS_CERT="./meus certs/cert.pem"
CENTRAL_TLS_KEY="./meus certs/key.pem"' 
write_secrets "$C4" "" "" "pw-real" "pw-real"
write_tls "$C4" "$C4/meus certs/cert.pem" "$C4/meus certs/key.pem"
: > "$STUB_LOG"
set +e; "$C4/scripts/up.sh" >/dev/null 2>&1; rc=$?; set -e
expect_eq "caminhos custom com espaço passam" "$rc" "0"
expect_contains "docker chamado" "$(cat "$STUB_LOG")" "build"

# caminho ABSOLUTO
C5="$(new_stage c5)"
ABS="$WORK/abs tls"; mkdir -p "$ABS"
write_env "$C5" "CENTRAL_TLS_CERT='$ABS/cert.pem'
CENTRAL_TLS_KEY='$ABS/key.pem'"
write_secrets "$C5" "" "" "pw-real" "pw-real"
write_tls "$C5" "$ABS/cert.pem" "$ABS/key.pem"
set +e; "$C5/scripts/up.sh" >/dev/null 2>&1; rc=$?; set -e
expect_eq "caminhos absolutos passam" "$rc" "0"

# arquivo configurado AUSENTE → erro
C6="$(new_stage c6)"
write_env "$C6" "CENTRAL_TLS_CERT=./ausente/cert.pem
CENTRAL_TLS_KEY=./ausente/key.pem"
write_secrets "$C6" "" "" "pw-real" "pw-real"
set +e; out="$("$C6/scripts/up.sh" 2>&1)"; rc=$?; set -e
[[ $rc -ne 0 ]] && ok "cert custom ausente recusado" || bad "cert custom ausente recusado"
expect_contains "erro mostra caminho ausente" "$out" "ausente/cert.pem"

# chave privada com permissão inadequada → erro
C7="$(new_stage c7)"
write_env "$C7" "CENTRAL_TLS_CERT=./tls2/cert.pem
CENTRAL_TLS_KEY=./tls2/key.pem"
write_secrets "$C7" "" "" "pw-real" "pw-real"
write_tls "$C7" "$C7/tls2/cert.pem" "$C7/tls2/key.pem"
chmod 644 "$C7/tls2/key.pem"
set +e; "$C7/scripts/up.sh" >/dev/null 2>&1; rc=$?; set -e
[[ $rc -ne 0 ]] && ok "chave 644 recusada" || bad "chave 644 recusada"

# configuração PADRÃO continua funcional
C8="$(new_stage c8)"
write_env "$C8" ""; write_secrets "$C8" "" "" "pw-real" "pw-real"; write_tls "$C8"
set +e; "$C8/scripts/up.sh" >/dev/null 2>&1; rc=$?; set -e
expect_eq "configuração padrão funciona" "$rc" "0"

echo "-- up/tls: $PASS ok, $FAIL falhas"
(( FAIL == 0 ))
