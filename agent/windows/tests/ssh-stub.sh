#!/usr/bin/env bash
# Registra os argumentos que o Restic repassa ao comando SFTP e sai com
# erro: prova de invocacao, sem conexao com laboratorio algum.
printf '%s\n' "$@" >> "${SSH_STUB_LOG:-/dev/null}"
exit 1
