#!/usr/bin/env bash
# Valida configuração e sobe a stack identificada.
set -euo pipefail
source "$(dirname "$0")/_lib.sh"
cd "$CENTRAL_DIR"

# configuração PRIMEIRO — TLS pode vir de caminhos configurados
require_file .env
require_file compose.yaml
set -a; source .env; set +a
require_var CENTRAL_HTTPS_PORT .env

# arquivos secretos sempre no lugar previsto
require_file secrets/postgres.env
require_file secrets/api.env
check_secret_perms secrets/postgres.env
check_secret_perms secrets/api.env

# TLS: valida EXATAMENTE os caminhos que o Compose vai montar
TLS_CERT="$(resolve_path "${CENTRAL_TLS_CERT:-./tls/cert.pem}")"
TLS_KEY="$(resolve_path "${CENTRAL_TLS_KEY:-./tls/key.pem}")"
require_file "$TLS_CERT"            # certificado público: precisa existir
require_file "$TLS_KEY"
check_secret_perms "$TLS_KEY"       # chave privada efetivamente usada

source secrets/postgres.env
require_var POSTGRES_USER secrets/postgres.env
require_var POSTGRES_DB secrets/postgres.env
require_var POSTGRES_PASSWORD secrets/postgres.env
source secrets/api.env
require_var MACIP_API_DATABASE_URL api.env
require_var MACIP_API_ADMIN_TOKEN api.env

[[ "${MACIP_API_SESSION_INSECURE_COOKIE:-}" == "1" ]] && {
  echo "ERRO: SESSION_INSECURE_COOKIE não pode nesta stack HTTPS" >&2; exit 1; }

# a senha deve ser IGUAL ao componente senha da URL — divergência é
# erro ANTES de qualquer chamada ao Docker (não imprime segredos)
URL_PW="$(url_password "$MACIP_API_DATABASE_URL")"
[[ -n "$URL_PW" && "$URL_PW" == "$POSTGRES_PASSWORD" ]] || {
  echo "ERRO: senha em api.env (MACIP_API_DATABASE_URL) não corresponde a POSTGRES_PASSWORD — corrija secrets/ antes de subir" >&2
  exit 1; }

compose build
compose up -d
echo "stack $PROJECT iniciada — verifique com scripts/check.sh"
