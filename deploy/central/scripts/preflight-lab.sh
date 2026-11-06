#!/usr/bin/env bash
# Preflight de laboratório ISOLADO — BLOQUEIA em qualquer conflito ou
# estado não verificável. Consulta falhando NUNCA significa "livre".
#
# Fase 1 (antes de extrair qualquer pacote):
#   LAB_DIR=~/macip-lab-monitoramento LAB_PROJECT=macip-monlab \
#   LAB_PORT=19443 [LAB_BIND=127.0.0.1] scripts/preflight-lab.sh
#
# Fase 2 (depois de configurar a cópia, antes de subir):
#   ... scripts/preflight-lab.sh --post-config <caminho>/deploy/central
#
# Saída 0 = livre e coerente; qualquer outra saída INTERROMPE o roteiro.
set -euo pipefail

fail() { echo "ERRO: $*" >&2; exit 1; }
req()  { [[ -n "${!1:-}" ]] || fail "defina $1 (ex: $1=$2)"; }

req LAB_DIR '~/macip-lab-monitoramento'
req LAB_PROJECT 'macip-monlab'
req LAB_PORT '19443'
LAB_BIND="${LAB_BIND:-127.0.0.1}"
[[ "$LAB_PROJECT" =~ ^[a-z0-9-]+$ ]] \
  || fail "LAB_PROJECT inválido: '$LAB_PROJECT'"
[[ "$LAB_PORT" =~ ^[0-9]+$ && "$LAB_PORT" -ge 1 && "$LAB_PORT" -le 65535 ]] \
  || fail "LAB_PORT inválida: '$LAB_PORT'"

# overrides herdados mudariam projeto/arquivo silenciosamente
for v in COMPOSE_PROJECT_NAME COMPOSE_FILE DOCKER_CONTEXT; do
  [[ -z "${!v:-}" ]] || fail "$v exportado pode alterar o projeto Compose — desfaça antes"
done

check_port() {
  LAB_BIND="$LAB_BIND" LAB_PORT="$LAB_PORT" python3 - <<'PY' \
    || fail "porta $LAB_BIND:$LAB_PORT ocupada ou não verificável"
import os, socket, sys
try:
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
    s.bind((os.environ["LAB_BIND"], int(os.environ["LAB_PORT"])))
    s.close()
except OSError as e:
    sys.exit(1)
PY
}

check_docker() {
  docker info >/dev/null 2>&1 \
    || fail "docker info falhou — daemon não verificável, nada é 'livre'"

  local found
  found="$(docker ps -a -q --filter \
    "label=com.docker.compose.project=$LAB_PROJECT" 2>/dev/null)" \
    || fail "docker ps falhou — projeto não verificável"
  [[ -z "$found" ]] \
    || fail "projeto '$LAB_PROJECT' já existe neste daemon (inclui parados)"

  found="$(docker volume ls -q --filter "name=^${LAB_PROJECT}_" 2>/dev/null)" \
    || fail "docker volume ls falhou — volumes não verificáveis"
  [[ -z "$found" ]] \
    || fail "volume(s) do projeto '$LAB_PROJECT' já existe(m)"
}

if [[ "${1:-}" == "--post-config" ]]; then
  DIR="${2:?uso: preflight-lab.sh --post-config <caminho>/deploy/central}"
  [[ -f "$DIR/.env" ]] || fail "falta $DIR/.env"

  # overrides DENTRO do .env: set -a; source .env os exportaria e
  # retargetariam a stack para outro projeto/arquivo/daemon
  for v in COMPOSE_PROJECT_NAME COMPOSE_FILE DOCKER_CONTEXT; do
    grep -qE "^[[:space:]]*(export[[:space:]]+)?$v[[:space:]]*=" "$DIR/.env" \
      && fail ".env contém $v — override silencioso proibido"
  done

  configured="$(grep -E '^CENTRAL_HTTPS_PORT=' "$DIR/.env" | tail -1 | cut -d= -f2)" \
    || fail ".env sem CENTRAL_HTTPS_PORT"
  [[ "$configured" == "$LAB_PORT" ]] \
    || fail "CENTRAL_HTTPS_PORT=$configured diverge da porta aprovada $LAB_PORT"

  # CENTRAL_BIND é opcional: ausente = default do compose (127.0.0.1)
  bind_line="$(grep -E '^CENTRAL_BIND=' "$DIR/.env" | tail -1 | cut -d= -f2 || true)"
  bind_eff="${bind_line:-127.0.0.1}"
  [[ "$bind_eff" == "$LAB_BIND" ]] \
    || fail "CENTRAL_BIND=$bind_eff diverge do bind aprovado $LAB_BIND"

  name_line="$(grep -E '^name:' "$DIR/compose.yaml")" \
    || fail "compose.yaml sem campo 'name:' explícito"
  [[ "$name_line" == "name: $LAB_PROJECT" ]] \
    || fail "compose.yaml declara '$name_line', esperado 'name: $LAB_PROJECT'"

  proj_line="$(grep -E '^PROJECT=' "$DIR/scripts/_lib.sh")" \
    || fail "scripts/_lib.sh sem PROJECT= — substituição não verificável"
  [[ "$proj_line" == "PROJECT=\"$LAB_PROJECT\"" ]] \
    || fail "_lib.sh declara '$proj_line', esperado PROJECT=\"$LAB_PROJECT\""

  check_docker # conflito de projeto/volume pode ter surgido entre as fases
  check_port   # a porta pode ter sido tomada entre as fases
  echo "post-config ok: projeto '$LAB_PROJECT', porta $LAB_BIND:$LAB_PORT conferidos"
  exit 0
fi

[[ ! -e "$LAB_DIR" ]] \
  || fail "LAB_DIR '$LAB_DIR' já existe — conteúdo preservado, escolha outro ou resolva manualmente"

check_docker
check_port

echo "preflight ok: dir='$LAB_DIR' projeto='$LAB_PROJECT' porta=$LAB_BIND:$LAB_PORT"
