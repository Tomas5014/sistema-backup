#!/usr/bin/env bash
# curl autenticado do laboratório: o Bearer vai por STDIN para
# 'curl -H @-' — nunca em argv, histórico de comando, log ou saída.
#
# Uso (rode de dentro de deploy/central — lê .env e secrets/ daqui):
#   scripts/lab-curl.sh admin GET  /api/v1/tasks
#   scripts/lab-curl.sh agent POST /api/v1/agent/executions '{"…"}'
#
# Credenciais provisionadas em arquivos 0600:
#   admin → secrets/api.env        (MACIP_API_ADMIN_TOKEN, do gen-secrets)
#   agent → secrets/agent-token.env (MACIP_AGENT_TOKEN — gravado pelo
#           passo de criação de credencial do roteiro, nunca digitado)
#
# TLS sempre verificado (--cacert, sem -k); redirecionamentos NÃO são
# seguidos (--max-redirs 0). Falha HTTP sai com código ≠0 e diagnóstico
# sem segredos (status + corpo da API).
set -euo pipefail
source "$(dirname "$0")/_lib.sh"
cd "$CENTRAL_DIR"

ROLE="${1:?uso: lab-curl.sh <admin|agent> <METHOD> <path> [json-body]}"
METHOD="${2:?falta METHOD}"
PATH_Q="${3:?falta path}"
BODY="${4:-}"

require_file .env
load_env .env
PORT="${CENTRAL_HTTPS_PORT:-8443}"
HOST="${CENTRAL_HOSTNAME:-localhost}"
CA="$(resolve_path "${CENTRAL_CA:-./tls/lab-ca.pem}")"
require_file "$CA"

case "$ROLE" in
  admin)
    require_file secrets/api.env
    check_secret_perms secrets/api.env
    TOKEN="$(load_env secrets/api.env; printf '%s' "${MACIP_API_ADMIN_TOKEN:?api.env sem MACIP_API_ADMIN_TOKEN}")"
    ;;
  agent)
    F=secrets/agent-token.env
    require_file "$F"
    check_secret_perms "$F"
    TOKEN="$(load_env "$F"; printf '%s' "${MACIP_AGENT_TOKEN:?agent-token.env sem MACIP_AGENT_TOKEN}")"
    ;;
  *) echo "ERRO: papel '$ROLE' inválido (admin|agent)" >&2; exit 1 ;;
esac
[[ -n "$TOKEN" ]] || { echo "ERRO: credencial $ROLE vazia" >&2; exit 1; }

RESP="$(mktemp)"; trap 'rm -f "$RESP"' EXIT
ARGS=(-sS --cacert "$CA" --max-redirs 0 -X "$METHOD"
      -H "Content-Type: application/json" -o "$RESP" -w '%{http_code}')
[[ -n "$BODY" ]] && ARGS+=(-d "$BODY")

# -H @-: o cabeçalho Authorization entra por stdin — ausente de argv
CODE="$(printf 'Authorization: Bearer %s\n' "$TOKEN" | curl "${ARGS[@]}" -H @- "https://$HOST:$PORT$PATH_Q")" || {
  echo "ERRO: requisição $METHOD $PATH_Q falhou (rede/TLS)" >&2; exit 1; }

cat "$RESP"   # corpo da API é evidência — não contém o Bearer
[[ "$CODE" =~ ^2[0-9][0-9]$ ]] || {
  printf '\nERRO: HTTP %s em %s %s\n' "$CODE" "$METHOD" "$PATH_Q" >&2; exit 1; }
printf '\n# HTTP %s\n' "$CODE" >&2
