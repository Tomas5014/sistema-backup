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

`keep_last` pode faltar, ser `null` ou inteiro positivo. Quando definido, o
destino aplica `restic forget --keep-last N --tag task:<id> --prune`
automaticamente após cada backup bem-sucedido — o escopo por tag limita a
retenção aos snapshots da própria tarefa. Falha na retenção falha o destino
com `snapshot_id` preservado. A mesma política pode ser aplicada sob demanda
pelo comando `forget` (ver Recuperação operacional).

Padrões em segundos: `initialize_seconds=120`, `backup_seconds=14400`,
`snapshots_seconds=120`, `check_seconds=3600`, `restore_seconds=14400`,
`forget_seconds=3600`. Ajuste
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

### Destino SMB

Restic não possui backend SMB nativo: o compartilhamento é montado pelo sistema
(`mount.cifs`, fstab ou automount) e a POC **valida a identidade do volume**
antes de ler ou gravar. Um ponto de montagem vazio nunca é confundido com o
NAS disponível.

```json
{
  "id": "nas-backup", "name": "NAS de backup", "kind": "smb",
  "repository": "restic/cliente-acme",
  "password_environment": "MACIP_RESTIC_PASSWORD_SMB",
  "smb": {
    "server": "10.10.20.11", "share": "backups",
    "mount_point": "/mnt/nas-backups",
    "marker": ".macip-volume", "probe_timeout_seconds": 10
  }
}
```

`repository` é caminho **relativo** dentro do compartilhamento; o repositório
vive em `mount_point/repository`. Na execução a POC confere, via `findmnt`,
que `mount_point` é de fato um ponto de montagem CIFS/SMB3 do
`//server/share` esperado; `marker` (opcional, recomendado) é um arquivo dentro
do compartilhamento cuja existência é sondada com `probe_timeout_seconds`,
evitando bloqueio quando o servidor cai.

Origem SMB usa a mesma identidade, como objeto em `sources`:

```json
"sources": [
  "/srv/local",
  {"path": "/mnt/nas-docs/financeiro",
   "smb": {"server": "10.10.20.10", "share": "docs",
           "mount_point": "/mnt/nas-docs", "marker": ".macip-volume"}}
]
```

A `path` da origem deve ser o próprio `mount_point` ou ficar dentro dele.
Origem SMB indisponível falha a tarefa inteira (não há dados a copiar);
destino SMB indisponível falha só aquele destino. Credenciais de montagem
pertencem ao sistema (arquivo `credentials=` restrito ou fstab), não à tarefa.

Exemplo: [tarefa SMB](examples/tarefa-smb.json). A homologação usa
`tests/test_integration_smb.py` com `MACIP_SMB_INTEGRATION=1`,
`MACIP_SMB_SERVER`, `MACIP_SMB_SHARE`, `MACIP_SMB_MOUNT_POINT` e
`MACIP_SMB_MARKER` (opcional). Windows/UNC fica para a etapa Windows.

### Destino S3

Usa o backend S3 nativo do restic; qualquer endpoint compatível serve
(AWS S3, MinIO, outros após homologação).

```json
{
  "id": "nuvem", "name": "Nuvem", "kind": "s3",
  "password_environment": "MACIP_RESTIC_PASSWORD_S3",
  "s3": {
    "endpoint": "https://s3.us-east-1.amazonaws.com",
    "bucket": "macip-backups-cliente-a", "prefix": "restic/servidor-01",
    "region": "us-east-1", "bucket_lookup": "path",
    "access_key_environment": "MACIP_S3_ACCESS_KEY",
    "secret_key_environment": "MACIP_S3_SECRET_KEY"
  }
}
```

O repositório vive em `endpoint/bucket/prefix` (`prefix` opcional; sem ele,
raiz do bucket). `endpoint` é URL `http(s)` sem credenciais nem caminho.
`bucket_lookup` aceita `auto`/`path`/`dns` — MinIO normalmente exige `path`.
`storage_class` é opcional.

Credenciais S3 chegam **somente por variáveis de ambiente** nomeadas em
`access_key_environment`/`secret_key_environment` — a configuração nunca
guarda chaves, e o adaptador as injeta como `AWS_ACCESS_KEY_ID`/
`AWS_SECRET_ACCESS_KEY` apenas no processo restic, nunca em argumentos ou
logs. Credencial ausente falha o destino antes de iniciar o restic.

Exemplo: [local + nuvem](examples/tarefa-s3.json). A homologação usa
`tests/test_integration_s3.py` ou `macip-backup-poc verify-s3-integration`
com `MACIP_S3_INTEGRATION=1`, `MACIP_S3_ENDPOINT`, `MACIP_S3_BUCKET`,
`MACIP_S3_PREFIX` (opcional), `MACIP_S3_REGION`/`MACIP_S3_BUCKET_LOOKUP`
(opcionais), `MACIP_S3_ACCESS_KEY_ENV`/`MACIP_S3_SECRET_KEY_ENV` (nomes das
variáveis com as chaves). O bucket precisa existir — restic não cria buckets.

### Combinações de destinos

`tests/test_integration_combinations.py` grava um único backup em
local+SFTP+SMB+S3 ao mesmo tempo e restaura de cada repositório — a matriz
de destinos do planejamento. Roda quando as variáveis `MACIP_SFTP_*`,
`MACIP_SMB_*` e `MACIP_S3_*` estão todas definidas; caso contrário é
pulada.

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

### Recuperação operacional

Os comandos de recuperação usam um destino identificado pelo `id` presente no
JSON. Eles leem a senha somente da variável `password_environment`, validam os
arquivos SSH do destino SFTP e nunca executam `init`. O único comando
destrutivo é `forget`, que aplica apenas a política `keep_last` declarada no
próprio destino e escopada à tag da tarefa. Também não exigem que as origens
da tarefa ainda existam; a proteção contra repositório local dentro de origem
continua obrigatória.

```bash
# Lista JSON de snapshots disponíveis no destino.
macip-backup-poc snapshots examples/tarefa-local-sftp.json local

# Verifica estrutura; --read-data lê e valida todos os dados.
macip-backup-poc check examples/tarefa-local-sftp.json local --read-data

# Restaura snapshot completo para um diretório novo.
macip-backup-poc restore examples/tarefa-local-sftp.json local SNAPSHOT_ID /srv/restore/cliente-2026-10-22

# Aplica a retenção declarada (keep_last) do destino, sob demanda.
macip-backup-poc forget examples/tarefa-local-sftp.json local
```

`restore` exige que `TARGET` ainda não exista e rejeita alvo dentro de uma
origem ou do repositório local. Isso evita restaurar sobre dados de produção ou
alterar o repositório. Use o `id` retornado por `snapshots`; a restauração não
substitui arquivos por meio da CLI.

## Agente local

O modo `agent` executa a tarefa como um agente offline: não há portal nem API
central — tarefa, credenciais e histórico vivem no host.

```json
"retry": {"attempts": 3, "backoff_seconds": 30}
```

`retry` é opcional por tarefa: cada destino é retentado de forma independente
até `attempts` tentativas com espera de `backoff_seconds` entre elas
(padrão: `attempts=1`, sem espera). O log marca `attempt` e emite
`backup.destination.retry` entre tentativas.

**Falhas repetíveis**: timeouts (`ResticTimeoutError`), erros de
transporte/disco (`OSError`) e erros de backend cujo detalhe indica falha
transitória (`connection refused/reset`, `timed out`, `no route to host`,
`network is unreachable`, `temporary failure`, `service unavailable`,
`i/o timeout`, `unexpected eof`, `broken pipe`).

**Falhas não repetíveis** (encerram o destino sem consumir tentativas):
configuração inválida e credencial ausente (`ConfigurationError`/
`CredentialError`), pré-condições internas do adaptador (`ResticError`) e
erros de backend cujo detalhe indica credencial rejeitada ou acesso negado
(`wrong password`, `no key found`, `permission denied`, `access denied`,
`host key`, `authentication failed`, `InvalidAccessKeyId`,
`SignatureDoesNotMatch`, `forbidden`).

**Limitação**: o restic usa códigos de saída genéricos (quase sempre 1), por
isso a classificação usa o texto do erro. Erros de backend que não batem em
nenhum padrão são tratados como **não repetíveis** — decisão conservadora que
prefere perder uma retentativa a martelar um erro determinístico; padrões
novos devem ser adicionados à medida que erros reais forem observados.

Se o backup de uma tentativa já gerou snapshot e a falha ocorre depois
(check, retenção ou listagem), a retentativa **não repete o backup** —
retoma das fases pendentes com o mesmo snapshot (evento
`backup.destination.resume`).

```bash
# Executa a tarefa como agente e registra no SQLite local.
macip-backup-poc agent examples/tarefa-smb.json --store /var/lib/macip-backup/agent.db

# Histórico de execuções do agente (JSON, com destinos e erros por tentativa).
macip-backup-poc history --store /var/lib/macip-backup/agent.db --task tarefa-financeiro-smb
```

O banco SQLite guarda execuções, resultados por destino e **cada tentativa**
(número, horários, duração, operação que falhou, snapshot quando existir) —
nunca segredos. A gravação é **incremental**: a execução nasce `running`
antes do backup; cada tentativa é gravada ao iniciar, ganha `snapshot_id`
logo que o backup retorna e é concluída ao terminar; o destino é gravado ao
terminar. Uma morte abrupta preserva tudo já gravado — `history` mostra
tentativas mesmo com a execução ainda `running`. Os eventos estruturados da
execução ficam em `<store-dir>/logs/<execution_id>.jsonl`.

Bancos criados por versões anteriores do agente são **migrados
transacionalmente** (`PRAGMA user_version`): o esquema inicial vira o atual
preservando todo o histórico; reabrir não repete a migração.

### Credenciais do SO

Todos os comandos (`run`, `agent`, `snapshots`, `check`, `restore`, `forget`)
usam a **mesma resolução**: `password_environment` e os campos
`*_key_environment` do S3 são **nomes** de segredo, resolvidos nesta ordem:
variável de ambiente → **kernel keyring** (`keyctl`, já presente via
`keyutils`/`cifs-utils`). `os.environ` nunca é modificado — o segredo só
entra no ambiente do processo filho restic; o conteúdo do keyring é usado
exatamente como armazenado, sem normalização.

```bash
# Provisiona o segredo sem argv nem histórico do shell (lido de stdin):
read -r -s -p 'Segredo: ' MACIP_SECRET && printf '%s' "$MACIP_SECRET" | \
  keyctl padd user macip.MACIP_RESTIC_PASSWORD_SMB @s; unset MACIP_SECRET
```

Limitações do keyring: `@s` é o keyring **da sessão** — não sobrevive a
logout/reboot, exigindo nova gravação a cada boot do agente; para chave
persistente por usuário existe `@u` (keyring do usuário, requer
`keyctl persist` e não funciona em todas as sessões). systemd
`LoadCredential`/Secret Service entram depois atrás da mesma interface.

Segredo ausente em ambos os lugares falha com erro controlado — nunca sai em
log — e não é repetido automaticamente.

### Agendamento systemd --user

`schedule` gera um par service+timer por tarefa em
`~/.config/systemd/user/` — sem root:

```bash
# Gera macip-backup-agent-<task>.service + .timer (Persistent, AccuracySec=1s)
macip-backup-poc schedule examples/tarefa-smb.json \
  --store ~/.local/share/macip-backup/agent.db \
  --calendar 'daily' --enable

# Remove as units da tarefa
macip-backup-poc unschedule <task_id>
```

O service é `Type=oneshot` rodando o mesmo `agent` da CLI — compartilha lock,
histórico e resolução de credenciais.

**Bloqueio de simultâneas**: `flock` em `<store>.lock` — uma segunda execução
(CLI ou serviço) falha imediatamente com erro controlado. O kernel libera o
lock mesmo em morte abrupta.

**Credenciais sem terminal**: duas vias — `EnvironmentFile` no service lê
`~/.config/macip-backup/agent.env` (opcional, modo 600) com `MACIP_*=valor`;
ou kernel keyring do usuário (`keyctl padd user macip.<nome> @u`), que
persiste logout mas não reboot. A resolução `%user:` cobre @s e @u.

**Encerramento controlado**: SIGTERM/SIGINT marcam a execução `interrupted`
antes de sair; execuções órfãs de mortes anteriores (kill -9, reboot) são
marcadas `interrupted` na próxima execução — o histórico nunca confunde
"parou" com "está rodando".

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

# Piloto SFTP contra servidor real (fora do Docker).
MACIP_SFTP_HOST=sftp.exemplo.com \
MACIP_SFTP_USER=backup-piloto \
MACIP_SFTP_IDENTITY_FILE=~/.ssh/macip-piloto_ed25519 \
MACIP_SFTP_REMOTE_PATH=backups/piloto \
MACIP_SFTP_HOST_KEY_SHA256=SHA256:... \
tests/sftp/run-pilot.sh
```

O provisionamento da conta SFTP dedicada no servidor está em
`tests/sftp/piloto.md`.

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

`run-pilot.sh` repete a suíte contra um servidor real informado por variáveis
de ambiente, sem Docker. Sem `MACIP_SFTP_KNOWN_HOSTS_FILE` a host key é obtida
por `ssh-keyscan` e exibida para conferência: o script só prossegue se
`MACIP_SFTP_HOST_KEY_SHA256` conferir (preferido) ou se
`MACIP_SFTP_TRUST_ON_FIRST_USE=1` aceitar a chave observada. Com o piloto
habilitado (`MACIP_SFTP_PILOT=1`, definido pelo script) roda também
`test_pilot_volume_and_many_files_survive_restore`: backup, `check
--read-data` e restauração de 300 arquivos distribuídos mais um volume de
64 MiB, com comparação SHA-256 de todo o conteúdo restaurado; volumes são
ajustáveis por `MACIP_SFTP_PILOT_FILES` e `MACIP_SFTP_PILOT_MEBIBYTES`.
Repositórios `macip-test-*` permanecem no servidor para inspeção;
`MACIP_SFTP_CLEANUP_SSH=1` tenta removê-los via shell SSH quando a conta
permite execução remota.

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
