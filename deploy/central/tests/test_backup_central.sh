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
# manifesto: hash de cada não-secreto confere DEPOIS de pronto;
# segredos marcados por role, MANIFEST não se auto-hasheia
bad_sha=0
while IFS= read -r line; do
  f="${line%% *}"; got="${line##* }"
  if [[ "$got" == "role=secret" ]]; then
    [[ -f "$STUB_STAGE_COPY/config/secrets/$(basename "$f")" || \
       "$f" == "config/tls/key.pem" || "$f" == "config/tls/lab-ca.key" ]] \
      || bad_sha=1
    continue
  fi
  real="$(cd "$STUB_STAGE_COPY" && sha256sum "$f" | cut -d' ' -f1)"
  [[ "$real" == "$got" ]] || bad_sha=1
done <<< "$(sed -n '/^-- sha256/,$p' "$STUB_STAGE_COPY/MANIFEST.txt" | tail -n +2)"
expect_eq "manifesto: hashes conferem" "$bad_sha" "0"
expect_eq "manifesto NÃO se auto-hasheia" \
  "$(grep -c 'MANIFEST.txt ' "$STUB_STAGE_COPY/MANIFEST.txt" || true)" "0"
expect_contains "manifesto identifica segredo por função" \
  "$(cat "$STUB_STAGE_COPY/MANIFEST.txt")" "key.pem role=secret"

# 4b) TLS: symlink coletado como CONTEÚDO; mesmo basename não colide
mkdir -p "$CENTRAL/keys tls"
ln -sf "cert.pem" "$CENTRAL/certs tls/link.crt"  # symlink relativo
echo KEY-CONTEUDO > "$CENTRAL/keys tls/link.crt" # MESMO basename!
write_env "$CENTRAL" 'CENTRAL_TLS_CERT="./certs tls/link.crt"
CENTRAL_TLS_KEY="./keys tls/link.crt"'
rm -rf "$STUB_STAGE_COPY"; mkdir -p "$STUB_STAGE_COPY"
set +e
"$CENTRAL/scripts/central-backup.sh" >/dev/null 2>&1
rc=$?
set -e
expect_eq "TLS symlink+mesmo nome → sucesso" "$rc" "0"
case "$(stat -c %F "$STUB_STAGE_COPY/config/tls/cert.pem")" in
  *link*) bad "cert coletado como conteúdo";; *) ok "cert coletado como conteúdo";; esac
expect_eq "cert restaura o CONTEÚDO do alvo" \
  "$(cat "$STUB_STAGE_COPY/config/tls/cert.pem")" "CERT-PUBLICO"
expect_eq "key de mesmo basename não colide" \
  "$(cat "$STUB_STAGE_COPY/config/tls/key.pem")" "KEY-CONTEUDO"
# restaura os originais: remova-os — conteúdo vem do snapshot
rm -f "$CENTRAL/certs tls/cert.pem" "$CENTRAL/keys tls/link.crt"
expect_eq "cert recuperável após remoção" \
  "$(cat "$STUB_STAGE_COPY/config/tls/cert.pem")" "CERT-PUBLICO"
write_tls "$CENTRAL" "$CENTRAL/certs tls/cert.pem" "$CENTRAL/certs tls/key.pem"
write_env "$CENTRAL" 'CENTRAL_TLS_CERT="./certs tls/cert.pem"
CENTRAL_TLS_KEY="./certs tls/key.pem"'

# 4c) VERSION do artefato (git archive) refletida no manifesto
echo "deadbeef1234 2026-10-28T00:00:00Z" > "$CENTRAL/VERSION"
rm -rf "$STUB_STAGE_COPY"; mkdir -p "$STUB_STAGE_COPY"
set +e
"$CENTRAL/scripts/central-backup.sh" >/dev/null 2>&1
rc=$?
set -e
expect_eq "VERSION-artifact run sucesso" "$rc" "0"
expect_contains "manifesto com version do artefato" \
  "$(grep '^version=' "$STUB_STAGE_COPY/MANIFEST.txt")" "version=deadbeef1234"
rm -f "$CENTRAL/VERSION"

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

# 6) cat config com erro ≠10 (senha/acesso/bloqueio) → falha SEM init
export STUB_CAT_RC=12
: > "$STUB_LOG"
set +e
out="$("$CENTRAL/scripts/central-backup.sh" 2>&1)"; rc=$?
set -e
expect_eq "erro de acesso → falha" "$rc" "1"
expect_contains "diagnóstico cita repositório" "$out" "repositório"
expect_eq "init NÃO tentado" "$(grep -c 'restic init' "$STUB_LOG" || true)" "0"
unset STUB_CAT_RC

# 6b) cat config rc=10 → repo inexistente → init é tentado
export STUB_CAT_RC=10
: > "$STUB_LOG"
"$CENTRAL/scripts/central-backup.sh" >/dev/null 2>&1
expect_contains "rc10 → init executado" "$(cat "$STUB_LOG")" "restic init"
unset STUB_CAT_RC

# 7) concorrência: segunda execução enquanto a 1ª retém o lock
export STUB_RESTIC_GATE="$WORK/restic-gate"
rm -f "$STUB_RESTIC_GATE"
: > "$STUB_LOG"   # só linhas FRESCAS desta execução contam no gate
"$CENTRAL/scripts/central-backup.sh" >/dev/null 2>&1 &
FIRST=$!
# espera a 1ª chegar ao restic backup (gate)
until grep -q "^restic backup " "$STUB_LOG" 2>/dev/null; do sleep 0.05; done
set +e
out="$("$CENTRAL/scripts/central-backup.sh" 2>&1)"; rc=$?
set -e
expect_eq "2ª execução bloqueada" "$rc" "1"
expect_contains "diagnóstico cita lock" "$out" "lock"
: > "$STUB_RESTIC_GATE"   # libera a 1ª
wait "$FIRST" || true
unset STUB_RESTIC_GATE

# 8) destino SFTP: sftp.args via shlex.join — caminhos com espaço
#    chegam como UM argumento, com host key estrita e timeouts
SFTP_KEY_SP="$WORK/minha chave.pem"
KH_SP="$WORK/known hosts lab"
cat > "$CENTRAL/secrets/central-backup.env" <<EOF
CENTRAL_BACKUP_REPOSITORY=sftp:op@host.lab:/srv/bk
CENTRAL_BACKUP_PASSWORD_NAME=MACIP_TEST_BACKUP_PW_XYZ
CENTRAL_BACKUP_SFTP_KEY="$SFTP_KEY_SP"
CENTRAL_BACKUP_SFTP_KNOWN_HOSTS="$KH_SP"
CENTRAL_BACKUP_SFTP_PORT=2222
EOF
chmod 600 "$CENTRAL/secrets/central-backup.env"
echo KEY > "$SFTP_KEY_SP"; chmod 600 "$SFTP_KEY_SP"
echo "host.lab ssh-ed25519 AAAA" > "$KH_SP"
: > "$STUB_LOG"
"$CENTRAL/scripts/central-backup.sh" >/dev/null 2>&1
rc=$?
expect_eq "sftp configurado passa" "$rc" "0"
# argumentos REALMENTE recebidos: extrai sftp.args e un-shlex
n_args="$(python3 - "$STUB_LOG" <<'PY'
import shlex, sys
line = [l for l in open(sys.argv[1]) if "sftp.args=" in l][0]
val = line.split("sftp.args=", 1)[1].rstrip()
print("\n".join(shlex.split(val)))
PY
)"
expect_contains "chave chega intacta (-i)" "$n_args" "-i
$SFTP_KEY_SP"
expect_contains "known_hosts com espaço intacto" \
  "$n_args" "-oUserKnownHostsFile=$KH_SP"
expect_contains "porta explícita" "$n_args" "-p
2222"
expect_contains "BatchMode" "$n_args" "-oBatchMode=yes"
expect_contains "strict host key" "$n_args" "-oStrictHostKeyChecking=yes"
expect_contains "timeout limitado" "$n_args" "-oConnectTimeout=15"

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
