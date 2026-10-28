#!/usr/bin/env bash
# check_secret_perms: bits de grupo/outros definem aceite.
set -u
TESTS_DIR="$(cd "$(dirname "$0")" && pwd)"
CENTRAL_REAL="$(cd "$TESTS_DIR/.." && pwd)"
source "$TESTS_DIR/_harness.sh"
source "$CENTRAL_REAL/scripts/_lib.sh"

echo "== permissões por bits de acesso =="
for m in 600 400; do
  f="$WORK/secret-$m"; touch "$f"; chmod "$m" "$f"
  ( check_secret_perms "$f" ) 2>/dev/null \
    && ok "modo $m aceito" || bad "modo $m aceito"
done
for m in 444 511 644 640 604; do
  f="$WORK/secret-$m"; touch "$f"; chmod "$m" "$f"
  ( check_secret_perms "$f" ) 2>/dev/null \
    && bad "modo $m recusado" || ok "modo $m recusado"
done

# mensagem mostra caminho e orientação, sem conteúdo
f="$WORK/secret-644b"; echo "SEGREDO-X" > "$f"; chmod 644 "$f"
out="$(check_secret_perms "$f" 2>&1)" || true
expect_contains "erro mostra caminho" "$out" "$f"
expect_contains "erro orienta chmod" "$out" "chmod 600"
case "$out" in *SEGREDO-X*) bad "erro não imprime conteúdo";; *) ok "erro não imprime conteúdo";; esac

echo "-- perms: $PASS ok, $FAIL falhas"
(( FAIL == 0 ))
