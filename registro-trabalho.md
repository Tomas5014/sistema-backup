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

## Agente Windows reutilizável (piloto) — feature/windows-pilot-agent

Base: `e448894` (main). `agent/windows/Backup-MacipCliente.ps1` — um
script, dois modos, PS 5.1. `backup.json` carrega origem/exclusoes/
repositorio/horario (operador) + repositorio_id/identidade_ssh/
known_hosts (MAC IP). `Preparar` valida config/executáveis/acesso na
conta da tarefa, protege a senha em DPAPI, confere o ID do repositório
existente (nunca `init`) e imprime os campos do Agendador. `Backup`
roda sem prompts: revalida tudo, confirma identidade do destino ANTES
do backup (ssh_config restrito: BatchMode, chave exclusiva, host key
estrita), senha só no processo, `--exclude` por padrão, sucesso só com
rc=0 + snapshot válido; rc=3 retorna erro. `ultimo-resultado.json` com
início/fim UTC, conta, estado, código e snapshot.

Suíte `agent/windows/tests/run-windows-agent-tests.ps1`: 73/73 em
pwsh 7.6.9 (Linux) — validação, exclusões vazia/preenchida/acentos,
destino divergente recusado sem chamar `backup`, rc 0/1/3, sigilo da
senha, duas configs com o mesmo script, campos do Agendador, recusas
de contexto. Ponte unittest: `tests/test_windows_agent_ps1.py`
(pula sem pwsh). Limite da prova: fora do Windows só a fronteira DPAPI
é simulada — DPAPI real + Agendador com sessão encerrada + SFTP ficam
para a VM Windows descartável (pendente). `restic.exe`, chaves, senhas
e `backup.json` real ficam fora do versionamento (.gitignore próprio).

## Agente Windows — correções de endereço, preparo e vínculo (review 2)

Sobre `d9e6968`, mesma branch. Achados corrigidos:

- **SFTP:** regex rejeitava a forma canônica `sftp:u@h:/caminho` (até o
  exemplo). Agora aceita `sftp:usuario@host:/caminho` (porta 22) e
  `sftp://usuario@host:porta//caminho`; a ambígua `:porta/caminho` é
  recusada (Restic lê como caminho). `ssh_config` ganhou `User` e
  `Port` — usuário SFTP explícito no comando. Prova com **Restic
  0.18.1 real** + ssh registrador: host e `-s sftp` chegam na invocação
  nas duas formas.
- **PS 5.1:** script agora 100% ASCII (travessão removido de string);
  `GetDirectoryName` no lugar de `Split-Path -Parent`; suíte salva em
  UTF-8 com BOM.
- **Preparo:** recusa pasta de estado existente (byte a byte
  preservada), só marca concluído após conferir o ID, e em erro remove
  apenas os artefatos da tentativa atual. `Backup` reconfere
  repositório, ID e SHA-256 de chave/known_hosts contra o
  `preparo.json`; divergência recusa antes do backup e orienta preparo
  em outra pasta — sem reuso silencioso de ssh_config.
- **Validação:** `origem` exige unidade local no Windows (UNC e volume
  de rede recusados); `exclusoes` exige lista no JSON bruto (texto
  isolado e null recusados; `[]` e `[x]` válidos). JSON inválido após
  sucesso grava falha nova apenas se houver preparo verificável; sem
  ele, erro ao Agendador sem criar pasta.
- **Roteiro:** README sem `-ExecutionPolicy Bypass`, `schtasks` sem
  `/F`, troca de horário/destino documentada (destino novo = pasta de
  estado nova, validada antes de apontar a tarefa).

Suíte: **118/118** em pwsh (com `MACIP_RESTIC_REAL`). Prova real de
DPAPI/Agendador/SFTP continua pendente de VM Windows descartável.

## Agente Windows — vínculo SSH efetivo e exclusoes no objeto (review 3)

Sobre `5136b5b`, mesma branch.

- **Vínculo SSH efetivo:** `preparo.json` agora registra caminhos
  resolvidos + SHA-256 de identidade e known_hosts **e** o SHA-256 do
  `ssh_config` gerado. `Backup` confere tudo antes de chamar o Restic:
  cópia com bytes idênticos em outro caminho é recusada (o SSH lê o
  caminho do preparo), `ssh_config` adulterado/ausente é recusado e
  preparo legado sem os novos campos é recusado — nada de regravar
  ssh_config fora do preparo.
- **exclusoes:** validação sobre o objeto interpretado
  (`-is [object[]]`), sem regex no JSON bruto; chave duplicada usa o
  último valor como o ConvertFrom-Json — documentado no código e no
  README. `[]`, `[x]`, espaços e acentos preservados.

Suíte: **129/129** em pwsh, incluindo duplicata, cópia idêntica,
ssh_config adulterado/ausente, preparo legado e mutação válida de
exclusões/horário — todos com prova de que o Restic não foi chamado.
Prova com Restic 0.18.1 real + ssh registrador mantida verde nas duas
formas de endereço. VM Windows continua pendente.

## Agente Windows — evidência de laboratório e roteiro do piloto

Sobre `5a2f3f8`, mesma branch. Registro de prova real + documento do
piloto (`docs/piloto-windows-sftp.md`); nenhuma linha de agente/API/
portal alterada.

**Homologação de laboratório (11/11/2026 14h22, Brasília):** Agendador
disparou o agente `5a2f3f8` em Windows PowerShell 5.1 com a sessão
encerrada — rc `0` no Agendador e no Restic, `status: sucesso`.
Snapshot `f0d283ee…3aa823a`; `restic check --read-data` sem erros no
SFTP; restauração independente da VM com `amostra.txt` (43 B, SHA-256
`18c4b63c…fc56`); origem com espaço/acento; `ignorar.tmp` e
`cache temporário.log` excluídos conforme configurado. Classificado
como laboratório — não é aceite de cliente nem produção.

Roteiro do piloto: ficha (autorização, pasta, exclusões, horário,
janela, retenção, alertas, meta), preparação MAC IP (repo exclusivo,
chave do servidor conferida, senha custodiada, **cópia protegida** com
prova fictícia de separação e restauração antes de dados reais —
pendência se a infra não oferecer), instalação (kit do SHA, Preparar na
conta de lote, tarefa nova, disparo único com sessão encerrada antes do
diário), acompanhamento de 7 disparos com alertas de falha e de
ausência (integração Windows-portal = **pendência**, verificação manual
na ficha), aceite exigindo os 7 disparos + 2 alertas + restaurações
(ponto recente e anterior, uma delas da cópia protegida) e interrupção
preservando repositório/logs/snapshots. Piloto **não iniciado**.

---

## 2026-11-12 — Correções do roteiro do piloto Windows e provas pendentes

Entrega **documental** sobre `968a989` (branch
`feature/windows-pilot-agent`), sem alterar agente Windows/Linux, API
ou portal. Três correções no `docs/piloto-windows-sftp.md`:

1. **Identidade do pacote:** o ZIP instalado na prova foi
   `macip-agente-windows-5a2f3f8.zip` (SHA-256 `4d9489626e…945be0`), não
   o ZIP de fontes `agente-windows-5a2f3f882fb1.zip` — referências
   corrigidas na evidência e na instalação; a verificação agora
   **aborta** se o arquivo estiver ausente ou o hash divergir.
2. **Disparo real sem sessão:** substituídos `schtasks /Run` +
   `/Change /SC DAILY` pelo fluxo validado na interface do Agendador —
   mesma conta do `Preparar`, "executar estando o usuário conectado ou
   não", argumentos conferidos sem prefixo de lista (`1. `), disparador
   **único** no futuro, `NextRunTime` conferido, sessão encerrada,
   `LastRunTime`/`LastTaskResult`/`ultimo-resultado.json` novo
   correlacionados, e só depois o disparador **diário** na interface.
   Senha apenas na janela nativa; falta de logon em lote vai para o
   administrador.
3. **Restauração na MAC IP:** procedimento executável com variáveis e
   pré-condições — conferir o ID do repositório, senha pelo cofre via
   `RESTIC_PASSWORD_COMMAND` (nunca impressa nem em argumentos),
   `check --read-data`, `snapshots`/`ls` para localizar o snapshot e a
   subpasta da origem, restore em diretório novo, manifesto SHA-256 e
   verificação das exclusões.

**Duas provas pendentes escritas para execução com dados fictícios:**
seção 6 — **cópia protegida** (preflight de capacidade/isolamento,
cópia consistente com `check --read-data`, sentinela descartável que a
credencial do cliente não pode alterar/apagar, restauração só pela
cópia sem primário nem Windows; append-only no primário não conta e
isolamento no mesmo disco não prova sobrevivência; decisão de
armazenamento pendente com critérios, sem presumir tecnologia). Seção 7
— **alertas** (execução com falha/incompleta e ausência de resultado
novo ao fim da janela: responsável, prazo, correlação
`inicio_utc`×`LastRunTime`, evidência de recebimento; resultado antigo
ou ausente não prova que a tarefa nunca rodou; conferência manual como
medida provisória — integração Windows→portal segue **não
comprovada**).

README do agente passou a citar o pacote efetivamente instalado e a
evidência como relatada pelo operador. Piloto continua **não iniciado**:
depende de autorização registrada + prova da cópia protegida aprovada.
Verificações: comandos conferidos contra `-Modo/-Config/-DiretorioEstado/
-ResticExe`; exemplos JSON/hash sem segredos; `git diff --check` limpo.

---

## 11/11/2026 — Homologação Windows: cópia protegida e avisos operacionais

Evidências de laboratório **relatadas pelo operador** (horários de
Brasília). Estes resultados vêm do relato — não foram reexecutados por
esta sessão. O piloto com dados reais **não começou**.

**Restauração pela cópia:** o snapshot sintético `f0d283ee…` foi copiado
com `restic copy` para um repositório novo e transferido para o PC
Linux de desenvolvimento. O pacote passou na conferência SHA-256 e
`restic check --read-data` não encontrou erros na cópia. O snapshot de
destino
`91f1a49ce0dca9408bad49a555fa94f96a6f54c58582bbe50b4dfafe8c443e3e` foi
restaurado usando somente a cópia local, sem depender do repositório
primário nem da VM Windows. `amostra.txt` bateu com o SHA-256 esperado
e os arquivos excluídos não apareceram.

**Separação de acesso:** no `srvdev1`, a conta `macipsftpwinlab`
tentou alterar e apagar uma sentinela descartável no diretório privado
de exportação. As duas operações foram negadas e o SHA-256 da sentinela
ficou intacto. Essa prova cobre as permissões da exportação **no
laboratório** — o armazenamento definitivo para dados de clientes ainda
precisa ser escolhido e testado.

**Avisos operacionais — conferência manual dos dois cenários:**

- **15h39 — Falha registrada:** o Agendador disparou a tarefa
  `MACIP-Restic-WinLab-Alertas` e retornou `1`. O JSON novo registrou
  `status: falha`, `codigo_restic: null`, `snapshot: null` — a origem
  fictícia estava indisponível de propósito e o agente registrou a
  falha antes de chamar o Restic.
- **15h59 — Sem resultado novo na janela:** disparo previsto para
  15h57, tolerância de 2 minutos. Ao fim da janela, o disparador
  continuava desabilitado, a última execução seguia a das 15h39 e o
  SHA-256 do `ultimo-resultado.json` permaneceu inalterado — caso
  classificado como **"sem resultado novo na janela"**.

Os dois cenários foram conferidos **manualmente**. Não há evidência de
confirmação de recebimento por outro responsável nem de envio
automático de alertas — a integração Windows → portal segue não
comprovada.

**Continuam pendentes:** autorização registrada do cliente e validação
da cópia protegida no armazenamento definitivo.

---

## 2026-11-12 — MAC IP Backup com interface grafica (piloto Windows)

Branch `feature/windows-backup-gui-pilot` sobre `main` (`8e690d0`,
linha Windows `45b45dc` ja integrada). **Sem alteracao no agente**: a
GUI (`agent/windows/gui/MacipBackup-Gui.ps1`, WinForms/PS 5.1,
somente-ASCII) dot-sourcing do `Backup-MacipCliente.ps1` reusa toda a
validacao homologada — `Read-AgenteConfig`, `Write-SshConfig`,
`Confirm-Destino`, `Invoke-Restic`, `Invoke-AgentePreparar`.

Fluxo sem terminal nem Agendador: **Ativar** (perfil `.json` da MAC IP
como dados — campos de segredo recusados; chave e senha Restic
separadas; *Testar conexao* prova identidade do servidor + ID do
repositorio em pasta temporaria sem gravar estado) -> **Configurar**
(seletor de pasta, exclusoes, horario; `backup.json` atomico que
preserva a config anterior em recusa; tarefa `MACIP-Backup` criada ou
atualizada via Schedule.Service com `TASK_LOGON_PASSWORD` e senha na
janela nativa — nunca duplica) -> **Operar** (pasta, proxima execucao,
resultado classificado — resultado antigo nao conta como execucao nova;
Executar agora na mesma tarefa/conta, pausa/retomada, bloqueio de
execucao dupla) -> **Restaurar** (ponto + arquivo em pasta separada
vazia; original intocado). Estado em `%ProgramData%\MacipBackup` sem
espacos; pastas de estado `estado-N` novas a cada preparo (recusa de
sobrescrita do agente preservada).

Instalador: `installer/macip-backup.nsi` + `BUILD.md`
(`makensis /DVERSAO=…` no Windows gera `MacipBackupSetup-<versao>.exe`;
checa OpenSSH, copia agente+GUI+restic aprovado; sem segredos no
pacote). `perfil.example.json` ficticio entregue.

Testes: `run-gui-tests.ps1` **32/32** em pwsh 7.4.6 (perfil e recusas,
atomica da config, classificacao de resultado, alvo de restauracao,
argumentos da tarefa; COM pulado fora do Windows). Agente sem
regressao: **119/119** (Restic real skip sem MACIP_RESTIC_REAL).
`unittest` 4/4. **Pendente (bloqueio para homologacao):** VM Windows —
fluxo completo na interface com sessao encerrada e reinicio, DPAPI
real, SFTP real e restauracao; registro/execucao da tarefa via COM e
PromptForCredential so testados estaticamente.

## 2026-11-12 — GUI do piloto: correcao de agendamento, salvamento atomico, instalador e restauracao

Branch `feature/windows-backup-gui-pilot`. Quatro defeitos revisados da
primeira versao da interface, todos corrigidos com regressao:

1. **Agendamento:** `TASK_LOGON_PASSWORD = 1` (PASSWORD, nao o 2/S4U) na
   definicao (`Principal.LogonType`) e no registro COM — a tarefa roda na
   mesma conta da sessao em que o DPAPI protege a senha Restic.
   Credencial Windows pedida so na janela nativa `PromptForCredential`,
   nunca gravada em arquivo/log/argumento. Tarefa so e alterada com a
   marca do aplicativo na descricao; homonima estranha e recusada.
   Executar agora dispara a mesma tarefa/conta e recusa conta divergente
   do preparo e execucao sobreposta (`State=RUNNING` +
   `MultipleInstances=IGNORE_NEW`).
2. **Salvamento em duas fases (rollback):** `Build-Configuracao` monta
   conjunto novo `config-N` (+`estado-N` so quando o perfil muda —
   mesmo destino reusa o DPAPI anterior) sem tocar no ativo;
   `Publish-Configuracao` registra a tarefa e so entao publica
   `gui.json`. Cancelamento da credencial, falha de preparo ou erro no
   COM removem o conjunto novo; falha apos o registro repoe o XML
   anterior da tarefa. Config+estado+tarefa anteriores continuam juntos
   e funcionais. `Get-ConfigAtual`/`Get-EstadoAtual` leem o que a TAREFA
   usa (a meta nao mente).
3. **Instalador NSIS:** `$PROGRAMDATA` (inexistente no NSIS) substituido
   por `SetShellVarContext all` — `$APPDATA` = `%ProgramData%`, mesmo
   caminho da GUI, e atalho no Menu Iniciar comum (visivel a conta
   dedicada). Desinstalacao desabilita a tarefa ANTES de remover
   executaveis; sem confirmacao de desativacao, informa a falha e
   preserva os arquivos. Dados/estado sempre preservados.
   **Nao compilado:** sem `makensis` nem `restic.exe` aprovado neste
   ambiente — build no Windows documentado em `installer/BUILD.md`.
4. **Restauracao:** `restic ls --json` filtrado a `type == 'file'`
   (dirs/symlinks fora), caminho exato do snapshot em ComboBox fechado
   (sem texto livre); `restic dump` do arquivo escolhido — nome com
   `[1]`/`*` e literal, nao arrasta outros arquivos. Destino novo ou
   vazio, fora da arvore protegida, equivalencia por link/junction
   recusada; destino e ID do repositorio conferidos antes de listar e
   restaurar; sucesso so quando o arquivo esperado existe no destino.

Testes: `run-gui-tests.ps1` **55/55** em pwsh 7.4.6 — inclui falha
simulada de preparo, cancelamento da credencial, erro no COM, conta e
`LogonType` da def real, restauracao real com SHA-256 conferido e caso
sem arquivo. Agente sem regressao **119/119**; `unittest` **5/5**
(contrato do instalador incluido). **Pendente (bloqueio para
homologacao):** VM Windows — instalador compilado, COM/DPAPI/
PromptForCredential reais, SFTP real, sessao encerrada, reinicio e
restauracao ponta a ponta.

## 2026-11-13 — GUI do piloto: SID na tarefa, limpeza unica, desinstalacao e restauracao seguras

Branch `feature/windows-backup-gui-pilot` sobre `de1ec98`. Segunda rodada
de quatro correcoes, cada uma com regressao no simulador:

1. **Conta da tarefa por SID:** `Definition.Principal.UserId` (formato
   real do COM — o backend falso passa a replica-lo) resolvido para SID
   e comparado com o `sid` do `preparo.json` do estado que a TAREFA
   referencia; identidade ilegivel ou SID divergente recusa o disparo —
   mesmo nome em dominio diferente nao equivale. Reuso de estado para
   mudar pasta/exclusoes/horario exige SID da sessao == SID do preparo.
   Teste positivo agora executa `Start-TarefaAgora` de verdade e confere
   `Run` chamado exatamente uma vez; nos recusados, zero chamadas.
2. **Limpeza com dono unico:** `Publish-Configuracao` marca `Publicado`
   — o tratador da interface nao repete a remocao. `Remove-ConjuntoNovo`
   so apaga quando o Agendador confirma que nenhuma tarefa/execucao
   referencia o conjunto; na duvida, preserva e avisa. Falha de meta
   apos o COM: reposicao confirmada -> conjunto novo removido e anterior
   valendo; sem reposicao (1o cadastro ou falha nela) -> tarefa segue
   apontando para arquivos preservados e funcionais, nada e apagado sob
   tarefa viva.
3. **Desinstalacao:** consulta `MACIP-Backup` via `/XML` e separa
   ausencia confirmada de erro de acesso; propriedade verificada pela
   marca `MACIP Backup` no XML (homonima estranha intocada). Para a
   nossa: `/End` + `/DISABLE` conferido e segunda consulta barra remocao
   com execucao ativa — sem confirmacao, preserva os executaveis.
   Config, credenciais DPAPI, resultados e repositorio preservados.
4. **Restauracao:** caminho efetivo resolvido em TODOS os componentes
   (link/junction ate em pai de pasta final inexistente); cadeia longa
   demais ou irrisolvivel recusa. Criacao exclusiva `CreateNew` —
   colisao nunca sobrescreve; falha no dump remove so o parcial criado;
   sucesso so com codigo 0 + arquivo esperado no destino.

Testes: `run-gui-tests.ps1` **82/82** em pwsh 7.4.6 — SID/dominio,
`Run` exatamente-uma-vez/zero-vez, tres casos de falha de publicacao,
SID divergente no reuso, pai-link com pasta nova, colisao, arquivo
ausente e binario `a[1].txt` com SHA-256. Agente sem regressao
**119/119**; `unittest` **5/5**. **Restic REAL 0.18.1** (repo local):
`ls --json` confirmou o schema (registro snapshot + nos `type:file`),
`dump` restaurou `a[1].txt` e binario com SHA-256 batendo e recusou
caminho inexato — o simulador nao substitui essa prova, ela existe.
**Pendente (bloqueio):** VM Windows — COM/DPAPI/PromptForCredential
reais, instalador compilado (sem makensis/restic.exe aprovado aqui),
sessao encerrada, reinicio, cancelamento e desinstalacao na interface.

## Revisao 4 (branch feature/windows-backup-gui-pilot)

1. **Consulta COM estruturada:** `New-BackendCom.Get` nao engole mais
   erro — `Get-TarefaInfo` classifica encontrada / ausente CONFIRMADA
   (HRESULT -2147024894 ou "nao encontrado") / erro inconclusivo.
   Cadastro novo exige ausencia confirmada; atualizacao exige a marca;
   inconclusivo recusa registrar, pausar e executar agora.
   `Test-ConjuntoEmUso` trata consulta negada como "em uso" = preserva.
2. **Limpeza pos-salvar:** consulta negada logo apos registrar ->
   conjunto novo preservado com aviso; reposicao + consulta ambas
   inconclusivas -> nada removido. Um so dono da limpeza, confirmacao
   no Agendador antes de apagar qualquer conjunto.
3. **Desinstalacao estruturada:** `installer/Desinstalar-Tarefa.ps1`
   (novo, empacotado) usa `Schedule.Service` — propriedade por marca em
   `RegistrationInfo.Description` + acao apontando para $INSTDIR;
   desabilita primeiro (`Enabled=false`), encerra instancias com
   `Stop()`, confirma estado final. Codigos: 0 ok / 2 ausente / 3
   homonima alheia (intocada, aborta) / 4 inconclusivo (preserva
   executaveis) / 5 instancia ativa-pendente (preserva). O `.nsi` nao
   usa mais `${StrContains}`/`StrFunc.nsh`/`schtasks`/`findstr` —
   decisao pelo codigo de saida do script.
4. **Restauracao anti-travamento:** `Invoke-ResticDump` grava em
   temporario exclusivo `.~nome.part`; `stdout` via `CopyToAsync` +
   `stderr` via `ReadToEndAsync` EM PARALELO (pipe cheio nao trava);
   `WaitForExit(DumpTimeoutMs)` com `Kill` no estouro; publica com
   `File.Move` (nao sobrescreve) somente apos codigo 0; falha remove
   apenas o parcial da tentativa.

Testes: `run-gui-tests.ps1` **109/109** — inclui consulta negada
pos-registro, reposicao+consulta inconclusivas, 10 cenarios do script
de desinstalacao com svc falso, falha apos bytes, stderr grande sem
travar, colisao na publicacao com original intacto e timeout com Kill.
Agente **119/119**; `unittest` **5/5**; `git diff --check` limpo.
Restic real 0.18.1 (Linux): prova anterior de `ls --json`/`dump`/
SHA-256 mantida. **Pendente:** mesma lista — VM Windows limpa, build
NSIS com restic.exe aprovado, prova real de COM/DPAPI/junction;
instalador segue nao compilado neste ambiente.

## Revisao 5 (branch feature/windows-backup-gui-pilot)

1. **Agendador preciso:** `Get-TarefaInfo` (GUI e `Desinstalar-Tarefa.ps1`)
   separa falha de conexao/pasta da consulta; 'ausente' SOMENTE quando o
   proprio `GetTask` devolve 0x80070002 — cadeia de InnerException cobre
   o empacotamento COM do PowerShell; texto de excecao e retorno vazio
   nunca provam ausencia (retorno inesperado = inconclusivo). Registro
   separa TASK_CREATE(2) — falha se homonima surgir entre consulta e
   cadastro — de TASK_UPDATE(4); rollback usa UPDATE e so repoe se a
   tarefa atual for confirmadamente a def publicada pela tentativa
   (propriedade + argumentos conferidos); sem confirmacao, preserva.
   Limpeza exige sem referencia E sem instancia ativa/pendente
   (`GetInstances`).
2. **Propriedade por campos:** regra compartilhada GUI/desinstalador —
   descricao EXATA do app, UMA acao, executavel powershell.exe e
   argumento `-File` igual ao caminho absoluto NORMALIZADO de
   `Backup-MacipCliente.ps1` da instalacao; prefixo parecido
   (`MACIP-Backup2`), caminho em outro argumento e acoes ambiguas sao
   recusados. Desinstalacao: desabilita primeiro, para instancias,
   RE-consulta e so libera com Enabled=false + contagem COM de
   instancias zero; falta de visibilidade = inconclusivo, preserva.
   Remocao de arquivos por nome, sem curinga.
3. **Publicacao do arquivo:** dump continua com stdout/stderr em
   paralelo; o temporario exclusivo so vira o nome final depois de
   codigo 0 + copia de stdout concluida sem erro + fechamento ok.
   Falha de escrita (mesmo apos bytes gravados) = FALHA mesmo com
   restic 0. Prazo cobre processo E leituras; qualquer falha fecha
   recursos e remove so o parcial da tentativa.

Regressoes novas: texto "not found" com HRESULT errado -> inconclusivo
(recusado); homonima entre consulta e cadastro -> CREATE falha e ela
fica intacta; -File em prefixo parecido e caminho so em outro
argumento -> recusados; def trocada apos registro -> sem reposicao e
arquivos preservados; instancia pendente bloqueia a limpeza; falha de
escrita com restic 0 -> nada publicado; desinstalacao: re-consulta
falha, Enabled persiste true e consulta de instancias negada ->
preservar. `run-gui-tests.ps1` **130/130**; agente **119/119**;
`unittest` **5/5**; `git diff --check` limpo; arquivos ps1 ASCII.
Pendente sem mudanca: VM Windows limpa, build NSIS com restic.exe
aprovado e prova real de COM/DPAPI/junction — instalador NAO compilado.

## Revisao 6 (branch feature/windows-backup-gui-pilot)

1. **Acao validada de ponta a ponta:** a regra de propriedade (GUI
   `Test-TarefaDoApp` e `Desinstalar-Tarefa.ps1` `Test-Propriedade`)
   deixou o sufixo `*powershell.exe` e a busca solta por `-File`. Agora
   exige: descricao EXATA do app; UMA unica acao de execucao;
   `Action.Path` IGUAL ao caminho absoluto esperado
   (`%WINDIR%\System32\WindowsPowerShell\v1.0\powershell.exe`); e
   argumentos casando a estrutura ANCORADA gerada pelo app
   (`-NoProfile -NonInteractive -File "<agente>" -Modo Backup -Config
   "<c>" -DiretorioEstado "<e>"`), com o caminho do `-File` conferido
   apos normalizacao. `C:\Temp\outropowershell.exe`, `powershell.exe`
   fora do lugar ou relativo, `-File` dentro de `-Command`, prefixo
   parecido e tokens extras sao recusados. A mesma regra vale em
   atualizar/pausar/executar/repor na GUI. Na desinstalacao, a
   RE-consulta final revalida a propriedade na definicao recem-lida —
   tarefa trocada no meio do caminho = inconclusivo: tarefa nova e
   executaveis preservados.
2. **Prazo monotonico ate o fim:** `Invoke-ResticDump` usa UM deadline
   em `Stopwatch` compartilhado pelo processo e pelas duas leituras
   (stdout via `CopyToAsync`, stderr via `ReadToEndAsync`). Cada espera
   recebe so o tempo restante e o retorno e conferido; `.Result` so e
   lido de tarefa concluida — stderr preso por um neto apos codigo 0
   nao bloqueia de novo. Timeout fecha recursos sem nova espera
   ilimitada, remove so o parcial da tentativa e reporta falha sem
   publicar.

Regressoes novas: acao legitima com `Program Files` passa;
`outropowershell.exe`, `powershell.exe` fora do caminho e relativo,
`-File` dentro de `-Command` -> recusados; troca por tarefa alheia
entre a 1a e a ultima consulta da desinstalacao -> codigo 4, tarefa
alheia nunca parada/desabilitada, executaveis preservados; subprocesso
que segura stderr aberto apos retornar 0 e fechar stdout -> FALHA
dentro do limite (2s, medido <20s), sem arquivo final e com
pre-existente intacto. `run-gui-tests.ps1` **145/145** (1 pulado:
COM real fora do Windows); agente **119/119**; `unittest` **5/5**
(2 pulados: pwsh fora do PATH do harness); `git diff --check` limpo;
ps1 ASCII puro. Pendente sem mudanca: VM Windows limpa, `makensis`,
`restic.exe` aprovado — instalador NAO compilado neste ambiente;
COM/DPAPI/junction sem prova real.

## Revisao 7 (branch feature/windows-backup-gui-pilot)

1. **Resultado certo na tela:** `Classify-Resultado` reescrita — "concluido"
   EXIGE resultado valido VINCULADO ao disparo (inicio_utc >= LastRunTime,
   tolerancia so de 1s; o sucesso das 14:00 nao e mais resultado do
   disparo das 14:00:50), com codigo 0 e snapshot de 64 hex. Novos
   estados: concluido/falhou/incompleto/em_execucao/sem_resultado (o
   Agendador tentou, nada gravado — codigo dele prova falha de
   inicializacao)/ainda_nao. Aba Operar mostra situacao deste disparo +
   "Ultimo backup confirmado" (data/hora proprias) + proxima execucao +
   acao necessaria. PAUSA preservada ao salvar: update nao reativa.
2. **Restauracao com a mesma verificacao do backup:**
   `Confirm-PreparoVigente` confere SID da sessao, repositorio+ID e
   VINCULO EFETIVO de identidade_ssh/known_hosts/ssh_config (caminho +
   SHA-256 do preparo.json) antes de listar pontos, listar arquivos e
   restaurar; divergencia bloqueia orientando refazer a ativacao. Falha
   na listagem descarta a selecao. Resolve-LinkPath recusa componente
   ilegivel; dois links encadeados cobertos.
3. **Janela responsiva, menos passos:** "Testar conexao + Ativar" virou
   "Validar e continuar" — avanca so com servidor e ID confirmados;
   token de ativacao (perfil+chave+senha) expira se qualquer um mudar.
   Rede fora da thread da janela: `Invoke-Assincrono` roda a funcao em
   runspace separado com DoEvents + prazo; consultas pre-dump usam
   `Invoke-ResticConsulta` (prazo monotonico + Kill). Erro de registro
   sem permissao orienta acionar o administrador; cadastro nao e
   apresentado como prova do agendamento.
4. **Instalacao fixa:** sem MUI_PAGE_DIRECTORY; `.onInit` aborta `/D=` ou
   pasta alternativa — piloto so instala em %ProgramFiles%\MACIP-Backup
   (ACL do Windows protege de usuarios comuns). Registro em Aplicativos
   instalados (HKLM Uninstall) + DeleteRegKey na remocao. Protecoes da
   desinstalacao e preservacao de dados mantidas.

Regressoes novas: sucesso das 14:00 com disparo 14:00:50 ->
sem_resultado; resultado do proprio disparo -> concluido; sucesso sem
snapshot valido -> falhou; disparo sem resultado -> sem_resultado;
codigo do Agendador != 0 -> falhou; salvar com pausada -> pausa
preservada; known_hosts/ssh_config/identidade alterados -> recusado;
dois links encadeados -> recusado; "cat" que nao responde -> falha
<15s com Kill. `run-gui-tests.ps1` **160/160** (1 pulado); agente
**119/119**; `unittest` **5/5**; `git diff --check` limpo; ASCII.
Pendente sem mudanca: VM Windows, makensis, restic.exe aprovado —
instalador NAO compilado; COM/DPAPI/agendamento sem prova real.

## Revisao 8 (branch feature/windows-backup-gui-pilot)

1. **Todas as listagens preservadas:** `Invoke-Assincrono` devolvia so
   `$out[0]` — a aba Restaurar perdia pontos e arquivos a partir do
   segundo item. Agora devolve o objeto unico, `$null` no vazio ou
   TODOS os itens (0/1/N preservados). Regressao cobre o caminho que
   alimenta a tela: dois pontos listados, segundo ponto escolhido,
   dois arquivos listados nele, segundo arquivo restaurado com SHA-256
   conferido. Correcao correlata: o runspace dot-source o arquivo de
   novo e perdia os overrides injetaveis da sessao (`ResticExeNome`,
   `SshExe`, prazos) — agora eles viajam para o trabalhador.
2. **Validacao vinculada aos dados testados:** `Get-TokenAtivacao`
   captura perfil (JSON) + caminho e SHA-256 da chave + senha (so no
   hash — nunca em argumento, mensagem ou log) no instante do clique.
   `OpRede` trava as entradas e ignora cliques repetidos durante a
   tentativa; ao fim, o token so e aceito se os dados continuam os
   capturados — tentativa antiga nunca libera configuracao nova.
   `Test-AtivacaoVigente` e conferida ANTES e DEPOIS do build ao
   salvar. Prazo aplicado direto as consultas Restic: ativacao usa
   `Test-ConexaoDestino` -> `Confirm-DestinoComPrazo` e o preparo ao
   salvar recebe o mesmo `Confirm-DestinoComPrazo` por scriptblock
   injetado (`Invoke-AgentePreparar -ConfirmaDestino`; o uso por
   script segue com `Confirm-Destino` padrao). Falha preserva
   configuracao e tarefa anteriores e reabilita os controles
   (finally). Regressoes: senha/perfil/chave trocados -> prova
   expirada; sem validacao -> salvar recusado; SSH que aceita mas nao
   responde SFTP -> falha dentro do prazo na ativacao E no preparo,
   sem travar a janela.
3. **Ultimo backup confirmado separado:** `ultimo-confirmado.json` e
   evidencia propria, independente de `ultimo-resultado.json`
   (tentativa mais recente). So grava com codigo 0 + snapshot de 64
   hex + `fim_utc` valido e mais recente que o guardado; falha,
   incompleto ou ausencia de resultado posteriores nao apagam —
   persistido em arquivo, sobrevive a reabertura. `repositorio_id`
   vinculado: destino novo nao herda confirmado do anterior.
   Regressoes: falha posterior preserva; sucesso mais antigo nao
   regride a data; repositorio divergente nao herda.
4. **UninstallString com aspas:** `macip-backup.nsi` grava
   `UninstallString` como `"$INSTDIR\desinstalar.exe"` (aspas literais
   no valor) — `Program Files` tem espaco e o Windows executa o valor
   como linha de comando. Contrato estatico Python passou a exigir a
   forma com aspas. Protecoes mantidas: propriedade revalidada na
   definicao recem-lida, desabilitada + sem instancias ativas ou
   pendentes ou preserva executaveis e registro; ProgramData
   (configuracao, DPAPI, resultados) e repositorio intactos.

Verificacao: `run-gui-tests.ps1` **179 ok, 0 falha** (1 pulado: COM
real fora do Windows) — pwsh 7.4.6, simulado; agente **119/119**;
`unittest` **5/5** (2 pulados: pwsh fora do PATH do harness — suites
rodadas direto); `git diff --check` limpo; ps1 ASCII puro.
Separacao de prova: tudo acima e SIMULADO em pwsh 7/Linux — sem prova
real de COM/DPAPI/WinForms/PS 5.1/NSIS. Restic real: prova de
`ls --json`/`dump`/SHA-256 do ciclo anterior mantida; VM Windows
limpa e SFTP de laboratorio continuam ausentes. Instalador NAO
compilado: sem `makensis` nem `payload\restic.exe` aprovado neste
ambiente — `BUILD.md` mantem o primeiro passo reproduzivel (copiar o
restic.exe aprovado para `installer\payload\`, `makensis /DVERSAO=...`,
registrar versoes e SHA-256 dos binarios).

## Revisao 9 (branch feature/windows-backup-gui-pilot)

**Defeito corrigido — ultimo sucesso perdido com a janela fechada:**
`ultimo-confirmado.json` so era gravado quando a interface lia um
resultado de sucesso; sucesso de segunda + falha de terca sem abrir a
GUI fazia o falha substituir o resultado e o confirmado nunca existir.

- O AGENTE (`Invoke-AgenteBackup`) agora grava o confirmado ele mesmo:
  so com codigo Restic `0`, snapshot de 64 hex, `fim_utc` e o
  `repositorio_id` retornado pelo `Confirm-Destino` DESTA execucao.
  Substituicao atomica (tmp + `Move-Item`, com guarda contra pasta
  homonima). Falha/incompleto/resultado invalido nao apagam; sucesso
  mais antigo nao regride (`ConvertTo-DataUtc`: DateTime/DateTimeOffset
  do ConvertFrom-Json do pwsh 7 ou texto em cultura invariante —
  corrigido um defeito latente de cultura: `[DateTimeOffset]::Parse`
  sobre `[string]` local falhava em culturas dd/MM/aaaa e regravava
  por cima). Se a gravacao do confirmado falhar, o backup CONTINUA
  sucesso, o arquivo anterior permanece byte a byte, o temporario e
  limpo e o erro vai para `confirmado_erro` no ultimo-resultado.json
  + Write-Warning.
- GUI: `Read-UltimoConfirmado`/`Update-UltimoConfirmado` locais
  removidos — le `Read-Confirmado` do agente (valida snapshot 64-hex +
  fim parseavel + repositorio_id do repositorio ATIVO) e faz backfill
  com `Update-Confirmado` para resultados de agentes antigos. A tela
  mostra "Falhou" neste disparo e "Ultimo backup confirmado" anterior
  ao mesmo tempo, sem depender de abertura anterior da janela.

Regressao OBRIGATORIA (caminho real do agente, janela fechada —
`Invoke-AgenteBackup` na suite do agente): sucesso -> confirmado com
snapshot/ID/fim; falha (restic 1) -> resultado recente = falha E
confirmado preservado; incompleto (restic 3) -> preservado; novo
sucesso -> atualiza; rc=0 sem snapshot -> falha, confirmado preservado;
sucesso com fim mais antigo -> nao regride; outro repositorio -> nao
herda; dir so de leitura na gravacao -> sucesso mantido + confirmado_erro
+ anterior byte a byte + tmp limpo. Na GUI: registro corrompido/invalido
nao e apresentado; snapshot invalido na gravacao -> recusado.

Verificacao: agente **144 ok, 0 falha** (pwsh 7.4.6, simulado); GUI
**181 ok, 0 falha** (1 pulado: COM real fora do Windows); `unittest`
**5/5** (2 pulados: pwsh fora do PATH do harness — suites rodadas
direto); `git diff --check` limpo; linhas novas em ASCII.
Separacao de prova: SIMULADO em pwsh 7/Linux — sem prova real de
COM/DPAPI/WinForms/PS 5.1/NSIS. Restic real: prova do ciclo anterior
mantida. Instalador NAO compilado: sem makensis nem payload\restic.exe
aprovado — BUILD.md mantem o primeiro passo reproduzivel. Pendente na
VM: sucesso -> falha com a janela fechada e SFTP sem resposta durante a
configuracao (janela deve voltar a aceitar comandos e nada publica
depois do timeout).

## Revisao 10 (branch feature/windows-backup-gui-pilot)

**Substituicao segura de ultimo-confirmado.json:** `Update-Confirmado`
usava `Move-Item -Force`, que pode apagar o destino antes de mover o
arquivo novo — uma falha no meio da troca perdia a confirmacao
anterior. Reescrito:

- Temporario EXCLUSIVO no mesmo diretorio
  (`ultimo-confirmado.<guid>.tmp` — mesmo volume, sem colidir com
  outra tentativa nem com nome fixo), JSON completo gravado e fechado
  (`[IO.File]::WriteAllText`, UTF-8 sem BOM) ANTES de publicar.
- Publicacao em `Publish-Confirmado`: `File.Move` SEM sobrescrita na
  primeira gravacao (destino existente -> erro, nunca apaga) e
  `File.Replace` quando ja existe confirmado — a troca e atomica ou
  nao toca em nada. Sem caminho alternativo que apague o anterior.
- Falha limpa APENAS o temporario da tentativa; a confirmacao
  anterior permanece byte a byte. Regras mantidas: so backup valido
  atualiza; sucesso mais antigo nao regride; outro repo nao herda;
  confirmado_erro em ultimo-resultado.json se a gravacao falhar com
  snapshot remoto intocado.

Regressao OBRIGATORIA (caminho real `Invoke-AgenteBackup`, restic
simulado, janela fechada): falha INJETADA na substituicao —
`Publish-Confirmado` redefinido na sessao para lancar DEPOIS de o
temporario estar completo (simulado; a prova com arquivo bloqueado de
verdade ficou no roteiro da VM, item 9 de 4b). Resultados: SHA-256 do
confirmado anterior IDENTICO; ultimo-resultado com o snapshot novo +
confirmado_erro; nenhum *.tmp resta; conjunto de arquivos do estado
inalterado. Cobertura complementar: primeira gravacao (File.Move sem
sobrescrita), substituicao bem-sucedida (File.Replace — "sucesso mais
novo atualiza"), sucesso -> falha -> incompleto preservando o
confirmado, criacao do temporario bloqueada (dir so de leitura, so
Linux) tambem preserva.

Verificacao: agente **150 ok, 0 falha** (pwsh 7.4.6, simulado); GUI
**181 ok, 0 falha** (1 pulado: COM real fora do Windows); `unittest`
**5/5** (2 pulados: pwsh fora do PATH do harness — suites rodadas
direto); `git diff --check` limpo; linhas novas em ASCII.
Separacao de prova: SIMULADO — sem prova real de COM/DPAPI/WinForms/
PS 5.1/NSIS/File.Replace no Windows. Instalador NAO compilado: sem
makensis nem payload\restic.exe aprovado — BUILD.md mantem o primeiro
passo reproduzivel. Na VM: sucesso -> falha com a janela fechada,
SFTP mudo na configuracao e confirmado bloqueado (piloto 4b itens 7-9).

## Revisao 11 (branch feature/windows-backup-gui-pilot)

**Atalho nao abria a interface (VM, instalador do commit 007f14c):**
Menu Iniciar -> PowerShell aparecia por um segundo e fechava. Execucao
direta do script instalado revelou `New-Object: TypeNotFound
[500.Label]` na linha 1228, codigo de saida 1. OpenSSH na mesma VM:
9.5p2 (fora da causa).

Causa raiz: dentro de `Show-Gui`, `$W = 'System.Windows.Forms'`, e os
helpers `New-Label`, `New-Button` e `New-TextBox` tem parametro
`[int]$w` (largura). O PowerShell nao distingue maiusculas de
minusculas em nomes de variavel — `$w` esconde `$W` no escopo do
helper, e `New-Object "$W.Label"` resolvia para `"500.Label"` (a
largura padrao) em vez do tipo WinForms. So ocorre sob Windows
PowerShell 5.1 com WinForms real; nos testes simulados em pwsh 7 o
erro nao se manifestava porque os helpers nunca eram invocados com
tipagem real.

Correcao: os tres helpers agora instanciam tipos COMPLETOS
('System.Windows.Forms.Label', '.Button', '.TextBox') — nunca via a
variavel — com comentario ATENCAO no codigo. Varrimento estatico
confirmou que nenhuma outra funcao tem parametro `[int]$w`; `New-Tab`
e os demais `New-Object "$W.*"` estao no corpo de `Show-Gui` ou em
handlers sem o parametro conflitante (fechadura correta). Fluxo de
configuracao, backup e restauracao intocados.

Regressao: criacao direta dos tres controles com as larguras padrao
assertando GetType().FullName (Label/Button/TextBox) + varrimento que
rejeita qualquer funcao com `[int]$w` contendo shorthand `"$W.` no
corpo. Sem abrir a janela (exigiria display Windows).

Verificacao: GUI **187 ok, 0 falha** (1 pulado: COM real fora do
Windows); agente **150 ok, 0 falha** (1 pulado: MACIP_RESTIC_REAL
indefinido); `unittest` **5/5** (2 pulados: pwsh fora do PATH do
harness — suites rodadas direto); `git diff --check` limpo; arquivos
ASCII.

Separacao de prova: SIMULADO em pwsh 7.4.6/Linux — a regressao prova
a resolucao de tipos, NAO a inicializacao do WinForms. Instalador NAO
recompilado: sem makensis nem installer\payload\restic.exe aprovado
(BUILD.md mantem o primeiro passo reproduzivel: copiar o restic.exe
aprovado para payload\ -> makensis /DVERSAO=... macip-backup.nsi ->
registrar SHA do codigo, versao e SHA-256 do .exe e do restic.exe).

ACEITE PENDENTE NA VM: instalar com autorizacao administrativa -> abrir
pelo Menu Iniciar na conta dedicada, SEM elevacao -> janela permanece
aberta, aba Ativar visivel, seletor de perfil abre. Para substituir o
instalador de teste: recompilar o .exe a partir deste commit com o
restic aprovado, desinstalar a versao anterior por Aplicativos
instalados (ProgramData e repositorio sao preservados) e instalar o
novo — o tecnico nao repete nenhuma configuracao. A homologacao do
backup segue separada (piloto 4b).
