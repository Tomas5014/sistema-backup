#!/usr/bin/env bash
# Valida configuração e sobe a stack identificada.
set -euo pipefail
source "$(dirname "$0")/_lib.sh"
cd "$CENTRAL_DIR"

require_file .env
require_file compose.yaml
require_file secrets/postgres.env
require_file secrets/api.env
require_file tls/cert.pem
require_file tls/key.pem
check_secret_perms secrets/postgres.env
check_secret_perms secrets/api.env
check_secret_perms tls/key.pem

set -a; source .env; set +a
require_var CENTRAL_HTTPS_PORT .env
source secrets/postgres.env
require_var POSTGRES_USER secrets/postgres.env
require_var POSTGRES_DB secrets/postgres.env
require_var POSTGRES_PASSWORD secrets/postgres.env
source secrets/api.env
require_var MACIP_API_DATABASE_URL api.env
require_var MACIP_API_ADMIN_TOKEN api.env
[[ "$MACIP_API_DATABASE_URL" == *"$POSTGRES_PASSWORD"* ]] || {
  echo "AVISO: senha do banco em api.env difere de postgres.env" >&2; }
[[ "${MACIP_API_SESSION_INSECURE_COOKIE:-}" == "1" ]] && {
  echo "ERRO: SESSION_INSECURE_COOKIE não pode nesta stack HTTPS" >&2; exit 1; }

compose build
compose up -d
echo "stack $PROJECT iniciada — verifique com scripts/check.sh"
