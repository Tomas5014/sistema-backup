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

# Permissão restrita por BITS: grupo/outros sem nenhum acesso.
# 600/400 passam; 444/511/640/644/604 são recusados (antes, a
# comparação numérica com 600 aceitava modos como 444).
check_secret_perms() {
  local file="$1" mode
  mode="$(stat -c %a "$file")"
  if (( 8#$mode & 8#077 )); then
    echo "ERRO: $file com permissão $mode dá acesso a grupo/outros — rode chmod 600" >&2
    exit 1
  fi
}

# Resolve caminho configurado: absoluto vence; relativo é ao projeto.
resolve_path() {
  case "$1" in
    /*) printf '%s\n' "$1" ;;
    *)  printf '%s/%s\n' "$CENTRAL_DIR" "$1" ;;
  esac
}

# Extrai APENAS o componente senha da URL (decodificado) para comparar
# com a senha do postgres.env — nunca imprime URL, senha ou token.
url_password() {
  python3 - "$1" <<'PY'
import sys, urllib.parse
print(urllib.parse.unquote(urllib.parse.urlsplit(sys.argv[1]).password or ""))
PY
}
