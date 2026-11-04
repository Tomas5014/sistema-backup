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
## Homologação da central HTTPS (deploy/central)

Stack Compose `macip-central`: PG17 (volume pgdata) + API (constraints
fixadas) + nginx com portal compilado via `npm ci` — HTTPS único em
`127.0.0.1:8443` configurável; api/db sem portas publicadas. Segredos
em `secrets/*.env` (600, fora do Git), certificado montado em runtime;
dois caminhos TLS (cert do administrador / CA de laboratório com
confiança explícita, nunca no SO). Scripts: gen-secrets, mk-lab-tls,
up (valida permissões/senha coerente/sem cookie inseguro), check
(openssl verify_hostname + /readyz), down (preserva volume), backup/
restore (pg_dump -Fc/pg_restore --clean), bootstrap-operator (CLI,
senha por prompt), prove.sh.

Prova real executada: prove.sh provisionou operador/cliente/agente/
credencial/execução, rodou backup real do agente (restic → outbox →
sync HTTPS) e dirigiu Chrome com CA no nssdb de HOME temporário —
1/1 Playwright: login, cookie HttpOnly+Secure, rotação CSRF,
detalhe, reload, revogação, restart com dados preservados.
Backup/restore: 15 execuções, 11 operadores e 10 agentes preservados.

---
## Endurecimento dos scripts da central

- `check_secret_perms` passa a testar BITS: grupo/outros sem acesso
  (600/400 ok; 444/511/640/644/604 recusados) — antes `444` passava
  na comparação numérica.
- `backup.sh` atômico: temporário 600 no diretório de destino,
  publicação por rename, recusa de destino existente, remoção só do
  próprio temporário, código original preservado, dump final 600.
- `restore.sh` atômico e isolado: valida o arquivo antes, para só
  api+web que estavam de pé (nada fora da stack), religa ao final,
  `pg_restore --single-transaction --exit-on-error` desfaz tudo em
  falha — rollback provado em PG real com falha no meio da aplicação.
- `up.sh`: senha divergente entre api.env e postgres.env é ERRO antes
  do Docker (comparação do componente senha decodificado, não
  substring); TLS validado nos caminhos configurados (relativos,
  absolutos, com espaço via aspas no .env), chave privada com bits
  restritos; `.env` é shell — espaço exige aspas.
- `check.sh`: `--cacert`/`--CAfile` como arrays — caminhos com
  espaço não quebram.
- `deploy/central/tests/`: 59 asserções — perms(10), backup(13),
  up/tls(15), restore-mock(16), restore-real-PG(5, com rollback
  provado por conexão independente).
- Stack real revalidada: up→check→prove.sh 1/1 (login HTTPS, CSRF,
  detalhe, revogação, restart, agente real sync) + backup real.

---
## restore.sh: validação pelo contêiner e retomada com erro real

- Validação do dump sai do host para o `pg_restore` do contêiner
  `db` (`-l` por stdin) — sem PostgreSQL no host; arquivo
  ausente/vazio/inválido recusado antes de parar serviços; contêiner
  indisponível vira diagnóstico de infraestrutura, não "dump
  corrompido".
- Retomada dos serviços deixa de ser `|| true`: 4 combinações
  restauração×retomada mapeadas — código original da restauração
  preservado, retomada falha vira retorno não-zero com aviso de que
  os serviços podem estar parados; nunca anuncia recuperação
  completa por engano; só religa o que estava de pé.
- Prova real reescrita pelo script: shim de `docker` roteia
  `compose exec` a um PG descartável — 9 asserções (restauração,
  falha no meio da aplicação, rollback de schema/dados/versão por
  conexão independente). Mocks: 30 asserções cobrindo validação,
  combinações e isolamento de serviços parados.

---
## restore.sh: estado reconsultado pós-`sim` e recuperação armada

- Estado dos serviços passa a ser consultado DEPOIS da confirmação —
  serviços ligados durante a espera são detectados e parados;
  a parada é confirmada com prazo (≤15 s) antes da aplicação e
  escritor ativo/estado não verificável aborta sem tocar o dump.
- Recuperação armada ANTES do primeiro `stop` (trap EXIT/INT/TERM):
  parada parcial, erro de preparação, falha de restore ou sinal
  sempre tentam religar só o que estava de pé — código da falha
  principal preservado, retomada falhando vira erro não-zero.
- Mocks: 51 asserções cobrindo parada parcial (código preservado,
  recuperação tentada), serviços ligados durante a espera, escritor
  remanescente, ps falhando pós-confirmação e na verificação.
- Prova real pelo script mantida: shim com estado realista de
  serviços; 9 asserções (restauração + falha no meio + rollback por
  conexão independente). Stack real: restore.sh executado —
  banco restaurado, api/web retomados, rc 0.

---
## restore.sh: sinais preservam código (129/130/143)

- `trap cleanup EXIT INT TERM HUP` deixava `$?=0` em sinal —
  anunciava "banco restaurado" sem ter aplicado. Agora cada sinal
  tem handler próprio (SIGHUP 129, SIGINT 130, SIGTERM 143) que
  diagnostica a ETAPA alcançada e sai com o código do sinal — o
  EXIT trap só trata retomada; "concluído" exige flag
  DUMP_APPLIED + retomada ok.
- Mocks: sinais reais enviados ao PID com o script pausado num gate
  na verificação dos escritores — código correto por sinal, uma
  única retomada, dump nunca aplicado, sem mensagem de sucesso;
  SIGTERM + retomada falhando preserva 143 e informa a falha.
- Detalhe: SIGINT é ignorado em background sem job control (POSIX) —
  o teste usa `set -m` para entregar o sinal.

---
## Consolidação operacional da central

- Registro da homologação real em srvdev1 (28/10/2026): portal+CA,
  backup/check/restore/exclusões, agendamento+sync, fila com API
  parada (pending/502/backoff) e dreno automático na volta
  (3 execuções → sent), dump 25.185 bytes conferido por SHA-256 e
  restaurado em PG temporário (1 cliente, 1 agente, 8 execuções,
  migrações 1-2). Evidência do operador, distinta dos testes locais.
- docs/operacao-central-srvdev1.md: comandos para timers da tarefa
  homologacao-central-srvdev1 (backup hourly, sync a cada 5 min) com
  MACIP_BIN/MACIP_HOME/MACIP_CONFIG forçando ~/macip-central/src e
  preservando launcher 0.2.1/piloto.
- central-backup.sh: dump (backup.sh) + config + TLS efetivo +
  lab CA + manifesto sem segredos → staging 700 → restic
  (criptografia nativa, sem forget/prune — cópia anterior sempre
  preservada). Senha via credentials.py (env/keyring). SFTP com
  chave + known_hosts strict. Lock por flock; docker testado no
  contexto do serviço.
- install-central-backup-timer.sh: par service+timer 02h15
  Persistent, marcador de propriedade, --enable explícito.
- Testes: mock 41 asserções (segredos fora de log/argv, staging,
  SFTP, lock, units alheias) + real 10 (restic real, restore byte a
  byte, dump em PG descartável). Execução REAL via systemd --user
  nesta máquina: snapshot c807da26, docker acessível no contexto.

---
## central-backup: correções da revisão

- TLS: `cp -aL` (desreferencia symlink) em destinos por FUNÇÃO
  (`config/tls/cert.pem` / `key.pem`) — mesmo basename em diretórios
  diferentes não colide mais; restauração provada com os originais
  apagados (mock + real).
- Senha do repositório: resolvida e injetada por wrapper Python
  (env do processo filho, bytes exatos — `$(...)` não apara mais
  quebras de linha). Prova real: senha com `\n`+espaços abre o repo.
- Init: `restic cat config` decide — init SÓ com código 10 (repo
  inexistente); senha errada (12)/acesso/bloqueio falham sem init.
- SFTP: `sftp.args` montado com shlex.join no wrapper (mesma regra
  do adaptador): BatchMode, StrictHostKeyChecking+UserKnownHostsFile,
  IdentitiesOnly, publickey-only, -i/-p, ConnectTimeout/ServerAlive —
  caminhos com espaço chegam como argumento único (testado).
- Manifesto: sem auto-hash; checksums só de não-secretos; secretos
  marcados role=secret por função; version= do artefato VERSION
  (export-subst do git archive) com fallback rev-parse.
- Runbook: caminho real do staging restaurado (find), reset-password
  via operator_cli, senha por read -s/keyctl padd interativo/
  EnvironmentFile 0600, sftp.args na recuperação, tarefa.json real.

---
## central-backup: ajustes finais do runbook

- Ativação: o fluxo agora é gerar units SEM --enable → daemon-reload
  → `systemctl --user start` → conferir snapshot → `enable --now` —
  porque o EnvironmentFile é carregado pela unit, não pela execução
  direta (o dry-run anterior falharia por credencial ausente).
  Arquivo criado com `install -m 600` antes da senha, sem clobber.
- Recuperação SFTP: SSH_ARGS/RESTIC_OPTS definidos uma vez e
  aplicados em snapshots, restore --include MANIFEST.txt e restore
  completo; manifesto extraído antes do checkout com `version=` →
  só o SHA passa ao git; aspas no UserKnownHostsFile.
- Regressão: teste confere sftp.args recebidos pelo próprio
  `restore` (chave/known_hosts com espaço, strict, timeout).
- systemd real executado de novo: serviço rodou com EnvironmentFile
  (sem keyring) — snapshot 00852210, status SUCCESS.
- Contagem correta: 165 simuladas + 22 reais = 187 asserções na
  suíte; provas reais desta revisão = central-backup (restic/PG,
  systemd) — restore real 9 e perms/up são de revisões anteriores.

---
## runbook SFTP: sftp.args por shlex.join + regressão com restic real

- SSH_ARGS agora é gerado por python shlex.join a partir de LISTA de
  argumentos (como o wrapper) — o texto com aspas manuais fazia o
  restic 0.18.1 entregar '-oUserKnownHostsFile=' e o caminho
  separados ao ssh.
- RESTIC_OPTS reaplicado em snapshots, restore --include e restore
  completo; linha antiga de checkout pré-manifesto removida.
- Regressão 8c: restic REAL + ssh falso registrando argv — confere
  -i e UserKnownHostsFile íntegros com espaços. Integração com
  servidor SFTP real segue pendente declarada.
- Nova contagem: 168 simuladas + 22 reais = 190 asserções.

---
## portal: cadastramento de clientes/agentes + gestão de credenciais

- "Novo cliente" (admin) na listagem — formulário com validação,
  erro do servidor visível, link "Abrir o cliente" mesmo fora da
  página atual.
- "Novo agente" no contexto do cliente (cliente citado no
  formulário) — vínculo por POST /clients/{id}/agents; agente vira
  link para a tela de credenciais.
- /agents/{id}: contexto cliente+agente+id, listagem com descrição/
  criação/estado, emissão com label, token em diálogo temporário
  (Copiar por gesto explícito + falha de cópia informada; aviso de
  consulta única; descarte ao fechar/sair/trocar), revogação com
  confirmação nominal e estado atualizado sem ação duplicada.
- Todo POST com CSRF; pending bloqueia envio duplicado; falha de
  rede na emissão vira "resultado incerto — consulte a listagem";
  respostas atrasadas descartadas por geração/agente.
- Token nunca em storage/URL/console — existe só em estado React.
- Testes: 15 unitários novos (51 total); e2e novo: login → cliente →
  agente → emite → heartbeat 204 → revoga → heartbeat rejeitado
  (API+PG17 descartáveis).

---
## portal provisioning: correções da revisão

- Cadastro pendente entre clientes: NewAgentForm remontado por
  key={clientId} — resposta atrasada do POST de A é descartada ao
  desmontar (alive=false); tela de B preserva título, listagem e não
  exibe confirmação de A. Pendência de A não bloqueia o form de B.
- Resolução além da 1ª página: findAgent/findClient varrem a
  paginação (limit/offset da API, sem GET por id) — agente na
  posição 501 é encontrado; agente inexistente mostra estado
  explícito "Agente não encontrado".
- stubFetch passa a URL ao handler → mocks paginam de verdade.
- README: credential_name (inexistente) → token_environment do
  central.json; token da API central ≠ senha do repositório Restic.

---

---

## 2026-10-29 — Cadastro e versionamento de tarefas (central + portal)

### Contexto recebido

Continuação da POC MAC IP: a partir do commit aprovado `2802894`
(provisionamento de clientes/agentes/credenciais), implementar o
cadastro de tarefas de backup na central com versionamento de
configuração — seguindo a decisão "um motor (Restic) + um cadastro":
documentos de tarefa únicos, apontadores por agente e histórico de
revisões. Explícito: não distribuir nem executar.

### Implementado

- **Migração 3** (`api/migrations.py`): `tasks` (UUIDv5 por agente,
  `current_revision`, `archived_at`) e `task_revisions` (snapshot JSONB
  imutável, revisão desde 1, `contract_version`, autor
  `operator`/`admin`, unicidade `(task_id,revision)`, transação única
  com o ponteiro).
- **`api/task_contract.py`**: validação do envelope central
  (name/spec/schedule), normalização canônica para detecção de
  salvamento idêntico e exportação mapeando UUID central →
  `TaskSpec.id`, cliente central e `device_id` do agente.
- **Rotas**: criação por agente, listagem paginada com filtros
  (cliente/agente/arquivada), detalhe com revisão atual, nova revisão
  com `expected_revision` (409 otimista + 409 arquivada), histórico,
  exportação `tarefa.json` por revisão e arquivamento sem deleção.
  Viewer lê; admin escreve com CSRF; tokens de agente não servem.
- **Portal**: `/tasks` (lista + filtros), `/tasks/new?agent=`,
  `/tasks/{id}` (detalhe, edição, histórico, exportação, arquivamento
  com explicação do alcance). Formulário estruturado com origens SMB,
  exclusões, destinos por tipo com campos condicionais, retenção,
  compressão, agenda (IANA) e opções avançadas. Entradas cruzadas nos
  agentes do cliente e na página de credenciais. Estado sempre
  "cadastrada — não distribuída"; `409` preserva valores digitados;
  respostas atrasadas descartadas por geração+`alive`.

### Verificação

- API real (PostgreSQL 17, contêiner descartável em :55433):
  `test_tasks.py` 30/30 + `test_task_contract.py` 24/24 + suíte total
  **110/110** — migração 2→3 preserva dados, corrida otimista (um 201,
  um 409), rollback do ponteiro, autor distinto (operador vs Bearer),
  segredos ausentes, exportação de revisão antiga idêntica, exemplos
  dos 4 tipos de destino parseados pelo `TaskSpec` do agente real.
- Portal simulado: **63/63** (9 novos de tarefas: lista/filtros,
  criação com contexto+CSRF, edição 409 preservando valores,
  expected_revision, arquivamento, viewer, resposta atrasada).
- `tsc --noEmit` limpo; `vite build` ok.
- **E2E real** (Chrome + API + PG descartável): 3/3 — inclui o fluxo
  novo: login → cliente/agente → criar → editar (rev 2) → exportar
  rev 1 com UUIDs centrais → 401 sem sessão → arquivar.
  Homologation segue skip por ambiente.

### Correção no caminho

Bug encontrado no E2E: `saveEdit` só desligava `pending` em erro — um
clique de arquivamento após edição bem-sucedida ficava travado.
Corrigido com `finally` antes do commit.

### Escopo respeitado

Sem distribuição ao agente, sem systemd/agenda aplicada, sem deleção
física, sem segredos persistidos. Alterações pré-existentes do
usuário (`atualizações.txt` deletado, `.claude/`, `.trail/`,
`tarefa-teste.json`) preservadas fora do commit.

---

## 2026-10-30 — Revisão do cadastro de tarefas (482f4bc)

Correções da revisão técnica do commit anterior — segredos em 422,
filtros do portal, rota obsoleta, contrato incompatível, exportação
mutável e corridas não cobertas.

### Implementado

- **Mascaramento 422** (`app.py`): padrão de chaves secretas
  (`password|secret|token|private|credential|api/access_key|identity…`)
  em loc/input/ctx/msg com scrub recursivo + userinfo de URL
  (`https://u:s@h` → `https://***@h`) — cobre campos desconhecidos
  com sentinela e credenciais embutidas em URL.
- **Contrato** (`task_contract.py`): caractere de controle rejeitado em
  todo texto; `..`/`//`/vazio rejeitado em repository local e origens
  (lexical, espaços legítimos preservados); `retry.attempts` ≤ 64
  (teto do recebimento `MAX_ATTEMPTS`).
- **Migração 4**: `task_revisions.client_name` — snapshot do nome do
  cliente no momento da revisão; backfill explícito com o nome vigente
  à época (nomes históricos não eram registrados).
- **Concorrência**: `FOR UPDATE` na linha da tarefa em toda escrita
  (revisão e arquivamento) — check e update na mesma transação,
  salvamento idêntico revalida expected sob o lock.
- **Portal**: TasksPage com gerações separadas (clientes vs tarefas),
  retry efetivo e erro explícito de clientes; TaskDetailPage com
  guarda de geração+taskIdRef em salvar/arquivar e conteúdo preso à
  rota vigente; TaskNewPage não redireciona nem trava ao trocar
  `?agent=`; viewer não recebe formulário de criação.
- **Documento**: identificador é `uuid4` (o relatório dizia UUIDv5).

### Verificação

- API real (PG 17, :55433): tasks 29/29 incluindo corridas com
  sessões independentes + observação de `pg_locks` (edição×edição,
  arquivar×editar, editar×arquivar, idêntico×expected obsoleto) e
  exportação histórica idêntica após rename; contrato 21/21 com
  bordas; HTTP sem banco 19/19 com sentinelas.
- Portal simulado: 70/70 (7 novos: seletor, retry, dados/callbacks
  de rota antiga, viewer).
- typecheck + `vite build` limpos; E2E real re-executado.

---

## 2026-10-30 — Segunda rodada de revisão do cadastro de tarefas

Consolida os três pontos restantes da revisão do 25df71e.

### Implementado

- **Userinfo de URL no 422**: a regex antiga parava no primeiro `@`
  ou em espaço — senhas como `pre@SENHA@h` e `pre SENHA@h` vazavam.
  Agora o userinfo inteiro (até o último `@` do segmento de
  autoridade) é removido; localização e tipo do erro preservados.
- **Aliases `.` em caminhos**: o agente resolve `.` lexicalmente e
  vê o repositório dentro da origem — a central passou a rejeitar
  segmentos `.`/`..` em origem, `repository` local e
  `smb.mount_point`. NUL e demais controles agora também em
  `sftp.host` e `smb.server` (tipos próprios, paridade com
  `_require_sftp_host`/`_require_smb_server` do agente).
- **Identidade de visita e de mutação no portal**: `visitRef`
  incrementa a cada visita à rota (`A→B→A` são identidades
  diferentes) — um callback antigo não volta a valer ao "retornar".
  `mutationRef` faz `pending` pertencer à mutação mais recente —
  o `finally` de uma operação antiga não libera uma nova, mas a
  própria operação libera normalmente após o `load()` que dispara.
  `agentName`/`clientName`/`revisions` são limpos a cada `load()` —
  metadados de A não aparecem como contexto de B.

### Verificação

- API real (PG 17): 151/151 — corridas FOR UPDATE e exportação
  imutável preservadas.
- Contrato: 29/29 — aliases `.` nos dois lados, NUL em host/server,
  comparação com `validate_recovery_for_execution` do agente.
- HTTP sem banco: 23/23 — URLs com `@`/espaço na senha, aninhadas.
- Portal: 73/73 vitest; typecheck + build limpos.
- E2E real (Chromium + PG): 3/3, homologação HTTPS pulada.

---

## 2026-10-30 — Distribuição gerenciada de tarefas (agente Linux)

Incremento separando três estados: revisão cadastrada ≠ revisão
publicada ≠ revisão aplicada confirmada pelo agente.

### Central (API + migração 5)

- `task_distributions`: estado desejado por tarefa — `generation`
  monotônica (publicar/atualizar/suspender na mesma revisão são
  distintos), `published_revision`, `desired_action`
  (none/apply/suspend), autor e colunas `confirmed_*` da geração
  vigente.
- `task_confirmations`: log por (task_id, generation) com
  payload_hash — deduplicação idêntica às execuções.
- `POST /tasks/{id}/publish {revision, expected_generation}` e
  `POST /tasks/{id}/suspend {expected_generation}`: admin+CSRF,
  FOR UPDATE na distribuição — corrida resolve uma única decisão.
  Arquivar NÃO suspende; suspender funciona em arquivada.
- `GET /agent/tasks/desired`: paginado (total explícito), envelopes
  com TaskSpec exportado (só referências de credencial), agenda
  separada, contract_version e hash canônico.
- `POST /agent/tasks/{id}/confirmations`: identidade pela credencial;
  geração antiga fica no log (sem promover); futura → 409; received_at
  do servidor.

### Agente Linux

- `distribution.py`: consulta paginada, validação de
  identidade/geração/hash/contrato + TaskSpec real, journal durável
  em `<state>/managed/tasks.d/<id>/` (gen-N imutável, journal.jsonl,
  current.json atômico), confirmações na fila SQLite `confirmations`
  (v6, separada da outbox), locks maintenance SH + manage-vínculo EX,
  vínculo `binding.json` (troca de central exige --rebind).
- `systemd.py`: units `macip-backup-managed-<id>` com marcadores de
  vínculo; agenda manual/hourly/daily/weekly → OnCalendar com fuso
  IANA + Persistent=true; manual = só service; suspensão = só disable
  do timer; timer independente de consulta `macip-backup-pull-<bond>`.
- CLI: `pull-check`, `pull-tasks`, `pull-status`, `schedule-pull`,
  `unschedule-pull`.

### Portal

Seção de distribuição no detalhe: revisão cadastrada/publicada/
aplicada, geração, ação desejada, última confirmação; "Publicar" e
"Suspender" com explicação prévia e controle de identidade de
visita/mutação; viewer lê sem escrever.

### Prova integrada (descartável)

`tests/test_e2e_distribution.py`: API real (uvicorn) + PG real +
`pull-tasks`/`agent`/`sync` como subprocessos + backup Restic local
real + units em HOME descartável + systemd --user real (enable/
disable) — fluxo cadastro→publish→pull→units→backup→resultado→
aplicada→update→suspend.

## Consolidação pós-revisão — correções do commit f2074ef

Revisão independente do `f2074ef` apontou falhas corrigidas em
commit(s) novo(s) (sem reescrever o anterior):

1. **Ordem de aplicação** — `install_managed_task` habilitava o timer
   antes de `current.json`. Novo protocolo em journal:
   received→validated→config-`N`.json→timer_off→units→published→
   enabled→applied. `current.json` só é publicado depois das units e
   o enable é o último passo; falha nos efeitos faz rollback para a
   última geração aplicada (units + configuração + estado do timer) ou
   remove o resíduo da primeira tentativa.
2. **Janela journal→fila** — aplicação concluída sem enfileiramento
   ficava sem confirmação definitiva. `_reconstruct_confirmations`
   recria a fila a partir do journal a cada rodada (evento com
   identidade e conteúdo estáveis — `agent_time` do journal).
3. **Evolução por tentativa** — identidade dos relatos passou a ser
   `(task_id, generation, apply_attempt)`: PostgreSQL migração 6
   (`attempt` + nova UNIQUE + `confirmed_attempt` + `last_applied_*`),
   SQLite v7 (rebuild preservando linhas). Falha→sucesso na mesma
   geração é relatável; histórico preservado; resposta atrasada nunca
   regride; `last_applied` mostra o último sucesso.
4. **Transitório nunca bloqueia** — rede/timeout/5xx/429 seguem em
   retry com backoff capado em 1h; só rejeições definitivas
   (401/403/404/409/422) marcam `blocked`, com recuperação documentada
   e `pull-unblock` para rearmar depois da correção.
5. **`--no-activate` = preparação** — grava envelope + configuração
   candidata + units; não ativa, não marca `applied`, não confirma.
   Execução normal posterior retoma a mesma tentativa e conclui.
6. **Validações** — `task.id`/`device_id` do documento conferidos
   contra envelope/vínculo; `TaskSpec.validate_for_execution` +
   destinos (origens existentes, repo-fora-da-origem, arquivos SFTP,
   montagem SMB) antes de qualquer efeito — sem init de repositório.
7. **Vínculo completo** — `managed/` agora chaveia por bond
   (endpoint+UUID+banco): `active.json` + `managed/<bond>/tasks.d`;
   marcador `managed-bond` nas units; `--rebind` desativa os timers do
   vínculo anterior e aponta o ativo; lock `manage.lock` global
   serializa adoções/aplicações; confirmações do vínculo antigo nunca
   migram.
8. **Prova integrada real** — `test_timer_real_dispara_backup`: API
   real + PG real + `pull-tasks` com ativação real + timer systemd
   DISPARANDO o backup Restic (agenda do minuto seguinte) + resultado
   na central + confirmação `applied`/`last_applied` + suspensão
   verificada no gerenciador real; limpeza garantida das units
   criadas.

## Correções da revisão do commit 92d4698

Seis achados corrigidos em novo commit (92d4698 preservado):

1. **Rollback preserva estado suspenso** — o rollback agora restaura
   DOIS aspectos: a última configuração instalada (action='apply') e
   o estado operacional anterior (apply=timer habilitado,
   suspend=timer desabilitado). Uma suspensão não é reconstruída como
   envelope apply (sem 'schedule'). Teste: apply→suspend→falha na
   gen3 → config da gen1 restaurada, timer PERMANECE suspenso.
2. **Preparação não altera units operacionais** — `--no-activate`
   escreve artefatos em `<task_dir>/staged/`, NÃO no diretório
   operacional. Timer existente não é removido, estado habilitado/ativo
   não muda. Ativação posterior promove os artefatos pelo protocolo
   seguro. Teste: automática aplicada → manual em staging → timer
   operacional intocado.
3. **Propriedade completa das units** — `_assert_managed_units` exige
   os TRÊS marcadores (managed-task, central-agent, managed-bond).
   Marcador ausente = recusa (não adota units legadas). Mesmo
   `allow_bond_replacement` exige task e agent correspondentes.
4. **Troca de vínculo retomável** — `transition.json` persiste a
   intenção (from_bond, to_bond, phase). Após reinício: se
   phase='active_switched', retoma com allow_bond_replacement=True;
   se phase<='timers_disabled', restaura o vínculo anterior. Teste:
   interrupção depois de active_switched → retoma sem --rebind.
5. **Resposta 200 ambígua continua reenviável** — HTTP 200 com JSON
   inválido ou sem status reconhecido NÃO bloqueia: preserva o evento
   para reenvio idempotente com backoff. Só `recorded`/`duplicate`
   marca como enviado. Teste: 200 com JSON inválido → pending; reenvio
   → duplicate.
6. **Suspensão não apaga last_applied_revision** — a API só atualiza
   `last_applied_*` quando `applied_revision is not None` (apply
   real). Suspensão (applied_revision=None) confirma a ação mas
   preserva a última configuração instalada. Teste: apply rev1 →
   suspend → last_applied PERMANECE rev1; publicar rev2 sem aplicar →
   suspend → last_applied NÃO anuncia rev2.

## Correções da revisão do commit 1f7d7eb

Quatro achados de transição corrigidos em novo commit (1f7d7eb
preservado):

1. **Rollback não habilita temporariamente tarefa suspensa** —
   `_effects_for_generation` agora aceita `target_operational`:
   'apply' (default) habilita o timer; 'suspend' mantém desabilitado
   durante TODO o rollback (journal `kept_suspended`). Nenhuma
   janela de enable→disable. Teste registra a sequência completa de
   chamadas e falha se houver qualquer enable no rollback suspenso.
2. **`--no-activate --rebind` permanece preparação** — preparação com
   rebind NÃO transita vínculo: preserva active.json, timers
   operacionais, estado habilitado. Só a execução posterior com
   ativação realiza a adoção pelo protocolo seguro.
3. **Não concluir troca com adoção pendente** — a transição só é
   concluída se `not summary["failed"]`. Falha de validação deixa
   `transition.json` em `active_switched` — a autorização persistente
   sobrevive para retomada após correção do pré-requisito, sem
   --rebind e sem remover arquivos manualmente.
4. **Interrupção após timers_disabled restaura operacional** —
   `_resume_transition` para phase≤timers_disabled agora re-grava
   active.json E reativa os timers das tarefas automáticas do
   vínculo anterior (não reativa suspensas/manuais). O teste
   verificado executa a recuperação e confere vínculo ativo,
   propriedade das units, configuração e estado operacional final.

## Correção da revisão do commit 060db0c

Duas inconsistências de recuperação da troca de central corrigidas em
novo commit (060db0c preservado):

1. **Rodada encerra quando A é restaurada** — `_resume_transition`
   agora retorna resultados EXPLÍCITOS (`_TRANSITION_NONE`,
   `_TRANSITION_RESUMED`, `_TRANSITION_RESTORED`) em vez de booleano.
   Quando restaura A, `reconcile` define `bond_restored=True` e
   ENCERRA a rodada antes de aplicar/preparar/confirmar tarefas da
   resposta de B. Adoção posterior de B exige `--rebind` novamente.
   Antes: rodada continuava e aplicava tarefas de B com active.json
   apontando A.
2. **Identidade restaurada completa** — `transition.json` agora
   persiste snapshot COMPLETO da identidade anterior
   (`from_endpoint`, `from_agent_id`) em todas as fases. A
   restauração re-grava active.json com bond+endpoint+agent_id de A
   (do snapshot ou do `binding.json` de A como fallback), NUNCA com
   `spec.url` de B. Snapshot e binding ausentes/corrompidos →
   `ConfigurationError` com diagnóstico (não fabrica identidade
   misturada).

Regressões: tarefa de B com UUID distinto não é aplicada nem
confirmada na rodada de restauração; identidade restaurada comparada
campo a campo; snapshot ausente diagnostica sem fabricar; tarefa
automática reativada mantendo suspensas/manuais; adoção posterior
autorizada de B funciona.

---

## 2026-11-03 — Homologação de distribuição e correção do CLI de consulta

### Evidências reais fornecidas pelo operador

Estas evidências foram registradas, não reexecutadas nesta revisão.
Central e agente de laboratório estavam no commit `469b356`; a central
rodava na VM `srvdev1` em `127.0.0.1:8444`, acessada no PC por
`https://localhost:18444`. O agente Linux usou configuração, estado,
cache, locks e repositório isolados.

- Agente `381143c2-d36e-4933-82cb-edafe49a0070`; tarefa
  `d96b57f2-740a-46ec-83e5-fb47b3fb3097`; destino local
  `local-homologacao`.
- `pull-tasks --no-activate` preparou a geração 1 somente em staging:
  sem unit operacional, repositório ou confirmação de aplicação.
- Revisão 1 aplicada e confirmada. Timer disparou às `15:04:01 BRT`.
  Execução do agente `e2bfa774-0f65-4a33-a7a1-1d85afd6c2a9` e registro
  central `f8fbddab-5e63-4dd8-9d73-3eb4f0eb9479`: sucesso, 2 arquivos,
  outbox `sent`, sem erro. Portal recebeu às `15:06:31 BRT`.
- Snapshot
  `955c28da45e3ebbb8ec575133958db300f9ba3289ba277d84b31d03866c60686`:
  `check --read-data`, restauração em diretório novo e `diff` passaram;
  `ignorar.tmp` permaneceu excluído.
- Revisão 2 publicada como geração 2. Timer de consulta aplicou às
  `15:19:15 BRT`, sem `pull-tasks` manual. Confirmação `sent`; portal
  mostrou revisão cadastrada/publicada/aplicada 2 e geração 2.
- Suspensão publicada como geração 3. Confirmação enviada às
  `15:23:12 BRT`. Timer de backup ficou desabilitado e inativo. Snapshot
  persistiu; nova restauração e comparação passaram; exclusão persistiu;
  timer continuou desabilitado e inativo.

### Defeito confirmado e correção desta revisão

`main()` tinha imports locais de `schedule_json` em outros ramos. Isso
tornava o nome local à função inteira, mas `schedule-pull` e
`unschedule-pull` o consultavam antes de qualquer atribuição. As novas
regressões da entrada pública reproduziram `UnboundLocalError` nos dois
comandos. A correção mínima importa `schedule_json` dentro de cada ramo
afetado, preservando parser, JSON, códigos de saída, propriedade e
semântica das units.

Testes desta revisão são simulados e isolados: chamadas diretas a
`main()` controlam helpers; um subprocesso real
`python3 -m macip_backup.cli` usa HOME temporário e executáveis falsos
de `systemd-analyze`/`systemctl`. Nenhuma unit real do desenvolvedor foi
alterada. O teste cobre instalação sem ativação, `--enable`, remoção do
vínculo correto, repetição idempotente e erro operacional com código 2.
O gate pertinente aprovou 76/76 testes de CLI, geração simulada de units
e distribuição. Os testes de distribuição usaram servidor HTTP local
descartável; nenhum serviço externo ou laboratório real foi acessado.

### Limites e pendências

- O ciclo real usou somente destino local. Não é homologação geral de
  produção, SMB, S3 ou Windows.
- O agendamento original chamou `schedule_pull()` internamente porque o
  CLI público falhava. A correção do CLI foi comprovada depois, em teste
  isolado; o ciclo real não foi reexecutado nesta revisão.
- A parada dos timers de consulta e sincronização foi orientada, mas o
  operador ainda não confirmou a limpeza.
- `srvdev1`, laboratório congelado, piloto 0.2.1, instalações globais,
  backups e credenciais reais não foram acessados nem alterados.

---

## 2026-11-03 — Validação real do CLI de consulta e cancelamento

### Cancelamento: diagnóstico

`restic.py` tem SHA-256
`7aa54545f5ec60f765d4434efe08517a2af38a878a6e4844036585193a9cc224`
tanto em `469b356` quanto em `ef3ff75`. Worktrees isolados em `/tmp`
compararam base e candidato; ambos passaram três rodadas completas de
`AdapterCancellationTests` (18/18 no total).

No candidato, a primeira rodada passou 3/3 em 5,477 s. Medições
individuais: 0,076 s no SIGTERM limpo e 5,451 s no descendente que exige
SIGKILL. Não foi reproduzido defeito funcional no adaptador.

A prova tinha duas fragilidades:

- `_spawn` observava somente a publicação de `Popen`, não o início
  confirmado do binário falso;
- `pgrep -f` não preservava identidade contra reuso de PID ou diferenças
  de namespace e não distinguia a regra para estado zumbi.

O teste passou a aguardar prontidão, registrar `PID:starttime`, consultar
o mesmo `/proc`, tratar `Z`/`X` como encerrado e limpar identidades
registradas no `tearDown`. A asserção `< 10 s` e a prova do descendente
encerrado foram mantidas. Nenhum código de produção mudou.

Verificações após o ajuste:

- stress: 110/110 em 64,340 s (100 SIGTERM limpos, 10 descendentes);
- `tests.test_restic_unit`: 9/9 em 5,149 s;
- gate CLI + units simuladas + cancelamento + distribuição: 79/79 em
  39,226 s.

### CLI público com systemd real

`systemctl --user` respondeu `degraded`, mas operou a identidade
descartável `macip-backup-pull-79b2a94fe0cb4308`. Os comandos foram
executados por `python3 -m macip_backup.cli`, usando o código do checkout,
sem helpers diretos e sem agente global.

- sem `--enable`: JSON válido, service+timer próprios criados, timer
  `disabled`/`inactive`;
- com `--enable`: JSON válido, timer `enabled`/`active`, próximo disparo
  `2099-12-31 23:59:59 -03`;
- primeira remoção: dois arquivos exatos removidos e timer parado;
- segunda remoção: sucesso com `removed: []`.

Recursos ficaram em `/tmp/macip-pull-systemd-Gp6AJ6`, com configuração
central sintética em `127.0.0.1:65534`, estado, cache, locks e binários
separados. O calendário distante impediu execução. Nenhuma comunicação
com central ocorreu. Trap e verificação final confirmaram ausência das
units, arquivos e processos; worktrees e diretórios temporários também
foram removidos.

### Alcance e pendências

- A prova aprova somente `schedule-pull`/`unschedule-pull` no Linux
  testado; não amplia aprovação para produção, Windows, SMB ou S3.
- A homologação do operador no commit `469b356` permanece histórica e
  não foi reexecutada.
- A parada dos timers antigos de consulta e sincronização do laboratório
  ainda depende de confirmação do operador.

## 2026-11-03 — Consolidação da candidata Linux 0.3.0rc1

Branch própria `release/linux-0.3.0rc1`, partindo de `d81ec86`. Ancestralidade
confirmada: `469b356` → `ef3ff75` → `7ec7b85` → `d81ec86`; sem merge ou
cherry-pick. Alterações preexistentes ficaram fora dos commits.

A identificação 0.3.0rc1 refere-se à distribuição composta para piloto
controlado. Agente continua 0.2.1; API/portal 0.1.0; PG17 com migrações
1..6, SQLite 7 e contrato de tarefas 1. SHA do commit será gravado pelo
gerador no manifesto externo após o commit final, evitando autorreferência.
Não é MVP completo nem aprovação de produção.

`deploy/package-candidate.py` usa somente `git archive` de commit explícito,
allowlist de fontes e staging temporário. Produz tar.gz com fontes e wheel,
MANIFEST.json interno/externo e SHA256SUMS. Exclui runtime de TLS/backups e
recusa bancos, logs e arquivos secretos; não empacota working tree sujo.
Não promete reprodução byte a byte de imagens ou dependências.

Validação do pacote preliminar: hashes, manifestos, importação/versão/ajuda
da CLI extraída, 23 scripts deploy com bash -n, instalação por install.sh em
caminho com espaços, versão/ajuda fora do checkout, permissões e install.json.
Extração e instalação removidas automaticamente; nenhuma unit criada. O
pacote definitivo é regenerado e conferido novamente após o commit final.

Evidência nova do operador encerra a pendência registrada acima: timers
`macip-backup-pull-a985252fb52df2de.timer` e
`macip-backup-sync-67ee9fbc7ff2b5da.timer` estão inactive/disabled. Não houve
acesso ao laboratório para verificar isso nesta consolidação. Laboratório,
SQLite, repositório e arquivos recuperados continuam preservados.

A revisão independente tinha PIDs divergentes entre Python e /proc no
sandbox. As falhas não foram reproduzidas na validação complementar. A nova
prova não é garantia universal para namespaces; adaptador não foi alterado.

Runbooks distinguem instalação nova, atualização da central e do agente,
backup/restauração de ensaio, compatibilidade de esquemas e retorno sem perda
silenciosa de escritas novas. Último principal informado `439e4f2` e
laboratório `469b356` são históricos, exigindo conferência futura.

Não foram reexecutados Docker/SFTP, build de API/portal, migração PG17 ou
atualização da central. São gates futuros, não aprovações inferidas. Windows,
portal restore, alertas, comandos remotos, segredos distribuídos, auto-update,
SMB/S3 completos, MFA, permissões por cliente, auditoria e volumes
representativos permanecem pendências de produto. Sem push/tag/deploy.

Gate executado: `PYTHONPATH=src python3 -m unittest tests.test_cli
tests.test_systemd.UnitGenerationTests tests.test_systemd.AdapterCancellationTests
tests.test_distribution tests.test_install -v`: **99 aprovados, 0 falhos,
0 ignorados**, 364,637 s. HTTP/systemd dos fixtures são simulados; cancelamento
usa processos reais; instalação testa Restic local. Warnings de sockets em
fixtures de distribuição foram observados. Recursos .test-inst/.test-dist
foram removidos pelo teardown. Nenhum teste systemd real foi acrescentado
nesta consolidação; a prova real anterior permanece identificada separadamente.

## 2026-11-03 — Correção dos três achados da candidata 0.3.0rc1

Base `23642fa`, branch própria `fix/linux-candidate-review`, sem reescrever
histórico. O pacote anterior permanece em dist/linux-0.3.0rc1. Nenhum segredo
foi constatado nesse pacote; a revisão demonstrou lacunas sintéticas.

1. Empacotamento: filtro cobre variantes de ambiente, modelos explicitamente
   permitidos, SQLite/WAL/SHM/journal e conteúdo reconhecível de chave sem
   extensão. Fontes/docs que citam cabeçalhos continuam aceitas. Staging
   completo é publicado por rename; falha não anuncia checksums parciais.
   Git temporário prova commit selecionado e exclusão de working tree sujo.
2. CLI manual: `macip_backup.manual` lê arquivo privado de formato literal
   restrito. Credenciais e SSL_CERT_FILE chegam à CLI e filhos sem depender
   de exports prévios. Testes com ambiente limpo comprovam espaços internos,
   caracteres especiais, ausência de vazamento e recusa segura de formatos
   incompatíveis. Keyring continua disponível para bytes fora do subconjunto;
   não há substituição de comando que apare quebras de linha.
3. Locks: executor, install, uninstall e backup-agent-db.sh usam a mesma
   precedência. Launcher fixa MACIP_LOCK_DIR e install.json registra caminho.
   Layout divergente, relativo ou customizado legado sem identidade gravada
   é recusado antes da manutenção. Testes com executor subprocesso SH e EX
   verificam recusa, preservação de código/banco/config e retomada. O padrão
   continua coberto. Backup SQLite usa lock EX e destino novo 0600.

Prova vermelha com código da base em fixtures isolados: sete inclusões
indevidas e publicação parcial produziram oito falhas esperadas nas duas
regressões de pacote. A regressão de locks falhou porque o instalador antigo
aceitou executar sob lock SH customizado. A prova de credencial somente em
arquivo confirma ausência antes da leitura explícita. Somente dados sintéticos.

Os documentos de instalação/atualização/retorno e piloto usam o fluxo manual
novo e o helper de backup. Não houve mudança no adaptador Restic, API/portal
ou esquema. As provas histórica local, posterior de systemd real e a
limitação de PIDs/namespaces permanecem separadas. Os timers antigos do
laboratório continuam registrados como inactive/disabled por confirmação
do operador, sem nova consulta ao laboratório.

Limites: filtro não é detector universal de segredos; parser manual não é
Bash/dotenv nem aceita todo EnvironmentFile. Layout customizado legado sem
identidade exige procedimento offline específico antes de manutenção.
Docker/SFTP, migração PostgreSQL e atualização real da central não foram
reexecutados. Sem aprovação de produção, Windows, SMB ou S3; sem push/tag/deploy.

Gate novo: `PYTHONPATH=src python3 -m unittest tests.test_cli
tests.test_systemd.UnitGenerationTests tests.test_systemd.AdapterCancellationTests
tests.test_distribution tests.test_install tests.test_credentials tests.test_manual
tests.test_packaging tests.test_maintenance -q`: **116 aprovados, 0 falhos,
0 ignorados**, 419,935 s. Último gate focado de pacote/manual/backup: 9/9 em
31,895 s; pacote+lock focado: 5/5 em 62,254 s. Asserts novos para layouts
customizado, divergente e legado sem identidade passaram. Shells modificados
validados por bash -n; git diff --check limpo. Warnings de sockets nos fixtures
antigos de distribuição continuam observados.

Natureza: Git/empacotador e instalação são reais e descartáveis; prova de
credencial/TLS em filho usa Restic falso e ambiente limpo. A suíte de instalação
usa também Restic local real. HTTP de distribuição/systemd são simulados.
Não se confunde esse gate com homologação histórica ou systemd real anterior.
Artefato definitivo será gerado do commit documental final exato, em diretório
novo dist/linux-0.3.0rc1-review1, preservando o pacote anterior.

Inspeção de sync/distribuição confirmou uso de task_lock_directory também
nos locks SH de manutenção. Gate adicional: `tests.test_outbox.OutboxTestCase.
test_sync_respeita_lock_de_manutencao` e `tests.test_outbox.RearmLockTestCase`:
7/7 aprovados, 33,125 s, com API local sintética e recursos descartáveis.

Smoke de extração do artefato corrigido: controles corrigidos em todas as
fontes, manifesto/hashes, 24 shells, instalação com espaços/custom locks,
identidade instalada, importação/versão/ajuda passaram. Com arquivo de
credenciais como única fonte de senha, backup/check/restore local real e
comparação passaram fora do sandbox. A primeira tentativa extra de restore
no sandbox falhou em lchown (invalid argument), ligada a identidade/permissões;
a repetição fora dele passou sem mudança no adaptador. As duas extrações e
instalações foram limpas automaticamente, inclusive credenciais e repositório.
Comando de prova: `python3 /tmp/macip-review1-verify.py` (helper descartável).
O registro dessa prova é incorporado antes de regenerar o artefato definitivo
em dist/linux-0.3.0rc1-review1-final, sem autorreferência ao SHA final.

## Correção da identidade registrada de manutenção

Base 652ec0e, branch fix/registered-maintenance-lock. O executor ignorava
lock_directory sem override; Python instalado direto podia contornar EX
registrado. Resolvedor único stdlib lock_identity identifica install.json
também pelo prefixo instalado, usa o registro na ausência de override e recusa
divergência/manifesto inválido sem expor conteúdo. Fluxo de desenvolvimento
sem manifesto continua válido. Scripts não copiam mais a regra de validação:
instalação executa o módulo do wheel, uninstall/backup reutilizam módulo.
Regressões cobrem entrada manual pública sob EX registrado, SH em A contra
manutenção, caminhos com espaços, ausência/override divergente, manifesto
inválido, preservação de arquivos e uso sem instalação. Nenhuma mudança de
API, portal ou contrato pertence a esta candidata corrigida.

Gates: CLI/distribuição/manual/identidade/unidades/cancelamento 84/84,
37,812 s. Pacote/instalação/credenciais/backup/identidade: 31 aprovações e
uma falha de restore em lchown no sandbox (32 testes, 324,710 s). A repetição
desse cenário fora do sandbox é necessária; nenhuma asserção foi removida.

Repetição fora do sandbox: restore de instalação, Python instalado/entrada
manual sem override e exclusão mútua customizada: 3/3, 159,757 s. Falha
ambiental original permanece registrada. Pacote desta correção será gerado
de commit final próprio em dist/linux-0.3.0rc1-registered-lock, sem o fluxo
de recuperação pelo portal da entrega seguinte.

## 2026-11-04 — Recuperação assíncrona pelo portal Linux (experimental)

Branch feature/linux-portal-recovery criada a partir de 11a589c, depois de
gerar e validar o pacote corrigido em dist/linux-0.3.0rc1-registered-lock.
O novo fluxo não integra nem amplia a homologação desse pacote. Identificação
de desenvolvimento da distribuição: 0.4.0dev1, não foi gerado release novo.

Migração PG7 expand-only registra operações snapshots/restore, solicitante,
vínculos derivados, configuração imutável/hash, claim, horários e resultado.
Admin+CSRF solicita; viewer lê; agentes só reivindicam/confirmam suas operações.
UUID de intenção e query/snapshot deduplicam restaurações, inclusive respostas
perdidas e término. Estados terminais não regridem. Novo restore deliberado
exige nova consulta. API não executa Restic nem recebe comandos arbitrários.

Agente: fila SQLite separada v1 (principal permanece v7), manutenção e tarefa
sob locks existentes, claim exclusivo, running durável antes do efeito,
interrupção/abandono sem repetição, resultado persistido/reenvio independente.
recovery-poll e recovery-work são comandos públicos separados; worker longo
não executa dentro de pull-tasks/sync. Raiz privada local, pasta UUID nova,
validação de links/sobreposições, catálogo Restic por tag da tarefa, sem init.
Restore --verify e timeout/término de grupo reutilizados; parciais preservados
e nunca apresentados como sucesso. SIGKILL exige conferência de órfãos pelo
operador; política de serviços recomendada KillMode=control-group.

Portal: seleção de destino/ponto, confirmação explícita, estados reais do
agente, erros como texto, polling e proteção de visitas/respostas atrasadas.
Testes: API 180/180 em PostgreSQL17, portal 88/88 + typecheck/build, worker/CLI/
identidade/adaptador 34/34. Playwright real 1/1 em23s com Chrome/API/PG17/Restic:
backup, consulta/solicitação pelo portal, worker, confirmação, conteúdo e
exclusão comparados. Suspensão da API permaneceu; não foi usado systemd real.
Casos negativos de API/Restic são controlados/simulados conforme runbook.

Recursos: contêiner exclusivo macip-recovery-pg17-20261104-01 (PG17), banco
macip_recovery_test, servidores API8180/preview4173/Chrome e diretórios
temporários de agente/repositório/credenciais. O E2E limpa arquivos em finally;
o contêiner e seus volumes são removidos ao fechar a prova. O contêiner PG16
inicial descartável foi removido antes de usar PG17. Nenhuma infraestrutura
operacional ou timer preexistente foi acessado; timers antigos continuam
confirmados inactive/disabled pelo operador. Roteiro e limites em
docs/recuperacao-portal-linux.md; sem aprovação operacional nem push/tag/deploy.

Limpeza concluída: contêiner PG17 e volume anônimo removidos; imagem/tag PG17
baixada para a prova removida. Portas 8180 e 4173 recusam conexão após teardown
do Playwright. Helper temporário do pacote removido; diretórios de extração,
agente e repositório dos testes foram limpos em finally. Pacotes anteriores e
o pacote corrigido 11a589c permanecem preservados em dist.

---

## 2026-11-04 — Correção do bloqueio da fila de recuperação

### Causa do bloqueio

Dois defeitos no processamento da fila de recuperação impediam o progresso
das operações elegíveis:

1. `acquire_task_lock()` era chamado fora do tratamento por operação em
   `recovery.work()`. Uma tarefa ocupada lançava `ResticError` e abortava
   todo o lote, deixando operações de outras tarefas pendentes.
2. Um claim recusado (409) deixava a operação na primeira posição elegível
   (ordem por `rowid`). Com `--limit 1`, três rodadas retornavam
   `{"completed": 0, "deferred": 1}` e a operação B nunca recebia claim.

### Solução

- `acquire_task_lock()` movido para dentro do tratamento por operação.
  Tarefa ocupada adia a operação sem abortar o lote.
- Adiamento persistente por `deferred_until` (esquema v2 da fila SQLite,
  migração transacional v1→v2 preservando registros/eventos). Operações
  adiadas são excluídas da seleção, então não monopolizam rodadas seguidas.
- Classificação local de bloqueio: tarefa ocupada e falha transitória
  (0/429/5xx) adiam por `DEFERRED_SECONDS` (30s); conflito definitivo de
  claim (409), resposta ambígua e contrato inválido bloqueiam localmente
  (`deferred_until = _BLOCKED`).
- API: `GET /agent/recovery` lista pendentes antes de running para que um
  claim ocupado não esconda operações novas na janela de descoberta (100).
- `--limit` = número máximo de operações selecionadas por rodada para
  avaliação (lock + claim), não trabalho executado.

### Arquivos alterados

- `src/macip_backup/recovery.py`: adiamento persistente, classificação de
  bloqueio, migração de esquema, `_now`/`DEFERRED_SECONDS`/`_BLOCKED`.
- `api/src/macip_backup_api/recovery_routes.py`: ordem de listagem remota.
- `tests/test_recovery_worker.py`: 9 regressões (cenários A e B, limite 1,
  reinício, liberação posterior, falha transitória, resposta ambígua,
  ausência de elegíveis).
- `api/tests/test_recovery.py`: regressão de listagem pendente-antes-running.
- `docs/recuperacao-portal-linux.md`: política de adiamento/conflito,
  significado de `--limit`, persistência, diagnóstico.
- `registro-trabalho.md`: esta entrada.

### Migração

Fila SQLite de recuperação v1→v2: `ALTER TABLE recovery_queue ADD COLUMN
deferred_until INTEGER NOT NULL DEFAULT 0`. Transacional, preserva
registros/eventos. SQLite principal não alterado.

### Regressões que falhavam antes e passaram depois

- `test_busy_task_defers_without_blocking_eligible_operation`
- `test_busy_task_with_limit_one_progresses_across_successive_rounds`
- `test_claim_conflict_does_not_monopolize_first_position`
- `test_restart_between_rounds_preserves_deferral`
- `test_busy_task_runs_again_after_deferral_expires_and_lock_released`
- `test_transient_communication_failure_stays_recoverable`
- `test_ambiguous_claim_response_starts_no_restic_effect`
- `test_no_eligible_operations_terminates_quickly`
- `test_pending_operations_are_listed_before_running` (API/PG)

### Comandos e resultados

- Worker/CLI/lock: `.venv/bin/python -m unittest tests.test_recovery_worker
  tests.test_lock_identity tests.test_cli` → 33/33 OK.
- Gate Python completo: 262 testes, 2 erros pré-existentes (setuptools
  ausente para build_wheel, pytest ausente para test_e2e_distribution),
  20 skipped. Nenhuma falha das alterações.
- API/PG17 real: `MACIP_API_TEST_DATABASE_URL=... PYTHONPATH=src
  ../.venv-api/bin/python -m pytest tests` → 181/181 OK (199,77s).
- Migração v1→v2 com banco preenchido: 3 registros preservados,
  `deferred_until=0`, eventos preservados.

### Integração real

E2E Playwright (portal/e2e/recovery.spec.ts) não reexecutado nesta sessão:
exige Chrome + API + PG17 + Restic reais em conjunto. Infraestrutura PG17
descartável estava disponível, mas o E2E completo exige levantar API, portal
e Chrome simultaneamente. Os testes de API/PG17 (181) e worker/CLI (33)
cobrem os contratos alterados. Não houve substituição de prova real por
simulação: o E2E foi simplesmente não reexecutado, não substituído.

### Recursos descartáveis e limpeza

Contêiner `macip-api-test-pg` (PG17, porta 55432, banco macip_api_test)
reutilizado para testes de API. Permanece ativo para sessões futuras; não
foi criado novo contêiner nesta sessão (tentativa de criar um novo foi
removida). Nenhum dado operacional acessado.

### Branch e SHA

Branch: `fix/recovery-queue-progress` (base: `59f640a`).
SHA: ver `git log` ao final.

### Limitações e pendências

- E2E Playwright não reexecutado (ver acima).
- `DEFERRED_SECONDS=30` é constante; não configurável por operação.
- `_BLOCKED` é marcador local; nenhuma UI de diagnóstico no portal.
