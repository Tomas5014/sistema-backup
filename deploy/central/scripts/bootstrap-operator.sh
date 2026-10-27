#!/usr/bin/env bash
# Primeiro operador via CLI existente — senha por getpass, nunca argv.
# Uso: scripts/bootstrap-operator.sh <login> <nome> [admin|viewer]
set -euo pipefail
source "$(dirname "$0")/_lib.sh"
cd "$CENTRAL_DIR"
LOGIN="${1:?uso: bootstrap-operator.sh <login> <nome> [admin|viewer]}"
NAME="${2:?falta nome}"
ROLE="${3:-admin}"
compose exec api python -m macip_backup_api.operator_cli create \
  --login "$LOGIN" --name "$NAME" --role "$ROLE"
