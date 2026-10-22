#!/usr/bin/env bash
# Executa a suíte SFTP real contra um servidor piloto fora do Docker.
#
# Obrigatório:
#   MACIP_SFTP_HOST            host do servidor
#   MACIP_SFTP_USER            usuário SFTP dedicado ao teste
#   MACIP_SFTP_IDENTITY_FILE   chave privada autorizada no servidor
#   MACIP_SFTP_REMOTE_PATH     diretório remoto base (absoluto ou relativo ao home SFTP)
#
# Opcional:
#   MACIP_SFTP_PORT                padrão 22
#   MACIP_SFTP_KNOWN_HOSTS_FILE    known_hosts já verificado; sem ele a chave é
#                                  obtida via ssh-keyscan e precisa ser aceita:
#   MACIP_SFTP_HOST_KEY_SHA256     fingerprint esperado (preferido), ou
#   MACIP_SFTP_TRUST_ON_FIRST_USE=1 aceita a chave observada agora (TOFU)
#   MACIP_SFTP_CLEANUP_SSH=1       tenta remover macip-test-* via shell SSH ao
#                                  final; contas internal-sftp não têm shell e a
#                                  limpeza permanece manual
#   MACIP_SFTP_PILOT_FILES         arquivos do teste de volume (padrão 300)
#   MACIP_SFTP_PILOT_MEBIBYTES     MiB do arquivo grande (padrão 64)
#
# Nenhum segredo é impresso. Repositórios macip-test-* ficam no servidor para
# inspeção, a menos que a limpeza SSH seja habilitada e disponível.

set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repository_root=$(cd -- "$script_dir/../.." && pwd)
test_root=$(mktemp -d --tmpdir macip-sftp-pilot.XXXXXX)
cleanup() {
  rm -rf -- "$test_root" || true
}
trap cleanup EXIT

for name in \
  MACIP_SFTP_HOST \
  MACIP_SFTP_USER \
  MACIP_SFTP_IDENTITY_FILE \
  MACIP_SFTP_REMOTE_PATH
do
  if test -z "${!name:-}"; then
    echo "piloto SFTP não executado: variável obrigatória ausente: $name" >&2
    exit 2
  fi
done
MACIP_SFTP_PORT=${MACIP_SFTP_PORT:-22}

if ! test -f "$MACIP_SFTP_IDENTITY_FILE"; then
  echo "piloto SFTP não executado: chave não encontrada: $MACIP_SFTP_IDENTITY_FILE" >&2
  exit 2
fi
for tool in ssh-keyscan ssh-keygen sftp ssh; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "piloto SFTP não executado: ferramenta ausente: $tool" >&2
    exit 2
  fi
done

known_hosts_file=${MACIP_SFTP_KNOWN_HOSTS_FILE:-}
if test -z "$known_hosts_file"; then
  known_hosts_file="$test_root/known_hosts"
  if ! ssh-keyscan -T 10 -p "$MACIP_SFTP_PORT" "$MACIP_SFTP_HOST" \
      >"$known_hosts_file" 2>/dev/null; then
    echo "piloto SFTP não executado: ssh-keyscan falhou para $MACIP_SFTP_HOST" >&2
    exit 1
  fi
  if ! test -s "$known_hosts_file"; then
    echo "piloto SFTP não executado: nenhuma host key obtida de $MACIP_SFTP_HOST" >&2
    exit 1
  fi
  chmod 600 "$known_hosts_file"
  echo "host keys observadas em $MACIP_SFTP_HOST:$MACIP_SFTP_PORT:" >&2
  ssh-keygen -lf "$known_hosts_file" >&2
  if test -n "${MACIP_SFTP_HOST_KEY_SHA256:-}"; then
    expected=${MACIP_SFTP_HOST_KEY_SHA256#SHA256:}
    if ! ssh-keygen -lf "$known_hosts_file" | grep -q "SHA256:$expected "; then
      echo "host key não confere com MACIP_SFTP_HOST_KEY_SHA256; abortando" >&2
      exit 1
    fi
    echo "host key conferida com MACIP_SFTP_HOST_KEY_SHA256" >&2
  elif test "${MACIP_SFTP_TRUST_ON_FIRST_USE:-}" != "1"; then
    echo "confirme o fingerprint por canal seguro e rode novamente com" >&2
    echo "MACIP_SFTP_HOST_KEY_SHA256=<fingerprint> (preferido) ou aceite TOFU" >&2
    echo "com MACIP_SFTP_TRUST_ON_FIRST_USE=1" >&2
    exit 1
  else
    echo "ATENÇÃO: host key aceita por confiança no primeiro uso (TOFU)" >&2
  fi
elif ! test -f "$known_hosts_file"; then
  echo "piloto SFTP não executado: known_hosts ausente: $known_hosts_file" >&2
  exit 2
fi

ssh_options=(
  -oBatchMode=yes
  -oIdentitiesOnly=yes
  -oStrictHostKeyChecking=yes
  -oUserKnownHostsFile="$known_hosts_file"
  -oIdentityFile="$MACIP_SFTP_IDENTITY_FILE"
)

if ! sftp "${ssh_options[@]}" -P "$MACIP_SFTP_PORT" -b /dev/null \
    "$MACIP_SFTP_USER@$MACIP_SFTP_HOST"; then
  echo "piloto SFTP não executado: autenticação falhou para" >&2
  echo "$MACIP_SFTP_USER@$MACIP_SFTP_HOST:$MACIP_SFTP_PORT" >&2
  exit 1
fi

status=0
(
  cd "$repository_root"
  MACIP_SFTP_INTEGRATION=1 \
  MACIP_SFTP_PILOT=1 \
  MACIP_SFTP_HOST="$MACIP_SFTP_HOST" \
  MACIP_SFTP_PORT="$MACIP_SFTP_PORT" \
  MACIP_SFTP_USER="$MACIP_SFTP_USER" \
  MACIP_SFTP_REMOTE_PATH="$MACIP_SFTP_REMOTE_PATH" \
  MACIP_SFTP_IDENTITY_FILE="$MACIP_SFTP_IDENTITY_FILE" \
  MACIP_SFTP_KNOWN_HOSTS_FILE="$known_hosts_file" \
  PYTHONPATH=src python3 -m unittest tests.test_integration_sftp -v
) || status=$?

printf 'ls %s\n' "$MACIP_SFTP_REMOTE_PATH" >"$test_root/list.sftp"
echo "repositórios de teste deixados no servidor (macip-test-*):" >&2
sftp "${ssh_options[@]}" -P "$MACIP_SFTP_PORT" -b "$test_root/list.sftp" \
  "$MACIP_SFTP_USER@$MACIP_SFTP_HOST" 2>/dev/null | grep 'macip-test-' >&2 || true

if test "${MACIP_SFTP_CLEANUP_SSH:-}" = "1"; then
  if ssh "${ssh_options[@]}" -p "$MACIP_SFTP_PORT" "$MACIP_SFTP_USER@$MACIP_SFTP_HOST" \
      "rm -rf -- \"$MACIP_SFTP_REMOTE_PATH\"/macip-test-*"; then
    echo "limpeza remota via SSH concluída" >&2
  else
    echo "limpeza via SSH indisponível (conta sem shell?); remova macip-test-* manualmente" >&2
  fi
fi

exit "$status"
