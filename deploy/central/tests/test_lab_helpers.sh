#!/usr/bin/env bash
# Regressões dos helpers de laboratório (achados A/B/C da revisão):
# verify-identity.sh, preflight-lab.sh, lab-curl.sh — docker/curl
# SIMULADOS via PATH; credenciais sintéticas; porta real via socket.
set -u
DIR="$(cd "$(dirname "$0")" && pwd)"
CENTRAL_REAL="$(cd "$DIR/.." && pwd)"
source "$DIR/_harness.sh"

SHA_OK="edb5bab0b751b5ccdfcf9f193cbb5787dc199c3e"

# ---------- stubs específicos desta suíte ------------------------------

# docker: compose ps -q, docker inspect -f, image inspect, info, ps -a,
# volume ls — tudo dirigido por STUB_* (consulta falhando ≠ 'livre').
# O log registra também DOCKER_HOST efetivo — o contexto das chamadas
# é comprovado, não apenas os argumentos.
stub_lab_docker() {
  cat > "$1/docker" <<'EOF'
#!/usr/bin/env bash
echo "docker $* [DH=${DOCKER_HOST:-unset}]" >> "$STUB_LOG"
case "$1" in
  info) exit "${STUB_DOCKER_INFO_RC:-0}" ;;
  compose)
    svc="${!#}"
    eval "cid=\"\${STUB_CID_$svc:-}\""
    [[ -n "$cid" ]] && echo "$cid"
    exit "${STUB_PSQ_RC:-0}" ;;
  ps)
    [[ "${STUB_DOCKER_PS_RC:-0}" == "0" ]] || exit "$STUB_DOCKER_PS_RC"
    printf '%s\n' ${STUB_PROJECT_CONTAINERS:-} | sed '/^$/d' ;;
  volume)
    [[ "${STUB_DOCKER_VOL_RC:-0}" == "0" ]] || exit "$STUB_DOCKER_VOL_RC"
    printf '%s\n' ${STUB_PROJECT_VOLUMES:-} | sed '/^$/d' ;;
  inspect)
    fmt="$3"; cid="$4"; svc="${cid#cid-}"
    case "$fmt" in
      *".Config.Image"*)   eval "echo \"\${STUB_IMGREF_$svc:-}\"" ;;
      *".Image"*)          eval "echo \"\${STUB_IMG_$svc:-}\"" ;;
      *"compose.project"*) eval "echo \"\${STUB_PROJ_$svc:-}\"" ;;
      *"compose.service"*) echo "$svc" ;;
    esac ;;
  image)
    # image inspect -f '<label>' <img-id> — img-<svc> → STUB_REV_<svc>
    iid="${!#}"; svc="${iid#img-}"
    [[ "${STUB_INSPECT_IMG_RC:-0}" == "0" ]] || exit "$STUB_INSPECT_IMG_RC"
    eval "echo \"\${STUB_REV_$svc:-}\""
    ;;
esac
exit 0
EOF
  chmod +x "$1/docker"
}

# curl: registra argv (uma linha por arg) e stdin; emula code/body
stub_lab_curl() {
  cat > "$1/curl" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$@" > "$STUB_CURL_ARGV"
cat > "$STUB_CURL_STDIN" 2>/dev/null || true
out=""; prev=""
for a in "$@"; do
  [[ "$prev" == "-o" ]] && out="$a"; prev="$a"
done
if [[ -n "$out" ]]; then
  if [[ -n "${STUB_BODY+x}" ]]; then printf '%s' "$STUB_BODY" > "$out"
  else printf '%s' '{"ok":true}' > "$out"; fi
fi
printf '%s' "${STUB_HTTP_CODE:-200}"
exit "${STUB_CURL_RC:-0}"
EOF
  chmod +x "$1/curl"
}

# central de laboratório em staging, com projeto próprio verificável
stage_lab() { # dir project
  mkdir -p "$1/scripts" "$1/secrets" "$1/tls"
  for s in _lib.sh verify-identity.sh preflight-lab.sh lab-curl.sh up.sh down.sh check.sh gen-secrets.sh mk-lab-tls.sh bootstrap-operator.sh prove.sh gen-agent-token.sh gen-operator-creds.sh; do
    cp "$CENTRAL_REAL/scripts/$s" "$1/scripts/"
  done
  sed -i 's/^PROJECT="macip-central"$/PROJECT="'"$2"'"/' "$1/scripts/_lib.sh"
  sed "s/^name: macip-central$/name: $2/" "$CENTRAL_REAL/compose.yaml" > "$1/compose.yaml"
}

write_manifest() { printf '{"commit":"%s"}\n' "$SHA_OK" > "$1"; }

# contexto padrão: projeto labproj, imagens rotuladas com SHA_OK
setup_identity_ok() {
  STUB_CID_api=cid-api STUB_CID_web=cid-web STUB_CID_db=cid-db
  STUB_PROJ_api=labproj STUB_PROJ_web=labproj STUB_PROJ_db=labproj
  STUB_IMG_api=img-api STUB_IMG_web=img-web STUB_IMG_db=img-db
  STUB_REV_api="$SHA_OK" STUB_REV_web="$SHA_OK"
  # referência efetiva do Compose publicado — a fixture reflete a
  # infraestrutura real, não uma forma simplificada divergente
  STUB_IMGREF_db="docker.io/library/postgres:17-alpine"
  export STUB_CID_api STUB_CID_web STUB_CID_db STUB_PROJ_api STUB_PROJ_web \
    STUB_PROJ_db STUB_IMG_api STUB_IMG_web STUB_IMG_db STUB_REV_api \
    STUB_REV_web STUB_IMGREF_db
}

BIN="$WORK/bin"; mkdir -p "$BIN"
stub_lab_docker "$BIN"; stub_lab_curl "$BIN"
export PATH="$BIN:$PATH"
export STUB_LOG="$WORK/stub.log" STUB_CURL_ARGV="$WORK/curl.argv" \
       STUB_CURL_STDIN="$WORK/curl.stdin"
: > "$STUB_LOG"

# ================= verify-identity.sh (achado A) ======================

echo "== verify-identity.sh =="
LAB="$WORK/lab"; stage_lab "$LAB" labproj; setup_identity_ok
write_manifest "$WORK/MANIFEST.json"
export MANIFEST_JSON="$WORK/MANIFEST.json"

if bash "$LAB/scripts/verify-identity.sh" >"$WORK/vi.out" 2>&1; then
  ok "identidade correta aceita"
  expect_contains "resumo" "$(tail -1 "$WORK/vi.out")" "identidade conferida: labproj @ $SHA_OK"
else
  bad "identidade correta deveria passar: $(cat "$WORK/vi.out")"
fi

STUB_REV_api="ffffffffffffffffffffffffffffffffffffffff"
bash "$LAB/scripts/verify-identity.sh" >"$WORK/vi.out" 2>&1 \
  && bad "SHA divergente foi aceito" || ok "SHA divergente recusado"
STUB_REV_api="$SHA_OK"

STUB_REV_web=""   # label OCI ausente na imagem do portal
bash "$LAB/scripts/verify-identity.sh" >/dev/null 2>&1 \
  && bad "metadado ausente foi aceito" || ok "metadado ausente recusado"
STUB_REV_web="$SHA_OK"

export STUB_PSQ_RC=7  # consulta ao contêiner falhando
bash "$LAB/scripts/verify-identity.sh" >/dev/null 2>&1 \
  && bad "consulta falhando foi aceita" || ok "consulta falhando recusada"
unset STUB_PSQ_RC

STUB_PROJ_api="outro-projeto"
bash "$LAB/scripts/verify-identity.sh" >/dev/null 2>&1 \
  && bad "contêiner de outro projeto foi aceito" || ok "outro projeto recusado"
STUB_PROJ_api="labproj"

STUB_REV_web="deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"  # api ok + web errado
bash "$LAB/scripts/verify-identity.sh" >/dev/null 2>&1 \
  && bad "portal divergente passou com API correta" || ok "API ok + portal divergente recusado"
STUB_REV_web="$SHA_OK"

MANIFEST_JSON="$WORK/ausente.json"
bash "$LAB/scripts/verify-identity.sh" >/dev/null 2>&1 \
  && bad "manifesto ausente foi aceito" || ok "manifesto ausente recusado"
export MANIFEST_JSON="$WORK/MANIFEST.json"

# fallback de arquivo local (o defeito anterior): VERSION local NÃO
# substitui a identidade em execução — com inspect quebrado falha mesmo
# existindo VERSION com o SHA certo
echo "$SHA_OK 2026-01-01T00:00:00Z" > "$LAB/VERSION"
STUB_CID_api=""   # contêiner da API inexistente
bash "$LAB/scripts/verify-identity.sh" >/dev/null 2>&1 \
  && bad "VERSION local serviu de substituto" || ok "VERSION local não substitui identidade"
STUB_CID_api="cid-api"

# política de equivalência do db (bloqueio B) — a fixture positiva usa
# a referência efetiva do Compose; formas normalizadas aceitas,
# major/repositório divergentes recusados
for ref in "postgres:17-alpine" "postgres:17" \
           "library/postgres:17-alpine" "index.docker.io/library/postgres:17-alpine"; do
  STUB_IMGREF_db="$ref"
  bash "$LAB/scripts/verify-identity.sh" >/dev/null 2>&1 \
    && ok "db ref '$ref' aceita" || bad "db ref '$ref' deveria ser aceita"
done
for ref in "postgres:170" "postgres:16-alpine" "postgres:latest" \
           "evil.io/postgres:17-alpine" "docker.io/evil/postgres:17-alpine"; do
  STUB_IMGREF_db="$ref"
  bash "$LAB/scripts/verify-identity.sh" >/dev/null 2>&1 \
    && bad "db ref '$ref' indevida foi aceita" || ok "db ref '$ref' recusada"
done
STUB_IMGREF_db="docker.io/library/postgres:17-alpine"

# ================= preflight-lab.sh (achado B) ========================

echo "== preflight-lab.sh =="
export LAB_DIR="$WORK/labdir-novo" LAB_PROJECT=labproj LAB_PORT=19443
for v in STUB_PROJECT_CONTAINERS STUB_PROJECT_VOLUMES STUB_DOCKER_PS_RC \
         STUB_DOCKER_VOL_RC STUB_DOCKER_INFO_RC COMPOSE_PROJECT_NAME COMPOSE_FILE DOCKER_CONTEXT; do
  unset "$v" 2>/dev/null || true
done

bash "$LAB/scripts/preflight-lab.sh" >"$WORK/pf.out" 2>&1 \
  && ok "caso livre aprovado" || bad "caso livre falhou: $(cat "$WORK/pf.out")"

# porta ocupada: socket REAL aberto durante a chamada
python3 -c 'import socket,time
s=socket.socket(); s.bind(("127.0.0.1",19443)); s.listen(1); time.sleep(8)' &
HOLDER=$!; sleep 0.4
bash "$LAB/scripts/preflight-lab.sh" >/dev/null 2>&1 \
  && bad "porta ocupada passou" || ok "porta ocupada recusada"
[[ ! -e "$LAB_DIR" ]] && ok "nenhuma mutação após recusa de porta" \
  || bad "mutação aconteceu apesar da recusa"
kill $HOLDER 2>/dev/null; wait $HOLDER 2>/dev/null

export STUB_PROJECT_CONTAINERS="cid-antigo"   # projeto existente (até parado)
bash "$LAB/scripts/preflight-lab.sh" >/dev/null 2>&1 \
  && bad "projeto existente passou" || ok "projeto existente recusado"
unset STUB_PROJECT_CONTAINERS

mkdir -p "$WORK/labdir-existente"; export LAB_DIR="$WORK/labdir-existente"
bash "$LAB/scripts/preflight-lab.sh" >/dev/null 2>&1 \
  && bad "diretório existente passou" || ok "diretório existente recusado"
export LAB_DIR="$WORK/labdir-novo"

export STUB_DOCKER_INFO_RC=1                  # daemon não consultável
bash "$LAB/scripts/preflight-lab.sh" >/dev/null 2>&1 \
  && bad "consulta impossível passou" || ok "docker info falhando recusado"
unset STUB_DOCKER_INFO_RC

export STUB_DOCKER_PS_RC=2                    # consulta de projeto falhando
bash "$LAB/scripts/preflight-lab.sh" >/dev/null 2>&1 \
  && bad "docker ps falhando passou" || ok "docker ps falhando recusado"
unset STUB_DOCKER_PS_RC

export COMPOSE_PROJECT_NAME=herdado
bash "$LAB/scripts/preflight-lab.sh" >/dev/null 2>&1 \
  && bad "override herdado passou" || ok "COMPOSE_PROJECT_NAME herdado recusado"
unset COMPOSE_PROJECT_NAME

# fase 2: pós-configuração — porta/nome/projeto coerentes ou recusa
LAB2="$WORK/labdir-novo/central"; stage_lab "$LAB2" labproj
printf 'CENTRAL_HTTPS_PORT=19443\n' > "$LAB2/.env"
bash "$LAB/scripts/preflight-lab.sh" --post-config "$LAB2" >"$WORK/pf.out" 2>&1 \
  && ok "post-config coerente aprovado" || bad "post-config falhou: $(cat "$WORK/pf.out")"

printf 'CENTRAL_HTTPS_PORT=19999\n' > "$LAB2/.env"  # porta divergente
bash "$LAB/scripts/preflight-lab.sh" --post-config "$LAB2" >/dev/null 2>&1 \
  && bad "porta divergente passou" || ok "porta configurada ≠ aprovada recusada"
printf 'CENTRAL_HTTPS_PORT=19443\n' > "$LAB2/.env"

sed -i 's/^name: labproj$/name: macip-central/' "$LAB2/compose.yaml"  # sed não aplicado
bash "$LAB/scripts/preflight-lab.sh" --post-config "$LAB2" >/dev/null 2>&1 \
  && bad "projeto não aplicado passou" || ok "compose name divergente recusado"
sed -i 's/^name: macip-central$/name: labproj/' "$LAB2/compose.yaml"

# .env com override de projeto (reprodução do revisor): recusado —
# carregá-lo retargetaria a stack silenciosamente
printf 'CENTRAL_HTTPS_PORT=19443\nCOMPOSE_PROJECT_NAME=foreign-fixture\n' > "$LAB2/.env"
bash "$LAB/scripts/preflight-lab.sh" --post-config "$LAB2" >/dev/null 2>&1 \
  && bad ".env com COMPOSE_PROJECT_NAME passou" || ok ".env com override de projeto recusado"
printf 'CENTRAL_HTTPS_PORT=19443\nCENTRAL_BIND=127.0.0.1\n' > "$LAB2/.env"

# bind efetivo divergente do aprovado
printf 'CENTRAL_HTTPS_PORT=19443\nCENTRAL_BIND=192.168.99.9\n' > "$LAB2/.env"
bash "$LAB/scripts/preflight-lab.sh" --post-config "$LAB2" >/dev/null 2>&1 \
  && bad "bind divergente passou" || ok "bind efetivo divergente recusado"
printf 'CENTRAL_HTTPS_PORT=19443\nCENTRAL_BIND=127.0.0.1\n' > "$LAB2/.env"

# conflito surgido ENTRE as fases: projeto criado depois do preflight
export STUB_PROJECT_CONTAINERS="cid-surgiu"
bash "$LAB/scripts/preflight-lab.sh" --post-config "$LAB2" >/dev/null 2>&1 \
  && bad "projeto surgido entre fases passou" || ok "conflito entre fases recusado"
unset STUB_PROJECT_CONTAINERS

# ---- entrypoint documental completo (achado A-repro1) ----------------
# recusa do post-config INTERROMPE: up.sh/check.sh nunca são chamados
printf 'CENTRAL_HTTPS_PORT=19999\n' > "$LAB2/.env"   # força recusa
: > "$STUB_LOG"
bash "$LAB/scripts/preflight-lab.sh" --post-config "$LAB2" \
  && bash "$LAB2/scripts/up.sh" && bash "$LAB2/scripts/check.sh" \
  >/dev/null 2>&1
[[ $? -ne 0 ]] && ok "bloco documental interrompido na recusa" \
  || bad "bloco documental seguiu após recusa"
grep -qE "build|up -d" "$STUB_LOG" \
  && bad "up.sh executou apesar da recusa" || ok "build/up não executados após recusa"

# fluxo aprovado alcança up.sh com projeto EXPLÍCITO (-p labproj)
printf 'CENTRAL_HTTPS_PORT=19443\nCENTRAL_BIND=127.0.0.1\n' > "$LAB2/.env"
write_secrets "$LAB2" "" "" "pw-real" "pw-real"; write_tls "$LAB2"
printf 'a%.0s' $(seq 1 40) > "$LAB2/VERSION"; printf ' ts\n' >> "$LAB2/VERSION"
: > "$STUB_LOG"
bash "$LAB/scripts/preflight-lab.sh" --post-config "$LAB2" \
  && bash "$LAB2/scripts/up.sh" >/dev/null 2>&1 \
  && ok "fluxo aprovado alcança up.sh" || bad "fluxo aprovado falhou"
grep -q "build" "$STUB_LOG" \
  && ok "build executado após aprovação" || bad "build não executado"
grep -q -- "-p labproj" "$STUB_LOG" \
  && ok "projeto explícito (-p) nas chamadas compose" || bad "-p ausente"

# up.sh também recusa override DENTRO do .env (defesa além do preflight)
printf 'CENTRAL_HTTPS_PORT=19443\nCENTRAL_BIND=127.0.0.1\nCOMPOSE_PROJECT_NAME=foreign-fixture\n' > "$LAB2/.env"
: > "$STUB_LOG"
bash "$LAB2/scripts/up.sh" >/dev/null 2>&1 \
  && bad "up.sh aceitou override do .env" || ok "up.sh recusou override do .env"
grep -q "build" "$STUB_LOG" \
  && bad "build rodou apesar do override" || ok "build não executado com override"
printf 'CENTRAL_HTTPS_PORT=19443\nCENTRAL_BIND=127.0.0.1\n' > "$LAB2/.env"

# up.sh recusa override herdado divergente; COMPOSE_FILE/CONTEXT sempre
COMPOSE_PROJECT_NAME=foreign bash "$LAB2/scripts/up.sh" >/dev/null 2>&1 \
  && bad "up.sh aceitou herdado divergente" || ok "up.sh recusou herdado divergente"
COMPOSE_FILE=/tmp/stack.yaml bash "$LAB2/scripts/up.sh" >/dev/null 2>&1 \
  && bad "up.sh aceitou COMPOSE_FILE" || ok "up.sh recusou COMPOSE_FILE herdado"

# ================= contexto de controle (achado A rodada 3) =========
# PROJECT/DOCKER_HOST (e toda a família de controle) dentro de arquivo
# de dados NÃO podem deslocar o destino aprovado — recusa por família,
# antes de build/up; DOCKER_HOST herdado e aprovado segue utilizável.

echo "== contexto de controle =="

# .env com PROJECT=foreign: post-config recusa e up.sh não chama build
printf 'CENTRAL_HTTPS_PORT=19443\nCENTRAL_BIND=127.0.0.1\nPROJECT=foreign-fixture\n' \
  > "$LAB2/.env"
bash "$LAB/scripts/preflight-lab.sh" --post-config "$LAB2" >/dev/null 2>&1 \
  && bad "post-config aceitou PROJECT no .env" || ok "post-config recusa PROJECT no .env"
: > "$STUB_LOG"
bash "$LAB2/scripts/up.sh" >/dev/null 2>&1 \
  && bad "up.sh aceitou PROJECT no .env" || ok "up.sh recusa PROJECT no .env"
grep -q "build" "$STUB_LOG" \
  && bad "build executou com PROJECT estranho" || ok "build não executou (PROJECT)"
! grep -q "foreign-fixture" "$STUB_LOG" \
  && ok "nenhuma chamada ao projeto estranho" || bad "compose foi para foreign-fixture"

# .env com DOCKER_HOST: mesma recusa, mesmo ponto — daemon não muda
printf 'CENTRAL_HTTPS_PORT=19443\nCENTRAL_BIND=127.0.0.1\nDOCKER_HOST=unix:///tmp/foreign-fixture.sock\n' \
  > "$LAB2/.env"
bash "$LAB/scripts/preflight-lab.sh" --post-config "$LAB2" >/dev/null 2>&1 \
  && bad "post-config aceitou DOCKER_HOST no .env" || ok "post-config recusa DOCKER_HOST no .env"
: > "$STUB_LOG"
bash "$LAB2/scripts/up.sh" >/dev/null 2>&1 \
  && bad "up.sh aceitou DOCKER_HOST no .env" || ok "up.sh recusa DOCKER_HOST no .env"
grep -q "build" "$STUB_LOG" \
  && bad "build executou com DOCKER_HOST estranho" || ok "build não executou (DOCKER_HOST)"
grep -q "foreign-fixture.sock" "$STUB_LOG" \
  && bad "chamada ao daemon estranho" || ok "daemon aprovado preservado nas chamadas"

# .env com linha que não é KEY=VALUE (código embutido) → recusado
printf 'CENTRAL_HTTPS_PORT=19443\nPROJECT=x\nexport PATH=/evil/bin\n' > "$LAB2/.env"
bash "$LAB/scripts/preflight-lab.sh" --post-config "$LAB2" >/dev/null 2>&1 \
  && bad "post-config aceitou PATH no .env" || ok "post-config recusa PATH no .env"
printf 'CENTRAL_HTTPS_PORT=19443\nCENTRAL_BIND=127.0.0.1\nfoo sem igual\n' > "$LAB2/.env"
bash "$LAB/scripts/preflight-lab.sh" --post-config "$LAB2" >/dev/null 2>&1 \
  && bad "post-config aceitou linha não-KEY=VALUE" || ok "post-config recusa linha solta"
printf 'CENTRAL_HTTPS_PORT=19443\nCENTRAL_BIND=127.0.0.1\n' > "$LAB2/.env"

# secrets/*.env também não podem declarar controle (arquivos carregados
# DEPOIS do .env): postgres.env com COMPOSE_FILE → post-config e up recusam
printf 'POSTGRES_USER=u\nPOSTGRES_DB=d\nPOSTGRES_PASSWORD=pw-real\nCOMPOSE_FILE=/tmp/x.yaml\n' \
  > "$LAB2/secrets/postgres.env"; chmod 600 "$LAB2/secrets/postgres.env"
bash "$LAB/scripts/preflight-lab.sh" --post-config "$LAB2" >/dev/null 2>&1 \
  && bad "post-config aceitou COMPOSE_FILE em secrets" || ok "post-config recusa controle em secrets"
: > "$STUB_LOG"
bash "$LAB2/scripts/up.sh" >/dev/null 2>&1 \
  && bad "up.sh aceitou controle em secrets" || ok "up.sh recusa controle em secrets"
grep -q "build" "$STUB_LOG" \
  && bad "build executou com secrets contaminado" || ok "build não executou (secrets)"

# secrets redefinindo chave de dados já aprovada → divergência recusada
printf 'POSTGRES_USER=u\nPOSTGRES_DB=d\nPOSTGRES_PASSWORD=pw-real\nCENTRAL_HTTPS_PORT=9999\n' \
  > "$LAB2/secrets/postgres.env"; chmod 600 "$LAB2/secrets/postgres.env"
: > "$STUB_LOG"
bash "$LAB2/scripts/up.sh" >/dev/null 2>&1 \
  && bad "up.sh aceitou redefinição divergente" || ok "redefinição divergente recusada"
grep -q "build" "$STUB_LOG" \
  && bad "build executou com porta redirecionada" || ok "build não executou (porta)"

# restaura secrets íntegros e prova o caso legítimo com daemon
# EXPLICITAMENTE aprovado: herdado continua sendo o daemon usado
write_secrets "$LAB2" "" "" "pw-real" "pw-real"
: > "$STUB_LOG"
DOCKER_HOST=unix:///tmp/aprovado.sock bash "$LAB2/scripts/up.sh" >"$WORK/up.out" 2>&1 \
  && ok "up com daemon herdado aprovado" || bad "up com daemon aprovado falhou: $(cat "$WORK/up.out")"
grep -q "build" "$STUB_LOG" && grep -q -- "-p labproj" "$STUB_LOG" \
  && ok "chamadas com projeto explícito" || bad "chamadas sem -p labproj"
grep -q "DH=unix:///tmp/aprovado.sock" "$STUB_LOG" \
  && ok "daemon aprovado chegou intacto às chamadas" || bad "daemon mudou nas chamadas"

# e o bloco documental segue interrompendo após recusa (fim a fim)
printf 'CENTRAL_HTTPS_PORT=19999\n' > "$LAB2/.env"
: > "$STUB_LOG"
bash "$LAB/scripts/preflight-lab.sh" --post-config "$LAB2" \
  && bash "$LAB2/scripts/up.sh" && bash "$LAB2/scripts/check.sh" \
  >/dev/null 2>&1
[[ $? -ne 0 ]] && ok "bloco interrompido após recusa (contexto)" \
  || bad "bloco seguiu após recusa"
grep -q "build" "$STUB_LOG" \
  && bad "build após recusa" || ok "nenhuma mutação após recusa"
printf 'CENTRAL_HTTPS_PORT=19443\nCENTRAL_BIND=127.0.0.1\n' > "$LAB2/.env"

# ================= lab-curl.sh (achado C) =============================

echo "== lab-curl.sh =="
export SENT_ADMIN="token-admin-SENTINELA" SENT_AGENT="token-agente-SENTINELA"
cat > "$LAB2/secrets/api.env" <<EOF
MACIP_API_DATABASE_URL=postgresql+psycopg://u:p@db:5432/d
MACIP_API_ADMIN_TOKEN=$SENT_ADMIN
EOF
printf 'MACIP_AGENT_TOKEN=%s\n' "$SENT_AGENT" > "$LAB2/secrets/agent-token.env"
chmod 600 "$LAB2/secrets/api.env" "$LAB2/secrets/agent-token.env"
echo "CA-DE-LABORATORIO" > "$LAB2/tls/lab-ca.pem"; chmod 644 "$LAB2/tls/lab-ca.pem"
printf 'CENTRAL_HTTPS_PORT=19443\nCENTRAL_HOSTNAME=localhost\n' > "$LAB2/.env"

bash "$LAB2/scripts/lab-curl.sh" admin GET /api/v1/tasks >"$WORK/lc.out" 2>&1 \
  && ok "admin GET aprovado" || bad "admin GET falhou: $(cat "$WORK/lc.out")"
grep -q "$SENT_ADMIN" "$STUB_CURL_ARGV" \
  && bad "token admin apareceu em argv" || ok "token admin ausente de argv"
grep -q "Bearer $SENT_ADMIN" "$STUB_CURL_STDIN" \
  && ok "token admin foi por stdin (-H @-)" || bad "token admin não foi por stdin"

export STUB_HTTP_CODE=500 STUB_BODY='{"detail":"falha"}'
bash "$LAB2/scripts/lab-curl.sh" agent POST /api/v1/agent/executions '{}' \
  >"$WORK/lc.out" 2>&1 \
  && bad "HTTP 500 apresentado como sucesso" || ok "HTTP 500 recusado"
expect_contains "diagnóstico HTTP" "$(cat "$WORK/lc.out")" "HTTP 500"
unset STUB_HTTP_CODE STUB_BODY

grep -q -- "--cacert" "$STUB_CURL_ARGV" \
  && ok "TLS por cadeia preservado" || bad "--cacert ausente"
grep -qx -- "--max-redirs" "$STUB_CURL_ARGV" \
  && ok "redirects desativados" || bad "redirects seguidos"
grep -q -- "-k" "$STUB_CURL_ARGV" \
  && bad "-k presente (TLS desligado)" || ok "sem -k"

chmod 644 "$LAB2/secrets/agent-token.env"   # permissão aberta recusada
bash "$LAB2/scripts/lab-curl.sh" agent GET /api/v1/tasks >/dev/null 2>&1 \
  && bad "arquivo 644 aceito" || ok "agent-token.env 644 recusado"
chmod 600 "$LAB2/secrets/agent-token.env"

rm "$LAB2/secrets/agent-token.env"          # credencial ausente
bash "$LAB2/scripts/lab-curl.sh" agent GET /api/v1/tasks >/dev/null 2>&1 \
  && bad "agente sem credencial passou" || ok "credencial ausente recusada"

export STUB_CURL_RC=7                              # falha de rede/TLS
bash "$LAB2/scripts/lab-curl.sh" admin GET /api/v1/tasks >/dev/null 2>&1 \
  && bad "falha de rede passou" || ok "falha de rede/TLS recusada"
unset STUB_CURL_RC

# ================= credenciais privadas (achado B rodada 3) =========
# gen-operator-creds.sh grava JSON 600: senha é DADO com parser —
# nunca código shell. Sentinelas sintéticas, sem credencial real.

echo "== gen-operator-creds.sh =="
OPDIR="$WORK/opcreds"; mkdir -p "$OPDIR"

json_pw() { python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["password"],end="")' "$1"; }

# sentinelas: simples, espaços, unicode, aspas, $ # ; \, e texto que
# parece substituição de comando — todas devem sobreviver LITERALMENTE
SIMPLES='senha123'
ESPACOS='  senha com espacos  '
UNICODE='senhaçãoção'
ASPAS="a'b\"c"
META='p$w#s;en\ha'
CMDLIKE='$(touch '"$WORK"'/pwnd)'
for i in SIMPLES ESPACOS UNICODE ASPAS META CMDLIKE; do
  eval "sent=\"\${$i}\""
  f="$OPDIR/cred-$i.json"
  printf '%s\n' "$sent" | bash "$LAB2/scripts/gen-operator-creds.sh" "$f" op \
    >"$OPDIR/out-$i" 2>&1
  [[ -f "$f" ]] || { bad "credencial $i não gravada"; continue; }
  got="$(json_pw "$f")"
  [[ "$got" == "$sent" ]] \
    && ok "senha $i preservada literalmente" \
    || bad "senha $i alterada: obtido '$got'"
  [[ "$(stat -c %a "$f")" == "600" ]] \
    && ok "credencial $i com 600" || bad "credencial $i sem 600"
  grep -qF "$sent" "$OPDIR/out-$i" \
    && bad "senha $i vazou na saída" || ok "senha $i fora da saída"
done
[[ ! -e "$WORK/pwnd" ]] \
  && ok '$(...) na senha não executou' || bad '$(...) executou!'

# arquivo existente NUNCA é sobrescrito silenciosamente
printf '%s\n' 'outra-senha' | bash "$LAB2/scripts/gen-operator-creds.sh" \
  "$OPDIR/cred-SIMPLES.json" op >/dev/null 2>&1 \
  && bad "arquivo existente sobrescrito" || ok "arquivo existente preservado"
[[ "$(json_pw "$OPDIR/cred-SIMPLES.json")" == "$SIMPLES" ]] \
  && ok "conteúdo original intacto" || bad "conteúdo original alterado"

# integração com a leitura da spec: mesmo JSON, mesmo par de campos
python3 -c 'import json,sys;c=json.load(open(sys.argv[1]));assert c["login"]=="op" and isinstance(c["password"],str)' \
  "$OPDIR/cred-SIMPLES.json" \
  && ok "JSON compatível com MACIP_HOMOLOG_SECRETS" || bad "JSON incompatível"

# ================= credencial do agente (achado C rodada 3) =========
echo "== gen-agent-token.sh =="
printf 'CENTRAL_HTTPS_PORT=19443\nCENTRAL_HOSTNAME=localhost\n' > "$LAB2/.env"
cat > "$LAB2/secrets/api.env" <<EOF
MACIP_API_DATABASE_URL=postgresql+psycopg://u:p@db:5432/d
MACIP_API_ADMIN_TOKEN=$SENT_ADMIN
EOF
chmod 600 "$LAB2/secrets/api.env"
export STUB_BODY='{"token":"token-agente-NOVO"}'
rm -f "$LAB2/secrets/agent-token.env"
AGUUID="11111111-2222-3333-4444-555555555555"
out="$(cd "$LAB2" && bash scripts/gen-agent-token.sh "$AGUUID" 2>&1)" \
  || bad "gen-agent-token falhou: $out"
[[ -f "$LAB2/secrets/agent-token.env" ]] \
  && ok "agent-token.env criado" || bad "agent-token.env ausente"
[[ "$(stat -c %a "$LAB2/secrets/agent-token.env")" == "600" ]] \
  && ok "agent-token.env 600" || bad "agent-token.env sem 600"
grep -q "token-agente-NOVO" <<<"$out" \
  && bad "token vazou na saída" || ok "token fora da saída"
grep -q "$AGUUID" "$STUB_CURL_ARGV" \
  && ok "POST usou o UUID real do agente" || bad "UUID do agente não usado"
grep -q "/credentials" "$STUB_CURL_ARGV" \
  && ok "rota de credencial correta" || bad "rota de credencial errada"

# UMA ÚNICA criação: segunda execução recusa e preserva o arquivo
printf 'TOKEN-ORIGINAL-PRESERVADO\n' > /dev/null
echo "MACIP_AGENT_TOKEN=token-original" > "$LAB2/secrets/agent-token.env"
chmod 600 "$LAB2/secrets/agent-token.env"
(cd "$LAB2" && bash scripts/gen-agent-token.sh "$AGUUID") >/dev/null 2>&1 \
  && bad "segunda criação passou" || ok "segunda criação recusada"
grep -q "token-original" "$LAB2/secrets/agent-token.env" \
  && ok "credencial existente preservada" || bad "credencial sobrescrita"
unset STUB_BODY

# ================= cercas Markdown dos roteiros (achado C) ==========
# Extrator com regra real de fechamento: só ``` puro fecha um bloco —
# ```bash DENTRO de bloco aberto permanece conteúdo. Cada bloco bash
# extraído precisa passar em bash -n.

echo "== blocos bash dos roteiros =="
DOCS_DIR="$(cd "$CENTRAL_REAL/../.." && pwd)/docs"
python3 - "$DOCS_DIR/instalacao-monitoramento.md" \
          "$DOCS_DIR/homologacao-monitoramento.md" <<'PY'
import re, subprocess, sys
bad = 0
for path in sys.argv[1:]:
    lines = open(path, encoding="utf-8").read().splitlines()
    blocks, cur, start = [], None, 0
    for i, ln in enumerate(lines, 1):
        if cur is None:
            m = re.match(r"^```(\w*)\s*$", ln)
            if m and m.group(1) == "bash":
                cur, start = [], i
        elif re.match(r"^```+\s*$", ln):
            blocks.append((start, cur)); cur = None
        else:
            cur.append(ln)
    if cur is not None:
        print(f"FALHA {path}: bloco aberto na linha {start} sem fechamento")
        bad += 1
    for start, blk in blocks:
        src = "\n".join(blk) + "\n"
        r = subprocess.run(["bash", "-n"], input=src, text=True,
                           capture_output=True)
        if r.returncode != 0:
            print(f"FALHA {path}:{start}: bash -n — {r.stderr.strip()[:120]}")
            bad += 1
    print(f"{path}: {len(blocks)} bloco(s) bash conferido(s)")
sys.exit(1 if bad else 0)
PY
[[ $? -eq 0 ]] && ok "blocos bash válidos nos roteiros" \
  || bad "bloco bash inválido nos roteiros"

# a credencial do agente é criada UMA única vez no roteiro (sem o bloco
# duplicado/cerca quebrada do achado C) — conta a INVOCAÇÃO, não menções
n_create="$(grep -c 'scripts/gen-agent-token.sh' \
  "$DOCS_DIR/instalacao-monitoramento.md" || true)"
[[ "$n_create" == "1" ]] \
  && ok "uma única criação de credencial no roteiro" \
  || bad "criação de credencial aparece $n_create vezes"

echo "== resultado: $PASS ok, $FAIL falhas =="
exit "$([ "$FAIL" = 0 ] && echo 0 || echo 1)"
