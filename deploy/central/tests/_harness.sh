#!/usr/bin/env bash
# Harness mínimo para os testes dos scripts da central —
# isolamento por mktemp, Docker simulado via PATH. Não imprime segredos.
set -u
PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); printf '  ok  %s\n' "$1"; }
bad()  { FAIL=$((FAIL+1)); printf '  FALHOU  %s\n' "$1" >&2; }
expect_eq()      { [[ "$2" == "$3" ]] && ok "$1" || { bad "$1 (esperado '$3', obtido '$2')"; }; }
expect_contains(){ [[ "$2" == *"$3"* ]] && ok "$1" || { bad "$1 (sem '$3')"; }; }
expect_file()    { [[ -f "$2" ]] && ok "$1" || bad "$1"; }
expect_no_file() { [[ ! -e "$2" ]] && ok "$1" || bad "$1"; }

WORK="$(mktemp -d /tmp/macip-test-XXXXXX)"
trap 'rm -rf "$WORK"' EXIT

# binário docker SIMULADO: registra chamadas e emula exec/pg_dump/ps
stub_docker() {
  local dir="$1"
  cat > "$dir/docker" <<'EOF'
#!/usr/bin/env bash
echo "$*" >> "$STUB_LOG"
# emula: docker info (acesso ao daemon neste contexto)
if [[ "$*" == "info" ]]; then
  exit "${STUB_DOCKER_INFO_RC:-0}"
fi
# emula: docker compose ... exec -T db pg_dump ...
if [[ "$*" == *"pg_dump"* ]]; then
  case "${STUB_PGDUMP_MODE:-ok}" in
    ok)      printf 'PGDUMP-BYTES-9f8e7d6c' ;;
    empty)   : ;;
    partial) printf 'PGDUMP-PARTIAL'; exit 3 ;;
    fail)    exit 5 ;;
  esac
  exit 0
fi
# emula: docker compose ... ps --status running --services
if [[ "$*" == *"ps --status running --services"* ]]; then
  if [[ -n "${STUB_PS_CNT:-}" ]]; then
    n=$(( $(cat "$STUB_PS_CNT" 2>/dev/null || echo 0) + 1 ))
    echo "$n" > "$STUB_PS_CNT"
    [[ "$n" == "${STUB_PS_FAIL_AT:--1}" ]] && exit 2
    if [[ "$n" == "${STUB_PS_GATE_AT:--1}" && -n "${STUB_PS_GATE:-}" ]]; then
      for _ in $(seq 1 600); do [[ -f "$STUB_PS_GATE" ]] && break; sleep 0.05; done
    fi
  fi
  [[ "${STUB_PS_RC:-0}" == "0" ]] || exit "$STUB_PS_RC"
  if [[ -n "${STUB_STATE_FILE:-}" ]]; then cat "$STUB_STATE_FILE"
  else printf '%s\n' ${STUB_RUNNING_SERVICES:-api web db}; fi
  exit 0
fi
# emula: docker compose ... exec -T db pg_restore -l  (validação)
if [[ "$*" == *"pg_restore -l"* ]]; then
  cat >/dev/null   # consome o dump do stdin
  exit "${STUB_L_RC:-0}"
fi
# emula: docker compose ... exec -T db pg_restore ...  (restauração)
if [[ "$*" == *"pg_restore"* ]]; then
  cat >/dev/null
  exit "${STUB_PGRESTORE_RC:-0}"
fi
# emula stop: remove serviços; STUB_STOP_KEPT deixa um escritor de pé
if [[ "$*" == *" stop "* ]]; then
  if [[ -n "${STUB_STATE_FILE:-}" ]]; then
    for s in api web; do
      [[ "$*" == *" $s"* ]] && grep -vx "$s" "$STUB_STATE_FILE" \
        > "$STUB_STATE_FILE.tmp" && mv "$STUB_STATE_FILE.tmp" "$STUB_STATE_FILE"
    done
    [[ -n "${STUB_STOP_KEPT:-}" ]] && echo "$STUB_STOP_KEPT" >> "$STUB_STATE_FILE"
  fi
  exit "${STUB_STOP_RC:-0}"
fi
# emula start: religa os serviços pedidos, resultado controlável
if [[ "$*" == *" start "* ]]; then
  if [[ -n "${STUB_STATE_FILE:-}" && "${STUB_START_RC:-0}" == "0" ]]; then
    for s in api web; do
      [[ "$*" == *" $s"* ]] && { grep -qx "$s" "$STUB_STATE_FILE" \
        || echo "$s" >> "$STUB_STATE_FILE"; }
    done
  fi
  exit "${STUB_START_RC:-0}"
fi
exit 0
EOF
  chmod +x "$dir/docker"
}

# stub restic: registra chamadas, controla snapshots/init/backup e
# opcionalmente CONGELA no backup (gate) para teste de concorrência
stub_restic() {
  local dir="$1"
  cat > "$dir/restic" <<'EOF'
#!/usr/bin/env bash
echo "restic $*" >> "$STUB_LOG"
all="$*"
if [[ "$all" == *"cat config"* ]]; then
  exit "${STUB_CAT_RC:-0}"
fi
if [[ "$all" == *"snapshots"* && "$all" == *"--json"* ]]; then
  echo '[{"short_id":"abc1234"}]'; exit 0
fi
if [[ "$all" == *"init"* ]]; then
  exit "${STUB_INIT_RC:-0}"
fi
if [[ "$all" == *"backup"* ]]; then
  # copia a árvore de staging para inspeção do teste
  for a in "$@"; do
    [[ -d "$a" && -n "${STUB_STAGE_COPY:-}" ]] && cp -a "$a/." "$STUB_STAGE_COPY/"
  done
  if [[ -n "${STUB_RESTIC_GATE:-}" ]]; then
    for _ in $(seq 1 600); do [[ -f "$STUB_RESTIC_GATE" ]] && break; sleep 0.05; done
  fi
  exit "${STUB_BACKUP_RC:-0}"
fi
exit 0
EOF
  chmod +x "$dir/restic"
}

# stub pg_restore para a validação -l do script (aceita qualquer -l)
stub_pg_restore() {
  local dir="$1"
  cat > "$dir/pg_restore" <<'EOF'
#!/usr/bin/env bash
[[ "$1" == "-l" ]] && exit 0
exit "${STUB_PGRESTORE_RC:-0}"
EOF
  chmod +x "$dir/pg_restore"
}

# espelha a estrutura mínima do projeto para rodar os scripts isolados
stage_central() {
  local dst="$1"
  mkdir -p "$dst/scripts" "$dst/secrets" "$dst/tls" "$dst/backups"
  cp "$CENTRAL_REAL/scripts/_lib.sh" "$dst/scripts/"
  cp "$CENTRAL_REAL/scripts/up.sh" "$dst/scripts/"
  cp "$CENTRAL_REAL/scripts/backup.sh" "$dst/scripts/"
  cp "$CENTRAL_REAL/scripts/restore.sh" "$dst/scripts/"
  touch "$dst/compose.yaml"
}

write_env() {
  cat > "$1/.env" <<EOF
CENTRAL_BIND=127.0.0.1
CENTRAL_HTTPS_PORT=8443
$2
EOF
}

write_secrets() { # dir user db password url-pw
  cat > "$1/secrets/postgres.env" <<EOF
POSTGRES_USER=macip_central
POSTGRES_DB=macip_central
POSTGRES_PASSWORD=$4
EOF
  cat > "$1/secrets/api.env" <<EOF
MACIP_API_DATABASE_URL=postgresql+psycopg://macip_central:$5@db:5432/macip_central
MACIP_API_ADMIN_TOKEN=fake-token-teste-000
EOF
  chmod 600 "$1/secrets/postgres.env" "$1/secrets/api.env"
}

write_tls() { # dir [cert-path key-path]
  local cert="${2:-$1/tls/cert.pem}" key="${3:-$1/tls/key.pem}"
  mkdir -p "$(dirname "$cert")" "$(dirname "$key")"
  echo "CERT-PUBLICO" > "$cert"; chmod 644 "$cert"
  echo "CHAVE-PRIVADA" > "$key"; chmod 600 "$key"
}
