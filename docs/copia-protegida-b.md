# Cópia protegida em B · laboratório

Para o ensaio com cliques no aplicativo Windows 0.1.3 e execução sem
sessão aberta, siga [Ensaio Windows → A → B](ensaio-windows-a-b.md).

A produz os backups. B lê A por SFTP com uma chave SSH própria, usa uma chave
Restic de leitura para A e escreve em seu repositório local independente. O
usuário que executa B é o único com acesso ao volume e ao cofre descartável de
B. O Windows continua usando o perfil 0.1.3.

O cadastro mostra o primário, a preparação de B e a última cópia confirmada.
`prepared` identifica estrutura pronta. O registro de uma cópia guarda os IDs
completos do ponto em A e em B. Somente `prove` produz `proven`, após
`check --read-data`, `restore --verify` e comparação do conjunto inteiro de
arquivos por SHA-256 com um manifesto feito antes do backup. A restauração
executa Restic local com um filtro seccomp que bloqueia sockets. O executor
testa o filtro antes de iniciar Restic, usa cache desativado e passa somente a
senha de B à verificação.

O botão e a rota de download exigem administrador, laboratório habilitado e
prova do vínculo atual. A migração expansiva 10 guarda a evidência e vincula a
instalação ao endereço, host key e ID de A e ao volume, caminho e ID de B.
Mudança no vínculo bloqueia o download e exige nova prova. A API confia no
executor autenticado de B como emissor da evidência. Use credenciais de B e a
opção de laboratório exclusivamente em ambientes descartáveis.

## Reproduzir de ponta a ponta

Requisitos da prova automatizada: Linux com `libseccomp.so.2`, `/dev/shm`
montado, Restic 0.18.1, OpenSSH cliente, Docker, ambiente Python da API e um
PostgreSQL exclusivo de testes. O navegador usa Chrome, as dependências do
portal e um build local. Use a imagem de OpenSSH de `tests/sftp/Dockerfile`, que
pode ser construída com `docker build -t macip-protection-lab-sshd tests/sftp`.
Escolha o socket do Docker local autorizado antes de começar.

```bash
cd portal && npm run build && cd ..
(
  set -e
  lab_pg=$(docker run --rm -d -e POSTGRES_HOST_AUTH_METHOD=trust \
    -e POSTGRES_DB=macip_protected_test -p 127.0.0.1::5432 postgres:16-alpine)
  trap 'docker stop "$lab_pg" >/dev/null' EXIT
  lab_port=$(docker port "$lab_pg" 5432 | cut -d: -f2)
  for attempt in {1..20}; do
    if docker exec "$lab_pg" pg_isready -h 127.0.0.1 -d macip_protected_test; then break; fi
    sleep 1
  done
  docker exec "$lab_pg" pg_isready -h 127.0.0.1 -d macip_protected_test
  export MACIP_API_TEST_DATABASE_URL="postgresql+psycopg://postgres@127.0.0.1:${lab_port}/macip_protected_test"
  export MACIP_TEST_PROVISIONING_SFTP_IMAGE=macip-protection-lab-sshd
  export MACIP_TEST_PROTECTED_BROWSER=1
  cd api
  PYTHONPATH=src ../.venv-api/bin/python -m pytest -o tmp_path_retention_policy=none \
    -q -s tests/test_protected_copy.py
)
```

A prova cria dois clientes e autoriza no OpenSSH uma chave de escrita para o
cliente e uma chave de leitura para B (`internal-sftp -R`). Gera os pontos
por SFTP usando o perfil interno de laboratório, testa a repetição e o
isolamento, desliga A e recupera o último ponto somente por B. O teste
exercita o botão no Chrome antes e depois da prova com o cadastro da mesma
API. Para executar somente a prova de API e Restic, remova a variável do
navegador.

Guarde a saída JSON do teste com os IDs da instalação, dos repositórios, do
volume, do vínculo e dos snapshots, os horários, a contagem de arquivos e os
SHA-256 comparados. Guarde também o resultado das recusas e o código de saída
do pytest. Para registrar a interface, defina
`MACIP_TEST_PROTECTED_SCREENSHOTS` como diretório descartável de sua autoria.
As evidências devem conter apenas dados fictícios. Preserve os registros
fora dos contêineres temporários.

## Executar B manualmente no laboratório

1. Habilite na central descartável `MACIP_API_LAB_PROTECTION=1` e defina
   `MACIP_API_PROTECTOR_TOKEN` com um token exclusivo de pelo menos 32
   caracteres. B usa esse valor em `MACIP_PROTECTOR_TOKEN`, separado dos
   tokens de A e de administração. A API recebe metadados e evidência, e B
   mantém sua senha e seu cofre de arquivos localmente.
2. No ambiente SFTP de A, autorize a chave pública de B somente para leitura
   da instalação. Use chroot e uma política efetiva que aplique
   `restrict,command="internal-sftp -R"` a essa chave. Um `ForceCommand`
   global pode substituir a regra por chave. Confira a recusa de escrita.
   Crie uma chave Restic adicional em A com `restic key add` e senha
   independente para B. Proteja a chave privada e o arquivo da senha com
   modo 0600 no executor. A cópia usa `--no-lock` na origem de leitura.
   Coordene a janela com eventuais operações destrutivas em A.
3. Em B, defina `MACIP_PROTECTOR_MOUNT` com o caminho absoluto da montagem
   e `MACIP_PROTECTOR_VOLUME` com uma subpasta exclusiva de modo 0700
   dentro da montagem. Grave um UUID gerado para esse volume no arquivo
   `.macip-volume` (modo 0600) e use o mesmo valor em
   `MACIP_PROTECTOR_VOLUME_ID`. Defina `MACIP_PROTECTOR_STATE` com outro
   diretório absoluto privado, fora do volume protegido. Use o mesmo estado
   de B nas repetições para manter o lock por instalação e recuperar a senha.
   O executor confere a montagem, o UUID e o espaço livre. A reserva padrão é
   64 MiB, ajustável em `MACIP_PROTECTOR_RESERVE_BYTES`; para cópia soma-se
   o tamanho lógico do ponto informado pelo Restic.
4. Configure `MACIP_PROTECTOR_API_URL` com HTTPS e CA confiável.
   No loopback descartável a exceção explícita é
   `MACIP_PROTECTOR_LAB_ALLOW_HTTP=1` com host literal `127.0.0.1`.
   Use apenas as instalações fictícias preparadas em A. Defina
   `install_id`, `key_a_read`, `password_a_read_file` e `snapshot_a`
   com UUID, caminhos privados e ID Restic completo da instalação de teste.

```bash
macip-backup-protector prepare "$install_id" \
  --source-key "$key_a_read" --source-password-file "$password_a_read_file"
macip-backup-protector copy "$install_id" --snapshot "$snapshot_a" \
  --source-key "$key_a_read" --source-password-file "$password_a_read_file"
```

`copy` sem `--snapshot` seleciona o ponto mais recente. Para preservar
outros pontos, informe os respectivos IDs em chamadas separadas. Repetir uma
cópia usa a deduplicação de snapshots do próprio Restic. A rotina não apaga
pontos e uma falha conserva o último registro confirmado. [Documentação do
Restic 0.18](https://restic.readthedocs.io/en/v0.18.0/045_working_with_repos.html#copying-snapshots-between-repositories)

Em seguida, desligue somente a origem fictícia e rode a prova com o caminho
do manifesto em `manifest_file`. O manifesto é um objeto JSON não vazio,
mapeando cada arquivo esperado (caminho relativo à raiz de restauração,
incluindo os segmentos da origem absoluta) ao SHA-256 hexadecimal calculado
antes do backup. Arquivos excluídos não fazem parte do manifesto.

```bash
macip-backup-protector prove "$install_id" --manifest "$manifest_file"
```

A fila de A e o cadastro sobrevivem à migração 10. Para retorno ao código
anterior, guarde um backup pré-migração e trate as escritas posteriores
antes de restaurar. O migrador anterior recusa um esquema mais novo.
