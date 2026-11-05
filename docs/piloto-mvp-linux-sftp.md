# Preparação do piloto Linux + SFTP — ainda não iniciado nem aprovado

Código instalado proposto: `ea74d5d5bf8792b374addbc6addf98345c3327a7`.
Distribuição experimental 0.4.0dev1; agente 0.2.1; PG migrações 1–7;
SQLite principal v7 e recuperação v3. Helpers deste roteiro são ferramentas
externas ao produto congelado: não exigem atualizar wheel ou regenerar pacote.

Recorte: Linux, origens locais, destino SFTP, gestão central e recuperação
pelo portal. Não é MVP completo, produção, Windows, SMB ou S3. Provas reais
anteriores Linux/local (inclusive reboot normal, interrupted e reenvio sem
worker) continuam históricas em [homologação](homologacao-recuperacao-ea74d5d.md)
e [reinício](prova-reinicio-recuperacao.md). SFTP anterior não aprova este
ciclo gerenciado na configuração nova escolhida.

## 1. Decisões do operador e áreas protegidas

Antes de executar: escolher máquina, central exclusiva ou 8445, conta SFTP
nova, horário, linger, credenciais, fingerprint e dimensionamento. Propostas:
área `~/macip-piloto-mvp-linux`, conta `macipsftpmvp`, `remote_path`
`repositories/mvp` relativo ao home SFTP; nunca `/repositories/mvp` por engano.
Origem sintética, dois binários de 256 MiB, sete ocorrências diárias e keep_last 7
são propostas, não representam volumes dos clientes.

Não alterar srvdev1/centrais8443/8444/8445/piloto original, conta
macipsftppiloto ou seu repository, chaves, backup diário da central e
macip-backup-agent-piloto-local.timer. Timers antigos de recuperação
macip-recuperacao-ea74d5d-poll/work permanecem disabled/inactive. Provisionar
máquina, conta e SSH só mediante autorização operacional posterior.

## 2. Preflight [PC] e decisões [VM]

Na VM, o operador mede armazenamento, verifica identidade da conta nova,
diretório relativo autorizado, acesso somente por chave e fingerprint por
canal seguro. Não use contas/repositórios existentes nem desabilite host key.
Se o PC usar túnel manual, registrar a dependência: logout/reboot derruba
o túnel; isso limita a conclusão de disponibilidade. Linger não é automático.

Comandos abaixo são para **execução futura pelo operador**, após aprovar a
área nova. Abra Bash com `set -euo pipefail`; toda recusa interrompe a etapa.
Retome somente depois do diagnóstico. Não execute blocos parcialmente.

```bash
set -euo pipefail
umask 077
unset PYTHONPATH PYTHONHOME
read -r -p 'Checkout revisado do repositório: ' REPO
REVIEW_TOOLS="$REPO/tools"
test -f "$REVIEW_TOOLS/pilot_data.py"
test -f "$REVIEW_TOOLS/pilot_units.py"
test -f "$REVIEW_TOOLS/pilot_prepared.py"
python3 --version
restic version
ssh -V
systemctl --user is-system-running
systemd-analyze calendar daily
read -r -p 'Área NOVA absoluta aprovada: ' AREA
test ! -e "$AREA"
mkdir -m 700 "$AREA"
export MACIP_HOME="$AREA/programa" MACIP_BIN="$AREA/bin"
export MACIP_CONFIG="$AREA/config" MACIP_STATE="$AREA/estado"
export MACIP_CACHE="$AREA/cache" MACIP_LOCK_DIR="$AREA/locks"
mkdir -m 700 "$AREA/config" "$AREA/estado" "$AREA/cache" "$AREA/locks" \
  "$AREA/recuperacoes" "$AREA/evidencias" "$AREA/.ssh"
install -m 600 /dev/null "$AREA/locks/maintenance.lock"
df -h "$AREA"
```

Não crie a origem aqui: quem a cria é `data generate` na seção 4.1, ANTES da
preparação — o produto recusa origem inexistente ao validar a tarefa
preparada, e generate recusa origem já existente. Origem vazia manual não é
o fluxo. Preserve ao menos 2GiB mais margem para origem, recuperações e
repositório; meça ambos os hosts.
Conexão/gerenciador indisponível é gate pendente, nunca aprovação por `|| true`.

## 3. Instalação congelada e configurações [PC]

Escolha artefato já congelado e checksum do manifesto por canal confiável:

```bash
read -r -p 'Wheel congelado absoluto: ' WHEEL
read -r -p 'install.sh do pacote congelado absoluto: ' INSTALLER
read -r -p 'SHA256 do wheel conferido no manifesto: ' WHEEL_SHA
test -f "$WHEEL"; test -f "$INSTALLER"
printf '%s  %s\n' "$WHEEL_SHA" "$WHEEL" | sha256sum -c -
bash "$INSTALLER" "$WHEEL" --commit ea74d5d5bf8792b374addbc6addf98345c3327a7
PY="$MACIP_HOME/current/bin/python"
"$PY" -m macip_backup.cli --version
CREDENTIALS="$MACIP_CONFIG/agent.env"
manual() { "$PY" -m macip_backup.manual --env-file "$CREDENTIALS" -- "$@"; }
data() { python3 "$REVIEW_TOOLS/pilot_data.py" --area "$AREA" --lock-file "$AREA/locks/maintenance.lock" "$@"; }
```

Operador provisiona somente o arquivo privado agent.env (mesmo do
EnvironmentFile): token API separado da senha Restic; SSL_CERT_FILE se CA
privada. Use editor/cofre, não argv, echo de valores, source ou histórico.
Formato literal de macip_backup.manual: UTF-8 KEY=value sem export/aspas/
escapes; modo 0600/0400 e dono atual. Consulte runbook de instalação.

Cadastre cliente/agente/tarefa novos no portal. Defina origem `$AREA/origem`,
exclusions `["*.tmp"]`, destino sftp-mvp com keep_last 7 e agenda escolhida.
SFTP exige host/porta/user aprovados, caminho relativo `repositories/mvp`,
identity_file `$AREA/.ssh/macip-mvp_ed25519`, known_hosts próprio com
fingerprint validado; password_environment `MACIP_MVP_RESTIC_PASSWORD`.
Token_environment da central: `MACIP_MVP_TOKEN`.
Não use rascunho local como configuração efetiva.

Crie central.json e recovery.json explicitamente, sem segredos:

```bash
read -r -p 'URL HTTPS aprovada da central: ' CENTRAL_URL
read -r -p 'UUID do cliente novo: ' CLIENT_ID
read -r -p 'UUID do agente novo: ' AGENT_ID
read -r -p 'UUID da tarefa nova: ' TASK_ID
DESTINATION_ID=sftp-mvp
python3 - "$AREA" "$CENTRAL_URL" "$AGENT_ID" <<'PY'
import json,sys
from pathlib import Path
a=Path(sys.argv[1])
for name,document in [('central.json',{'url':sys.argv[2],'agent_id':sys.argv[3],'token_environment':'MACIP_MVP_TOKEN'}),
                      ('recovery.json',{'restore_root':str(a/'recuperacoes')})]:
    with (a/'config'/name).open('x') as f: json.dump(document,f)
PY
```

O token/CA são resolvidos no processo correto via manual. Erro de HTTPS,
host key ou credencial interrompe o ensaio; não contorne com modo inseguro.

## 4. Preparação exata, units e backup inicial [PC + portal]

### 4.1 Dataset inicial e manifesto — antes da preparação

O produto valida a existência das origens ao preparar a tarefa
(`validate_for_execution` dentro da validação do envelope em
`pull-tasks --no-activate`). Gerar DEPOIS da preparação falha numa
instalação nova. Origem vazia manual contornaria a validação e seria
recusada por `generate`: a sequência correta é gerar primeiro.

```bash
data generate --generation inicial --seed piloto-v1 --large-bytes 268435456
```

Manifestos JSON ficam fora da origem/repositório, em referencias.
Publicação exclusiva recusa sobrescrita. Hashes definem os bytes: seed não
torna os binários os.urandom reproduzíveis. Não modifique origem com
editores externos. Helper usa EX no maintenance.lock registrado e recusa
mutação enquanto uma geração não estiver associada; backup usa SH. Entre
captura e fim do backup, origem fica estável. Divergência exige parar e
diagnosticar, nunca refazer referência para fingir que o ponto confere.

### 4.2 Preparar antes de ativar

Publique revisão/agenda aprovadas na central. Não execute pull normal ainda.

```bash
PREPARATION="$AREA/evidencias/preparacao.json"
(set -o noclobber; manual pull-tasks --central "$MACIP_CONFIG/central.json" \
  --store "$MACIP_STATE/agent.db" --no-activate > "$PREPARATION")
manual pull-status --central "$MACIP_CONFIG/central.json" --store "$MACIP_STATE/agent.db"
read -r -p 'Bond completo ou prefixo16 retornado na preparação: ' BOND
read -r -p 'Geração preparada exata: ' GENERATION
read -r -p 'Revisão publicada exata: ' REVISION
CFG="$(python3 "$REVIEW_TOOLS/pilot_prepared.py" --state "$MACIP_STATE" \
  --central "$MACIP_CONFIG/central.json" --store "$MACIP_STATE/agent.db" \
  --preparation "$PREPARATION" --task "$TASK_ID" --client "$CLIENT_ID" \
  --agent "$AGENT_ID" --destination "$DESTINATION_ID" --bond "$BOND" \
  --install "$MACIP_HOME" --source "$AREA/origem" \
  --generation "$GENERATION" --revision "$REVISION")"
```

O selector exige um config-N e gen-N exatos, hash do envelope, tarefa,
cliente/agente/destino, revisão/geração e marcadores completos no staging.
Ausência/múltiplos/vínculo diferente recusa. Não lê current.json nem escolhe
`find | head`. Preparação não ativa nem confirma aplicação.

### 4.3 Plano e conflitos

```bash
PLAN="$AREA/evidencias/units-plan.json"
python3 - "$PLAN" "$AREA" "$TASK_ID" "$AGENT_ID" "$BOND" <<'PY'
import json,sys
with open(sys.argv[1],'x') as f: json.dump({'area':sys.argv[2],'task_id':sys.argv[3],
 'agent_id':sys.argv[4],'bond':sys.argv[5],'managed_timer':True,'schedule_results':[]},f,indent=2)
PY
units() { python3 "$REVIEW_TOOLS/pilot_units.py" --plan "$PLAN" "$@"; }
units preflight
units install-recovery
```

Este plano assume agenda automática aprovada; se manual, registre
managed_timer=false antes do preflight. preflight recusa colisões inclusive
units inativas, unidades próprias omitidas e marcadores ambíguos; nomes
parecidos não atribuem propriedade. Diretório é o do produto congelado:
`$HOME/.config/systemd/user`, não XDG_CONFIG_HOME.

install-recovery valida a instalação ea74d5d e gera **quatro arquivos
completos** com os valores absolutos do plano. Valida antes de publicar,
não sobrescreve arquivo e não habilita nada. Para inspecionar os arquivos
gerados use os quatro nomes exatos macip-mvp-poll.service/.timer e
macip-mvp-work.service/.timer. Ambos services usam o Python instalado,
macip_backup.manual e o mesmo agent.env, com todos os MACIP_* explicitamente
no Environment do processo, Type=oneshot, UMask=0077, KillMode=control-group,
TimeoutStartSec=infinity e TimeoutStopSec=45s; worker --limit 1. Timers:
poll aos segundos 00/30, worker 15/45, Persistent=true. Não há ExecStart com
reticências. São propostas que o operador deve inspecionar antes de ativar.

### 4.4 Backup inicial e associação ao snapshot

```bash
(set -o noclobber; manual agent "$CFG" --store "$MACIP_STATE/agent.db" \
  --central "$MACIP_CONFIG/central.json" > "$AREA/evidencias/inicial-result.json")
(set -o noclobber; manual snapshots "$CFG" "$DESTINATION_ID" > "$AREA/evidencias/inicial-snapshots.json")
read -r -p 'Snapshot completo (64 hex) desse resultado/inventário: ' SNAPSHOT
data bind --generation inicial --result "$AREA/evidencias/inicial-result.json" \
  --snapshots "$AREA/evidencias/inicial-snapshots.json" --task "$TASK_ID" \
  --destination "$DESTINATION_ID" --snapshot "$SNAPSHOT"
manual check "$CFG" "$DESTINATION_ID" --read-data
manual sync --store "$MACIP_STATE/agent.db" --central "$MACIP_CONFIG/central.json" --limit 50
```

Bindings são publicados em referencias sem sobrescrita, e só após backup
success com execução/tarefa/destino/snapshot compatíveis, tag da tarefa e
origem ainda igual ao manifesto; registra hash do manifesto e snapshot
completo. Divergência exige parar e diagnosticar, nunca refazer referência
para fingir que o ponto confere.

### 4.5 Aplicação e inventário completo antes da ativação restante

Somente após aprovação, o pull normal aplica a configuração e pode habilitar
o timer gerenciado imediatamente (Persistent). Prepare a geração seguinte
apenas quando a anterior estiver associada; não crie disparo vencido inesperado.

```bash
manual pull-tasks --central "$MACIP_CONFIG/central.json" --store "$MACIP_STATE/agent.db"
manual pull-status --central "$MACIP_CONFIG/central.json" --store "$MACIP_STATE/agent.db"
SYNC_RESULT="$AREA/evidencias/schedule-sync.json"
PULL_RESULT="$AREA/evidencias/schedule-pull.json"
(set -o noclobber; manual schedule-sync --store "$MACIP_STATE/agent.db" \
  --central "$MACIP_CONFIG/central.json" --calendar '*-*-* *:*:00,30' > "$SYNC_RESULT")
(set -o noclobber; manual schedule-pull --store "$MACIP_STATE/agent.db" \
  --central "$MACIP_CONFIG/central.json" --calendar '*-*-* *:0/5:00' > "$PULL_RESULT")
python3 - "$PLAN" "$SYNC_RESULT" "$PULL_RESULT" <<'PY'
import json,sys
from pathlib import Path
p=Path(sys.argv[1]);d=json.loads(p.read_text());d['schedule_results']=sys.argv[2:]
p.write_text(json.dumps(d,indent=2)+'\n')
PY
INV="$AREA/evidencias/units-inventory-final.json"
units --inventory "$INV" inventory
units --inventory "$INV" verify
# Só após conferir todo o inventário/propriedade e aprovar automação:
units --inventory "$INV" activate
```

schedule-sync/pull acima não recebem --enable. Inventário lê os nomes reais
retornados, confere hashes de identidade, marcadores literais completos e
presença de todas as units. Managed: install/task/agent/bond; pull/sync:
install/tipo/hash banco+central; recovery: install/task/agent/bond/role.
Qualquer novo recurso ou mudança de arquivo/plano invalida inventário antigo:
gere arquivo novo, nunca sobrescreva. Depois de aplicação/suspensão gerenciada,
confira novamente a agenda/timer e registre revisão do inventário se mudou.

## 5. D1–D7, históricos e retenção

Antes de cada ocorrência agendada: confirme que geração anterior foi
associada e nenhum backup está executando. A mutação é byte-safe inclusive
binários e produz nova referência exclusiva; um arquivo removido continuará
conferível no snapshot histórico, não na origem atual.

```bash
read -r -p 'Dia numérico da ocorrência seguinte: ' DIA
GEN="dia-$DIA"
data mutate --generation "$GEN" --day "$DIA" --force-binary
# Aguarde o OnCalendar e o término; não rode backup manual para fingir ocorrência.
HISTORY="$AREA/evidencias/$GEN-history.json"
POINTS="$AREA/evidencias/$GEN-snapshots.json"
(set -o noclobber; manual history --store "$MACIP_STATE/agent.db" --task "$TASK_ID" > "$HISTORY")
(set -o noclobber; manual snapshots "$CFG" "$DESTINATION_ID" > "$POINTS")
read -r -p 'UUID exato da execução AGENDADA: ' EXECUTION
read -r -p 'Snapshot completo dessa execução: ' SNAPSHOT
data bind --generation "$GEN" --result "$HISTORY" --execution "$EXECUTION" \
  --snapshots "$POINTS" --task "$TASK_ID" --destination "$DESTINATION_ID" --snapshot "$SNAPSHOT"
```

São necessárias sete ocorrências reais de agenda. Inicial manual e ensaios
extras não contam. Antes de cada mutação certifique-se de que o próximo
disparo não está próximo; se a janela foi perdida, registre inconclusivo,
não execute gerações concorrentes para obter sete aprovações.

Em dois dias distintos, consulte pontos pelo portal e restaure snapshot
selecionado para pasta nova. Anote a geração e snapshot exatos. Conferência:

```bash
read -r -p 'Geração do snapshot escolhido (não a origem atual): ' RESTORE_GEN
read -r -p 'Snapshot completo escolhido: ' RESTORE_SNAPSHOT
read -r -p 'UUID da operação concluída no portal: ' OPERATION
python3 "$REVIEW_TOOLS/pilot_data.py" --area "$AREA" compare \
  --binding "$AREA/referencias/$RESTORE_GEN.binding.json" \
  --operation-root "$AREA/recuperacoes/$OPERATION" --task "$TASK_ID" \
  --destination "$DESTINATION_ID" --snapshot "$RESTORE_SNAPSHOT"
```

Compare deriva a origem restaurada como operação/caminho-absoluto-da-origem
sem a barra inicial. Não usa primeira pasta encontrada. Recusa links, caminhos
fora da raiz, referência/snapshot/tarefa/destino divergentes, arquivo alterado,
ausente/extra e tmp indevidamente presente. Faça a primeira prova histórica
cedo, enquanto o snapshot antigo ainda está entre os sete pontos retidos.

### 5.1 Ensaio adicional de retenção automática — isolado

Não use CFG, origem, tarefa, conta ou repositório do piloto. O teste
automatizado fornecido executa nove backups com Restic **local real**, keep_last 7
desde o início, inventários por tarefa/ID completo e associações de geração:

```bash
PYTHONPATH=src python3 -m unittest \
  tests.test_pilot_data.PilotResticRealTests.test_integrated_historical_restore_and_automatic_retention_nine_backups -v
```

Execute no checkout de ferramentas revisado com Restic disponível. Isso é
ensaio descartável local, não prova SFTP gerenciada e não conta como D1–D7.
Confira o nome da classe no teste antes de executar se a revisão mudar.
O inventário esperado cresce 1..7 e permanece 7 nos backups 8/9. Registre
exatamente quais IDs 1 e 2 saíram e quais permaneceram (3..9), não espere
nove antes de forget: o agente já aplica retenção após cada backup.
O teste restaura/confronta ponto mantido e ponto histórico após mutação.
Sem Restic, skip explícito é gate pendente. Não há forget explícito neste
ensaio; um teste manual extra de forget exigiria outra preparação exclusiva.

Para provar retenção em SFTP antes do aceite, o operador deverá autorizar
**outra** conta/subrepositório de ensaio, tarefa/configuração/fila e volume
próprios. Repita nove execuções e os inventários esperados sem alterar política
da tarefa do piloto. Essa prova SFTP continua pendente até evidência real.

## 6. Registro, critérios e encerramento

Para cada D1–D7 registre data/janela OnCalendar, execução, status, início/fim,
geração/manifesto/hash, snapshot, envio à central, comparação de recuperação,
espaço nos dois hosts e falhas. Instantes e conjuntos comparados precisam ser
do mesmo contexto de tarefa/destino. Correção decide aceite; tempos apenas
informam capacidade. Propostas de início até 30 min e execução até 60 min exigem
aprovação do operador. Não há alerta automático de ocorrência ausente.

Aceite: sete ocorrências com success e central correspondente, filas sem
pendências residuais, duas restaurações em dias distintos conferidas pela
referência histórica, retenção no recorte escolhido e sem perda de resultado.
Dados divergentes/perdidos ou sucesso falso reprovam. Energia/rede/central
indisponível na janela sem evidência suficiente é inconclusivo: registrar e
repetir janela, não descontar silenciosamente.

Diagnóstico: outbox-rearm é da outbox; pull-unblock é das confirmações de
distribuição. Recovery não tem rearme público: não UPDATE SQL, editar claims
ou apagar filas. `completed`/serviço success não significa restore succeeded.

Antes de encerrar, verify novamente. ActiveState=activating/deactivating
também significa execução. Sem autorização de interromper, aguarde oneshots:

```bash
units --inventory "$INV" verify
units --inventory "$INV" stop
```

O `stop` primeiro DESABILITA os timers do inventário (impede novos
disparos), depois reinspeciona serviços e jobs pendentes — a primeira
consulta sozinha não basta: um timer pode disparar durante o próprio
disable. Active/activating/deactivating contam como execução, e
`list-jobs` cobre jobs `start` ainda pendentes. Espera limitada por
`--settle-seconds` (padrão 30s; não é loop infinito). Se algo ainda
executar e não houver autorização de interromper, o helper sai com
código 3 e `status:"incomplete"` informando `timers:"disabled"`:
os timers já foram desligados — aguarde o término e repita `stop`, ou
use `--allow-stop`. Ele nunca retorna `ok` com serviço rodando, job
pendente ou estado não verificável; ao final confirma timers inativos.
Somente `status:"ok"` (rc 0) prova encerramento completo.

Com decisão explícita de interromper execuções e registrar parcial:

```bash
units --inventory "$INV" --allow-stop stop
```

`--allow-stop` para somente os serviços próprios ocupados/com job e
reconfirma o estado final; se a parada não concluir dentro da espera,
recusa (não finge sucesso).

O helper reconfere hashes/propriedade/completude antes das ações, desabilita
nomes exatos de timers e para somente serviços próprios em execução. Não usa
glob/xargs; não remove arquivos. Se inventário ficou desatualizado, recusa e
exige novo inventário antes de qualquer ação. Guarde bancos com backup SQLite
online consistente, manifests, journals, units, locks, WAL/SHM, repositório e
pastas recuperadas/parciais; não remova snapshots ou evidências.

SIGKILL, energia abrupta e running abandonado não pertencem a este ensaio.
Roteiro e helpers locais não autorizam provisionar recursos operacionais.
As verificações reais/simuladas desta revisão estão no registro de trabalho;
o piloto continua **não iniciado e não aprovado**.
