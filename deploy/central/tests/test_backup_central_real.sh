#!/usr/bin/env bash
# Prova REAL do central-backup.sh: restic real + PostgreSQL
# descartável (shim docker roteia compose exec ao container).
# Comprova: dump real + config/TLS criptografados no repositório,
# restauração com comparação byte a byte e recuperação do banco em
# PostgreSQL descartável. Skip limpo sem runtime.
set -u
TESTS_DIR="$(cd "$(dirname "$0")" && pwd)"
CENTRAL_REAL="$(cd "$TESTS_DIR/.." && pwd)"
source "$TESTS_DIR/_harness.sh"

echo "== central-backup.sh ponta a ponta — restic + PG reais =="

REAL_DOCKER="$(command -v docker || true)"
RESTIC_BIN="$(command -v restic || true)"
[[ -n "$REAL_DOCKER" && -n "$RESTIC_BIN" ]] \
  && "$REAL_DOCKER" info >/dev/null 2>&1 \
  || { echo "  SKIP: sem runtime de contêiner ou restic (prova pendente)"; exit 0; }

CTR="macip-cbk-real-$$"
PW="cbk-pw-$$"
"$REAL_DOCKER" run -d --name "$CTR" -e "POSTGRES_PASSWORD=$PW" \
  docker.io/library/postgres:17-alpine >/dev/null
trap '"$REAL_DOCKER" rm -f "$CTR" >/dev/null 2>&1; rm -rf "$WORK"' EXIT
for i in $(seq 1 30); do
  "$REAL_DOCKER" exec "$CTR" pg_isready -U postgres >/dev/null 2>&1 && break
  sleep 1
done
"$REAL_DOCKER" exec "$CTR" createdb -U postgres rst_db
psql() { "$REAL_DOCKER" exec "$CTR" psql -U postgres -d rst_db -tAc "$1"; }
psql "CREATE TABLE exec_a(id serial PRIMARY KEY, task_id text);
      INSERT INTO exec_a VALUES(1,'a1'),(2,'a2');"

# projeto espelhado com secrets do container
PROJ="$WORK/proj"; CENTRAL="$PROJ/deploy/central"
stage_central "$CENTRAL"
cp "$CENTRAL_REAL/scripts/central-backup.sh" \
   "$CENTRAL_REAL/scripts/install-central-backup-timer.sh" "$CENTRAL/scripts/"
chmod +x "$CENTRAL/scripts/"*.sh
mkdir -p "$PROJ/src"
ln -s "$CENTRAL_REAL/../../src/macip_backup" "$PROJ/src/macip_backup"
write_env "$CENTRAL" ""
cat > "$CENTRAL/secrets/postgres.env" <<'EOF'
POSTGRES_USER=postgres
POSTGRES_DB=rst_db
POSTGRES_PASSWORD=fixture-sem-segredo
EOF
cat > "$CENTRAL/secrets/api.env" <<'EOF'
MACIP_API_DATABASE_URL=postgresql+psycopg://postgres:x@db:5432/rst_db
MACIP_API_ADMIN_TOKEN=fake-token-fixture
EOF
chmod 600 "$CENTRAL/secrets/"*.env
write_tls "$CENTRAL"
echo "LAB-CA-PUB-FIXTURE" > "$CENTRAL/tls/lab-ca.pem"
echo "LAB-CA-KEY-FIXTURE" > "$CENTRAL/tls/lab-ca.key"; chmod 600 "$CENTRAL/tls/lab-ca.key"

# shim: compose exec → docker exec real; demais chamadas ok/log
SHIM="$WORK/shim"; mkdir -p "$SHIM"
cat > "$SHIM/docker" <<EOF
#!/usr/bin/env bash
echo "\$*" >> "\$STUB_LOG"
all="\$*"
if [[ "\$all" == "info" ]]; then exec "$REAL_DOCKER" info; fi
if [[ "\$all" == *" exec -T db "* ]]; then
  cmd="\${all#* exec -T db }"
  exec "$REAL_DOCKER" exec -i "$CTR" \$cmd
fi
exit 0
EOF
chmod +x "$SHIM/docker"
export STUB_LOG="$WORK/docker.log" PATH="$SHIM:$PATH"

# repo restic LOCAL real (destino externo SFTP = pendência documentada)
REPO="$WORK/restic-repo"
cat > "$CENTRAL/secrets/central-backup.env" <<EOF
CENTRAL_BACKUP_REPOSITORY=$REPO
CENTRAL_BACKUP_PASSWORD_NAME=MACIP_CBK_TEST_PW
EOF
chmod 600 "$CENTRAL/secrets/central-backup.env"
export MACIP_CBK_TEST_PW="senha-repo-fixture-1q2w"
export RESTIC_PASSWORD="$MACIP_CBK_TEST_PW" RESTIC_REPOSITORY="$REPO"

"$CENTRAL/scripts/central-backup.sh" > "$WORK/run.out" 2>&1
rc=$?
expect_eq "central-backup real = 0" "$rc" "0"
expect_contains "snapshot anunciado" "$(cat "$WORK/run.out")" "snapshot"
runlog="$(cat "$STUB_LOG"; cat "$WORK/run.out")"
case "$runlog" in *senha-repo*|*fixture-sem-segredo*|*fake-token*)
  bad "segredos fora do log";; *) ok "segredos fora do log";; esac

# restaura do repositório e compara byte a byte
OUT="$WORK/restored"; mkdir -p "$OUT"
"$RESTIC_BIN" -r "$REPO" restore latest --target "$OUT" >/dev/null
RC_DIR="$(find "$OUT" -name config -type d | head -1)"
expect_file "dump restaurado" "$(dirname "$RC_DIR")/postgres.pgdump"
expect_eq "postgres.env idêntico" \
  "$(md5sum "$RC_DIR/secrets/postgres.env" | cut -d' ' -f1)" \
  "$(md5sum "$CENTRAL/secrets/postgres.env" | cut -d' ' -f1)"
expect_eq "key TLS idêntica" \
  "$(md5sum "$RC_DIR/tls/key.pem" | cut -d' ' -f1)" \
  "$(md5sum "$CENTRAL/tls/key.pem" | cut -d' ' -f1)"
expect_eq "lab CA key idêntica" \
  "$(md5sum "$RC_DIR/tls/lab-ca.key" | cut -d' ' -f1)" \
  "$(md5sum "$CENTRAL/tls/lab-ca.key" | cut -d' ' -f1)"
expect_eq "key restaurada 600" \
  "$(stat -c %a "$RC_DIR/tls/lab-ca.key")" "600"

# recuperação do banco em PostgreSQL descartável
"$REAL_DOCKER" exec "$CTR" createdb -U postgres rst_db2 2>/dev/null
cat "$(dirname "$RC_DIR")/postgres.pgdump" | "$REAL_DOCKER" exec -i "$CTR" \
  pg_restore -U postgres -d rst_db2 --clean --if-exists --no-owner \
  --single-transaction --exit-on-error
rc=$?
expect_eq "dump restaura em PG descartável" "$rc" "0"
expect_eq "conteúdo do banco recuperado" \
  "$("$REAL_DOCKER" exec "$CTR" psql -U postgres -d rst_db2 -tAc \
    "SELECT string_agg(task_id,',' ORDER BY id) FROM exec_a")" "a1,a2"

"$REAL_DOCKER" rm -f "$CTR" >/dev/null 2>&1
trap 'rm -rf "$WORK"' EXIT
echo "-- backup central real: $PASS ok, $FAIL falhas"
(( FAIL == 0 ))
