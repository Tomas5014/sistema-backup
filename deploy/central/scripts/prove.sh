#!/usr/bin/env bash
# Prova integrada REAL da stack HTTPS: provisiona operador (CLI),
# cliente/agente/credencial e execução (Bearer admin + Bearer do
# agente), prepara HOME temporário com a CA de laboratório confiável
# e dirige um navegador real pela interface.
#
# Nada é instalado na confiança do SO: o nssdb fica num HOME
# temporário usado só pelo Chrome do teste. Requer `certutil`
# (libnss3-tools) ou CERTUTIL=<caminho>.
#
# Uso: MACIP_HOMOLOG_AGENT=0|1 scripts/prove.sh
set -euo pipefail
source "$(dirname "$0")/_lib.sh"
cd "$CENTRAL_DIR"
REPO_ROOT="$(cd "$CENTRAL_DIR/../.." && pwd)"

require_file .env
require_file secrets/api.env
set -a; source .env; set +a
PORT="${CENTRAL_HTTPS_PORT:-8443}"
HOST="${CENTRAL_HOSTNAME:-localhost}"
BASE="https://$HOST:$PORT"
CA="$CENTRAL_DIR/tls/lab-ca.pem"
require_file "$CA"
CERTUTIL="${CERTUTIL:-$(command -v certutil || true)}"
[[ -x "$CERTUTIL" || -n "$CERTUTIL" ]] || {
  echo "ERRO: certutil ausente (libnss3-tools) — confiança do browser" >&2; exit 1; }

# TLS já deve estar íntegro ANTES de qualquer provisionamento
curl --fail -sS --cacert "$CA" "$BASE/readyz" >/dev/null || {
  echo "ERRO: stack não está saudável — rode scripts/check.sh" >&2; exit 1; }

# --- provisionamento (senha gerada aqui; nunca impressa) ------------
STAMP="$(date +%s)"
OP_LOGIN="homolog-$STAMP"
OP_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
set -a; source secrets/api.env; set +a   # MACIP_API_ADMIN_TOKEN (não exibido)

echo "== operador $OP_LOGIN via CLI (senha por stdin) =="
printf '%s\n' "$OP_PASSWORD" | compose exec -T api \
  python -m macip_backup_api.operator_cli create \
    --login "$OP_LOGIN" --name "Operador Homolog" --role admin \
    --password-stdin >/dev/null

ADMIN_H=(-H "Authorization: Bearer $MACIP_API_ADMIN_TOKEN" -H "Content-Type: application/json")
post() { # path body [bearer]
  local bearer="${3:-$MACIP_API_ADMIN_TOKEN}"
  curl --fail -sS --cacert "$CA" -X POST "$BASE$1" \
    -H "Authorization: Bearer $bearer" -H "Content-Type: application/json" \
    -d "$2"
}

echo "== cliente/agente/credencial/execução (Bearer admin + agente) =="
CLIENT_ID="$(post /api/v1/clients "{\"name\": \"Cliente Homolog $STAMP\"}" | python3 -c 'import json,sys;print(json.load(sys.stdin)["id"])')"
AGENT_ID="$(post "/api/v1/clients/$CLIENT_ID/agents" "{\"name\": \"agente-homolog-$STAMP\"}" | python3 -c 'import json,sys;print(json.load(sys.stdin)["id"])')"
AGENT_TOKEN="$(post "/api/v1/agents/$AGENT_ID/credentials" '{"label":"homolog"}' | python3 -c 'import json,sys;print(json.load(sys.stdin)["token"])')"
TASK="task-homolog-$STAMP"
post /api/v1/agent/executions '{
  "execution_id": "exec-homolog-'"$STAMP"'",
  "task_id": "'"$TASK"'",
  "status": "success",
  "started_at_utc": "2026-10-27T10:00:00Z",
  "finished_at_utc": "2026-10-27T10:05:00Z",
  "destinations": [{
    "destination_id": "dest-homolog",
    "status": "success",
    "started_at_utc": "2026-10-27T10:00:00Z",
    "finished_at_utc": "2026-10-27T10:04:00Z",
    "snapshot_id": "snapshot-homolog",
    "attempts": [{
      "attempt": 1, "status": "success",
      "started_at_utc": "2026-10-27T10:00:00Z",
      "finished_at_utc": "2026-10-27T10:04:00Z",
      "duration_seconds": 240, "snapshot_id": "snapshot-homolog",
      "files_processed": 42, "bytes_processed": 1048576
    }]
  }]
}' "$AGENT_TOKEN" >/dev/null

# --- fluxo opcional: agente real descartável → outbox → sync HTTPS --
AGENT_TASK=""
if [[ "${MACIP_HOMOLOG_AGENT:-0}" == "1" ]] && command -v restic >/dev/null; then
  echo "== agente local descartável: backup → outbox → sync HTTPS =="
  WORK="$(mktemp -d /tmp/macip-homolog-XXXX)"
  trap 'rm -rf "$WORK"' EXIT
  mkdir -p "$WORK/dados" "$WORK/repo"
  echo "conteúdo homologação $STAMP" > "$WORK/dados/arquivo.txt"
  cat > "$WORK/task.json" <<EOT
{"id": "agente-task-$STAMP", "name": "Agente homolog",
 "client": {"id": "cliente-homolog", "name": "Homolog"},
 "device_id": "homolog-dev",
 "sources": ["$WORK/dados"],
 "destinations": [{"id": "repo-local", "name": "local", "kind": "local",
   "repository": "$WORK/repo",
   "password_environment": "MACIP_HOMOLOG_RESTIC_PW"}]}
EOT
  cat > "$WORK/central.json" <<EOT
{"url": "$BASE", "agent_id": "$AGENT_ID",
 "token_environment": "MACIP_HOMOLOG_TOKEN"}
EOT
  export MACIP_HOMOLOG_RESTIC_PW="pw-local-descartavel-$STAMP"
  export MACIP_HOMOLOG_TOKEN="$AGENT_TOKEN"
  export SSL_CERT_FILE="$CA"   # urllib do agente confia na CA explicitamente
  PYTHONPATH="$REPO_ROOT/src" python3 -m macip_backup.cli \
    agent "$WORK/task.json" --store "$WORK/agent.db" --central "$WORK/central.json"
  PYTHONPATH="$REPO_ROOT/src" python3 -m macip_backup.cli \
    sync --store "$WORK/agent.db" --central "$WORK/central.json"
  AGENT_TASK="agente-task-$STAMP"
elif [[ "${MACIP_HOMOLOG_AGENT:-0}" == "1" ]]; then
  echo "AVISO: MACIP_HOMOLOG_AGENT=1 mas restic ausente — fluxo pulado" >&2
fi

# --- HOME temporário com a CA no nssdb (confiança explícita) --------
HOMOLOG_HOME="$(mktemp -d /tmp/macip-homolog-home-XXXX)"
mkdir -p "$HOMOLOG_HOME/.pki/nssdb"
"$CERTUTIL" -N -d "sql:$HOMOLOG_HOME/.pki/nssdb" --empty-password
"$CERTUTIL" -A -n macip-lab-ca -t "CT,C,C" \
  -d "sql:$HOMOLOG_HOME/.pki/nssdb" -i "$CA"

echo "== navegador real via Playwright (HOME=$HOMOLOG_HOME) =="
cd "$REPO_ROOT/portal"
env \
  MACIP_HOMOLOG_URL="$BASE" \
  MACIP_HOMOLOG_LOGIN="$OP_LOGIN" \
  MACIP_HOMOLOG_PASSWORD="$OP_PASSWORD" \
  MACIP_HOMOLOG_TASK="$TASK" \
  MACIP_HOMOLOG_AGENT_TASK="$AGENT_TASK" \
  MACIP_HOMOLOG_HOME="$HOMOLOG_HOME" \
  MACIP_HOMOLOG_COMPOSE="docker compose --project-directory $CENTRAL_DIR -f $CENTRAL_DIR/compose.yaml restart" \
  NODE_EXTRA_CA_CERTS="$CA" \
  npx playwright test e2e/homologation.spec.ts

echo "prova integrada concluída"
