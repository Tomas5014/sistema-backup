#!/usr/bin/env bash
# central-backup.sh + install-central-backup-timer.sh com Docker e
# restic SIMULADOS: coleta, manifesto, segredos fora do log, lock,
# preservação da cópia anterior, destino SFTP e units.
set -u
TESTS_DIR="$(cd "$(dirname "$0")" && pwd)"
CENTRAL_REAL="$(cd "$TESTS_DIR/.." && pwd)"
source "$TESTS_DIR/_harness.sh"

PROJ="$WORK/proj"
CENTRAL="$PROJ/deploy/central"
stage_central "$CENTRAL"
cp "$CENTRAL_REAL/scripts/central-backup.sh" "$CENTRAL/scripts/"
cp "$CENTRAL_REAL/scripts/install-central-backup-timer.sh" "$CENTRAL/scripts/"
chmod +x "$CENTRAL/scripts/"*.sh
write_env "$CENTRAL" 'CENTRAL_TLS_CERT="./certs tls/cert.pem"
CENTRAL_TLS_KEY="./certs tls/key.pem"'
write_secrets "$CENTRAL" "" "" "pw-banco" "pw-banco"
write_tls "$CENTRAL" "$CENTRAL/certs tls/cert.pem" "$CENTRAL/certs tls/key.pem"
# código do agente resolvido em ../../src
mkdir -p "$PROJ/src"
ln -s "$CENTRAL_REAL/../../src/macip_backup" "$PROJ/src/macip_backup"
# CA de laboratório presente
echo "LAB-CA-PUB" > "$CENTRAL/tls/lab-ca.pem"
echo "LAB-CA-KEY" > "$CENTRAL/tls/lab-ca.key"; chmod 600 "$CENTRAL/tls/lab-ca.key"
touch "$CENTRAL/tls/lab-ca.srl"
# configuração do backup (senha via env-name → mecanismo de credenciais)
cat > "$CENTRAL/secrets/central-backup.env" <<'EOF'
CENTRAL_BACKUP_REPOSITORY=local:/tmp/macip-test-repo
CENTRAL_BACKUP_PASSWORD_NAME=MACIP_TEST_BACKUP_PW_XYZ
EOF
chmod 600 "$CENTRAL/secrets/central-backup.env"
export MACIP_TEST_BACKUP_PW_XYZ="senha-repo-SEGREDO-7x9"

STUB_DIR="$WORK/stubbin"; mkdir -p "$STUB_DIR"
stub_docker "$STUB_DIR"; stub_restic "$STUB_DIR"
export STUB_LOG="$WORK/docker.log"; : > "$STUB_LOG"
export PATH="$STUB_DIR:$PATH"
export STUB_STATE_FILE="$WORK/state.txt"
printf 'api\nweb\ndb\n' > "$STUB_STATE_FILE"

echo "== central-backup.sh (docker+restic simulados) =="

# 1) config ausente → recusa antes de chamar docker
mv "$CENTRAL/secrets/central-backup.env" "$WORK/backup.env.bak"
set +e
"$CENTRAL/scripts/central-backup.sh" >/dev/null 2>&1
rc=$?
set -e
expect_eq "sem config → falha" "$rc" "1"
expect_eq "docker NÃO chamado" "$(wc -l < "$STUB_LOG")" "0"
mv "$WORK/backup.env.bak" "$CENTRAL/secrets/central-backup.env"
chmod 600 "$CENTRAL/secrets/central-backup.env"

# 2) docker inacessível neste contexto → diagnóstico de permissão
export STUB_DOCKER_INFO_RC=1
set +e
out="$("$CENTRAL/scripts/central-backup.sh" 2>&1)"; rc=$?
set -e
expect_eq "docker indisponível → falha" "$rc" "1"
expect_contains "diagnóstico de contexto" "$out" "docker"
unset STUB_DOCKER_INFO_RC

# 3) senha do repositório ausente → erro de credencial, sem dump
export MACIP_TEST_BACKUP_PW_XYZ=""
: > "$STUB_LOG"
set +e
out="$("$CENTRAL/scripts/central-backup.sh" 2>&1)"; rc=$?
set -e
expect_eq "sem senha repo → falha" "$rc" "1"
expect_contains "diagnóstico cita credencial" "$out" "credencial"
expect_eq "dump NÃO gerado" "$(grep -c 'pg_dump' "$STUB_LOG" || true)" "0"
export MACIP_TEST_BACKUP_PW_XYZ="senha-repo-SEGREDO-7x9"

# 4) sucesso: staging completo, manifesto, segredos fora do log
export STUB_STAGE_COPY="$WORK/stage-copy"
mkdir -p "$STUB_STAGE_COPY"
"$CENTRAL/scripts/central-backup.sh" >/dev/null 2>&1
rc=$?
expect_eq "backup central sucesso" "$rc" "0"
expect_file "dump no staging enviado" "$STUB_STAGE_COPY/postgres.pgdump"
expect_file "postgres.env coletado" "$STUB_STAGE_COPY/config/secrets/postgres.env"
expect_file "api.env coletado" "$STUB_STAGE_COPY/config/secrets/api.env"
expect_file "cert TLS custom coletado" "$STUB_STAGE_COPY/config/tls/cert.pem"
expect_file "key TLS custom coletada" "$STUB_STAGE_COPY/config/tls/key.pem"
expect_file "lab CA pública" "$STUB_STAGE_COPY/config/tls/lab-ca.pem"
expect_file "lab CA privada" "$STUB_STAGE_COPY/config/tls/lab-ca.key"
expect_file "manifesto" "$STUB_STAGE_COPY/MANIFEST.txt"
expect_contains "manifesto com data UTC" \
  "$(cat "$STUB_STAGE_COPY/MANIFEST.txt")" "created_utc"
expect_contains "manifesto com projeto" \
  "$(cat "$STUB_STAGE_COPY/MANIFEST.txt")" "compose_project"
man="$(cat "$STUB_STAGE_COPY/MANIFEST.txt")"
case "$man" in *pw-banco*|*senha-repo*|*SEGREDO*) bad "manifesto sem segredos";;
  *) ok "manifesto sem segredos";; esac
log="$(cat "$STUB_LOG")"
case "$log" in *pw-banco*|*senha-repo*|*SEGREDO-7x9*) bad "log sem segredos";;
  *) ok "log sem segredos";; esac
expect_eq "staging temporário removido" \
  "$(find "$CENTRAL/backups" -name '.stage.*' | wc -l)" "0"
expect_eq "segredos coletados 600" \
  "$(stat -c %a "$STUB_STAGE_COPY/config/secrets/api.env")" "600"

# 5) falha no envio → código preservado, cópia anterior intacta
: > "$STUB_LOG"
export STUB_BACKUP_RC=4
set +e
out="$("$CENTRAL/scripts/central-backup.sh" 2>&1)"; rc=$?
set -e
expect_eq "falha no envio → código preservado" "$rc" "4"
expect_contains "cópia anterior citada" "$out" "anterior"
expect_eq "staging limpo após falha" \
  "$(find "$CENTRAL/backups" -name '.stage.*' | wc -l)" "0"
unset STUB_BACKUP_RC

# 6) repositório inacessível (snapshots+init falham) → erro de repositório
export STUB_SNAPS_RC=1 STUB_INIT_RC=3
set +e
out="$("$CENTRAL/scripts/central-backup.sh" 2>&1)"; rc=$?
set -e
expect_eq "destino indisponível → falha" "$rc" "1"
expect_contains "diagnóstico cita repositório" "$out" "repositório"
unset STUB_SNAPS_RC STUB_INIT_RC

# 7) concorrência: segunda execução enquanto a 1ª retém o lock
export STUB_RESTIC_GATE="$WORK/restic-gate"
rm -f "$STUB_RESTIC_GATE"
"$CENTRAL/scripts/central-backup.sh" >/dev/null 2>&1 &
FIRST=$!
# espera a 1ª chegar ao restic backup (gate)
until grep -q "restic.*backup" "$STUB_LOG" 2>/dev/null; do sleep 0.05; done
set +e
out="$("$CENTRAL/scripts/central-backup.sh" 2>&1)"; rc=$?
set -e
expect_eq "2ª execução bloqueada" "$rc" "1"
expect_contains "diagnóstico cita lock" "$out" "lock"
: > "$STUB_RESTIC_GATE"   # libera a 1ª
wait "$FIRST" || true
unset STUB_RESTIC_GATE

# 8) destino SFTP: chave + known_hosts viram sftp.command
cat > "$CENTRAL/secrets/central-backup.env" <<EOF
CENTRAL_BACKUP_REPOSITORY=sftp:op@host.lab:/srv/bk
CENTRAL_BACKUP_PASSWORD_NAME=MACIP_TEST_BACKUP_PW_XYZ
CENTRAL_BACKUP_SFTP_KEY=$WORK/sftp-key
CENTRAL_BACKUP_SFTP_KNOWN_HOSTS=$WORK/known_hosts
EOF
chmod 600 "$CENTRAL/secrets/central-backup.env"
echo KEY > "$WORK/sftp-key"; chmod 600 "$WORK/sftp-key"
echo "host.lab ssh-ed25519 AAAA" > "$WORK/known_hosts"
: > "$STUB_LOG"
"$CENTRAL/scripts/central-backup.sh" >/dev/null 2>&1
rc=$?
expect_eq "sftp configurado passa" "$rc" "0"
expect_contains "sftp.command com chave" "$(cat "$STUB_LOG")" "-i $WORK/sftp-key"
expect_contains "sftp.command com known_hosts" \
  "$(cat "$STUB_LOG")" "UserKnownHostsFile=$WORK/known_hosts"
expect_contains "host verificado (strict)" \
  "$(cat "$STUB_LOG")" "StrictHostKeyChecking=yes"

# 9) units: geração, caminho com espaço, units alheias preservadas
UNIT_HOME="$WORK/unithome"; mkdir -p "$UNIT_HOME"
export HOME="$UNIT_HOME" XDG_CONFIG_HOME="$UNIT_HOME/.config"
"$CENTRAL/scripts/install-central-backup-timer.sh" "*-*-* 02:15:00" >/dev/null
UDIR="$UNIT_HOME/.config/systemd/user"
expect_file "service gerado" "$UDIR/macip-central-backup.service"
expect_file "timer gerado" "$UDIR/macip-central-backup.timer"
expect_contains "OnCalendar no timer" \
  "$(cat "$UDIR/macip-central-backup.timer")" "OnCalendar=*-*-* 02:15:00"
expect_contains "Persistent" \
  "$(cat "$UDIR/macip-central-backup.timer")" "Persistent=true"
expect_contains "ExecStart citado" \
  "$(cat "$UDIR/macip-central-backup.service")" "ExecStart=\"$CENTRAL/scripts/central-backup.sh\""
expect_contains "marcador de propriedade" \
  "$(cat "$UDIR/macip-central-backup.service")" "central-backup=$CENTRAL"
# unit alheia (sem marcador) não é sobrescrita
echo "UNIT-ALHEIA" > "$UDIR/macip-central-backup.service"
set +e
"$CENTRAL/scripts/install-central-backup-timer.sh" >/dev/null 2>&1
rc=$?
set -e
expect_eq "unit alheia recusada" "$rc" "1"
expect_eq "conteúdo alheio preservado" \
  "$(cat "$UDIR/macip-central-backup.service")" "UNIT-ALHEIA"

echo "-- backup central: $PASS ok, $FAIL falhas"
(( FAIL == 0 ))
