#!/usr/bin/env bash
# Roda todos os testes dos scripts da central. O teste com PostgreSQL
# real skipa limpo sem runtime de contêiner.
set -u
DIR="$(cd "$(dirname "$0")" && pwd)"
rc=0
for t in test_perms.sh test_backup.sh test_up.sh test_restore_mock.sh test_restore_real.sh test_backup_central.sh test_backup_central_real.sh; do
  echo "### $t"
  bash "$DIR/$t" || rc=1
done
echo "=== rc final: $rc ==="
exit "$rc"
