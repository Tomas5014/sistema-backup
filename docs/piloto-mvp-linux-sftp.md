# Piloto controlado de 7 dias — MVP Linux com destino SFTP e recuperação pelo portal

Documento de **preparação** (revisado após revisão independente). Nenhuma
etapa deste piloto foi iniciada ou aprovada. Todos os valores de
dimensionamento são **propostas** pendentes de decisão do operador; nada
aqui declara o piloto executado.

Código alvo da preparação: `ea74d5d5bf8792b374addbc6addf98345c3327a7`
(distribuição experimental 0.4.0dev1, agente 0.2.1, PG migrações 1–7,
SQLite principal v7, fila de recuperação v3). Se o operador escolher outro
commit, este documento deve ser revisado antes do início.

Recorte proposto: **Linux, origens locais, destino SFTP, gestão e
acompanhamento centralizados e recuperação pelo portal**. Windows, SMB e
S3 não fazem parte deste piloto; o recorte não equivale ao cumprimento do
produto completo.

## 0. Convenções deste roteiro

- Comandos marcados **[PC]** (máquina do agente) ou **[VM]** (servidor
  SFTP/central). Nomes marcados como **proposta** esperam aprovação.
- Um único arquivo de credenciais: `$MACIP_CONFIG/agent.env`. É o
  **mesmo caminho** que as units geradas pelo produto carregam —
  `EnvironmentFile=-$MACIP_CONFIG/agent.env` é o que `systemd.py` escreve
  (`_environment_file_line`). Com o layout proposto, resolve para
  `$AREA/config/agent.env`. Nenhuma cópia extra de credenciais.
- O parser literal `macip_backup.manual` exige dono atual, modo
  0600/0400, `KEY=value` UTF-8 e **recusa** as variáveis de layout
  (`MACIP_HOME`, `MACIP_BIN`, `MACIP_CONFIG`, `MACIP_STATE`,
  `MACIP_CACHE`, `MACIP_LOCK_DIR`) — credenciais não recebem variáveis de
  layout.
- Nomes reais gerados pelo produto (vistos em `systemd.py`):
  `macip-backup-managed-<task_id>` (tarefa gerenciada),
  `macip-backup-pull-<hash16>` (schedule-pull),
  `macip-backup-sync-<hash16>` (schedule de sync),
  `macip-backup-agent-<task_id>` (schedule local avulso). Os timers de
  recuperação são criados **pelo operador** — o produto não os instala —
  com nomes exclusivos escolhidos (proposta: `macip-mvp-poll`,
  `macip-mvp-work`).
- `inactive` ≠ `disabled`; `completed` do worker ≠ `succeeded` da
  operação; `sent` no histórico pode permanecer como registro.

## 1. Matriz de aceite do MVP Linux

| # | Requisito mínimo | Implementação existente | Evidência já disponível | Evidência que o piloto deve produzir | Critério de aprovação |
|---|---|---|---|---|---|
| 1 | Cadastro e publicação | Portal + API de revisões | Homologação ea74d5d | Tarefa SFTP nova publicada, revisão conferida antes de aplicar | IDs de tarefa/cliente/agente exportados pela central conferidos |
| 2 | Preparação sem ativação | `pull-tasks --no-activate` (staging em `tasks.d/<id>/staged/`) | Código + testes | Artefatos preparados inspecionados antes de qualquer ativação | `config-<gen>.json` + units em staging; `pull-status` não marca aplicado |
| 3 | Backup inicial controlado | `agent` manual sobre a configuração candidata da central | Homologação ea74d5d | Execução `success` + `check --read-data` | `success` no histórico local, `sent` após sync |
| 4 | Aplicação autorizada | `pull-tasks` normal (transição completa, confirma) | Código + testes | Revisão/geração aplicadas e confirmadas | `pull-status` applied; timer gerenciado enabled |
| 5 | Backup agendado SFTP | Timer `macip-backup-managed-<task_id>` OnCalendar+Persistent | Homologação de distribuição | Todas as ocorrências agendadas evidenciadas | Cada ocorrência esperada tem execução registrada (ver 6.2) |
| 6 | Histórico central | Outbox + `sync` (ou timer `macip-backup-sync-*`) | Envio durável homologado | Resultados visíveis no portal por execução | Sem pendentes/bloqueados na outbox; conjunto local = conjunto na central |
| 7 | Consulta dos pontos | `snapshots` + consulta no portal | Homologação ea74d5d | Conjunto filtrado da tarefa comparado no mesmo instante | Mesma coleção task+destino, com instante da consulta registrado |
| 8 | Restauração pelo portal | Fila recovery.db v3, poll/work | Homologação ea74d5d + reboot | ≥2 restaurações em dias distintos, comparadas a referência imutável | `succeeded`/sent=1 no portal; conteúdo = manifesto da geração |
| 9 | Persistência local | Outbox, fila v3, journals | Reboot + offline homologados | Indisponibilidades reais do período registradas | Nenhum resultado perdido; envio somente por poll/sync |

### 1.1 Propostas de dimensionamento (não aprovadas)

| Parâmetro | Proposta | Observação |
|---|---|---|
| Duração | 7 dias corridos | Janela D1–D7, primeira ocorrência agendada observada como D1 |
| Frequência | `daily` (horário escolhido pelo operador) | Publicada na central; `Persistent=true` cobre disparo com máquina desligada |
| Retenção | `keep_last: 7` | Ensaio de retenção separado (5.3) com volume próprio |
| Perfil de dados | Sintético gerado por script (3.5): pequenos, 2 grandes ≥256 MiB, 8 níveis, espaços/acentos, `*.tmp` excluídos | Volume funcional de laboratório — **não** representa volume de clientes |
| Alteração diária | ~5% modificados + 1 novo + 1 removido (script `mutar-dia.sh`) | Incrementalidade + exclusão exercitadas |
| Detecção de ausência | Mesma rotina diária verifica a ocorrência do dia | Não há alerta automático (lacuna registrada em 6.3) |
| Limites temporais (propostas) | início ≤30 min após OnCalendar; execução ≤60 min; envio na próxima rodada de sync/poll; indisponibilidade agente/central registrada como inconclusivo | Ajustar antes de aprovar |
| Margem de armazenamento | Repositório ≤50% do livre no destino; origem+recuperações ≤50% no PC | Última leitura da VM (~9 GiB) é obsoleta — medir no preflight |

Correção vs desempenho: correção (integridade, exclusões, conteúdo)
decide aprovação; durações só alimentam registro e decisões futuras.

## 2. Arquitetura operacional

### 2.1 Opção recomendada

- **Origem**: máquina Linux dedicada ligada durante o piloto, com
  `systemd --user` entre sessões. `loginctl enable-linger` é decisão do
  operador — sem linger, timers --user morrem no logout e a agenda fica
  inconclusiva.
- **Destino SFTP**: outra máquina (ex.: `srvdev1`). Conta e repositório
  **exclusivos e novos** — proposta: usuário `macipsftpmvp` com
  `ForceCommand internal-sftp` (que **não** é chroot), somente chave
  pública. `remote_path` **relativo** `repositories/mvp` — o adaptador
  monta `sftp:macipsftpmvp@<host>:repositories/mvp`, que o restic resolve
  **relativo ao diretório inicial SFTP**
  (= `/home/macipsftpmvp/repositories/mvp`). Caminho absoluto
  (`/repositories/mvp`) apontaria para a raiz do servidor — **não usar**,
  salvo se o provisionamento criar explicitamente esse absoluto.
  `macipsftppiloto` e `/home/macipsftppiloto/repository` ficam
  preservados; chaves anteriores também.
- **Identidade SSH**: chave Ed25519 dedicada
  (`$AREA/.ssh/macip-mvp_ed25519`) + `known_hosts` próprio com
  fingerprint conferido por canal seguro (`ssh-keygen -lf` no servidor).
  O agente já usa `BatchMode`, `StrictHostKeyChecking=yes`,
  `IdentitiesOnly`, somente chave pública, `-i` e `-p` fixos.
- **Senha Restic** sob custódia do operador, fora da origem, entregue só
  via `agent.env` (por nome de variável). Nunca em argv/log/histórico.
- **Token da API** em variável **separada** da senha Restic
  (`token_environment` nomeia a variável; `token` literal é recusado).
- **Central**: instância exclusiva do piloto ou a central de recuperação
  existente (`8445`) — **decisão do operador**. Se reutilizada: agente e
  vínculo novos; nunca o agente do laboratório congelado.
- **Consequências**: suspensão na central desabilita o timer gerenciado
  sem reativar; logout sem linger para timers; reboot com
  `Persistent=true` dispara rodada atrasada na ativação seguinte —
  **fronteira**: o primeiro backup automático só pode ocorrer depois da
  aplicação concluída (4.4), no próximo OnCalendar ou em recuperação de
  disparo vencido; perda do túnel/rede deixa envio em `retry`, sem perda.

### 2.2 Dependência declarada

Se o piloto usar o PC e o túnel SSH manual para 18445 por limitação de
laboratório: **dependência e limite do resultado**. O túnel morre em
logout/queda; enquanto fechado, a central fica inacessível (backups
locais seguem, envio fica `retry`). Esse modo prova só o ciclo funcional,
não a arquitetura-alvo. Registrar no início.

### 2.3 Decisões pendentes do operador (não executáveis)

1. Máquina de origem definitiva (dedicada vs PC de laboratório).
2. Central exclusiva vs reutilização da 8445.
3. Provisionar conta SFTP nova + diretório `repositories/mvp`; medir
   espaço livre real.
4. Fingerprint da host key por canal seguro.
5. Conteúdo de `agent.env` (valores reais sob custódia).
6. Horário da agenda e `enable-linger`.
7. UUIDs de cliente/agente/tarefa gerados no cadastro; nomes exclusivos.
8. Aprovação das propostas da seção 1.1.

### 2.4 Preservação obrigatória

Laboratórios congelados (PC e VM), central 8443, laboratório 8444,
`macip-backup-agent-piloto-local.timer`, backup diário da central, conta
e chaves `macipsftppiloto`, evidências anteriores — nada disso é tocado.
`macip-recuperacao-ea74d5d-{poll,work}.timer` seguem disabled/inactive.
Nenhuma atualização silenciosa da instalação congelada.

## 3. Ambiente e preflight

### 3.1 Requisitos do HOST [PC] — antes de instalar

```bash
umask 077
python3 --version          # >= 3.11
python3 -m venv --help >/dev/null
restic version             # 0.18.x
ssh -V
systemctl --user is-system-running || true
systemd-analyze calendar 'daily' | head -3
df -h "$HOME" | tail -1
```

Parada: python/restic/systemd --user indisponível, espaço abaixo da
margem. Inconclusivo: `systemctl --user` sem sessão persistente — decidir
linger antes.

**[VM]** (pelo operador):

```bash
df -h /home
getent passwd macipsftpmvp || echo "conta ainda não existe — provisionar"
sshd -t 2>/dev/null; ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub
```

O fingerprint do host é anotado aqui e conferido por canal seguro.

### 3.2 Definir área e recusar conflitos [PC]

```bash
AREA='/home/estagiario01/macip-piloto-mvp-linux'
export MACIP_HOME="$AREA/programa" MACIP_BIN="$AREA/bin"
export MACIP_CONFIG="$AREA/config" MACIP_STATE="$AREA/estado"
export MACIP_CACHE="$AREA/cache" MACIP_LOCK_DIR="$AREA/locks"
mkdir -m 700 -p "$AREA" "$MACIP_CONFIG" "$MACIP_STATE" \
  "$MACIP_CACHE" "$MACIP_LOCK_DIR" "$AREA/origem" \
  "$AREA/recuperacoes" "$AREA/evidencias" "$AREA/.ssh"

# Units existentes (mesmo inactive/failed) pertencentes ao piloto:
for u in "$HOME/.config/systemd/user/"macip-backup-*.{service,timer} \
         "$HOME/.config/systemd/user/"macip-mvp-*.{service,timer}; do
  [ -e "$u" ] && grep -L "macip-backup: install=$MACIP_HOME" "$u" \
    && echo "CONFLITO: $u existe e não é desta instalação"
done; true
```

Esperado: nenhum CONFLITO. `systemctl status` **não** prova inexistência —
a checagem é por arquivo e marcador de instalação.

### 3.3 Instalação isolada de versão exata [PC]

```bash
# 1) Conferir o wheel ANTES de instalar: nome + sha256 + manifesto
WHEEL='/caminho/macip_backup_poc-0.2.1-py3-none-any.whl'
sha256sum "$WHEEL"          # comparar com o sha256 publicado do artefato
python3 - <<PY
import zipfile, sys
with zipfile.ZipFile("$WHEEL") as w:
    meta = [n for n in w.namelist() if n.endswith('METADATA')][0]
    head = w.read(meta).decode().splitlines()[:5]
print(head)
PY

# 2) Instalar na área exclusiva (install.sh honra MACIP_* já exportados)
bash deploy/install.sh "$WHEEL" --commit ea74d5d5bf8792b374addbc6addf98345c3327a7

# 3) Conferir identidade instalada
PY="$MACIP_HOME/current/bin/python"
"$PY" -m macip_backup.cli --version
"$PY" - "$MACIP_HOME/install.json" <<'PY'
import json, sys
m = json.loads(open(sys.argv[1]).read())
print(m['commit'], m['version'])
assert m['commit'] == 'ea74d5d5bf8792b374addbc6addf98345c3327a7'
assert m['version'] == '0.2.1'
PY
```

Parada: sha256 divergente, METADATA/commit divergentes → gerar artefato
de commit exato (`docs/candidata-linux.md`); nunca improvisar build.

### 3.4 Credenciais — um único arquivo [PC]

```bash
CREDENTIALS="$MACIP_CONFIG/agent.env"     # mesmo arquivo do EnvironmentFile
manual() { "$PY" -m macip_backup.manual --env-file "$CREDENTIALS" -- "$@"; }
stat -c '%a %U' "$CREDENTIALS"            # exige 600/400 e dono atual
grep -cE '^[A-Za-z_][A-Za-z0-9_]*=.+$' "$CREDENTIALS"   # conta chaves, sem valores
```

Conteúdo (provisionado pelo operador, nunca transcrito): token da API,
senha Restic, `SSL_CERT_FILE` se CA privada — somente `KEY=value`.
Prova de unicidade: `manual` carrega `$MACIP_CONFIG/agent.env`; as units
escritas pelo produto carregam `EnvironmentFile=-$MACIP_CONFIG/agent.env`
— mesmo caminho absoluto, nenhuma cópia. Parada: parser recusa formato,
permissão ou dono → corrigir antes de prosseguir.

### 3.5 Configurações e dados sintéticos [PC]

`$MACIP_CONFIG/central.json` (sem `token`; HTTPS fora de localhost):

```json
{"url":"https://localhost:18445","agent_id":"<uuid-agente-piloto>",
 "token_environment":"MACIP_MVP_TOKEN"}
```

`$MACIP_CONFIG/recovery.json` (raiz privada de restaurações):

```json
{"restore_root":"/home/estagiario01/macip-piloto-mvp-linux/recuperacoes"}
```

```bash
mkdir -m 700 -p "$AREA/recuperacoes"
"$PY" - "$AREA/recuperacoes" <<'PY'
import json, sys
from pathlib import Path
root = Path(sys.argv[1]); assert root.is_dir() and not root.is_symlink()
assert root.resolve().is_relative_to(Path.home().resolve())
p = json.loads(Path(__import__('os').environ['MACIP_CONFIG'],'recovery.json').read_text())
assert Path(p['restore_root']).resolve() == root.resolve()
print('restore_root validado:', root)
PY
```

`$MACIP_CONFIG/tarefa-mvp.json` — **rascunho apenas para validar
o formato com `validate` (abaixo); a configuração efetiva da tarefa é a
publicada na central e preparada em `tasks.d/` (4.2), nunca este
rascunho**:

```json
{"id":"task-uuid","name":"Piloto MVP Linux","client":{"id":"cli-1","name":"Cliente"},
 "device_id":"dev-1","sources":["/home/estagiario01/macip-piloto-mvp-linux/origem"],
 "exclusions":["*.tmp"],
 "destinations":[{"id":"sftp-mvp","name":"SFTP MVP","kind":"sftp",
   "sftp":{"host":"srvdev1","port":22,"user":"macipsftpmvp",
     "remote_path":"repositories/mvp",
     "identity_file":"/home/estagiario01/macip-piloto-mvp-linux/.ssh/macip-mvp_ed25519",
     "known_hosts_file":"/home/estagiario01/macip-piloto-mvp-linux/.ssh/known_hosts",
     "connect_timeout_seconds":15},
   "password_environment":"MACIP_MVP_RESTIC_PASSWORD",
   "retention":{"keep_last":7}}]}
```

Dataset reproduzível (gera origem + manifesto da geração):

```bash
gerar_dados() {  # $1=origem $2=manifesto $3=seed
  "$PY" - "$1" "$2" "$3" <<'PY'
import hashlib, os, random, sys
from pathlib import Path
src, man, seed = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
r = random.Random(seed)
src.mkdir(parents=True, exist_ok=True)
# 200 pequenos em árvore de 8 níveis; nomes com espaço/acento
niveis = [src]
for i in range(8):
    novo = niveis[-1] / f"nivel {i} ação"
    novo.mkdir(exist_ok=True); niveis.append(novo)
for i in range(200):
    (niveis[r.randrange(len(niveis))] / f"arquivo {i:03d} ção.txt").write_text(
        f"gen {seed} arquivo {i}\n" * r.randrange(1, 50))
for nome in ("grande-a.bin", "grande-b.bin"):
    (src / nome).write_bytes(os.urandom(268435456))
(src / "ignorar.tmp").write_text("nunca deve entrar no snapshot")
# manifesto imutável: hash + caminho relativo, fora da origem
linhas = []
for p in sorted(src.rglob("*")):
    if p.is_file() and p.suffix != ".tmp":
        linhas.append(f"{hashlib.sha256(p.read_bytes()).hexdigest()}  ./{p.relative_to(src)}")
Path(man).write_text("\n".join(linhas) + "\n")
print(f"gen {seed}: {len(linhas)} arquivos no manifesto {man}")
PY
}
mutar_dia() {  # $1=origem $2=manifesto $3=dia
  "$PY" - "$1" "$2" "$3" <<'PY'
import hashlib, os, random, sys
from pathlib import Path
src, man, dia = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
r = random.Random(f"mut-{dia}")
files = [p for p in src.rglob("*") if p.is_file() and p.suffix != ".tmp"]
for p in r.sample(files, max(1, len(files) // 20)):
    p.write_text(p.read_text() + f"mutacao dia {dia}\n")
(src / f"novo-dia-{dia}.txt").write_text(f"criado no dia {dia}\n")
vitima = r.choice(files); vitima.unlink()
linhas = []
for p in sorted(src.rglob("*")):
    if p.is_file() and p.suffix != ".tmp":
        linhas.append(f"{hashlib.sha256(p.read_bytes()).hexdigest()}  ./{p.relative_to(src)}")
Path(man).write_text("\n".join(linhas) + "\n")
print(f"dia {dia}: manifesto atualizado, removido {vitima.name}")
PY
}
```

Ordem obrigatória por dia: **`gerar_dados`/`mutar_dia` → manifesto →
aguardar/rodar backup**. A origem não muda entre a captura do manifesto e
o término do backup daquela geração (verificação: rerun do manifesto em
arquivo novo deve ser idêntico antes do backup disparar; se divergir,
refazer manifesto — a geração muda).

Volume funcional (~600 MiB–1 GiB) prova correção do ciclo; **não**
representa volume de clientes — extrapolação exige ensaio de capacidade
separado.

## 4. Cadastro, preparação e ativação

### 4.1 Publicação na central [portal]

Operador: cliente e agente novos; token emitido → `agent.env`. Publica a
tarefa com destino SFTP e agenda `daily` — **anotar**: UUIDs de
tarefa/cliente/agente exportados pela central, revisão publicada.

### 4.2 Preparação sem ativação [PC]

```bash
manual pull-tasks --central "$MACIP_CONFIG/central.json" \
  --store "$MACIP_STATE/agent.db" --no-activate
manual pull-status --central "$MACIP_CONFIG/central.json" \
  --store "$MACIP_STATE/agent.db"
```

`--no-activate` (semântica do código): grava envelopes,
`config-<gen>.json` e units em `tasks.d/<task_id>/staged/`; **não** faz
daemon-reload/enable, **não** marca aplicado, **não** confirma.
`current.json` ainda **não existe**. Inspecionar:

```bash
# caminho real: $MACIP_STATE/managed/<bond>/tasks.d/<task_id>/
find "$MACIP_STATE/managed" -maxdepth 4 -type f | sort
cat "$MACIP_STATE/managed"/*/tasks.d/*/config-*.json | "$PY" -m json.tool | head -40
ls "$MACIP_STATE/managed"/*/tasks.d/*/staged/
```

Esperado: `config-<gen>.json` igual ao publicado; staging com
`macip-backup-managed-<task_id>.service`/`.timer`; `pull-status` sem
applied/blocked. Parada: divergência com a revisão publicada → parar.

### 4.3 Backup inicial controlado [PC]

Usa a **configuração candidata da central** (não o rascunho local, não
`current.json`):

```bash
CFG="$(find "$MACIP_STATE/managed" -name 'config-*.json' -path "*tasks.d*" | head -1)"
manual agent "$CFG" --store "$MACIP_STATE/agent.db" \
  --central "$MACIP_CONFIG/central.json"
manual sync --store "$MACIP_STATE/agent.db" \
  --central "$MACIP_CONFIG/central.json" --limit 50
manual history --store "$MACIP_STATE/agent.db" --task "$(basename "$(dirname "$CFG")")"
manual check "$CFG" sftp-mvp --read-data
```

Esperado: `success` no histórico, `check --read-data` exit 0, envio na
rodada de sync. `completed` do serviço não é sucesso — exigir `success`.

### 4.4 Aplicação autorizada [PC]

Somente com aprovação explícita do operador:

```bash
manual pull-tasks --central "$MACIP_CONFIG/central.json" \
  --store "$MACIP_STATE/agent.db"
manual pull-status --central "$MACIP_CONFIG/central.json" \
  --store "$MACIP_STATE/agent.db"
systemctl --user cat "macip-backup-managed-<task_id>.timer" 2>/dev/null \
  | grep -E "OnCalendar|Persistent"
systemctl --user is-enabled "macip-backup-managed-<task_id>.timer"
```

Esperado: resultado `applied`, confirmação registrada, timer enabled com
OnCalendar/Persistent da agenda publicada. **Fronteira**: a partir daqui
o primeiro backup automático ocorre no próximo OnCalendar (ou imediato
se houver disparo vencido e a unit acabou de ativar — `Persistent=true`).
Parada: `failed`/rollback → diagnóstico antes de reexecutar.

### 4.5 Inventário explícito das units do piloto [PC]

```bash
INV="$AREA/evidencias/units-inventory.txt"
: > "$INV"
for f in "$HOME/.config/systemd/user/"*.service \
         "$HOME/.config/systemd/user/"*.timer; do
  [ -e "$f" ] || continue
  if grep -qE "macip-backup: install=$MACIP_HOME|macip-mvp" "$f"; then
    printf '%s\t%s\n' "$(basename "$f")" \
      "$(grep -oE 'macip-backup: (managed-task|pull-id|sync-id|install)=[^ ]*' "$f" | tr '\n' ' ')" >> "$INV"
  fi
done
cat "$INV"
systemctl --user list-unit-files 'macip-*' --no-legend --no-pager
systemctl --user list-timers --all --no-pager | grep -E 'macip' || true
```

O inventário é a lista **autoritativa** de recursos do piloto: qualquer
disable/removal posterior usa **esses nomes**, nunca glob.

### 4.6 Sync independente da outbox [PC]

Se a outbox precisar de reenvio periódico independente do agente:

```bash
manual schedule-sync --store "$MACIP_STATE/agent.db" \
  --central "$MACIP_CONFIG/central.json" --calendar "*-*-* *:45:00" --enable
```

(Nome gerado `macip-backup-sync-<hash16>` — entrar no inventário 4.5.)

### 4.7 Units de recuperação do piloto [PC]

O produto **não** instala timers de recuperação: o operador escreve os
pares `macip-mvp-poll`/`-work` manualmente, espelhando o padrão das units
geradas (Type=oneshot, EnvironmentFile do mesmo `agent.env`,
KillMode=control-group, Persistent=true). Conteúdo de referência —
propriedade verificável pelo marcador `# macip-mvp: install=`:

```ini
# ~/.config/systemd/user/macip-mvp-poll.service
# macip-mvp: install=$MACIP_HOME   (expandir o valor real ao gravar)
[Unit]
Description=MAC IP piloto MVP — recovery-poll
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
UMask=0077
EnvironmentFile=-/home/estagiario01/macip-piloto-mvp-linux/config/agent.env
ExecStart=/home/estagiario01/macip-piloto-mvp-linux/programa/current/bin/python \
  -m macip_backup.cli recovery-poll \
  --central /home/estagiario01/macip-piloto-mvp-linux/config/central.json \
  --queue /home/estagiario01/macip-piloto-mvp-linux/estado/recovery.db

# ~/.config/systemd/user/macip-mvp-poll.timer
# macip-mvp: install=$MACIP_HOME
[Unit]
Description=Agendamento piloto MVP — recovery-poll

[Timer]
OnCalendar=*-*-* *:0/1:00
Persistent=true

[Install]
WantedBy=timers.target
```

`macip-mvp-work.service` é idêntico, trocando o ExecStart por:

```ini
ExecStart=... -m macip_backup.cli recovery-work \
  --central .../config/central.json --queue .../estado/recovery.db \
  --policy .../config/recovery.json --limit 1
```

e seu `KillMode=control-group` (para que `stop` encerre o Restic inteiro
no cgroup). Conferir com `systemd-analyze --user verify` após gravar;
**enable só em 4.4**, junto com a aplicação autorizada.

## 5. Rotina diária e provas deliberadas

### 5.1 Rotina diária [PC + portal]

```bash
DIA=N
mutar_dia "$AREA/origem" "$AREA/evidencias/manifest-dia$DIA" "$DIA"
# aguardar a janela agendada; depois conferir:
manual history --store "$MACIP_STATE/agent.db" --task "<task-uuid>"
manual pull-status --central "$MACIP_CONFIG/central.json" --store "$MACIP_STATE/agent.db"
systemctl --user list-timers 'macip-backup-*' 'macip-mvp-*' --no-pager
df -h "$AREA" | tail -1
```

Registrar no modelo 6.1. Ausência de erro na tela não conta: exigir a
execução do dia no histórico/portal.

### 5.2 Restaurações deliberadas pelo portal [PC + portal]

Em ≥2 dias distintos:

1. Portal → consultar snapshots → solicitar restauração (destino
   `restore_root`); anotar UUID da operação e snapshot escolhido.
2. Poll/work:

```bash
manual recovery-poll --central "$MACIP_CONFIG/central.json" --queue "$MACIP_STATE/recovery.db"
manual recovery-work --central "$MACIP_CONFIG/central.json" --queue "$MACIP_STATE/recovery.db" \
  --policy "$MACIP_CONFIG/recovery.json" --limit 1
manual recovery-poll --central "$MACIP_CONFIG/central.json" --queue "$MACIP_STATE/recovery.db"
```

3. Conferir contra o **manifesto da geração** (não contra a origem
   atual, que já mutou):

```bash
conferir() {  # $1=manifesto da geração $2=pasta restaurada
  local man="$1" dir="$2" falha=0 tmp
  tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' RETURN
  cut -d' ' -f3- "$man" | sort > "$tmp/esperado"
  (cd "$dir" && find . -type f | sort) > "$tmp/presente"
  comm -23 "$tmp/esperado" "$tmp/presente" | sed 's/^/AUSENTE: /'
  comm -13 "$tmp/esperado" "$tmp/presente" | sed 's/^/EXTRA: /'
  comm -3 "$tmp/esperado" "$tmp/presente" | grep -q . && falha=1
  while read -r hash path; do
    f="$dir/${path#./}"
    if [ -f "$f" ]; then
      [ "$(sha256sum "$f" | cut -d' ' -f1)" = "$hash" ] \
        || { echo "ALTERADO: $path"; falha=1; }
    fi
  done < "$man"
  [ "$falha" -eq 0 ] && echo "CONFERIDO: $dir" || { echo "DIVERGENTE"; return 1; }
}
conferir "$AREA/evidencias/manifest-dia<gen>" "$AREA/recuperacoes/<uuid-operacao>"
```

Detecta: arquivo alterado, ausente, extra, exclusão (`*.tmp`) indevidamente
incluída. Para provar ponto antigo: restaurar snapshot de geração
anterior e conferir contra **seu** manifesto — inclui arquivo removido
depois (recuperabilidade preservada). Esperado: `succeeded`/sent=1,
portal “Concluída pelo agente”, conteúdo = manifesto.

### 5.3 Ensaio de retenção [PC] — volume separado, não agenda

Separado dos disparos agendados (outra configuração/dia, marcado no
registro como "ensaio"):

```bash
# a) N+2 backups manuais rápidos (ex.: keep_last 7 -> 9 snapshots no destino)
for i in $(seq 1 9); do
  mutar_dia "$AREA/origem" "$AREA/evidencias/manifest-ret-$i" "ret-$i" >/dev/null
  manual agent "$CFG" --store "$MACIP_STATE/agent.db" --central "$MACIP_CONFIG/central.json"
done
manual snapshots "$CFG" sftp-mvp > "$AREA/evidencias/snaps-antes.txt"
# b) Aplicar retenção declarada
manual forget "$CFG" sftp-mvp
manual snapshots "$CFG" sftp-mvp > "$AREA/evidencias/snaps-depois.txt"
# c) Conferir: quantidade final == keep_last; restantes válidos
manual check "$CFG" sftp-mvp
# d) Restaurar um ponto mantido e conferir contra seu manifesto
```

Esperado: inventário depois = `keep_last` mais recentes; check ok;
restauração do ponto mantido confere com seu manifesto. Nunca apontar o
ensaio a repositório de outra tarefa/pessoa.

## 6. Diagnóstico, registro diário e decisão

### 6.1 Modelo diário D1–D7

| Campo | Valor |
|---|---|
| Data / janela | |
| Ocorrência agendada esperada (OnCalendar) | |
| Execução registrada (`success/partial/failed`) | |
| Início real vs agenda; duração | |
| Arquivos/tamanho na geração (manifesto) | |
| Snapshot criado | |
| Envio (sent/pending/blocked na outbox) | |
| Restauração do dia: op UUID, manifesto, resultado conferir | |
| Espaço livre origem/destino | |
| Falhas + diagnóstico + recuperação | |
| Conclusão do dia | ok / divergente / inconclusivo |

### 6.2 Critérios de decisão

- **Ocorrências esperadas**: uma por dia (OnCalendar `daily`), D1–D7. O
  backup manual inicial (4.3) e o ensaio de retenção (5.3) são
  **execuções extra**, não contam como agendadas.
- **Aprovado**: toda ocorrência esperada tem execução `success` no
  histórico + central; nenhum pendente/bloqueado residual na outbox;
  ≥2 restaurações conferidas pelo manifesto; ensaio de retenção com
  resultado esperado; nenhum resultado perdido.
- **Reprovado**: perda/corrupção de dados; restauração divergente;
  ocorrência sem execução e sem classificação de inconclusivo; retenção
  que apague ponto exigido ou não reduza ao limite.
- **Inconclusivo**: indisponibilidade de infraestrutura (energia, rede,
  túnel, máquina desligada, central fora) cobrindo a ocorrência medida —
  registrar causa e repetir os dias afetados ou descontar com justificativa.
- **Interrompido**: decisão do operador ou defeito bloqueante → 6.4/6.5.

### 6.3 Lacuna de observabilidade

O produto **não** alerta "ocorrência esperada não executou" — coberto
pelo registro manual diário (5.1). Pendência de produto proposta (não
implementada aqui): relatório "última execução por tarefa vs agenda".

### 6.4 Diagnóstico [PC]

- Backup: `history` → journal da tarefa (`tasks.d/<id>/journal*` se
  existir) → `systemctl --user status <unit>`/`journalctl --user -u`.
  Falha real (restic/SFTP/config) vs inconclusiva (sem rede/túnel).
- **Outbox de resultados de backup** bloqueada: `outbox-rearm --store
  --central` (somente auth_blocked; `--include-permanent` explícito).
- **Confirmações de distribuição** bloqueadas: `pull-unblock --store
  --central`, depois de corrigir a causa.
- **Fila de recuperação** (`recovery.db`): **não há** comando público de
  rearme — `pull-unblock`/`outbox-rearm` tratam outras filas. Diagnóstico
  seguro: leitura `mode=ro`, `pull-status`, journal; nunca UPDATE SQL nem
  editar claims/idempotência. Registrar a limitação: se persistir,
  escalar como defeito.

### 6.5 Interrupção e encerramento [PC]

```bash
# 1) Aguardar ou parar oneshots ativos ANTES de desabilitar:
systemctl --user list-units 'macip-backup-*.service' 'macip-mvp-*.service' \
  --state=active --no-legend --no-pager
# se houver ativo e a parada for necessária: systemctl --user stop <unit>
# (KillMode=control-group encerra o grupo inteiro)
# 2) Desabilitar SOMENTE os nomes do inventário 4.5 (1ª coluna):
cut -f1 "$INV" | xargs -r systemctl --user disable --now
systemctl --user daemon-reload
```

`disable --now` não encerra oneshot já rodando — conferir
`--state=active` antes. Registrar o que restou active/failed. Não usar
glob para disable: nomes do inventário apenas. Preservar bancos
(backup SQLite consistente), journals, units, logs, pastas e
repositório. Não apagar WAL/SHM/locks/snapshots/parciais.

## 7. Fora do roteiro principal

SIGKILL do worker, queda de energia, abandono de `running`, destruição
de repositório — ensaios separados com preparação própria. Não fazem
parte deste aceite.

## Limites desta preparação

Sem acesso a PC/VM/LAN, credenciais, contas ou bancos operacionais. Sem
início do piloto, sem novas funcionalidades, sem alteração do pacote
congelado. Helpers `gerar_dados`/`mutar_dia`/`conferir` são de suporte ao
roteiro — exercitados em ambiente descartável, não são produto.
