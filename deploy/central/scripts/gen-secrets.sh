#!/usr/bin/env bash
# Gera secrets/{postgres,api}.env coerentes — senha única, gravada
# apenas nos arquivos (600), nunca impressa.
set -euo pipefail
source "$(dirname "$0")/_lib.sh"

cd "$CENTRAL_DIR"
for f in secrets/postgres.env secrets/api.env; do
  [[ -f "$f" ]] && { echo "ERRO: $f já existe — remova manualmente se quiser regenerar" >&2; exit 1; }
done

PASSWORD="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
PGUSER="${1:-macip_central}"
PGDB="${2:-macip_central}"

umask 077
cat > secrets/postgres.env <<EOT
POSTGRES_USER=$PGUSER
POSTGRES_DB=$PGDB
POSTGRES_PASSWORD=$PASSWORD
EOT
cat > secrets/api.env <<EOT
MACIP_API_DATABASE_URL=postgresql+psycopg://$PGUSER:$PASSWORD@db:5432/$PGDB
MACIP_API_ADMIN_TOKEN=$TOKEN
EOT
chmod 600 secrets/postgres.env secrets/api.env
echo "secrets/ gerado (600): postgres.env + api.env — senha coerente, não exibida."
