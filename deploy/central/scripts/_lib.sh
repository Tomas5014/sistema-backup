#!/usr/bin/env bash
# Comum dos scripts da stack — sem segredos na saída.
set -euo pipefail

CENTRAL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT="macip-central"

compose() {
  # -p explícito: o projeto alvo NUNCA depende de variáveis de ambiente
  # ou do campo name: — a identidade aprovada vem de PROJECT
  docker compose --project-directory "$CENTRAL_DIR" -p "$PROJECT" \
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

# ---- contexto aprovado vs. dados carregados --------------------------
# Chaves de CONTROLE definem PARA ONDE vão as chamadas: projeto,
# arquivo e diretório Compose, daemon Docker, PATH de binários e
# variáveis que alteram a execução de interpretadores. Arquivos de
# dados (.env, secrets/*.env, ENV_CFG) jamais podem declará-las —
# a recusa é por FAMÍLIA, não por lista fechada de nomes.
_CTX_FORBIDDEN='^(PROJECT|CENTRAL_DIR|PATH|HOME|IFS|CDPATH|ENV|BASH_ENV|PYTHONPATH|PYTHONHOME|NODE_OPTIONS|NODE_PATH|COMPOSE_[A-Za-z0-9_]*|DOCKER_[A-Za-z0-9_]*|LD_[A-Za-z0-9_]*)$'

# load_env <arquivo>: carregamento RESTRITO de dados —
#   * aceita SOMENTE linhas KEY=VALUE ('#'/vazias ignoradas; prefixo
#     'export ' tolerado); qualquer outra linha é recusada — um arquivo
#     de dados nunca executa código
#   * valor LITERAL: nada é expandido/substituído; aspas externas
#     simples ou duplas são removidas literalmente (senha com $, #, ;,
#     aspas ou $(...) sobrevive intacta)
#   * chave de controle declarada      → recusa antes de build/up
#   * chave já estabelecida (ambiente aprovado ou arquivo anterior)
#     com valor DIFERENTE              → recusa: seria redirecionamento
#     silencioso do contexto (porta, bind, caminho, credencial...)
#   * mesma chave com valor igual      → segue (idempotente)
#   * chave nova                       → absorvida como dado
# DOCKER_HOST/DOCKER_CONTEXT herdados continuam valendo: quem aprovou
# o destino escolheu o daemon; arquivo algum o troca depois.
load_env() {
  local f="$1" line key val n=0
  require_file "$f"
  while IFS= read -r line || [[ -n "$line" ]]; do
    n=$((n+1))
    line="${line%$'\r'}"
    line="${line#"${line%%[![:space:]]*}"}"
    case "$line" in ''|'#'*) continue ;; esac
    line="${line#export }"
    [[ "$line" =~ ^[A-Za-z_][A-Za-z0-9_]*= ]] || {
      echo "ERRO: $f:$n não é KEY=VALUE — arquivo de dados não executa código" >&2
      exit 1; }
    key="${line%%=*}"; val="${line#*=}"
    if ((${#val} >= 2)); then
      case "$val" in
        \"*\") val="${val:1:${#val}-2}" ;;
        \'*\') val="${val:1:${#val}-2}" ;;
      esac
    fi
    if [[ "$key" =~ $_CTX_FORBIDDEN ]]; then
      echo "ERRO: $f:$n declara $key — chave de controle do contexto não cabe em arquivo de dados" >&2
      exit 1
    fi
    if [[ ${!key+x} == x && "${!key}" != "$val" ]]; then
      echo "ERRO: $f:$n redefine $key com valor divergente — o contexto aprovado não muda por arquivo" >&2
      exit 1
    fi
    printf -v "$key" '%s' "$val"
    export "$key"
  done < "$f"
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
