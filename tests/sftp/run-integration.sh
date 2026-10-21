#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
test_root=$(mktemp -d --tmpdir macip-sftp-integration.XXXXXX)
cleanup() {
  MACIP_SFTP_TEST_ROOT="$test_root" docker compose -f "$script_dir/compose.yaml" down --volumes
  rm -rf -- "$test_root"
}
trap cleanup EXIT

ssh-keygen -q -t ed25519 -N '' -f "$test_root/id_ed25519"
cp "$test_root/id_ed25519.pub" "$test_root/authorized_keys"
chmod 600 "$test_root/authorized_keys"
MACIP_SFTP_TEST_ROOT="$test_root" docker compose -f "$script_dir/compose.yaml" up --build --detach

for attempt in $(seq 1 30); do
  if ssh-keyscan -p 2222 127.0.0.1 >"$test_root/known_hosts" 2>/dev/null; then
    break
  fi
  sleep 1
done
test -s "$test_root/known_hosts"

MACIP_SFTP_INTEGRATION=1 \
MACIP_SFTP_HOST=127.0.0.1 \
MACIP_SFTP_PORT=2222 \
MACIP_SFTP_USER=backup \
MACIP_SFTP_REMOTE_PATH=repository \
MACIP_SFTP_IDENTITY_FILE="$test_root/id_ed25519" \
MACIP_SFTP_KNOWN_HOSTS_FILE="$test_root/known_hosts" \
PYTHONPATH=src python3 -m unittest tests.test_integration_sftp -v
