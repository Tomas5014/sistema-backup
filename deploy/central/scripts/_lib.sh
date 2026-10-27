#!/usr/bin/env bash
# Comum dos scripts da stack — sem segredos na saída.
set -euo pipefail

CENTRAL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT="macip-central"

compose() {
  docker compose --project-directory "$CENTRAL_DIR" \
    -f "$CENTRAL_DIR/compose.yaml" "$@"
}

require_file() {
  [[ -f "$1" ]] || { echo "ERRO: falta $1 — ver docs/homologacao-central.md" >&2; exit 1; }
}

require_var() {
  [[ -n "${!1:-}" ]] || { echo "ERRO: variável $1 não definida em $2" >&2; exit 1; }
}

# permissão restrita é requisito — recusa arquivo legível por grupo/outros
check_secret_perms() {
  local mode
  mode="$(stat -c %a "$1")"
  [[ "$mode" -le 600 ]] || {
    echo "ERRO: $1 com permissão $mode — rode chmod 600" >&2; exit 1; }
}
