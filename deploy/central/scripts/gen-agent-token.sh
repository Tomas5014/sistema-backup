#!/usr/bin/env bash
# Cria a credencial do agente UMA vez e grava secrets/agent-token.env
# (600) — a resposta nunca é impressa; arquivo existente é preservado.
#
# Uso: scripts/gen-agent-token.sh <agent-uuid> [label]
# O UUID vem do cadastro do agente (portal ou GET /api/v1/agents).
set -euo pipefail
source "$(dirname "$0")/_lib.sh"
cd "$CENTRAL_DIR"

AGENT="${1:?uso: gen-agent-token.sh <agent-uuid> [label]}"
LABEL="${2:-lab}"
F=secrets/agent-token.env
[[ -e "$F" ]] && {
  echo "ERRO: $F já existe — remova manualmente se quiser regenerar" >&2
  exit 1; }

# body vai só para a variável — nunca para a tela; '# HTTP' do helper
# segue em stderr como evidência de status
RESP="$(bash "$CENTRAL_DIR/scripts/lab-curl.sh" admin POST \
  "/api/v1/agents/$AGENT/credentials" "{\"label\":\"$LABEL\"}")" || {
  echo "ERRO: criação de credencial falhou — $F não gravado" >&2; exit 1; }

umask 077
printf '%s' "$RESP" | python3 -c \
  'import json,sys; print("MACIP_AGENT_TOKEN="+json.load(sys.stdin)["token"])' \
  > "$F" || { rm -f "$F"; echo "ERRO: resposta sem token válido" >&2; exit 1; }
echo "credencial do agente gravada em $F (600) — token não exibido."
