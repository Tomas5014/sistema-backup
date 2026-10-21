# POC: núcleo de backup MAC IP

POC Linux para backups restic independentes em destinos `local` e `sftp`.
Código legado foi preservado; esta POC não grava no formato legado.

## Entrega atual

- Validação JSON antes de criar ou gravar repositórios.
- Cada destino executa `cat config`, `init` somente com código restic 10,
  `backup`, `check` e `snapshots` de modo independente.
- `sftp` usa backend nativo restic. Não existe upload paralelo de arquivos.
- SSH por chave sem prompt: `BatchMode=yes`, host key obrigatória,
  `known_hosts` dedicado, identidade explícita, timeout de conexão e keepalive.
- Timeout inicia sessão Linux própria. Falha envia `SIGTERM` ao grupo, espera 5 s,
  envia `SIGKILL` e limita a coleta final a 5 s.
- Senha restic fica somente em `password_environment`. Chave e senha não entram
  em JSON, argumentos, logs ou resultados.
- Retenção permanece declarativa. `forget` e `prune` não executam nesta entrega.

`success`: todos os destinos concluídos. `partial`: ao menos um concluído e outro
falhou. `failed`: nenhum concluiu. Cada destino informa operação, código ou timeout.

## Instalação

```bash
sudo apt install python3.14-venv restic openssh-client docker.io
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
restic version
```

Python 3.11+ é exigido. Não há dependências Python externas.

## Formato da tarefa

`sources`, `exclusions` e `destinations` são listas. `sources` e `destinations`
exigem item; `exclusions` aceita `[]`. `client`, cada destino, `retention`,
`timeouts` e `sftp` são objetos. String no lugar de lista é inválida. Essa
validação estrutural acontece antes de gravar qualquer repositório. Arquivos SSH
ausentes são pré-requisitos do destino SFTP: falham somente aquele destino, e
destinos locais continuam.

`keep_last` pode faltar, ser `null` ou inteiro positivo. A política ainda não é
aplicada.

Padrões em segundos: `initialize_seconds=120`, `backup_seconds=14400`,
`snapshots_seconds=120`, `check_seconds=3600`, `restore_seconds=14400`. Ajuste
backup e restore para conjuntos grandes. Na API do adaptador, `None` usa esse
padrão; valor fornecido deve ser inteiro positivo. Zero, booleano, fração e
negativo falham antes de iniciar processo.

### Destino local

```json
{
  "id": "local", "name": "Cópia local", "kind": "local",
  "repository": "/srv/macip/cliente/local",
  "password_environment": "MACIP_RESTIC_PASSWORD_LOCAL",
  "retention": {"keep_last": 14}
}
```

Repositório local não pode ficar dentro de origem.

### Destino SFTP

```json
{
  "id": "macip-sftp", "name": "MAC IP SFTP", "kind": "sftp",
  "password_environment": "MACIP_RESTIC_PASSWORD_SFTP",
  "sftp": {
    "host": "backup.macip.example", "port": 22, "user": "backup_cliente",
    "remote_path": "/srv/restic/cliente-acme",
    "identity_file": "/etc/macip-backup/keys/cliente-acme_ed25519",
    "known_hosts_file": "/etc/macip-backup/known_hosts",
    "connect_timeout_seconds": 15,
    "server_alive_interval_seconds": 60,
    "server_alive_count_max": 3
  }
}
```

`remote_path` com `/` inicial é absoluto no servidor. Sem `/`, como
`repositories/cliente`, é relativo ao diretório inicial SFTP. Nunca use `~`.
SFTP não possui `repository`; caminho remoto nunca passa por `Path.resolve()`.

Não há campos `command`, `args`, senha SSH ou conteúdo de chave. Campos SFTP
desconhecidos são rejeitados. `identity_file` e `known_hosts_file` devem existir
na execução. Senha de chave não é suportada; use chave de serviço com permissões
restritas e agente operacional apropriado.

Confirme impressão digital por canal confiável antes de cadastrar servidor:

```bash
ssh-keyscan -p 22 backup.macip.example > /tmp/macip-known-hosts-candidato
ssh-keygen -lf /tmp/macip-known-hosts-candidato
# Compare com impressão digital recebida por canal confiável.
install -m 600 /tmp/macip-known-hosts-candidato /etc/macip-backup/known_hosts
```

Não aceite chave nova automaticamente. Usuário remoto precisa apenas de acesso
ao diretório do repositório e não deve ser conta humana.

Exemplos: [somente SFTP](examples/tarefa-sftp.json) e
[local + SFTP](examples/tarefa-local-sftp.json).

## Execução

```bash
export MACIP_RESTIC_PASSWORD_LOCAL='senha-restic-local-em-cofre'
export MACIP_RESTIC_PASSWORD_SFTP='senha-restic-sftp-em-cofre'
macip-backup-poc validate examples/tarefa-local-sftp.json
macip-backup-poc run examples/tarefa-local-sftp.json
```

Senha restic perdida impede recuperação. Guarde cada senha em cofre e teste
restauração. Para recuperar acesso, restaure a mesma senha no cofre e a chave
privada no caminho configurado; não execute `init` após erro de senha. Troca de
chave ou de host exige atualizar `known_hosts` somente após nova confirmação por
canal confiável. `RESTIC_CACHE_DIR` é cache local; nunca é destino de backup.

## Testes

```bash
# Unitários e integração local real.
PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py' -v

# Integração local; falha sem restic.
macip-backup-poc verify-integration

# SFTP efêmero: chave, known_hosts e contêiner isolado.
tests/sftp/run-integration.sh

# Apenas integração SFTP real; falha sem pré-requisitos ou execução real.
macip-backup-poc verify-sftp-integration
```

Ambiente SFTP gera diretório e chave temporários; não edita `~/.ssh`,
`sshd_config` do host nem usa dados reais. Cobre SFTP isolado, local + SFTP,
exclusões, `snapshots`, `check --read-data`, restauração com cache novo e
reutilização de repositório. Cada teste cria caminho remoto único
`MACIP_SFTP_REMOTE_PATH/macip-test-...`; a senha é criada uma vez por teste.
O script usa projeto Compose exclusivo e remove somente esse projeto e a raiz
temporária, inclusive se `compose up` falhar parcialmente; o código de saída
original é preservado. Os testes reais também cobrem chave inválida, host key
desconhecida, acesso negado, servidor indisponível, senha restic incorreta e
interrupção após o restic confirmar envio de bytes. Depois da interrupção, o
teste congela o filho ainda ativo, confirma que ele não concluiu normalmente,
executa nova cópia, `check --read-data` e restauração com comparação de
`report.txt` e `transfer.bin`. Timeout SFTP seguido de destino local é teste simulado no
orquestrador, separado das integrações reais.

## Compatibilidade e limites

Validado neste ambiente: Linux, Python 3.14.4, OpenSSH 10.2p1, restic 0.18.1.
Integrações locais reais foram homologadas. Host atual não possui `sshd` e o
daemon Docker/Podman está indisponível; SFTP está pendente de homologação real.
`cat config` e
código 10 seguem [scripting restic 0.18](https://restic.readthedocs.io/en/v0.18.0/075_scripting.html).
Backend segue [repositórios SFTP](https://restic.readthedocs.io/en/stable/030_preparing_a_new_repo.html).

### Registro desta revisão

- Aprovados: 31 testes unitários e integrações locais reais.
- Ignorados: 7 integrações SFTP reais, pois não há daemon Docker/Podman ativo.
- Falhas de asserção: nenhuma.
- Não executado: `tests/sftp/run-integration.sh` encerrou com código 1 na
  pré-checagem do daemon; isso não é homologação SFTP.

Próximo passo: executar `tests/sftp/run-integration.sh` em host Docker funcional,
registrar resultado e homologar falhas controladas. SMB, S3, Windows, serviço,
agenda, API e portal permanecem fora do escopo.
