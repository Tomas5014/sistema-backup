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
CA="${CENTRAL_CA:-$CENTRAL_DIR/tls/lab-ca.pem}"

echo "== containers ($PROJECT) =="
compose ps
echo "== TLS (verificação de cadeia + hostname) =="
[[ -f "$CA" ]] && VCA="--cacert $CA" || VCA=""
# openssl s_client: verify_return_code + hostname check
if ! echo | openssl s_client -connect "$BIND:$PORT" -servername "$HOST" \
      ${VCA:+--CAfile "$CA"} -verify_hostname "$HOST" -brief 2>&1 | grep -q "CONNECTION ESTABLISHED"; then
  echo "FALHA: handshake TLS/verificação em $HOST:$PORT" >&2; exit 1
fi
echo "TLS ok em https://$HOST:$PORT"
echo "== /readyz (API→banco) =="
curl --fail -sS $VCA "https://$HOST:$PORT/readyz" && echo
echo "== portal =="
curl --fail -sS -o /dev/null -w "static: %{http_code}\n" $VCA "https://$HOST:$PORT/"
