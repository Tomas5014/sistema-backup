#!/usr/bin/env bash
# Para a stack preservando volumes operacionais (pgdata).
set -euo pipefail
source "$(dirname "$0")/_lib.sh"
cd "$CENTRAL_DIR"
compose down          # sem -v: dados de homologação preservados
echo "stack parada — volume pgdata preservado"
