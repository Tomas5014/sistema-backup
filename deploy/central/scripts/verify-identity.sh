#!/usr/bin/env bash
# Cadeia verificável de identidade: MANIFEST.json do pacote → labels
# OCI das imagens construídas → contêineres efetivamente em execução.
#
# Só leitura de metadados — nunca imprime inspect completo (pode
# conter segredos de ambiente). Identidade ausente, divergente ou
# impossível de consultar FALHA: arquivo local não é substituto.
#
# Uso: scripts/verify-identity.sh [caminho/MANIFEST.json]
set -euo pipefail
source "$(dirname "$0")/_lib.sh"
cd "$CENTRAL_DIR"

MANIFEST="${1:-${MANIFEST_JSON:-$CENTRAL_DIR/../../../MANIFEST.json}}"
[[ -f "$MANIFEST" ]] || {
  echo "ERRO: MANIFEST.json não encontrado em '$MANIFEST' — sem identidade para conferir" >&2; exit 1; }

EXPECTED_COMMIT="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["commit"])' "$MANIFEST")"
[[ "$EXPECTED_COMMIT" =~ ^[0-9a-f]{40}$ ]] || {
  echo "ERRO: manifesto sem 'commit' válido — '$MANIFEST'" >&2; exit 1; }

fail() { echo "ERRO: $*" >&2; exit 1; }

# campo de inspect por caminho seguro — falha de consulta propaga
inspect_field() { # container-id format
  docker inspect -f "$2" "$1" 2>/dev/null \
    || fail "inspect falhou para contêiner $1 (inexistente? daemon?)"
}

# metadado mínimo do contêiner: projeto compose, serviço e imagem
check_container() { # service
  local svc="$1" cid project service image
  cid="$(compose ps -q "$svc" 2>/dev/null)" \
    || fail "não foi possível consultar contêiner de '$svc' (projeto $PROJECT)"
  [[ -n "$cid" ]] || fail "nenhum contêiner para '$svc' no projeto $PROJECT"

  project="$(inspect_field "$cid" '{{index .Config.Labels "com.docker.compose.project"}}')"
  service="$(inspect_field "$cid" '{{index .Config.Labels "com.docker.compose.service"}}')"
  [[ "$project" == "$PROJECT" ]] \
    || fail "contêiner de '$svc' pertence ao projeto '$project', não a '$PROJECT'"
  [[ "$service" == "$svc" ]] \
    || fail "contêiner de '$svc' declara serviço '$service' — compose divergente"

  image="$(inspect_field "$cid" '{{.Image}}')"
  [[ -n "$image" ]] || fail "imagem do contêiner '$svc' não identificável"
  printf '%s %s\n' "$svc" "$image"
}

REVISION_LABEL='org.opencontainers.image.revision'
image_revision() { # image-id → label OCI; ausência/divergência falha
  local rev
  rev="$(docker image inspect -f \
    "{{index .Config.Labels \"$REVISION_LABEL\"}}" "$1" 2>/dev/null)" \
    || fail "docker image inspect falhou para $1"
  printf '%s\n' "$rev"
}

echo "== identidade esperada (manifesto): $EXPECTED_COMMIT =="

ERRORS=0
for svc in api web; do
  out="$(check_container "$svc")" || fail "$out"
  image="${out##* }"
  rev="$(image_revision "$image")"
  if [[ "$rev" != "$EXPECTED_COMMIT" ]]; then
    echo "DIVERGENTE: $svc image revision '${rev:-<ausente>}' ≠ manifesto" >&2
    ERRORS=$((ERRORS+1))
  else
    echo "ok: $svc @ $EXPECTED_COMMIT"
  fi
done

out="$(check_container db)" || fail "$out"
db_image_ref="$(docker inspect -f '{{.Config.Image}}' "$(compose ps -q db)")" \
  || fail "inspect do db falhou"
# Política explícita de equivalência: aceita 'postgres:17[-x]',
# 'library/postgres:17[-x]' e '[index.]docker.io/library/postgres:17[-x]'
# — o Compose declara docker.io/library/postgres:17-alpine e o daemon
# pode gravar a forma normalizada. Rejeita postgres:170, outros
# repositórios e qualquer major diferente.
db_ref_norm="$db_image_ref"
db_ref_norm="${db_ref_norm#index.docker.io/}"
db_ref_norm="${db_ref_norm#docker.io/}"
db_ref_norm="${db_ref_norm#library/}"
[[ "$db_ref_norm" == "postgres:17" || "$db_ref_norm" == postgres:17-* ]] || {
  echo "DIVERGENTE: db usa '$db_image_ref' (normalizado '$db_ref_norm'), esperado postgres:17[-*]" >&2
  ERRORS=$((ERRORS+1)); }
[[ "$ERRORS" == "0" ]] && echo "ok: db = $db_image_ref (projeto $PROJECT)"

[[ "$ERRORS" == "0" ]] || fail "$ERRORS divergência(s) de identidade"
echo "identidade conferida: $PROJECT @ $EXPECTED_COMMIT"
