#!/usr/bin/env bash
# Grava a credencial do operador do portal para o C15 em JSON privado
# (600) — formato de dados com parser próprio: a senha NUNCA vira
# código shell (sem 'source', sem expansão; $, aspas e $(...) ficam
# literais). A spec portal/e2e/monitoring-fault.spec.ts lê o arquivo
# diretamente via MACIP_HOMOLOG_SECRETS.
#
# Uso: scripts/gen-operator-creds.sh <saida.json> [login]
#   A senha é digitada em prompt oculto — nunca em argv, histórico,
#   log ou saída. Limite real: a leitura é por linha, portanto a senha
#   não pode conter quebra de linha (demais caracteres são preservados
#   byte a byte no JSON).
set -euo pipefail
source "$(dirname "$0")/_lib.sh"

OUT="${1:?uso: gen-operator-creds.sh <saida.json> [login]}"
LOGIN="${2:-}"
[[ -e "$OUT" ]] && {
  echo "ERRO: $OUT já existe — remova manualmente se quiser regenerar" >&2
  exit 1; }
[[ -n "$LOGIN" ]] || read -r -p 'login do operador: ' LOGIN
# IFS= preserva espaços das bordas; -s não ecoa; readline = 1 linha
IFS= read -rs -p "senha de '$LOGIN': " PW; echo >&2

umask 077
LOGIN="$LOGIN" PW="$PW" python3 - "$OUT" <<'PY'
import json, os, sys
# 'x': criação exclusiva — arquivo privado existente NÃO é sobrescrito
try:
    with open(sys.argv[1], "x", encoding="utf-8") as f:
        json.dump({"login": os.environ["LOGIN"],
                   "password": os.environ["PW"]}, f)
except FileExistsError:
    sys.exit(f"ERRO: {sys.argv[1]} já existe — remova manualmente")
PY
unset PW
echo "credencial do operador gravada em $OUT (JSON 600) — senha não exibida."
