# Registro de trabalho — Sistema de Backup MAC IP

Log das ações realizadas pelo agente (Devin) neste repositório.
Entradas em ordem cronológica, da mais antiga para a mais recente.

---

## 2026-10-22 — Fechamento da segurança e cobertura da CLI de recuperação

### Contexto recebido

Documento de continuidade descrevendo o estado da POC Restic e a tarefa
pendente: corrigir a validação do `TARGET` no restore (bloqueio simétrico)
e provar o fluxo completo de recuperação através da CLI real
(seções 18–20 do documento de contexto).

### Estado encontrado no início da sessão

- `git status` mostrava modificações não commitadas em
  `src/macip_backup/cli.py`, `tests/test_cli.py` e
  `tests/test_integration_restic.py` — trabalho parcial de uma sessão
  anterior já implementava `_paths_overlap` (comparação simétrica) e parte
  dos testes exigidos.
- Working tree também continha itens não relacionados preservados fora do
  commit: `atualizações.txt` deletado, `.claude/`, `.trail/` e
  `tarefa-teste.json` não monitorados.
- Durante a sessão, o usuário criou o commit `8c08358`
  ("Inicio do SWE-2 High"), que removeu `pendencias.txt`. Sem conflito.

### Análise feita

- `_paths_overlap` já cobria `TARGET == caminho`, `TARGET` descendente e
  `TARGET` ancestral, para todas as origens e para o repositório local,
  com `resolve(strict=False)` — funciona sem a origem existir.
- Faltavam testes unitários para `TARGET` igual e descendente.
- O teste de integração novo executava o backup via `BackupOrchestrator`
  diretamente; o requisito pedia todo o fluxo pela CLI pública.

### Alterações realizadas

- `tests/test_cli.py`
  - Novo teste `test_restore_rejects_equal_or_nested_targets`: cobre
    `TARGET == origem`, `TARGET` dentro da origem, `TARGET == repositório`
    e `TARGET` dentro do repositório; verifica `SystemExit(2)`, ausência
    de traceback no stderr e que o adaptador Restic não é instanciado.
  - `test_recovery_rejects_missing_credential_without_exposing_secret`:
    adicionada verificação de ausência de traceback no stderr.
- `tests/test_integration_restic.py`
  - `test_cli_recovery_commands_restore_after_source_removal`: backup
    passou a ser executado por `cli_main` com o subcomando `run`
    (anteriormente chamava `BackupOrchestrator` diretamente).
    O fluxo inteiro `run → snapshots → check --read-data → restore`
    agora passa pelo caminho público da CLI com restic real.
    `RESTIC_CACHE_DIR` é apontado para dentro do diretório temporário
    do teste para não tocar o cache do usuário.
  - A origem é removida antes do restore, provando recuperação
    independente da origem.
- `src/macip_backup/cli.py`
  - Sem alterações nesta sessão; a correção simétrica já presente foi
    revisada e mantida (`_paths_overlap` em `_safe_restore_target`).

### Comandos executados

```bash
git status --short
git log --oneline -10
pdftotext ~/Downloads/output.pdf /tmp/output.txt   # leitura do plano
PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py' -v
git diff --check
git add src/macip_backup/cli.py tests/test_cli.py tests/test_integration_restic.py
git commit -m "test(cli): complete recovery safety coverage"
```

### Resultados

- Suíte completa: **47 testes OK** (7 testes SFTP pulados — ambiente
  Docker isolado não estava ativo; comportamento esperado, sem regressão).
- `git diff --check`: sem erros.
- Commit criado: **`30b99f8` — `test(cli): complete recovery safety coverage`**
  - 3 arquivos, +220/−11 linhas.
- Sem push, conforme instrução.
- Arquivos não relacionados (`atualizações.txt` deletado, `.claude/`,
  `.trail/`, `tarefa-teste.json`) preservados fora do commit.

### Limitações restantes

- Testes SFTP reais não foram executados nesta sessão (requerem
  `tests/sftp/run-integration.sh` com Docker funcional).
- Retenção (`forget`/`prune`), SMB, S3, agente, API e portal continuam
  fora do escopo, conforme o plano.

---

## 2026-10-22 — Consolidação da baseline da POC (tag)

### Ação

- Criada a tag anotada **`poc-baseline`** apontando para o commit
  `30b99f8` (`test(cli): complete recovery safety coverage`), o commit
  onde a CLI de recuperação foi fechada — conforme o plano, a tag não
  usa `5c860d9` porque houve commits posteriores necessários.
- Mensagem da tag descreve o conteúdo da baseline: núcleo Python com
  destinos local e SFTP reais, múltiplos destinos independentes,
  tratamento de timeout/lock, check, snapshots e restore seguro via CLI;
  47 testes OK (7 SFTP requerem Docker isolado).

### Comandos executados

```bash
git tag -a poc-baseline -m "Baseline da POC Restic: ..."
git rev-parse "poc-baseline^{commit}"   # confirma 30b99f8
git log --oneline -3 poc-baseline
```

### Resultado

- `poc-baseline` → `30b99f8`
- Tag local apenas; **nenhum push foi feito**.
- Se a tag precisar ir para o remoto: `git push origin poc-baseline`
  (não executado).

---

## 2026-10-22 — Piloto SFTP em servidor real: preparação

### Contexto

Item 2 do roadmap pós-POC: validar SFTP contra servidor real/piloto
(rede real, latência, autenticação, host key, grandes volumes, muitos
arquivos, restore real). Docker não está disponível neste host e o host
não possui `sshd`, então a execução real exige um servidor externo
fornecido pelo usuário.

### Alterações realizadas

- `tests/sftp/run-pilot.sh` (novo, executável): runner da suíte SFTP
  contra servidor real, sem Docker.
  - Obrigatórios: `MACIP_SFTP_HOST`, `MACIP_SFTP_USER`,
    `MACIP_SFTP_IDENTITY_FILE`, `MACIP_SFTP_REMOTE_PATH`
    (`MACIP_SFTP_PORT` opcional, padrão 22).
  - Host key: usa `MACIP_SFTP_KNOWN_HOSTS_FILE` se fornecido; senão
    captura via `ssh-keyscan`, exibe fingerprints e só prossegue se
    `MACIP_SFTP_HOST_KEY_SHA256` conferir (preferido) ou
    `MACIP_SFTP_TRUST_ON_FIRST_USE=1` (TOFU explícito).
  - Pré-checagem de autenticação via `sftp -b /dev/null` antes da suíte.
  - Exporta `MACIP_SFTP_PILOT=1` e roda `tests.test_integration_sftp`.
  - Ao final lista repositórios `macip-test-*` deixados no servidor;
    `MACIP_SFTP_CLEANUP_SSH=1` tenta removê-los via shell SSH (contas
    `internal-sftp` não têm shell — limpeza fica manual).
  - Exit code da suíte é propagado; nenhum segredo é impresso.
- `tests/test_integration_sftp.py`: novo
  `test_pilot_volume_and_many_files_survive_restore`, habilitado por
  `MACIP_SFTP_PILOT=1`. Cria 300 arquivos em lotes + volume de 64 MiB
  (ajustáveis via `MACIP_SFTP_PILOT_FILES`/`MACIP_SFTP_PILOT_MEBIBYTES`),
  executa backup, `check --read-data` e restore com comparação SHA-256
  de todo o conteúdo; imprime tempos de backup/restore no stderr.
- `README.md`: documentação de uso do piloto na seção Testes.

### Comandos executados

```bash
bash -n tests/sftp/run-pilot.sh                     # sintaxe OK
./tests/sftp/run-pilot.sh                           # exit=2, variáveis ausentes
MACIP_SFTP_HOST=192.0.2.1 ... ./tests/sftp/run-pilot.sh  # exit=1, keyscan falhou (host TEST-NET)
PYTHONPATH=src python3 -m unittest tests.test_integration_sftp -v  # 8 pulados, OK
git diff --check                                    # sem erros
git commit -m "test(sftp): add real-server pilot runner and volume coverage"
```

### Resultado

- Commit `4375536` (3 arquivos, +222 linhas). Sem push.
- Caminhos de erro do script verificados localmente: variável ausente →
  exit 2; host inalcançável → exit 1 após `ssh-keyscan` falhar.
- Teste piloto coletado e pulado corretamente sem o ambiente.

### Pendente — precisa do usuário

- **Execução real** exige um servidor SFTP piloto: host, porta, usuário,
  chave privada autorizada, diretório remoto e fingerprint da host key
  (ou aceite TOFU). Sem isso o teste não pode rodar.

### Provisionamento (sequência)

- Usuário ainda não tinha servidor piloto; escolheu "preciso provisionar"
  e "decidir volume depois".
- Gerada chave do cliente em `~/.ssh/macip-piloto_ed25519`
  (ed25519, sem passphrase, comment `macip-backup-piloto`). A chave
  privada fica local; somente a `.pub` vai ao servidor.
- Criado `tests/sftp/piloto.md`: guia de provisionamento da conta
  `macipsftptest` (sem shell, key-only, `ForceCommand internal-sftp`,
  bloco `Match` no sshd), conferência de fingerprint, execução do
  `run-pilot.sh` e limpeza dos `macip-test-*`.
- `README.md`: referência ao guia na seção Testes.
- Commit `7ce7236` (`docs(sftp): pilot server provisioning guide`).

### Aguardando o usuário

- Rodar a etapa 1 de `tests/sftp/piloto.md` no servidor (requer sudo) e
  informar host/porta + fingerprint da host key para eu executar
  `run-pilot.sh`.
- Usuário confirmou que o piloto será uma VM Ubuntu no servidor de
  desenvolvimento. Adicionados pré-requisitos Ubuntu ao `piloto.md`
  (`openssh-server`, `ufw allow 22`, rede bridged/NAT com port-forward).
  Não commitado ainda — entra junto da próxima alteração ou do resultado
  da execução do piloto.
- Provisionamento iniciado em `srvdev1`: `openssh-server` instalado,
  `ufw allow 22`, usuário `macipsftptest` criado, `repositories/` e
  bloco `Match` aplicados. Dois pontos corrigidos em seguida:
  `authorized_keys` recebeu o placeholder do guia em vez da chave real
  (repetido com a `.pub` correta) e `systemctl reload sshd` falhou porque
  o Ubuntu usa ativação por socket — `ssh.socket` já lê a config nova a
  cada conexão.
- O comando `install /dev/stdin` do guia falhou na prática na VM
  ("No such file or directory"); `piloto.md` corrigido para gravar
  `authorized_keys` via `tee` + `chown`/`chmod`, método confirmado mais
  robusto.

### Execução do piloto — RESULTADO: 8/8 OK

Servidor: `srvdev1` (VM Ubuntu, `10.10.20.239:22`), usuário
`macipsftptest`, `internal-sftp`, key-only. Host key ed25519 conferida
via `MACIP_SFTP_HOST_KEY_SHA256` antes da suíte.

```bash
MACIP_SFTP_HOST=10.10.20.239 MACIP_SFTP_PORT=22 \
MACIP_SFTP_USER=macipsftptest \
MACIP_SFTP_IDENTITY_FILE=~/.ssh/macip-piloto_ed25519 \
MACIP_SFTP_REMOTE_PATH=repositories/piloto \
MACIP_SFTP_HOST_KEY_SHA256=SHA256:BlCxNhR6RDe4FLNXvBC2XXPwFYo/b3h54DoyfEhTkrk \
tests/sftp/run-pilot.sh
```

- Primeira execução: 7/8 ok; erro no teste piloto novo — bug na
  comparação de caminhos restaurados (`restic restore id:<subpasta>`
  restaura o conteúdo da subpasta diretamente no target, sem recriar o
  diretório da origem). Corrigido para `path.relative_to(restored)`.
- Segunda execução: **8/8 OK em 115.9s**. Piloto: backup 21.3s,
  restore 9.4s, 301 arquivos + 64 MiB com comparação SHA-256 total.
- Commit `eb6036a` (`test(sftp): fix pilot restore comparison, pass
  real server 8/8`).
- Repositórios `macip-test-*` permanecem no servidor para inspeção
  (conta sem shell; remoção manual em
  `/home/macipsftptest/repositories/piloto/`).
- Observação de escopo: LAN valida protocolo/auth/host key/volume/
  restore em rede real; condições de WAN (latência alta, perda) ficam
  para um servidor remoto de produção.

---

## 2026-10-22 — Homologação SMB: implementação (commit 6405c2c)

### Decisões (usuário)

- **Sistema monta, POC valida**: montagem via fstab/systemd/`mount.cifs`
  é responsabilidade do ambiente; a POC confere identidade do volume.
- Servidor SMB de teste: **Samba na VM srvdev1**.

### Implementação

- `models.py`
  - `DestinationKind.SMB`; `Destination.smb: SMBMount`; `repository`
    de destino SMB é **subpath relativo** dentro do compartilhamento
    (rejeita absoluto/`..`); `sftp`/`smb` cruzados entre kinds agora são
    rejeitados explicitamente (antes, campo estranho era ignorado).
  - `SMBMount{server, share, mount_point, marker?, probe_timeout_seconds}`:
    `validate_for_execution` confere via `findmnt -T` que o ponto é
    montagem CIFS/SMB3 do `//server/share` esperado — sem I/O de rede —
    e sonda o `marker` via `stat` em subprocesso com timeout (NAS morto
    não trava a validação).
  - `Source{path, smb?}`: `task.sources` agora é `tuple[Source]`;
    construtor aceita `Path`/`str` (normalizado em `__post_init__`);
    `task.source_paths` mantém consumidores Path. Origem SMB exige path
    dentro do `mount_point` e valida a montagem — origem indisponível
    falha a tarefa; destino indisponível falha só o destino.
- `restic.py`: destino SMB resolve `--repo` como caminho local no
  ponto de montagem (`mount_point/repository`).
- `cli.py`: proteções de TARGET do restore estendidas a destino SMB
  (mesmo tratamento de caminho local); origens passam por `source.path`.
- `orchestrator.py`: backup usa `task.source_paths`.

### Testes

- `tests/test_models.py`: `SMBMountValidationTests` (mount correto,
  não-montagem, share errada, fstype não-CIFS, marcador ausente/timeout,
  parsing `findmnt -P`), `SMBSourceValidationTests`, `SMBConfigurationTests`
  (parsing, campos inválidos, kind cruzado). Mensagem de `sources`
  ajustada para "texto ou objeto" (novo schema).
- `tests/test_integration_smb.py` (novo, gateado por
  `MACIP_SMB_INTEGRATION` + `SERVER/SHARE/MOUNT_POINT[/MARKER]`):
  backup/check/restore real, share errada falha só o destino SMB,
  marcador ausente, origem fora do mount.
- `examples/tarefa-smb.json` + seção "Destino SMB" no README.
- Suíte: **65 testes OK** (8 SFTP + 4 SMB pulados sem os ambientes).

### Comandos de provisionamento repassados ao usuário

- srvdev1: `apt install samba`, usuário `macipsmb`, shares `backups`(rw)
  e `docs`(ro) com `server min protocol = SMB3`, `smbpasswd -a`.
- Host: `apt install cifs-utils`, `credentials` em
  `/etc/samba/macip-piloto.cred` (600), `mount -t cifs //10.10.20.239/backups
  /mnt/macip-smb/backups -o vers=3.0,uid=$USER`, marcador `.macip-volume`.

### Pendente

- Executar `tests.test_integration_smb` com a montagem real criada
  (aguarda provisionamento do Samba + montagem cifs pelo usuário).
- Comportamento Windows/UNC fica para a etapa Windows do roadmap.

### Execução do piloto SMB — RESULTADO: 4/4 OK

Ambiente real: Samba na VM `srvdev1` (`10.10.20.239`), compartilhamento
`backups` (rw, `server min protocol = SMB3`, usuário `macipsmb`);
montagem cifs neste host em `/mnt/macip-smb/backups` com credencial em
`~/.macip-smb.cred` (600) e marcador `.macip-volume`.

```bash
MACIP_SMB_INTEGRATION=1 MACIP_SMB_SERVER=10.10.20.239 \
MACIP_SMB_SHARE=backups MACIP_SMB_MOUNT_POINT=/mnt/macip-smb/backups \
MACIP_SMB_MARKER=.macip-volume \
PYTHONPATH=src python3 -m unittest tests.test_integration_smb -v
```

- **4/4 OK em 32.7s**: backup + `check --read-data` + restore com
  conteúdo conferido; identidade de share errada falha só o destino SMB
  (local conclui → PARTIAL); marcador ausente falha o destino; origem
  fora do mount rejeitada.
- Repositórios de teste removidos pelo próprio tearDown (montagem é
  gravável); marcador `.macip-volume` permanece por convenção.
- Incidentes de provisionamento registrados: comandos de cliente foram
  rodados uma vez na VM por engano; `install /dev/stdin` e
  `/etc/samba/` ausente corrigidos com `tee`/`~/.macip-smb.cred`;
  `mount error(95)` com `vers=3.0` — Samba exige SMB3.1.1,
  resolvido com `vers=3.1.1`.

### Estado

- Origem SMB e destino SMB homologados em Linux via montagem cifs
  gerenciada pelo sistema + validação de identidade do volume.
- Restante do item SMB: comportamento Windows/UNC (etapa Windows) e
  indisponibilidade física real do NAS (desligar share/servidor) — o
  caso simulado (identidade errada/marcador ausente) já é coberto.

---

## 2026-10-22 — Nuvem/S3: implementação (commit 40d0c12)

### Decisão

Backend S3 **nativo do restic**. Endpoint compatível com API S3 —
para homologação local usa-se **MinIO** (binário único) na VM srvdev1;
o mesmo código aponta para AWS/B2/etc. mudando apenas endpoint,
região e bucket_lookup.

### Implementação

- `models.py`
  - `DestinationKind.S3`; `S3Connection{endpoint, bucket,
    access_key_environment, secret_key_environment, prefix?, region?,
    bucket_lookup?, storage_class?}`.
  - `endpoint`: URL http(s) validada por `urlsplit` — sem credenciais,
    caminho, query ou fragmento.
  - `bucket`: regex de nome S3 (3-63, minúsculas).
  - `prefix`: caminho remoto relativo (sem `/` inicial, `..`, `~`).
  - Chaves S3 **nunca** na config: nomes de variáveis de ambiente, mesmo
    padrão de `password_environment`. `validate_for_execution` falha
    cedo se a variável não existir.
  - `kind s3` exige bloco `s3`, proíbe `repository`/`sftp`/`smb`.
- `restic.py`
  - `--repo s3:{endpoint}/{bucket}[/{prefix}]`; opções `-o s3.region`,
    `-o s3.bucket-lookup`, `-o s3.storage-class` quando configuradas.
  - `_inject_destination_credentials`: lê as variáveis nomeadas e injeta
    `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` **só no processo restic**
    (também no `unlock` pós-timeout) — nunca em argv/log.
- `cli.py`: subcomando `verify-s3-integration` (padrão SFTP).
- `tests/test_models.py`: `S3ConfigurationTests` — parsing, URL/bucket/
  prefix/bucket_lookup inválidos, kind cruzado, credencial ausente.
- `tests/test_integration_s3.py` (novo, gateado por `MACIP_S3_INTEGRATION`
  + ENDPOINT/BUCKET/ACCESS_KEY_ENV/SECRET_KEY_ENV e as variáveis nomeadas
  com as chaves): backup/check/restore real, independência local+S3,
  secret errada e credencial ausente falham só o destino S3.
- `examples/tarefa-s3.json` + seção "Destino S3" no README.
- Suíte: **74 testes OK** (16 pulados — SFTP, SMB e S3 aguardam
  ambientes).

### Execução do piloto S3 — RESULTADO: 4/4 OK

- MinIO descartado: `dl.min.io` retorna `410 Gone` (binários comunitários
  removidos). Substituído por **SeaweedFS 4.48** — binário único
  (`weed mini`), instalado em `/usr/local/bin` na VM srvdev1.
- Endpoint real: `http://10.10.20.239:8333`, bucket `macip-test`,
  credenciais `macips3`/`macip-teste-s3` (teste), `bucket_lookup=path`.
- `weed mini` roda em background com `nohup` + `~/weed.log`
  (não é serviço persistente — reiniciar com o mesmo comando).

```bash
MACIP_S3_INTEGRATION=1 MACIP_S3_ENDPOINT=http://10.10.20.239:8333 \
MACIP_S3_BUCKET=macip-test MACIP_S3_PREFIX=piloto \
MACIP_S3_BUCKET_LOOKUP=path \
MACIP_S3_ACCESS_KEY_ENV=MACIP_S3_TEST_ACCESS \
MACIP_S3_SECRET_KEY_ENV=MACIP_S3_TEST_SECRET \
MACIP_S3_TEST_ACCESS=macips3 MACIP_S3_TEST_SECRET='macip-teste-s3' \
PYTHONPATH=src python3 -m unittest tests.test_integration_s3 -v
```

- **4/4 OK em ~262s**: backup + `check --read-data` + `snapshots` +
  restore com conteúdo conferido; local+S3 independentes; secret errada
  e variável de credencial ausente falham só o destino S3.
- Prefixos de teste `macip-test-*` ficam no bucket (pequenos); limpeza
  via `weed` ou API S3 quando desejado.

### Pendente do item nuvem

- Homologação contra **provedor gerenciado** (AWS/B2/etc.): exige conta
  do usuário — cobre custo, latência WAN e proteção contra exclusão.
- O código já é provider-agnóstico (endpoint/região/bucket_lookup).

---

## 2026-10-22 — Matriz de combinações de destinos: homologada

`tests/test_integration_combinations.py` (novo): um único `execute`
grava nos 4 backends ao mesmo tempo e cada repositório é verificado
(`check --read-data`) e restaurado com conteúdo conferido.

- **2/2 OK**:
  - `local + sftp + smb + s3` numa tarefa — 4 snapshots, 4 restores ok;
  - `sftp + smb + s3` sem local — ok.
- Incidente: `~/.ssh/known_hosts` do usuário não tinha a chave do srvdev1
  (o piloto SFTP usava arquivo dedicado). Gerado
  `/tmp/macip-combo-known-hosts` via `ssh-keyscan -t ed25519` com
  fingerprint conferida (`BlCxNhR6...`). Erro de teste no campo
  `destination.id` → corrigido para `destination_id`.

### Homologação de destinos — estado final do item

| Destino | Resultado real |
|---|---|
| local | ✓ suíte local |
| sftp | ✓ 8/8 piloto |
| smb | ✓ 4/4 real |
| s3 | ✓ 4/4 real (SeaweedFS) |
| combinações | ✓ 2/2 (4 backends + trio remoto) |

---

## 2026-10-22 — Retenção aplicada: forget/prune por tarefa

### Implementação

- `OperationTimeouts.forget_seconds` (padrão 3600).
- `ResticAdapter.forget(destination, password, keep_last, tag, timeout)` →
  `restic forget --keep-last N --tag <tag> --prune` — escopo por tag limita
  a remoção aos snapshots da própria tarefa (`task:<task_id>`).
- `orchestrator`: após `check` bem-sucedido, se `destination.retention.
  keep_last` declarado → aplica forget+prune e o `snapshots_available` do
  resultado já reflete a contagem pós-retenção. Falha na retenção falha o
  destino, mas o resultado **preserva `snapshot_id`** do backup feito.
- `cli`: subcomando `forget <config> <destination_id>` — aplica sob demanda
  a política declarada; erro se `keep_last` não declarado.

### Testes

- Unitários: `forget` no ControlledAdapter do orquestrador (aplicado com
  tag `task:x`, sem política não chama, falha → FAILED c/ snapshot_id);
  comando restic `forget --keep-last 5 --tag task:x --prune` sem senha
  em argv; `forget_seconds` no parsing.
- Real: `test_retention_policy_prunes_old_snapshots_after_backup` — 2
  backups com `keep_last=1` → resta 1 snapshot (o mais recente), restore
  do conteúdo novo verificado.
- Suíte: **80 testes OK** (18 pulados, gateados por ambiente).

### Fechamento de itens do PDF

- "Política de retenção por tarefa ou destino": **aplicada** (era só
  declarativa). Critérios avançados (período, tags além de keep_last)
  ficam para decisão posterior — o modelo suporta extensão.

---

## 2026-10-22 — Agente local: retry, SQLite e credenciais do SO

### Implementação

- `models.py`: `RetryPolicy{attempts=1, backoff_seconds=0}` por tarefa
  (`retry` opcional no JSON; validador `_optional_non_negative_integer`
  porque backoff=0 é válido — não cabia em `_optional_positive_integer`).
- `orchestrator.py`: `_execute_destination` virou loop de tentativas;
  `_attempt_destination` faz init→backup→check→retention→snapshots por
  tentativa; log `backup.destination.retry` com `attempt` e
  `previous_error`; `sleep(backoff_seconds)` entre tentativas.
- `credentials.py` (novo): `credential_from_environment_or_os` — env
  primeiro, depois `keyctl pipe %user:macip.<nome>` no kernel keyring
  (keyutils já instalado; timeout de 10s; vazio/ausente → erro
  controlado). Interface aberta a systemd `LoadCredential`/Secret
  Service depois.
- `store.py` (novo): `ExecutionStore` SQLite — tabelas `executions` e
  `destination_results` com status, snapshot_id, erro, exit_code,
  failed_operation, timeout, duration. Nada de segredos persistidos.
- `agent.py` (novo): `run_task` executa via orchestrator resolvendo
  credenciais pelo SO e registra o resultado; `history`/`history_json`
  consultam o banco. Offline por construção — sem portal/API.
- `cli.py`: `agent <config> [--store db]` (executa e persiste) e
  `history --store db [--task id]` (lista JSON).

### Testes

- Unitários: retry recupera (falha uma vez → 2ª tentativa ok, log de
  retry), exaustão → FAILED; parsing de `retry` (valores inválidos);
  resolução env→keyctl, keyctl ausente, segredo vazio, chave inexistente.
- Reais: round-trip `keyctl` no kernel keyring da sessão; agente e2e
  (`run_task` → backup local → `history` → CLI `history` imprime JSON).
- Suíte: **95 testes OK** (18 pulados, gateados por ambiente).

### Estado do item agente

Implementado: execução local offline, retry por destino, histórico/auditoria
SQLite, credenciais do SO (kernel keyring). **Pendente do item**: daemon
systemd com agendamento (item separado de schedule), sincronização com a
API central (item API), update seguro e credenciais via
systemd/Secret Service (produção).

---

## 2026-10-22 — Consolidação do agente: tentativas e credencial unificada

Revisão do commit b0e590c antes de systemd/Windows/API/portal.

### Correções

- **Histórico por tentativa**: `AttemptResult` no orquestrador e tabela
  `attempts` no SQLite — número, horários, duração, `failed_operation`,
  status e `snapshot_id` por tentativa. `record_start` grava `running`
  antes do backup; `record_finish` conclui — execução interrompida fica
  identificável. Eventos persistidos em `logs/<execution_id>.jsonl`
  (não mais `StringIO` descartado). Nenhum segredo persistido.
- **Credenciais unificadas**: `credential_from_environment_or_os` —
  env → `keyctl pipe %user:macip.<nome>` — é a única resolução, usada por
  `run`, `agent`, `snapshots`, `check`, `restore`, `forget` **e** pela
  injeção S3 (`AWS_*` injetados só no processo filho; `os.environ`
  intocado; pre-flight S3 saiu do modelo para a injeção). Conteúdo do
  keyring preservado exatamente (sem `strip()`). `CredentialError` é
  `ConfigurationError` — não-repetível por natureza.
- **Retry explícito**: repetíveis = `ResticCommandError`,
  `ResticTimeoutError`, `OSError`; não-repetíveis = `ConfigurationError`/
  `CredentialError` (config/credencial) e `ResticError` puro
  (precondições). Backup que já gerou snapshot não é refeito na
  retentativa — retoma check/retenção/listagem com o mesmo snapshot.
- Documentado: provisioning via `keyctl padd` (stdin, sem argv/histórico)
  e limitação de `@s` entre sessões/reboot.

### Testes novos/corrigidos

- Falha transitória + sucesso → 2 tentativas consultáveis no histórico.
- Falha pós-backup → retentativa sem novo `backup` (1 chamada) e
  `snapshot_id` preservado na tentativa falha.
- Credencial **somente no keyring** → `run_task` e `snapshots` (CLI) OK
  com ambiente limpo (e2e real: keyctl + restic).
- Segredo com espaços nas extremidades preservado byte a byte.
- Segredo ausente do log JSONL e do banco SQLite.
- Execução sem `record_finish` → histórico mostra `running`.
- Suíte: **101 testes** — 83 aprovados, 0 falhos, 18 ignorados (SFTP 8,
  SMB 4, S3 4, combinações 2 — ambientes remotos não preparados nesta
  execução; homologados anteriormente).

---

## 2026-10-23 — Consolidação do agente (2ª rodada): migração, persistência incremental e classificação de retry

Revisão do commit 4e07339 — três lacunas corrigidas.

### 1. Migração SQLite versionada

- `PRAGMA user_version` governa o esquema (atual: **2**).
- v0→v1 (esquema de b0e590c: `executions.finished_at_utc NOT NULL`, sem
  `attempts`): rebuild transacional de `executions` com `finished_at_utc`
  anulável, preservando linhas.
- v1→v2 (esquema de 4e07339): rebuild transacional de `attempts` e
  `destination_results` relaxando `NOT NULL` em `finished_at_utc`/
  `duration_seconds` — necessário para linhas `running`.
- Bancos novos saltam direto para v2; reabrir é idempotente — nunca apaga
  nem recria dados.

### 2. Persistência incremental

- `ExecutionRecorder` (Protocol) injetado em `execute(recorder=...)`;
  `ExecutionStore` o implementa. Sem recorder, `run` permanece stateless.
- Hooks: `attempt_started` (linha `running` + destino `running`),
  `attempt_snapshot` (grava `snapshot_id` logo que o backup retorna),
  `attempt_finished`, `destination_finished`. Cada escrita commita —
  morte abrupta preserva o já gravado.
- `record_finish` só conclui a execução (backfill idempotente cobre
  fluxos sem hooks). `history` mostra tentativas com execução `running`.

### 3. Classificação de retry

- `ResticCommandError` deixou de ser repetível por padrão: o detalhe é
  classificado por padrões (saída real do restic/ssh/S3).
- Não-repetível: `wrong password`, `no key found`, `permission denied`,
  `access denied`/`AccessDenied`, `host key`, `authentication failed`,
  `InvalidAccessKeyId`, `SignatureDoesNotMatch`, `forbidden`.
- Transitório: `connection refused/reset`, `timed out`/`timeout`,
  `no route to host`, `network is unreachable`, `temporary failure`,
  `service unavailable`, `i/o timeout`, `unexpected eof`, `broken pipe`.
- Limitação documentada: restic usa exit codes genéricos; erros fora dos
  padrões são **não repetíveis** (conservador) — novos padrões entram
  conforme erros reais aparecem.
- Retomada preserva snapshot (sem novo backup) e independência entre
  destinos.

### Testes novos

- Migração: banco criado com o schema v0 real + histórico → migra,
  preserva linhas, aceita `running`/conclusão; reabertura idempotente.
- Kill abrupto: subprocesso do agente com destino local (conclui) e
  destino SFTP preso em listener TCP mudo → `SIGKILL` no process group
  durante a tentativa → reabertura mostra execução `running`, destino
  local `success` com snapshot, tentativa `running` sem conclusão.
- Incremental: tentativa `running` + `snapshot_id` visíveis no histórico
  antes do fim da execução.
- Classificação: senha errada, host key desconhecida, permission denied,
  AccessDenied, SignatureDoesNotMatch e erro genérico → 1 tentativa;
  `connection refused`/`timed out` → esgota tentativas.
- Suíte: **106 testes** — 88 aprovados, 0 falhos, 18 ignorados (SFTP 8,
  SMB 4, S3 4, combinações 2 — ambientes remotos não preparados nesta
  execução).

---

## 2026-10-23 — Migração SQLite atômica

Correção de três falhas reproduzidas no `f5da460`:

- **`executescript` comitava no meio da migração** — falha após
  `DROP TABLE` deixava `executions` removida, `executions_v1` órfã e
  `user_version=0`. Agora toda a migração roda numa única transação
  explícita (`BEGIN IMMEDIATE` + DDL por `execute()` + `user_version` +
  `COMMIT`), com `ROLLBACK` integral em qualquer erro — esquema, versão
  e histórico voltam exatamente ao estado anterior.
- **Versão futura era sobrescrita** por 2 silenciosamente — agora
  `version > 2` é rejeitado sem tocar o banco.
- **Conexão ficava aberta** quando a inicialização do store falhava —
  `__init__` fecha antes de relançar o erro.
- Rebuilds preservam chaves estrangeiras (cláusulas `REFERENCES`
  mantidas nas tabelas novas) e o índice `idx_executions_task` (recriado
  após o rename — ele morre junto com a tabela original).
- Bônus defensivo: resíduos de migrações antigas quebradas
  (`executions_v1`/`attempts_v2`/`destination_results_v2` órfãos ou
  duplicados) são saneados — o resíduo nunca assume o lugar da tabela
  original quando ambas existem.

Testes: falha injetada no `ALTER...RENAME` (após o `DROP`) → reabertura
com conexão nova confirma esquema/v0/histórico originais; nova migração
conclui depois; versão futura rejeitada intacta; índice e FKs
verificados após migração. Suíte: **108 testes** — 90 aprovados,
0 falhos, 18 ignorados (ambientes remotos não preparados nesta execução).

---

## 2026-10-23 — Execução automática via systemd --user

### Implementação

- `systemd.py` (novo): `install_schedule` gera
  `macip-backup-agent-<task>.service` (`Type=oneshot`,
  `python -m macip_backup.cli agent`, `EnvironmentFile=-%h/.config/
  macip-backup/agent.env` opcional) + `.timer` (`OnCalendar` do operador,
  `Persistent=true`, `AccuracySec=1s` — o padrão 1min do systemd atrasava
  o primeiro disparo). `uninstall_schedule` desativa e remove.
  CLI: `schedule <config> --store db --calendar <expr> [--enable]` e
  `unschedule <task_id>`.
- `agent.py`: `flock` em `<store>.lock` (LOCK_EX|LOCK_NB) — impede
  execuções simultâneas na mesma base, igualmente para CLI e service
  (mesmo caminho de código); kernel libera em morte abrupta. SIGTERM/
  SIGINT marcam `interrupted` antes de sair (best-effort — sweep cobre
  falha da marcação). No início, `mark_stale_interrupted` converte
  `running` órfãs (kill -9, reboot) em `interrupted`.
- `store.py`: `mark_stale_interrupted` (todas as execuções running) e
  `mark_interrupted` (escopo de uma execução) — status `interrupted` em
  executions, attempts e destination_results.
- Credenciais sem terminal: `@u` (kernel keyring do usuário — persistido
  entre logins até reboot, já coberto pela busca `%user:`) e
  `EnvironmentFile` opcional no service. Homologado: service rodou com
  segredo apenas no `@u` e ambiente limpo.

### Homologação real (systemctl --user ativo neste host)

- `systemctl --user start` no service → execução `success` no SQLite +
  restore real do snapshot criado pelo serviço.
- Timer `*-*-* *:*:0/10` com `--enable` disparou execução sozinho.
- Destino SFTP travado + `systemctl kill -s SIGTERM` → execução e
  tentativa marcadas `interrupted`; enquanto o serviço corria, `agent`
  na CLI falhou imediatamente pelo lock compartilhado.
- Unitários: geração de units (ExecStart, OnCalendar, Persistent,
  EnvironmentFile), lock concorrente, marcação de interrompidas.
- Suíte: **116 testes** — 98 aprovados, 0 falhos, 18 ignorados
  (SFTP/SMB/S3/combinações — ambientes remotos não preparados).

---

## 2026-10-23 — Consolidação systemd: exclusão por tarefa, cancelamento limpo e units escapadas

Revisão do incremento 34d7f8e — quatro lacunas corrigidas.

### 1. Exclusão de execução

- O lock migrou de `<store>.lock` (por banco) para **flock por tarefa** em
  `~/.local/state/macip-backup/locks/<task_id>.lock` — `run`, `agent` e o
  service compartilham o mesmo arquivo; `--store` diferente não contorna.
  `MACIP_LOCK_DIR` sobrescreve o diretório em testes.
- Colisão real testada: `agent` em subprocesso (destino SFTP travado) +
  `python -m macip_backup.cli run` da mesma tarefa → recusa imediata.

### 2. Cancelamento

- `ResticCancelledError` (ResticError → não-repetível por natureza) +
  `adapter.cancel(motivo)`: registra o motivo e encerra o grupo de
  processos com SIGTERM → espera limitada → SIGKILL (reusa a escalada já
  homologada nos timeouts). Handler de sinal sem locks — atribuições
  atômicas sob a GIL (handler roda na main thread; lock congelaria).
- Janela Popen→registro coberta: `_run` re-verifica `cancel_reason` após
  registrar o processo e limpa ali mesmo.
- `mark_interrupted` só acontece depois que `execute()` retorna — ou
  seja, após o encerramento confirmado. `executions.interrupt_detail`
  (v3) registra o sinal e eventual `cancel_cleanup_error`.
- Migração v2→v3: `ALTER TABLE executions ADD COLUMN interrupt_detail`.
- Lock permanece adquirido durante toda a limpeza (finally no fim).
- Teste real sem cgroup: `agent` em subprocesso, `SIGTERM` só no PID →
  grupo restic+ssh extinto (verificado por pgrep), execução `interrupted`
  com `sinal 15`, e `run_task` seguinte adquire o lock e conclui.

### 3. Inicialização

- `store_path.parent.mkdir` antes de abrir o lock; se `ExecutionStore`
  falhar, o lock é liberado imediatamente.
- Teste de primeira execução com diretórios inexistentes.

### 4. Units

- `_unit_value`/`_escape_unit`: escape próprio de systemd (`"..."` com
  `\\` e `\"`) em argumentos de ExecStart e Environment — não shlex.
- `systemd-analyze calendar` valida `OnCalendar` antes de sobrescrever
  units existentes (inválida → erro, units intactas).
- Homologação real: service com `dir com espaços/` nos caminhos de
  config e store executou com sucesso — argumentos recebidos exatos.

Suíte: **122 testes** — 104 aprovados, 0 falhos, 18 ignorados
(SFTP/SMB/S3/combinações — ambientes remotos não preparados).

---

## 2026-10-23 — Consolidação systemd (2ª rodada): sweep com posse de lock e cancelamento confirmado

Revisão de b0ae606 — três casos reproduzidos corrigidos.

### 1. Varredura de execuções antigas

- `mark_stale_interrupted` marcava TODAS as execuções `running` — uma
  tarefa B sobrescrevia uma execução legítima de A. Agora escopo por
  tarefa: a própria (lock já adquirido) e outras só após prova de lock
  LIVRE por tentativa de aquisição — lock ocupado → execução intacta.
- `running_task_ids()` lista tarefas com `running`; `_sweep_stale_executions`
  faz a sonda por lock antes de tocar em cada uma.
- Teste A/B no mesmo SQLite: A com lock ocupado permanece `running`;
  órfã real (lock livre) vira `interrupted`.

### 2. Confirmação do encerramento

- `_process_group_exists` passou a varrer `/proc` ignorando zumbis —
  killpg(0) conta zumbis e o líder não recolhido (thread presa em
  communicate) fazia o grupo parecer vivo: consumia toda a espera de
  SIGKILL e gerava `cancel_cleanup_error` espúrio.
- Encerramento limpo por SIGTERM agora é confirmado em ~ms; descendente
  que ignora SIGTERM ainda escala para SIGKILL (testado).
- `_unlock_after_timeout` também registra o processo como corrente —
  cancel() alcança o unlock; com cancelamento pendente o unlock nem
  inicia.
- Lock do agente mantido durante toda a limpeza.

### 3. Cancelamento multi-destino

- `DestinationStatus.INTERRUPTED` e `ExecutionStatus.INTERRUPTED` novos;
  destinos não iniciados viram `interrupted` (sem tentativas, sem tocar
  o adaptador), destino interrompido no meio finaliza `interrupted`
  quando cancelado; retries não disparam após cancelamento.
- Consistência: SQLite (`interrupt_detail` + status), evento
  `backup.execution.interrupted`/`backup.destination.interrupted` e JSON
  da CLI (`run` também passa por cancel handlers + task lock).
- Teste real: A (local) conclui → SIGTERM só no PID do agente durante B
  (SFTP travado) → execução `interrupted` com `sinal 15`, snapshot de A
  preservado, C registrado `interrupted` sem tentativas.

Suíte: **126 testes** — 108 aprovados, 0 falhos, 18 ignorados
(SFTP/SMB/S3/combinações — ambientes remotos não preparados).

---

## 2026-10-23 — Cancelamento durante o backoff entre tentativas

Caso reproduzido: falha transitória → espera entre tentativas →
`adapter.cancel()` chega na espera → o loop só saía DEPOIS de dormir o
backoff inteiro e ainda chamava `initialize` da próxima tentativa.

### Correção

- `_sleep_until_cancelled`: a espera dorme em fatias de ≤0,1s e retorna
  assim que `adapter.cancelled` vira verdade — resposta limitada e
  documentada. Não usa `threading.Event`: `Event.set()` adquire um
  cond-lock e o handler de sinal roda na main thread — poderia
  congelar se a thread segurasse o lock (deadlock). Poll em fatias é
  a alternativa sem locks.
- Verificação de cancelamento também no TOPO do loop — antes de
  registrar/iniciar qualquer nova tentativa, inclusive logo após a
  espera.
- Tentativa concluída e snapshot (quando existir) preservados; destino
  finaliza `interrupted` via `_finalize_destination`; destinos seguintes
  nem iniciados (`_cancelled_destination`).

### Teste determinístico

`backoff_seconds=600`, falha transitória na tentativa 1, cancel logo
após entrar na espera (sincronizado por hook em
`_sleep_until_cancelled`):

- saída em ~0,1s (não 600s);
- exatamente 1 tentativa e nenhum initialize/backup extra;
- destino seguinte `interrupted` sem tentativas;
- execução `interrupted` — inclusive no histórico persistido com
  recorder real.

`ExecutionStore` ganhou `check_same_thread` opcional para o cenário
multi-thread do teste.

Suíte: **127 testes** — 109 aprovados, 0 falhos, 18 ignorados
(SFTP/SMB/S3/combinações — ambientes remotos não preparados).

---

## 2026-10-23 — Cancelamento entre seleção do destino e tentativa 1

Caso reproduzido: `execute()` verifica cancelamento na seleção → sinal
chega antes da primeira iteração de `_execute_destination` → checagem
no topo do loop encerra com `attempts=[]` → `_finalize_destination`
acessava `attempts[-1]` → `IndexError`.

### Correção

- `attempts` vazio após o loop → retorna `_cancelled_destination`
  (interrupted, zero tentativas, persistido pelo recorder) — o
  `IndexError` da finalização por `attempts[-1]` desaparece.

### Teste determinístico

`FlipAdapter` — `cancelled` False na 1ª consulta (seleção) e True na
2ª (topo do loop), reproduzindo o sinal que cai exatamente entre as
duas verificações:

- execução e ambos os destinos `interrupted`;
- `adapter.calls` vazio — nenhuma chamada de initialize/backup;
- zero tentativas em todos os destinos;
- histórico persistido `interrupted` sem tentativas.

Suíte: **128 testes** — 110 aprovados, 0 falhos, 18 ignorados
(SFTP/SMB/S3/combinações — ambientes remotos não preparados).

---

## 2026-10-23 — Instalação reproduzível e piloto Linux

### Escopo

Instalação, atualização, desinstalação, documentação e validação
operacional. Windows/API/portal/auto-update fora do escopo; nenhum
servidor, firewall, mount ou política de AV alterada.

### Artefato e layout

- Wheel `macip_backup_poc-0.2.0-py3-none-any.whl` (versão em
  `pyproject.toml`; `build_wheel` do setuptools — pip não é requisito).
- `deploy/install.sh <wheel> [--commit SHA]` — instalação por usuário:
  `releases/<versão>` + `current` + launcher `~/.local/bin/macip-backup`
  (shebang `/bin/sh` — funciona com espaços, ao contrário do shebang do
  script venv). Config 0700, `agent.env` 0600, state/logs/locks/cache
  por usuário. Manifesto `install.json` com versão + commit.
- Recusa instalar/atualizar com execução ativa (flock nos locks).
- Atualização: backup de `agent.db` em `db-backups/` antes de qualquer
  toque; flip do `current` só no fim — falha mantém a anterior.
- `deploy/uninstall.sh` — recusa com execução ativa; remove units
  `macip-backup-*`, launcher próprio e releases; preserva config/creds/
  SQLite/logs/repos; `--purge` remove também os dados operacionais.
- `MACIP_CACHE_DIR` → cache restic por usuário (default
  `~/.cache/macip-backup`).

### Homologação desta sessão (laboratório — HOME isolado)

`tests/test_install.py` — 7 testes:
- wheel instalado e executado fora do checkout (sem PYTHONPATH);
- primeira instalação + backup → snapshots → check --read-data →
  restore + comparação de conteúdo pela CLI instalada;
- reinstalação preserva config/histórico;
- HOME com espaços;
- atualização com falha controlada + instalação anterior utilizável +
  recuperação;
- recusa de install/uninstall com lock ativo;
- desinstalação preservando dados operacionais.

### Pendente (não homologado)

- Segunda máquina Linux real: sem host disponibilizado nesta sessão —
  guia executável entregue; validação registrada como pendente.
- Piloto de 7 dias: checklist em `docs/piloto-operacao.md` §7 — ainda
  não executado; não se declara concluído.

### Suíte

**135 testes** — 117 aprovados, 0 falhos, 18 ignorados
(SFTP/SMB/S3/combinações — ambientes remotos não preparados).

---

## 2026-10-23 — Ciclo install/update/uninstall consolidado

### 1. Releases imutáveis

- Release nomeada `<versão>-<sha256[:12]>` — artefato distinto →
  diretório distinto; mesmo artefato → `.ok` reutilizado (idempotente).
- Candidato montado em `.candidate-*`, validado (venv + pip + smoke),
  promovido por `mv -T`; flip de `current` por symlink temporário +
  `mv -T` — nunca apagamos a release apontada por current.
- `install.json` registra versão + wheel + sha256 + commit.

### 2. Serviço sobrevive à atualização

- Units geradas na instalação operacional usam o launcher
  `~/.local/bin/macip-backup` como ExecStart (resolve `current` a cada
  chamada) e NÃO carregam PYTHONPATH — geradas uma vez, executam o
  código novo após update. Dev (sem launcher) segue `python -m` +
  PYTHONPATH.
- `--version` expõe a release realmente executada.

### 3. Exclusão de manutenção

- `maintenance.lock` compartilhado: executores (run/agent/service)
  seguram SH; install/uninstall seguram EX|NB — corrida recusada nos
  dois sentidos, tarefas diferentes seguem paralelas.

### 4. Preservação na desinstalação

- Só units com marcador `# macip-backup:` são removidas — prefixo
  semelhante alheio preservado. Falha ao desativar uma unit nossa
  ABORTA antes de remover o programa. `--purge` removido — dados
  operacionais sempre preservados.

### 5. Histórico e guia

- Backup do `agent.db` via `sqlite3.Connection.backup` (Python) — sem
  fallback de `cp`.
- Guia: `history` vem de `agent` (não `run`); agent.env acrescentado,
  nunca sobrescrito; `keyctl padd` alimentado por stdin.

### Homologação desta sessão (isolada, HOME fake + systemctl stub)

15 testes em `tests/test_install.py`: wheel inválido da mesma versão
→ CLI anterior intacta; reinstalação idempotente; mesmo nome +
conteúdo distinto → releases distintos; unit gerada em A executa B
(--version); corrida de manutenção nos dois sentidos; unit alheia +
systemctl falhando preservados. Validação real de systemd (timer
disparando código novo) segue pendente com a segunda máquina.

Suíte: **143 testes** — 125 aprovados, 0 falhos, 18 ignorados
(SFTP/SMB/S3/combinações — ambientes remotos não preparados).

---

## 2026-10-23 — Identidade de caminhos e propriedade da instalação

### 1. Layout consistente

- `task_lock_directory()`/`cache_directory()` passam a resolver
  `MACIP_STATE`/`MACIP_CACHE` — as mesmas variáveis do instalador.
- O launcher exporta `MACIP_HOME`, `MACIP_CONFIG`, `MACIP_STATE`,
  `MACIP_CACHE`, `MACIP_BIN` — run/agent/service resolvem exatamente
  os mesmos caminhos; layout personalizado não contorna o lock.
- `EnvironmentFile` da unit usa `MACIP_CONFIG` quando definido
  (escapado); senão `%h/.config/macip-backup/agent.env`.
- Testes: manutenção EX em `MACIP_STATE` customizado recusa `run`;
  executor SH no mesmo layout recusa `install.sh`.

### 2. Propriedade dos arquivos

- Antes de escrever `macip-backup`, o instalador exige que o arquivo
  existente pertença a ESTA instalação (conteúdo referencia
  `$MACIP_HOME` + `macip_backup.cli`); executável alheio → recusa,
  byte a byte intacto e nada criado.
- Units ganham identidade por instalação: `# macip-backup:
  install=<MACIP_HOME>` (ou `<src_root>` em dev). O desinstalador só
  remove units cuja identidade bate com o `$MACIP_HOME` atual —
  units de OUTRA instalação MAC IP são preservadas, assim como
  executáveis/units sem marcador confirmável.
- `install_root` derivado de `sys.executable` sem `resolve()` (venv é
  symlink do python do sistema) e sem avaliar o fallback ansiosamente.

### Homologação desta sessão (isolada)

18 testes em `tests/test_install.py`: executável alheio preservado
byte a byte; exclusão de manutenção em layout customizado nos dois
sentidos; units de outra instalação preservadas; reinstalação
idempotente mantida.

Suíte: **146 testes** — 128 aprovados, 0 falhos, 18 ignorados
(SFTP/SMB/S3/combinações — ambientes remotos não preparados).

---

## 2026-10-23 — Comparação exata de propriedade

### Correções

- Units: `grep -Fxq -- "$marker"` — linha completa e literal. Com
  substring, `install=/x/macip-backup` batia em
  `install=/x/macip-backup-teste` e units de outra instalação seriam
  removidas.
- Launcher: marcador explícito `# macip-backup: install=<MACIP_HOME>`
  gravado na geração e validado com comparação exata — sem busca por
  substring de MACIP_HOME. Sem marcador exato → preservado.
- Instalador valida o launcher existente pelo mesmo marcador — instalar
  A sobre launcher de B recusa sem tocar o arquivo.

### Testes (prefixo compartilhado A=`…/macip-backup`, B=`…/macip-backup-teste`)

- Desinstalar A preserva units de B (marker `-teste`) + units sem
  marcador + binário alheio.
- Instalar A sobre launcher de B recusa — bytes intactos.
- Reinstalar A sobre seu próprio launcher segue idempotente.

Suíte: **148 testes** — 130 aprovados, 0 falhos, 18 ignorados
(SFTP/SMB/S3/combinações — ambientes remotos não preparados).

---

## 2026-10-23 — Homologação operacional real (systemd verdadeiro)

Segunda máquina não disponibilizada nesta sessão — por instrução,
comprovado o que era possível com systemd --user real nesta máquina e
entregue runbook executável (`docs/homologacao-segunda-maquina.md`).

### Comprovado em sessão real

1. Wheel instalado fora do checkout (`/tmp/macip-homolog`, layout
   isolado por env) — launcher, marker `install=`, install.json.
2. Backup → `check --read-data` → `restore` + `diff -r` idêntico,
   credencial SOMENTE via `keyctl ... @u` (por stdin).
3. **Update pelo systemd real**: unit gerada em 0.2.0, `install.sh`
   com wheel 0.2.1, mesmo service startado → `Result=success`,
   `macip-backup --version` = 0.2.1 → o serviço executou o código
   novo. Execuções 1→3 sucessivas no histórico.
4. `unschedule` removeu as units reais corretamente.
5. `loginctl show-user $USER -p Linger` → **`Linger=no`** —
   requisito administrativo para timer-sem-login documentado (não
   alterado automaticamente).

### Pendente (não comprovado)

- Reboot real: timer disparando sem sessão de terminal.
- Segunda máquina física (não disponibilizada).
- Piloto de 7 dias (checklist pronto, não iniciado — não se declara).

Suíte: 148 testes — 130 aprovados, 0 falhos, 18 ignorados.

---

## 2026-10-23 — Correção de EnvironmentFile (defeito real da VM do piloto)

### Defeito

Unit gerada em `0.2.0` (f4b624c) emitia
`EnvironmentFile=-"/path/agent.env"` — o `-` antes das aspas faz o
systemd tratar `"..."` como parte do filename → journal:
`EnvironmentFile= path is not absolute, ignoring` → serviço sem
credencial → backup falhou na VM do piloto (Ubuntu 26.04, estagio01).

### Correção

`systemd.py`: EnvironmentFile usa a sintaxe própria — `-` literal
seguido do caminho bruto; a linha inteira é o filename, aspas NÃO são
suportadas nessa diretiva (verificado com `systemd-analyze verify` —
`-"path"` e `"-path"` ignoram; `-/path com espaços` é aceito literal).
Caminho padrão `-%h/...` e `MACIP_CONFIG` personalizado com espaços
cobertos. Versão → 0.2.1 (pyproject + `__version__`).

### Homologação real desta sessão

- `test_service_loads_credential_from_environment_file` (systemd real):
  credencial existia SÓ em `agent.env` dentro de `cfg com espaços/` —
  sem variável herdada, sem entrada no keyring. Service → sucesso;
  journal sem `path is not absolute`.
- `test_environment_file_with_spaced_config_dir_is_unquoted`: linha
  gerada bate literalmente + `systemd-analyze verify` limpo.

Suíte: **150 testes** — 132 aprovados, 0 falhos, 18 ignorados
(SFTP/SMB/S3/combinações — ambientes remotos não preparados).

### Artefato para a VM

`dist/macip_backup_poc-0.2.1-py3-none-any.whl` —
SHA-256 `ac7ba45455453bf2312762ccf613ac983ec898e55e8cab5a3b2f40134a1a2136`.
A unit da VM (corrigida manualmente) segue válida — marcador
`install=` preservado; atualização por `install.sh` não toca units.

---

## 2026-10-26 — API central: cadastro + recebimento autenticado (etapa 1)

Branch `feature/control-api`. Pacote separado `api/` (`macip-backup-api`)
— o agente Linux NÃO exige FastAPI/PostgreSQL. Piloto de 7 dias segue
em acompanhamento (iniciado 26/10, término previsto 02/11) — a VM não
foi alterada.

### Entregue

- `api/` FastAPI + SQLAlchemy + psycopg + PostgreSQL:
  - clients / agents / agent_credentials (token só como SHA-256,
    entregue uma vez, revogação imediata);
  - executions / destination_results / attempt_results — espelho do
    ExecutionResult do agente (success/partial/failed/interrupted e
    success/failed/interrupted preservados);
  - `last_seen_at` atualizado em horário do servidor a cada chamada
    autenticada; `received_at`/`started/finished_at_utc` separados;
  - migrações versionadas + transacionais + `pg_advisory_lock` —
    nunca apagam histórico;
  - admin Bearer de ambiente (sem default) × token de agente — auth
    separadas; identidade vem da credencial, corpo contraditório → 403;
  - `/healthz` + `/readyz` (SELECT 1); paginação + filtros cliente/
    agente/tarefa/status/período; limites de corpo e itens; timezone
    obrigatória;
  - idempotência: `UNIQUE(agent_id, execution_id)` +
    `ON CONFLICT DO NOTHING` + hash do payload → created / duplicate /
    409 conflict — corrida coberta por constraint, não por consulta.

### Homologação real desta sessão

PostgreSQL 17 (podman, banco exclusivo :55432) — **17/17 testes**:
cadastro/vínculos, admin+agente auth, token inválido e revogado
(imediato), cruzamento de clientes → 403, envio completo com
destinos+tentativas, 4 estados preservados, heartbeat + last_seen,
reenvio idêntico sem duplicação, conflito de conteúdo, **duas
requisições simultâneas com o mesmo execution_id → 1 linha**,
paginação/filtros/validações (timezone naive → 422, >32 destinos →
422), token ausente de logs/respostas e armazenado só como hash,
migração reaplicada sem perda de dados.

Suíte do agente (regressão): **150 testes — 132 aprovados, 0 falhos, 18 ignorados** — zero regressão.

### Pendente (não alegado)

- Sincronização/outbox do agente (incremento posterior);
- distribuição de tarefas, comandos remotos, portal, Windows;
- HTTPS: documentado como exigência fora de localhost — não há proxy
  configurado nesta etapa;
- piloto de 7 dias: segue em acompanhamento (não concluído).

---

## 2026-10-26 — Consolidação da API central (5 correções)

### 1. Limite de corpo por bytes efetivos

Middleware media só `Content-Length` → `int(header inválido)` quebrava
em 500 e envios chunked passavam ilimitados. Agora: header inválido →
400 JSON; corpo medido por `request.stream()` com corte ao ultrapassar
→ 413 JSON; corpo re-injetado no request. Testes: abaixo/exato/acima,
sem Content-Length, soma de blocos pequenos, header inválido,
declarado < real, rejeitado não grava.

### 2. Detalhe por UUID central

`GET /api/v1/executions/{id}` usa o `id` (UUID central da listagem) —
unicidade continua `(agent_id, execution_id)`. Dois agentes (clientes
diferentes) com mesmo `execution_id` consultáveis separadamente —
`scalar_one_or_none` no filtro ambíguo dava 500.

### 3. Validação completa

Bigint ≤ 2⁶³-1, int ≤ 2³¹-1, `duration_seconds` `allow_inf_nan=False`,
`finished ≥ started` em execução/destino/tentativa (422 antes de
gravar), filtros `since`/`until` exigem timezone e `since ≤ until`.
Nulos legítimos, contadores zero e destino interrompido sem tentativas
preservados — testados nos dois sentidos.

### 4. Migrações comprovadas

`schema_migrations` com versão futura/desconhecida → `MigrationError`
sem tocar no banco. Testes PG real: falha pós-DDL → rollback + versão
não registrada + histórico intacto + retry após unlock; inicialização
concorrente aplica uma vez e libera o advisory lock em sucesso e falha.

### 5. Execução e alegações

`conftest`: env ausente → integração pulada explicitamente; env
presente mas inacessível → `pytest.fail` claro; nome do banco deve
conter `test` (guarda de banco descartável). `test_schemas.py` roda
sempre (9 testes sem banco). Captura de logs (`caplog`) comprova
ausência de token admin, token de agente e `Bearer` em sucesso e erro.
`api[test]` declara pytest+httpx.

### Homologação real (PostgreSQL 17, banco exclusivo)

**36/36** testes passam com `MACIP_API_TEST_DATABASE_URL`; sem a env:
9 passam + 27 pulam explicitamente.

Regressão do agente: **150 testes — 132 aprovados, 0 falhos, 18
ignorados** (SFTP/SMB/S3/combinações — ambientes remotos).

### Pendente

Outbox/sincronização, distribuição de tarefas, portal — próximos
incrementos. Piloto de 7 dias em acompanhamento (término 02/11).

---

## 2026-10-26 — Correção pontual: 422 seguro + provas de migração

### 1. Serialização da resposta de validação

Reproduzido por HTTP: `duration_seconds` com `1e309`, `NaN`,
`Infinity`, `-Infinity` → `500` (o detalhe do erro carregava o valor
não finito → `json.dumps` falhava). Correção: handler próprio de
`RequestValidationError` — `jsonable_encoder` (serializa `ctx`) +
sanitização recursiva de floats não finitos → sempre `422` JSON válido,
sem vazar token/`Authorization`. Valores inválidos nunca são
convertidos em zero/nulo — são rejeitados.

`test_http_validation.py` (sem banco, deps substituídas): 4 literais,
não finito em campo inteiro, erros comuns, payload válido, sem 500.
`test_api.py` (PG real): 422 + **nada gravado**.

### 2. Provas de migração reforçadas

- Rollback com histórico POVOADO (cliente+agente+execução+destino+
  tentativa via API): DDL revertida, versão ausente, snapshot de todas
  as linhas byte a byte igual — não só contagem.
- Retentativa com a MESMA versão pendente e SQL válido: aplicada de
  fato (`mig_probe` existe), versão registrada uma vez, próxima
  execução sem pendências.
- Concorrência com migração realmente pendente em sessões
  independentes: aplicada uma vez, lock liberado — comprovado por
  `pg_locks` em conexão terceira (após sucesso e após falha).
- Cleanup autouse: versões de teste não contaminam o próximo start.

### Homologação real

PostgreSQL 17 exclusivo: **41/41**. Sem `MACIP_API_TEST_DATABASE_URL`:
13 passam (schemas + http) + 28 pulam explicitamente.

Agente/VM: código do agente intocado — regressão coberta pela suíte
anterior (150 testes verdes em 0f08607). Piloto em acompanhamento.

---

## 2026-10-26 — Outbox durável do agente + sync à API central

Branch `feature/agent-outbox` (base: 50eda22 em feature/control-api).
VM srvdev1 intocada — piloto em acompanhamento.

### Implementado

- `central.py` — CentralSpec (url/agent_id UUID/token_environment/
  timeouts); HTTPS fora de localhost; `token` no arquivo é recusado;
  client_id local da tarefa NÃO é a identidade central.
- `store.py` v3→v4 (migração transacional, histórico preservado):
  `outbox` (execution_id único, endpoint+agent_id vinculados ao
  enfileiramento, payload estável, status/attempts/next_attempt_at/
  last_error sanitizado). Enfileira NA MESMA transação de
  `record_finish`, `mark_interrupted` e `mark_stale_interrupted` —
  cobre success/partial/failed/interrupted e órfãs; `running` nunca.
- `sync.py` — `macip-backup sync --store X --central Y --limit N`:
  urllib stdlib (sem deps novas no agente), sem redirects, TLS
  verificado, nenhuma txn SQLite aberta durante HTTP, locks
  maintenance-SH + sync-EX. Classificação: 200/201 created/duplicate
  → sent; 401/403 → auth_blocked; 409/413/422 → failed_permanent;
  5xx/rede/timeout/inesperado → retry com backoff (30s→1h, 10 máx).
- `history --outbox` — fila consultável.
- `agent --central` — opt-in; sem a flag, comportamento idêntico.

### Testes (reais, não mockados)

`tests/test_outbox.py` — **14/14**: backup real com API fora →
pendente; reabertura preserva fila; envio OK; resposta perdida →
reenvio sem duplicar; 500→retry; sem token→skip; 403→auth_blocked sem
retry; 409/422→permanente; mudança de endpoint/agente não redireciona;
lock de manutenção; migração v3→v4; HTTPS e token-no-arquivo.

`tests/test_integration_outbox.py` — **1/1 REAL**: API uvicorn +
PostgreSQL 17 (podman, banco descartável) → provisiona via admin HTTP
→ backup local → sync → admin consulta destinos/tentativas → reenvio =
1 execução central.

### Pendente

Agendamento automático do sync (timer systemd), outbox para tarefas
legadas anteriores à v4, distribuição de tarefas — incrementos
posteriores. Piloto em acompanhamento (término 02/11).

---

## 2026-10-26 — Consolidação da outbox: locks do rearme + prova SIGKILL

### 1. outbox-rearm sob o mesmo protocolo do sync

O comando abria o SQLite sem locks. Agora `run_rearm` (sync.py)
adquire maintenance-SH + sync.lock EX ANTES de abrir o banco (migração
incluída), libera em sucesso e falha, e compartilha a mesma exclusão do
sync — sync e rearme nunca correm juntos. Backups continuam livres
(manutenção é SH).

Testes: manutenção ativa → recusa sem tocar no banco; sync ativo →
recusa; rearme ativo → sync recusado; liberação → funciona; falha ao
abrir o banco não prende lock; `sent` nunca é rearmado.

### 2. Prova SIGKILL (determinística, subprocesso)

`KillRecoveryTestCase`: servidor aceita o POST (marcador em disco) e
segura a resposta → o pai envia SIGKILL na janela → locks liberados
pelo kernel (probe EX imediato) → item segue `pending` → novo processo
reenvia → `duplicate` → `sent`, 2 POSTs, 1 execução.

### 3. Integração REAL agente → API → PostgreSQL

`test_integration_outbox` (uvicorn + PG17 podman :55432, banco
descartável): provisionamento admin → backup local → sync → consulta
com destinos/tentativas → reenvio = 1 execução central. **SIGKILL
contra a API real**: o pai segura a escrita SQLite (BEGIN IMMEDIATE)
→ o subprocesso POSTa e trava no `sent` → central confirma a execução
→ kill → item `pending` → reenvio → `duplicate`, uma única execução.
Distinguido do teste com servidor HTTP controlado.

### 4. Regressão

Suíte única: **186 — 168 aprovados, 0 falhos, 18 ignorados** (SFTP/SMB/S3/combinações — ambientes remotos). API: **42/42** com PostgreSQL real.

---
## Isolamento de filas de sync + recuperação automática do timer

### Problema

A exclusão de `sync`/`outbox-rearm` era um lock GLOBAL (`sync.lock`):
um único vínculo segurando HTTP bloqueava todas as outras filas —
rodadas coincidentes de B eram recusadas e ficavam permanentemente
pendentes.

### Correção

Identidade da fila = `(caminho canônico do banco, endpoint normalizado,
UUID do agente central)` → `locks/sync-<sha256>.lock` (sem credencial).
`run_sync` e `run_rearm` compartilham a mesma fila; filas distintas
progridem em paralelo. Manutenção SH preservada; locks antes de abrir
o SQLite; liberação garantida em sucesso, exceção e morte (flock).

### Provas

- `QueueConcurrencyTestCase` (HTTP controlado, ThreadingHTTPServer):
  A preso no POST → B conclui sem esperar; 3 rodadas disputadas sem
  fome; bancos distintos independentes; dois UUIDs no mesmo banco =
  filas distintas; dois `central.json` do mesmo vínculo = mesmo lock;
  sync×rearm da mesma fila se excluem.
- `test_sync_timer_outage_retry_e_recuperacao_automatica`
  (systemctl --user REAL): backup `--central` → pendente → timer
  dispara com API fora → tentativa+backoff gravados, resultado do
  backup intacto → API volta no MESMO endpoint → disparo posterior
  entrega ao ficar elegível → `sent`, 1 execução aceita. Sem sync
  manual e sem tocar SQLite; token só no EnvironmentFile, verificado
  pelo servidor.
- `test_timers_coincidentes_de_duas_filas_entregam_ambas`
  (systemd real): dois timers no mesmo calendário, duas filas,
  entregam juntas.

### Regressão

Suíte única: **202 testes — 182 aprovados, 0 falhos, 20 ignorados**
(backends remotos SFTP/SMB/S3 sem ambiente). API: **42/42** com
PostgreSQL 17 real (podman :55432 descartável). Integração agente →
API → PG: 2/2 (end-to-end + SIGKILL entre aceite e confirmação).

---
## Autenticação de operadores e sessões (API central)

Primeira etapa do portal: base de autenticação/autorização na API —
a interface web fica para um incremento posterior.

### Modelo

- `operators`: login normalizado (trim+lower) UNIQUE, nome, Argon2id,
  perfil `admin|viewer`, ativo, created/last_login — migração v2
  transacional preservando clientes/agentes/execuções.
- `operator_sessions`: id opaco renovado a cada login; só SHA-256 no
  banco; expires_at validada no servidor; revoked_at.
- `login_attempts`: falhas por login+IP em PostgreSQL — rate limit
  válido entre processos; sem confiança em X-Forwarded-*.

### Contrato

- `GET /auth/csrf` emite cookie pré-sessão; `POST /auth/login` exige
  par cookie+`X-CSRF-Token` (double-submit); `POST /auth/logout`
  revoga; `GET /auth/me` devolve o operador.
- Cookie `macip_session`: HttpOnly + SameSite=Lax + Secure (exceção
  local explícita via env). Token NUNCA em JSON — só o cookie.
- CRUD de operadores sob Bearer admin OU operador admin; leituras
  aceitam qualquer operador ativo; `viewer` → 403 em escrita.
- Desativar/redefinir senha revoga sessões; mudança de perfil já vale
  na próxima requisição. Bootstrap/recuperação local:
  `macip-backup-api-operator` (senha por getpass/stdin, nunca argv).
- Respostas de login genéricas + verify contra hash dummy — não vazam
  existência/desativação nem por corpo nem por tempo.
- Campos sensíveis mascarados no `input` dos 422; auditoria em logger
  dedicado sem segredos.

### Provas (PostgreSQL 17 real)

66/66: unicidade normalizada+concorrente, hash-only, login ok/errado/
inativo genérico, sessão expirada/revogada/logout, renovação de id,
desativação+reset revogando, perfil na próxima requisição, viewer×
admin, separação agente/operador/Bearer, CSRF login+escritas+logout,
rate limit por login e IP entre conexões, 422 sem eco de senha, logs
sem segredos, bootstrap via subprocesso. Migrações: v1 preenchido →
v2 preservando dados (mais rollback/concorrência/versão futura).
Integração agente→API inalterada: 2/2.

---
## Consolidação da autenticação de operadores

Três correções na feature/operator-auth:

1. **422 sem segredos**: mascaramento por `loc` não cobria erros na
   raiz — lista de login, lista de operador e string-senha nua
   devolviam o corpo inteiro. Agora: erro na raiz → `input` removido;
   erro em campo sensível → "***"; demais → scrub recursivo por chave
   sensível em qualquer profundidade. `loc`/`type` preservados.
   Provas HTTP sem banco com sentinela no corpo e nos logs.

2. **Rate limit atômico**: dois `pg_advisory_xact_lock`
   (login depois IP — ordem fixa) serializam contagem+registro por
   request; tentativas simultâneas do mesmo login/IP não ultrapassam
   o limite por leitura desatualizada. Provas reais: 7 logins/1 login
   × limite 3 → exatamente 3 gravados; 5 logins/1 IP → 3 gravados;
   janela expira e volta a contar.

3. **Corrida login×reset**: `SELECT ... FOR UPDATE` na linha do
   operador (HTTP e CLI local). Reset que vence revoga até a sessão
   criada por um login iniciado antes; login posterior lê hash novo.
   Reativação nunca ressuscita sessão revogada. Sem nova migração.

Suíte API: **77/77** com PostgreSQL 17 real; sem banco: 19 ok,
58 skips explícitos; integração agente→API: 2/2.

---
## Provas de corrida da autenticação (somente testes)

Os testes de corrida passaram a observar o lock NO PostgreSQL, não só
eventos de thread: `pg_stat_activity` + `pg_blocking_pids` confirmam o
backend adversário bloqueado pela transação pausada (`idle in
transaction`) antes de qualquer liberação.

- `test_reset_na_janela_...`: login pausado entre verify e
  create_session; reset API chega ao banco e espera (prova por PID);
  após liberar, a sessão recém-criada é revogada → me 401, senha velha
  401, senha nova 200.
- `test_login_espera_reset_...`: ordem inversa — reset trava a linha
  sem commitar (pausa em hash_password); o login aparece bloqueado no
  banco; depois do commit do reset o login velho é 401 sem sessão.
- `test_reset_cli_concorrente_...`: subprocesso real da CLI espera o
  lock do login (prova no banco) e revoga a sessão criada por ele.
- `test_desativacao_durante_...`: PATCH active=false bloqueia atrás do
  login; após liberar, a sessão do login morre e a reativação posterior
  não a ressuscita.

Verificação por mutação (temporária, não commitada): removido
`with_for_update()` do SELECT do login → os 4 testes falham em
"backend não chegou a esperar o lock" / sessão sobrevivendo; código
restaurado → 80/80.

Suíte API: **80/80** PostgreSQL 17 real; sem banco: 19 ok + 61 skips;
integração agente→API: 2/2. Nenhum arquivo de produção alterado.

---
## Portal operacional (React + TypeScript)

Primeira entrega do portal em `portal/` (Vite, pacote independente):
login/logout com cookie HttpOnly + CSRF double-submit, restauração e
revalidação de sessão, clientes e agentes, execuções com filtros em
URL (cliente/agente/tarefa/status/período) e paginação limit/offset
sem total inventado, detalhe por UUID central com destinos e
tentativas — null vira "—", erros como texto puro.

Verificação: `npm ci` limpo, `tsc` estrito, **24/24** testes vitest
(mockados: login ok/errado/429, CSRF ausente/rotação, restauração,
expiração, logout com revogação e falha visível, proteção de rota,
viewer, filtros/paginação/vazio/rede, detalhe com tentativas) e
**1/1 prova real** Playwright+Chrome contra API+PostgreSQL reais
(operador provisionado pelo endpoint admin, execução via Bearer de
agente, login→detalhe→logout revogado). Build 59 KiB gzip. Telas
conferidas em 1280px e 390px (tabela com rolagem horizontal).

Pendências: administração de operadores, distribuição de tarefas,
comandos remotos — etapas posteriores.

---
## Correções de corridas no portal

- `AuthProvider`: geração de operações — refresh/signIn/signOut
  incrementam o contador; sucesso e erro de respostas antigas são
  descartados (me() antigo não derruba login nem ressuscita sessão
  após logout; falha de rede descartada não vira "indisponível").
  Desmontar invalida pendências; falha de logout segue visível.
- `ExecutionDetailPage`/`ClientAgentsPage`: mesma geração + limpeza
  do conteúdo ao trocar de rota — dados ficam presos ao id atual,
  inclusive retry fora de ordem.
- Playwright: `webServer` só existe com `MACIP_API_TEST_DATABASE_URL`
  — sem a variável o teste skipa sem subir Python/Chrome/PG; com ela
  inválida, falha clara.

Testes novos com promises controladas (sem sleeps): 8 cenários —
mutação temporária dos guards fez os testes falharem; restaurado,
32/32. E2E: `1 skipped` sem env (nenhum servidor) e `1 passed` real.

---
## Prioridade de transição sobre revalidação (portal)

Restava uma corrida: refresh iniciado DURANTE login/logout
incrementava a geração e descartava o resultado da transição do
usuário. Regra final: transições (signIn/signOut) têm prioridade —
refresh pedido durante transição é suprimido (sem fetch, sem tocar a
geração); refresh já em voo fica obsoleto quando a transição começa.
Depois dela, refresh novo volta a detectar expiração real.
Testes com promises controladas para login/logout pendentes +
refresh durante/antes/depois; mutação sem a supressão falha 2
testes; restaurado, 36/36. E2E: skip limpo sem env + 1/1 real.

---
