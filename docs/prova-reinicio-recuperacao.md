# Prova de persistência após reinício do PC — ainda não executada

Somente o operador executa este roteiro, em janela escolhida. Não é teste
de SIGKILL, queda de energia ou recuperação de running abandonado. Objetivo:
um restore **succeeded local, sent=0** sobreviver ao reboot e ser confirmado
por poll, sem repetir worker. Use apenas o laboratório ea74d5d.

## 1. PC — identificar e impedir automação inesperada

Execute como estagiario01. Não use sudo nos comandos do agente. Em cada
terminal novo, inclusive depois do reboot, redefina este bloco:

```bash
umask 077
unset PYTHONPATH PYTHONHOME
AREA='/home/estagiario01/macip-homolog-recuperacao-ea74d5d'
export MACIP_HOME="$AREA/programa" MACIP_BIN="$AREA/bin"
export MACIP_CONFIG="$AREA/config" MACIP_STATE="$AREA/estado"
export MACIP_CACHE="$AREA/cache" MACIP_LOCK_DIR="$AREA/locks"
PY="$MACIP_HOME/current/bin/python"
POLL='macip-recuperacao-ea74d5d-poll'
WORK='macip-recuperacao-ea74d5d-work'
"$PY" -m macip_backup.cli --version
systemctl --user show "$POLL.timer" "$WORK.timer" \
  -p Id -p ActiveState -p UnitFileState
```

enabled/inactive significa parado agora, mas habilitado para próxima sessão.
disabled/inactive impede a ativação por habilitação persistente. Não impede
start manual nem outras units: confira também os serviços e não os dispare.
Registre os estados anteriores; depois desabilite **somente estes timers**:

```bash
systemctl --user disable --now "$POLL.timer" "$WORK.timer"
systemctl --user show "$POLL.timer" "$WORK.timer" \
  -p Id -p ActiveState -p UnitFileState
systemctl --user show "$POLL.service" "$WORK.service" \
  -p Id -p ActiveState -p SubState -p MainPID -p ControlGroup
```

Exija disabled/inactive nos dois timers, serviços inactive e MainPID=0.
Disable do timer não encerra um oneshot já iniciado: se houver execução,
aguarde conclusão e reveja a fila. Não interrompa uma operação para preparar
este teste. Confira os cgroups próprios (se ainda existirem):

```bash
systemd-cgls --user-unit "$POLL.service" --no-pager
systemd-cgls --user-unit "$WORK.service" --no-pager
df -h "$AREA/estado" "$AREA/recuperacoes"
```

Ausência de cgroup parado pode ser normal; erro de permissão/namespace que
impeça provar ausência de processos torna o preflight inconclusivo. Não mate
processos por glob/pgrep. Verifique que não há wrapper/worker manual desse
laboratório em outro terminal. Reserve espaço para a recuperação nova
(mais de 512 MiB, margem recomendada 2 GiB) e pequenas cópias dos bancos.
Não use a mesma pasta de uma restauração anterior.

Confira install.json, lock e política **sem ler agent.env**:

```bash
"$PY" - "$AREA" <<'PY'
import json, sys
from pathlib import Path
from macip_backup.agent import task_lock_directory
a = Path(sys.argv[1]); m = json.loads((a/'programa/install.json').read_text())
assert m['commit'] == 'ea74d5d5bf8792b374addbc6addf98345c3327a7'
assert m['version'] == '0.2.1'
assert Path(m['lock_directory']).resolve() == (a/'locks').resolve() == task_lock_directory()
p = json.loads((a/'config/recovery.json').read_text())
assert p == {'restore_root': str(a/'recuperacoes')}
central = json.loads((a/'config/central.json').read_text())
assert central['url'] == 'https://localhost:18445'
assert central['agent_id'] == 'dd7d5bae-4717-408c-9e57-b9f4db63aed2'
assert central['token_environment'] == 'MACIP_RECOVERY_API_TOKEN'
print('Identidade e política conferidas')
PY
```

Identifique o arquivo privado efetivamente usado por executar.sh, **sem cat
ou source**. Defina somente seu caminho (substitua antes de usar):

```bash
CREDENTIALS='/caminho/exato/do/arquivo/privado/usado/pelo/wrapper'
manual() { "$PY" -m macip_backup.manual --env-file "$CREDENTIALS" -- "$@"; }
stat -c '%a %U' -- "$CREDENTIALS"
```

O arquivo já deve fornecer MACIP_RECOVERY_API_TOKEN,
MACIP_RECOVERY_RESTIC_PASSWORD e SSL_CERT_FILE. Não regrave valores no shell.
Exija dono estagiario01 e modo 0600 ou 0400 no arquivo privado; o parser
manual documentado recusa formatos incompatíveis. Não imprima seu conteúdo.
Sem credenciais/TLS provisionados, interrompa o roteiro e corrija localmente.

## 2. PC — fila limpa e nova operação real

Leia a fila por SQLite somente leitura, sem chamar _database() (que migra).
Não imprima payload, claim ou corpo do evento:

```bash
"$PY" - "$AREA/estado/recovery.db" <<'PY'
import sqlite3, sys
from pathlib import Path
with sqlite3.connect(Path(sys.argv[1]).resolve().as_uri()+'?mode=ro', uri=True) as d:
    assert d.execute('PRAGMA user_version').fetchone()[0] == 3
    print(d.execute('SELECT state,sent,count(*) FROM recovery_queue GROUP BY state,sent').fetchall())
    assert d.execute("SELECT count(*) FROM recovery_queue WHERE state IN ('pending','running') OR sent<>1").fetchone()[0] == 0
PY
```

Se houver operação anterior pendente, running, bloqueada ou evento não
enviado, resolva em outra janela antes desta prova; não edite linhas.

1. Reconecte o túnel da seção 5 se necessário. Portal: tarefa
   df9f5cee-66ea-438d-9656-224b41be4a95, destino local-recuperacao, revisão 1.
2. Faça **nova consulta** pelo portal. Execute manualmente poll → work → poll
   para concluir somente a consulta (comandos abaixo). Confira catálogo real.
3. Selecione o snapshot 350165be… de 512 MiB, confirme uma **nova restauração**.
   Anote o UUID novo exibido no portal; não reutilize os UUIDs históricos.
4. Execute **poll uma vez** para receber a intenção. Confirme na leitura
   somente leitura que há exatamente uma pending, do UUID anotado, e nenhum
   running/evento não enviado anterior. Então execute **work uma vez**.

```bash
manual recovery-poll --central "$AREA/config/central.json" --queue "$AREA/estado/recovery.db"
manual recovery-work --central "$AREA/config/central.json" --queue "$AREA/estado/recovery.db" \
  --policy "$AREA/config/recovery.json" --limit 1
```

Na etapa 2, execute o poll de confirmação da consulta. **Na etapa 4, não
execute poll após o worker da restauração.** Essa é a fronteira da prova.
completed:1/exit 0 não bastam: exija row succeeded, event succeeded e sent=0.
Se deferred/retry/blocked ou interrupted/failed, não prossiga ao reboot.
Confirme a pasta nova e diff dos arquivos não excluídos; ignorar.tmp ausente.

## 3. PC — evidência consistente antes/depois, sem alterações na fila

Defina UUID e um diretório de evidência novo **fora da raiz recuperacoes**:

```bash
OP='SUBSTITUA-PELO-UUID-NOVO-DA-RESTAURACAO'
EVID="$AREA/evidencias-reinicio-$(date +%Y%m%dT%H%M%S)"
mkdir -m 700 "$EVID"
PHASE=before
```

Guarde OP, EVID e o caminho CREDENTIALS por referência segura para o retorno;
não guarde valores secretos. Execute o mesmo bloco abaixo antes e depois do
reboot, mudando somente PHASE. Ele usa backup SQLite online consistente
(inclui commits presentes em WAL), lock EX existente e não imprime eventos.
Não copie agent.db/recovery.db ativos por cp, nem exclua WAL/SHM/locks.

```bash
"$PY" - "$AREA" "$EVID" "$OP" "$PHASE" <<'PY'
import fcntl, hashlib, json, os, sqlite3, sys, uuid
from pathlib import Path
from macip_backup.agent import task_lock_directory
a, evidence = map(Path, sys.argv[1:3]); op, phase = sys.argv[3:5]
assert str(uuid.UUID(op)) == op and phase in ('before','after','sent')
assert evidence.is_dir() and not evidence.is_symlink()
assert not evidence.resolve().is_relative_to((a/'recuperacoes').resolve())
def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()
with (task_lock_directory()/'maintenance.lock').open('r+') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    rows = None
    for name, version in [('recovery.db',3), ('agent.db',7)]:
        source = a/'estado'/name; target = evidence/(phase+'-'+name)
        assert source.is_file() and not target.exists()
        with sqlite3.connect(source.resolve().as_uri()+'?mode=ro', uri=True) as src:
            with sqlite3.connect(target) as dst:
                src.backup(dst)
                assert dst.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
                assert dst.execute('PRAGMA user_version').fetchone()[0] == version
                if name == 'recovery.db':
                    rows = dst.execute('SELECT id,bond,state,sent,event FROM recovery_queue').fetchall()
    selected = [r for r in rows if r[0] == op]; assert len(selected) == 1
    row = selected[0]; event = json.loads(row[4])
    assert row[1].startswith('b73f475a0294f140')
    assert row[2] == event['state'] == 'succeeded'
    assert row[3] == (1 if phase == 'sent' else 0)
    assert not any(r[2] in ('pending','running') for r in rows)
    assert not any(r[0] != op and r[3] != 1 for r in rows)
    folder = a/'recuperacoes'/op
    assert Path(event['result']['target']) == folder and folder.resolve() == folder
    assert folder.is_dir() and not folder.is_symlink()
    entries = {}
    for path in folder.rglob('*'):
        assert not path.is_symlink(), 'link exige revisão manual desta prova'
        if path.is_file():
            s = path.stat()
            entries[str(path.relative_to(folder))] = [s.st_size, s.st_ino, s.st_mtime_ns, digest(path)]
    assert entries and not any(Path(n).name == 'ignorar.tmp' for n in entries)
    facts = {'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
             'op':op, 'state':row[2], 'sent':row[3], 'bond':row[1],
             'event_sha256':hashlib.sha256(row[4].encode()).hexdigest(),
             'install':digest(a/'programa/install.json'), 'policy':digest(a/'config/recovery.json'),
             'central':digest(a/'config/central.json'),
             'wrapper':digest(a/'executar.sh'), 'locks':str(task_lock_directory()), 'files':entries}
    facts['python_prefix'] = str(Path(sys.prefix).resolve())
    output = evidence/(phase+'.json'); assert not output.exists()
    if phase != 'before':
        previous = json.loads((evidence/'before.json').read_text())
        assert facts['boot_id'] != previous['boot_id'], 'reinício não comprovado'
        assert {k:v for k,v in facts.items() if k not in ('boot_id','sent')} == {k:v for k,v in previous.items() if k not in ('boot_id','sent')}
    output.write_text(json.dumps(facts, indent=2)+'\n'); output.chmod(0o600)
    print('Evidência consistente:', phase, op, row[2], row[3], 'arquivos:', len(entries))
PY
```

As cópias podem conter metadados operacionais: mantenha-as privadas, fora do
Git/pacote; não envie bancos ou credenciais na resposta. Falha em assert/lock
exige diagnóstico; preserve as evidências parciais, não sobrescreva arquivos.
Registre também os estados systemd da seção 1 e o horário local antes/depois.

## 4. PC — reinício MANUAL, sem execução do worker no retorno

Reconfirme timers disabled/inactive, serviços inactive, nenhuma operação
running, before.json gravado e fila succeeded/sent=0. Escolha a janela e
reinicie o PC manualmente pela interface do sistema. Este documento não
automatiza reboot, shutdown, logout nem restart do gerenciador de usuário.

Depois de entrar novamente: não abra executar.sh, não rode worker, não
habilite timers. Refaça bloco de variáveis, OP/EVID/CREDENTIALS e função
manual. Confira systemd outra vez. Se algum serviço/timer já processou a
fila, registre e classifique esta prova como inconclusiva.

Defina PHASE=after e repita somente o bloco de evidência da seção 3, **antes
do túnel/poll**. Exija boot_id novo, mesmo evento, identidade, lock, política,
wrapper e arquivos, succeeded/sent=0. Se mudou o relógio/atributo relevante,
investigue antes de concluir; não altere o comparador para obter aprovação.

## 5. PC — reconstruir comunicação e enviar sem restaurar

Em terminal separado do PC, como o mesmo usuário, reconstrua o túnel:

```bash
ssh -N -o ExitOnForwardFailure=yes \
  -L 127.0.0.1:18445:127.0.0.1:8445 estagio01@10.10.20.239
```

Confira host key pela referência já autorizada; nunca desabilite verificação.
Falha de bind indica porta ocupada: investigue, não mate processos arbitrários.
Valide TLS/hostname com a CA pública (sem -k):

```bash
curl --fail --silent --show-error \
  --cacert '/home/estagiario01/macip-recuperacao-acesso-ea74d5d/lab-ca.pem' \
  'https://localhost:18445/readyz'
manual recovery-poll --central "$AREA/config/central.json" --queue "$AREA/estado/recovery.db"
```

Exija sent:1, blocked:0, retry:0 no contexto isolado. received pode ser zero
ou indicar descoberta; **não rode worker**. Refaça evidência com PHASE=sent:
mesmo evento/arquivos e sent=1. No portal, atualize a página e confira o UUID
novo, snapshot, pasta e “Concluída pelo agente”. Opcional: novo poll sem
worker deve enviar zero desse evento. Não faça nova solicitação para testar
o reenvio: ela mudaria a prova.

## VM — observação pelo operador, sem mudanças

A VM e sua central não reiniciam nesta prova. Pelo portal via túnel, registre
estado running/aguardando confirmação antes do envio (não sucesso antecipado)
e succeeded após o poll. Se indispensável, o operador pode conferir saúde no
diretório/projeto exclusivos já informados. Não é necessário executar Compose,
migração, deploy ou restaurar bancos para esta prova. Não toque na central
8443, no piloto ou nos volumes. O assistente não executa acesso à VM.

## Aprovação, falha, inconclusão e encerramento

- **Aprovada**: UUID/evento/arquivos e identidades iguais, boot_id diferente,
  succeeded/sent=0 antes e após reboot; só poll muda sent para1; portal confirma;
  nenhuma chamada ao worker após a preparação; logs/cgroups próprios corroboram
  ausência de execução extra e comparação de conteúdo/exclusão preservada.
- **Falha**: perda/alteração da fila/evento, reexecução, pasta nova inesperada,
  estado falso de sucesso, mudança de identidade ou dados inconsistentes.
- **Inconclusiva**: antes já sent=1, operação errada, timers ativos no retorno,
  ausência de evidência de boot/processos, sem envio por túnel/TLS/credencial
  indisponível. Guarde dados; não fabrique estados SQL nem declare aprovação.

Mantenha os dois timers disabled/inactive até decisão explícita do operador.
Não remova fila, backups de evidência, repositório ou pastas parciais/recuperadas.
Uma retomada automática futura é ação separada, com nomes exatos e registro.
Abandono de running após queda abrupta/SIGKILL exige ensaio distinto, inspeção
de órfãos e política de cgroups; não é comprovado pelo reboot gracioso deste teste.
