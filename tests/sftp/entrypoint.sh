#!/bin/sh
set -eu

ssh_directory=/home/macipsftptest/.ssh
authorized_keys="$ssh_directory/authorized_keys"

mkdir --parents "$ssh_directory"
chown macipsftptest:macipsftptest "$ssh_directory"
chmod 700 "$ssh_directory"
cp /run/macip-sftp-authorized_keys "$authorized_keys"
chown macipsftptest:macipsftptest "$authorized_keys"
chmod 600 "$authorized_keys"

exec "$@"
