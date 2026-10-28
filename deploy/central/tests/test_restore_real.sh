#!/usr/bin/env bash
# Atomicidade REAL da restauração em PostgreSQL descartável:
# valida --single-transaction + --exit-on-error (o mesmo par de flags
# que restore.sh usa), com falha NO MEIO da aplicação — o TOC filtrado
# restaura uma tabela inteira primeiro e depois tenta carregar dados
# de uma tabela cujo schema foi omitido da lista (erro tardio).
set -u
TESTS_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$TESTS_DIR/_harness.sh"

echo "== restore atômico — PostgreSQL real descartável =="

command -v docker >/dev/null && docker info >/dev/null 2>&1 \
  || { echo "  SKIP: sem runtime de contêiner"; exit 0; }

CTR="macip-rst-test-$$"
PW="rst-pw-$$"
docker run -d --name "$CTR" -e "POSTGRES_PASSWORD=$PW" \
  docker.io/library/postgres:17-alpine >/dev/null
trap 'docker rm -f "$CTR" >/dev/null 2>&1; rm -rf "$WORK"' EXIT
for i in $(seq 1 30); do
  docker exec "$CTR" pg_isready -U postgres >/dev/null 2>&1 && break
  sleep 1
done

psql() { docker exec "$CTR" psql -U postgres -d rst_db -tAc "$1"; }
docker exec "$CTR" createdb -U postgres rst_db

# esquema com histórico + linha de versão de migração
psql "CREATE TABLE schema_migrations(version text PRIMARY KEY);
      INSERT INTO schema_migrations VALUES('v02');
      CREATE TABLE exec_a(id serial PRIMARY KEY, task_id text);
      CREATE TABLE exec_b(id serial PRIMARY KEY, task_id text);
      INSERT INTO exec_a VALUES(1,'a1'),(2,'a2');
      INSERT INTO exec_b VALUES(1,'b1'),(2,'b2');"
DUMP="$WORK/base.pgdump"
docker exec "$CTR" pg_dump -U postgres -d rst_db -Fc > "$DUMP"

# --- caso 1: restauração BEM-SUCEDIDA devolve o estado do dump -----
psql "INSERT INTO exec_a VALUES(9,'depois-do-dump');
      UPDATE schema_migrations SET version='v99';"
cat "$DUMP" | docker exec -i "$CTR" pg_restore -U postgres -d rst_db \
  --clean --if-exists --no-owner --single-transaction --exit-on-error
rc=$?
expect_eq "restauração sucesso" "$rc" "0"
expect_eq "linha inserida após dump sumiu" \
  "$(psql "SELECT count(*) FROM exec_a WHERE task_id='depois-do-dump'")" "0"
expect_eq "versão de migração restaurada" \
  "$(psql "SELECT version FROM schema_migrations")" "v02"

# --- caso 2: falha NO MEIO da aplicação → banco anterior preservado --
# marca pré-restore (schema + dados + versão)
psql "INSERT INTO exec_a VALUES(50,'pre'),(51,'pre');
      ALTER TABLE exec_a ADD COLUMN extra int NOT NULL DEFAULT 7;
      UPDATE schema_migrations SET version='vAA';
      DROP TABLE exec_b;"
sig_pre="$(psql "SELECT md5(string_agg(md5(t::text),'' ORDER BY md5(t::text)))
                FROM (SELECT * FROM exec_a ORDER BY id) t")-$(psql "SELECT version FROM schema_migrations")-$(psql "SELECT count(*) FROM information_schema.columns WHERE table_name='exec_a' AND column_name='extra'")-$(psql "SELECT count(*) FROM information_schema.tables WHERE table_name='exec_b'")"

# TOC reordenado: schema+dados de exec_a (aplicado), dados de exec_b
# sem o schema de exec_b (falha tardia) — itens iniciais já aplicados
docker exec -i "$CTR" pg_restore -l < "$DUMP" | grep -v '^;' > "$WORK/toc.txt"
{
  grep " TABLE public exec_a " "$WORK/toc.txt"
  grep " TABLE DATA public exec_a " "$WORK/toc.txt"
  grep " TABLE DATA public exec_b " "$WORK/toc.txt"
} > "$WORK/toc-midfail.txt"
grep -c "TABLE DATA public exec_b" "$WORK/toc-midfail.txt" >/dev/null \
  || { echo "  SKIP: TOC inesperado"; exit 0; }
docker cp "$WORK/toc-midfail.txt" "$CTR:/tmp/toc.txt"

set +e
cat "$DUMP" | docker exec -i "$CTR" pg_restore -U postgres -d rst_db \
  --clean --if-exists --no-owner --single-transaction --exit-on-error \
  -L /tmp/toc.txt >/dev/null 2>&1
rc=$?
set -e
[[ $rc -ne 0 ]] && ok "restauração quebra no meio (dado sem schema)" \
  || bad "restauração quebra no meio (dado sem schema)"

# conexão INDEPENDENTE confirma rollback total de schema+dados
sig_post="$(psql "SELECT md5(string_agg(md5(t::text),'' ORDER BY md5(t::text)))
                 FROM (SELECT * FROM exec_a ORDER BY id) t")-$(psql "SELECT version FROM schema_migrations")-$(psql "SELECT count(*) FROM information_schema.columns WHERE table_name='exec_a' AND column_name='extra'")-$(psql "SELECT count(*) FROM information_schema.tables WHERE table_name='exec_b'")"
expect_eq "schema+dados+versão+exec_b idênticos ao pré-restore" \
  "$sig_post" "$sig_pre"

docker rm -f "$CTR" >/dev/null 2>&1
trap 'rm -rf "$WORK"' EXIT
echo "-- restore real: $PASS ok, $FAIL falhas"
(( FAIL == 0 ))
