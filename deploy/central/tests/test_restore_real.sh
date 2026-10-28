#!/usr/bin/env bash
# Prova REAL do restore.sh: o script dirigido a um PostgreSQL
# descartável por um shim de `docker` que traduz as chamadas
# `compose exec -T db` para `docker exec` no container real.
# As operações PostgreSQL são reais — só o roteamento é adaptado.
# Comprova: restauração bem-sucedida, falha no MEIO da aplicação e
# rollback completo visto por conexão independente. Sem pg_restore
# instalado no host (a validação também passa pelo container).
set -u
TESTS_DIR="$(cd "$(dirname "$0")" && pwd)"
CENTRAL_REAL="$(cd "$TESTS_DIR/.." && pwd)"
source "$TESTS_DIR/_harness.sh"

echo "== restore.sh ponta a ponta — PostgreSQL real descartável =="

REAL_DOCKER="$(command -v docker || true)"
[[ -n "$REAL_DOCKER" ]] && "$REAL_DOCKER" info >/dev/null 2>&1 \
  || { echo "  SKIP: sem runtime de contêiner (prova real pendente)"; exit 0; }

CTR="macip-rst-real-$$"
PW="rst-pw-$$"
"$REAL_DOCKER" run -d --name "$CTR" -e "POSTGRES_PASSWORD=$PW" \
  docker.io/library/postgres:17-alpine >/dev/null
trap '"$REAL_DOCKER" rm -f "$CTR" >/dev/null 2>&1; rm -rf "$WORK"' EXIT
for i in $(seq 1 30); do
  "$REAL_DOCKER" exec "$CTR" pg_isready -U postgres >/dev/null 2>&1 && break
  sleep 1
done
"$REAL_DOCKER" exec "$CTR" createdb -U postgres rst_db

psql() { "$REAL_DOCKER" exec "$CTR" psql -U postgres -d rst_db -tAc "$1"; }

# projeto espelhado apontando secrets para o container descartável
CENTRAL="$WORK/central"; stage_central "$CENTRAL"
cat > "$CENTRAL/secrets/postgres.env" <<'EOF'
POSTGRES_USER=postgres
POSTGRES_DB=rst_db
POSTGRES_PASSWORD=fixture-sem-segredo
EOF
chmod 600 "$CENTRAL/secrets/postgres.env"

# shim: compose exec → docker exec real; ps → sempre "de pé"
SHIM="$WORK/shim"; mkdir -p "$SHIM"
cat > "$SHIM/docker" <<EOF
#!/usr/bin/env bash
echo "\$*" >> "\$STUB_LOG"
all="\$*"
if [[ "\$all" == *" exec -T db "* ]]; then
  cmd="\${all#* exec -T db }"
  if [[ "\$cmd" == pg_restore* && -n "\${INJECT_TOC:-}" && "\$cmd" != *" -l"* ]]; then
    cmd="\$cmd -L /tmp/toc-midfail.txt"
  fi
  exec "$REAL_DOCKER" exec -i "$CTR" \$cmd
fi
if [[ "\$*" == *"ps --status running --services"* ]]; then
  printf 'api\nweb\ndb\n'; exit 0
fi
exit 0   # stop/start ficam só no log (o "banco" é o container real)
EOF
chmod +x "$SHIM/docker"
export STUB_LOG="$WORK/docker.log" PATH="$SHIM:$PATH"
export STUB_RUNNING_SERVICES="api web db"

# esquema com histórico + versão de migração
psql "CREATE TABLE schema_migrations(version text PRIMARY KEY);
      INSERT INTO schema_migrations VALUES('v02');
      CREATE TABLE exec_a(id serial PRIMARY KEY, task_id text);
      CREATE TABLE exec_b(id serial PRIMARY KEY, task_id text);
      INSERT INTO exec_a VALUES(1,'a1'),(2,'a2');
      INSERT INTO exec_b VALUES(1,'b1'),(2,'b2');"
DUMP="$WORK/base.pgdump"
"$REAL_DOCKER" exec "$CTR" pg_dump -U postgres -d rst_db -Fc > "$DUMP"

# --- caso 1: restauração BEM-SUCEDIDA pelo script -------------------
psql "INSERT INTO exec_a VALUES(9,'depois-do-dump');
      UPDATE schema_migrations SET version='v99';"
printf 'sim\n' | "$CENTRAL/scripts/restore.sh" "$DUMP" >/dev/null 2>&1
rc=$?
expect_eq "restauração pelo script = 0" "$rc" "0"
expect_eq "linha pós-dump removida" \
  "$(psql "SELECT count(*) FROM exec_a WHERE task_id='depois-do-dump'")" "0"
expect_eq "versão de migração restaurada" \
  "$(psql "SELECT version FROM schema_migrations")" "v02"
expect_contains "serviços stop/start pelo script" \
  "$(cat "$STUB_LOG")" "stop api web"
expect_contains "validação -l pelo contêiner" \
  "$(cat "$STUB_LOG")" "pg_restore -l"

# --- caso 2: falha NO MEIO → banco anterior preservado --------------
psql "INSERT INTO exec_a VALUES(50,'pre'),(51,'pre');
      ALTER TABLE exec_a ADD COLUMN extra int NOT NULL DEFAULT 7;
      UPDATE schema_migrations SET version='vAA';
      DROP TABLE exec_b;"
sig_pre="$(psql "SELECT md5(string_agg(md5(t::text),'' ORDER BY md5(t::text)))
                FROM (SELECT * FROM exec_a ORDER BY id) t")-$(psql "SELECT version FROM schema_migrations")-$(psql "SELECT count(*) FROM information_schema.columns WHERE table_name='exec_a' AND column_name='extra'")-$(psql "SELECT count(*) FROM information_schema.tables WHERE table_name='exec_b'")"

# TOC com schema+dados de exec_a (aplicado) e dados de exec_b SEM o
# schema — injetado via -L pelo shim: falha tardia dentro da transação
"$REAL_DOCKER" exec -i "$CTR" pg_restore -l < "$DUMP" \
  | grep -v '^;' > "$WORK/toc.txt"
{
  grep " TABLE public exec_a " "$WORK/toc.txt"
  grep " TABLE DATA public exec_a " "$WORK/toc.txt"
  grep " TABLE DATA public exec_b " "$WORK/toc.txt"
} > "$WORK/toc-midfail.txt"
"$REAL_DOCKER" cp "$WORK/toc-midfail.txt" "$CTR:/tmp/toc-midfail.txt"
export INJECT_TOC=1

set +e
out="$(printf 'sim\n' | "$CENTRAL/scripts/restore.sh" "$DUMP" 2>&1)"
rc=$?
set -e
unset INJECT_TOC
[[ $rc -ne 0 ]] && ok "restauração quebra no meio (via script)" \
  || bad "restauração quebra no meio (via script)"
expect_contains "mensagem preserva o banco anterior" "$out" "preservado"

# conexão INDEPENDENTE confirma rollback total de schema+dados+versão
sig_post="$(psql "SELECT md5(string_agg(md5(t::text),'' ORDER BY md5(t::text)))
                 FROM (SELECT * FROM exec_a ORDER BY id) t")-$(psql "SELECT version FROM schema_migrations")-$(psql "SELECT count(*) FROM information_schema.columns WHERE table_name='exec_a' AND column_name='extra'")-$(psql "SELECT count(*) FROM information_schema.tables WHERE table_name='exec_b'")"
expect_eq "schema+dados+versão+exec_b idênticos ao pré-restore" \
  "$sig_post" "$sig_pre"
expect_contains "serviços religados mesmo falhando" \
  "$(cat "$STUB_LOG")" "start api web"

"$REAL_DOCKER" rm -f "$CTR" >/dev/null 2>&1
trap 'rm -rf "$WORK"' EXIT
echo "-- restore real via script: $PASS ok, $FAIL falhas"
(( FAIL == 0 ))
