#!/usr/bin/env bash
# Verifica a stack: TLS com verificação REAL de certificado+hostname,
# /readyz (API→banco) e processos. Diagnóstico, não prova formal.
set -euo pipefail
source "$(dirname "$0")/_lib.sh"
cd "$CENTRAL_DIR"
set -a; source .env 2>/dev/null || true; set +a
PORT="${CENTRAL_HTTPS_PORT:-8443}"
BIND="${CENTRAL_BIND:-127.0.0.1}"
HOST="${CENTRAL_HOSTNAME:-localhost}"

# argumentos sempre separados e citados — caminho com espaço funciona
CA_ARG=()
if [[ -n "${CENTRAL_CA:-}" ]]; then
  CA_ARG=(--CAfile "$CENTRAL_CA"); C_CACERT=(--cacert "$CENTRAL_CA")
elif [[ -f "$CENTRAL_DIR/tls/lab-ca.pem" ]]; then
  CA_ARG=(--CAfile "$CENTRAL_DIR/tls/lab-ca.pem")
  C_CACERT=(--cacert "$CENTRAL_DIR/tls/lab-ca.pem")
else
  C_CACERT=()
fi

echo "== containers ($PROJECT) =="
compose ps
echo "== TLS (verificação de cadeia + hostname) =="
if ! echo | openssl s_client -connect "$BIND:$PORT" -servername "$HOST" \
      "${CA_ARG[@]}" -verify_hostname "$HOST" -brief 2>&1 \
      | grep -q "CONNECTION ESTABLISHED"; then
  echo "FALHA: handshake TLS/verificação em $HOST:$PORT" >&2; exit 1
fi
echo "TLS ok em https://$HOST:$PORT"
echo "== /readyz (API→banco) =="
curl --fail -sS "${C_CACERT[@]}" "https://$HOST:$PORT/readyz" && echo
echo "== portal =="
curl --fail -sS -o /dev/null -w "static: %{http_code}\n" \
  "${C_CACERT[@]}" "https://$HOST:$PORT/"
