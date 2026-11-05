# Piloto controlado de 7 dias — MVP Linux com destino SFTP e recuperação pelo portal

Documento de **preparação**. Nenhuma etapa deste piloto foi iniciada ou
aprovada. Todos os valores de dimensionamento são **propostas** pendentes
de decisão do operador; nada aqui declara o piloto executado.

Código alvo da preparação: `ea74d5d5bf8792b374addbc6addf98345c3327a7`
(distribuição experimental 0.4.0dev1, agente 0.2.1, PG migrações 1–7,
SQLite principal v7, fila de recuperação v3). Se o operador escolher outro
commit, este documento deve ser revisado antes do início.

Recorte proposto: **Linux, origens locais, destino SFTP, gestão e
acompanhamento centralizados e recuperação pelo portal**. Windows, SMB e
S3 não fazem parte deste piloto; o recorte não equivale ao cumprimento do
produto completo.

## 1. Matriz de aceite do MVP Linux

| # | Requisito mínimo | Implementação existente | Evidência já disponível | Evidência que o piloto deve produzir | Critério de aprovação |
|---|---|---|---|---|---|
| 1 | Cadastro e publicação de tarefa | Portal + API de revisões; `pull-tasks` aplica estado desejado | Homologação ea74d5d (consulta/restore publicados) | Tarefa SFTP nova publicada e aplicada, com revisão conferida | Revisão aplicada = revisão publicada, confirmada pelo agente sem `blocked` |
| 2 | Aplicação pelo agente | `pull-tasks`/`schedule-pull`; units gerenciadas com nome exclusivo | Homologação de distribuição anterior | Aplicação da agenda SFTP na versão do piloto | Unit criada com o vínculo do piloto; `pull-status` coerente |
| 3 | Backup agendado para SFTP | Destino `kind: "sftp"` no restic nativo; timer `agent` por tarefa | Provas SFTP anteriores (pré-portal); homologação local ea74d5d | ≥6 execuções agendadas em 7 dias com resultado `success` | Todas as execuções esperadas ocorreram; sem falha não diagnosticada |
| 4 | Histórico central | Outbox SQLite + `sync`; histórico no portal | Envio durável homologado (sent=1 após túnel) | Resultados visíveis no portal para cada execução | Cada execução registrada localmente aparece na central |
| 5 | Consulta dos pontos de recuperação | `snapshots <config> <dest>` e consulta pelo portal | Homologação ea74d5d | Lista de snapshots do destino SFTP conferida local e pelo portal | Snapshots locais = snapshots no portal (mesmo conjunto) |
| 6 | Restauração pelo portal | Fila recovery.db v3, poll/work independentes | Homologação ea74d5d incluindo reboot | ≥2 restaurações deliberadas em dias distintos | `succeeded` no portal, sem repetição de worker |
| 7 | Conferência de conteúdo | Restore em pasta nova autorizada + exclusões | diff/hash aprovados na homologação | Comparação byte-a-byte dos restaurados | 100% dos arquivos não excluídos idênticos à origem; excluídos ausentes |
| 8 | Persistência local | Outbox, fila v3, journal por tarefa | Reboot + offline homologados | Eventuais indisponibilidades reais do período registradas | Nenhum resultado perdido; envio somente por poll/sync |

### Propostas de dimensionamento (não aprovadas)

| Parâmetro | Proposta | Observação |
|---|---|---|
| Duração | 7 dias corridos | Inicia no primeiro backup agendado observado |
| Frequência | 1 backup/dia (`daily`, horário definido pelo operador) | Agenda central publicada; `Persistent=true` já cobre atraso de boot |
| Retenção | `keep_last: 7` | Coerente com 7 dias; aplicada por `forget` |
| Perfil de dados | Sintético: ~200 arquivos pequenos, 2 grandes (≥256 MiB cada), árvore com 8 níveis, nomes com espaços/acentos, `*.tmp` excluídos | Gerado por script local; nenhum dado real de cliente |
| Volume inicial | ~1 GiB | Cabe na margem; medir antes |
| Alteração diária | ~5% dos arquivos modificados + 1 arquivo novo + 1 removido | Exercita incrementalidade e exclusão |
| Atraso máximo p/ detectar ausência | 2 rodadas de agenda sem execução registrada | Ex.: daily → ausência > 48 h dispara investigação; comunicação perdida ≠ ausência de execução |
| Tempo esperado de recuperação | < 15 min para o conjunto de referência (~1 GiB) em LAN | Meta de observação, não SLA; medir e registrar real |
| Margem de armazenamento | Repositório ≤ 50% do espaço livre no destino; origem + recuperações ≤ 50% no PC | Última leitura histórica da VM (~9 GiB) é obsoleta — medir no preflight |

Testes de correção (conteúdo restaurado íntegro, exclusão respeitada) são
separados de testes de desempenho (durações): os primeiros definem
aprovação; os segundos alimentam o registro e futuras decisões.

## 2. Arquitetura operacional

### Opção recomendada

- **Origem (agente)**: máquina Linux dedicada que permanece ligada durante
  o piloto, com `systemd --user` disponível entre sessões
  (`loginctl enable-linger` é decisão do operador — sem linger, os timers
  --user param no logout e o piloto de agenda fica inconclusivo).
- **Destino SFTP**: outra máquina (ex.: `srvdev1`), conta e repositório
  **exclusivos e novos** — proposta: usuário `macipsftpmvp`, diretório
  `repositories/mvp` sob seu home, `ForceCommand internal-sftp`,
  somente chave pública. O usuário/repositório `macipsftppiloto`
  (`/home/macipsftppiloto/repository`) contém backups anteriores e **não**
  será reutilizado nem limpo.
- **Identidade SSH**: chave Ed25519 dedicada do piloto
  (`~/.ssh/macip-mvp_ed25519`), `known_hosts` próprio com fingerprint
  conferido por canal seguro (`ssh-keygen -lf` no servidor, não
  `ssh-keyscan` cego). O agente já usa `StrictHostKeyChecking=yes`,
  `BatchMode=yes`, `IdentitiesOnly=yes`, somente chave pública.
- **Senha Restic**: sob custódia do operador, fora da máquina de origem —
  entregue somente via arquivo de credenciais 0600 (mesmo padrão do
  laboratório: nome da variável no config, valor no `agent.env`). Nunca em
  argv, unidade legível, log ou histórico de shell.
- **Credencial da API**: token do agente em variável **separada** da senha
  Restic (`token_environment` no `central.json` nomeia a variável, nunca o
  valor; campo `token` é recusado pelo parser).
- **Central**: instância de laboratório exclusiva do piloto ou a central de
  recuperação já existente em `127.0.0.1:8445` — **decisão do operador**.
  Se reutilizar a central de recuperação, agente e vínculo novos; nunca o
  agente `dd7d5bae-…` do laboratório congelado.
- **Consequências**: suspensão da tarefa na central desabilita o timer
  gerenciado (não reativa sozinha); logout sem linger para timers --user;
  reboot requer `Persistent=true` (já aplicado pelas units gerenciadas) e
  reativação do túnel se houver; perda do túnel/rede adia envio
  (`retry`), não perde resultado.

### Dependência declarada

Se o piloto usar o PC `estagiario01-Aspire-A315-53` e o túnel SSH manual
para 18445 por limitação de laboratório, isso é **dependência e limite do
resultado**: túnel interativo morre em logout/queda; comunicação com a
central fica indisponível enquanto o túnel estiver fechado (backups locais
continuam, envio fica `retry`). Esse modo não prova a arquitetura-alvo —
somente o ciclo funcional. Registrá-lo no início do piloto.

### O que depende do operador (decisões em aberto)

1. Máquina de origem definitiva (dedicada vs PC de laboratório).
2. Central: instância nova exclusiva vs reutilizar a de recuperação.
3. Provisionar conta SFTP nova (`macipsftpmvp` ou nome equivalente) e
   repositório exclusivo; confirmar espaço livre real no destino.
4. Fingerprint da host key por canal seguro.
5. Cofre/arquivo de credenciais do piloto (0600, fora do Git).
6. Horário da agenda e definição de `linger`.
7. Nomes exclusivos: área `~/macip-piloto-mvp-linux` (proposta), units
   `macip-mvp-*`, agente/tarefa/UUIDs novos gerados no cadastro.
8. Aprovação dos valores propostos da seção 1.

### Preservação obrigatória

Laboratórios congelados (`macip-homolog-recuperacao-ea74d5d` no PC e na
VM), central 8443, laboratório 8444, piloto
`macip-backup-agent-piloto-local.timer`, backup diário da central, conta
`macipsftppiloto` e evidências anteriores **não são tocados**. Os timers
`macip-recuperacao-ea74d5d-{poll,work}.timer` seguem disabled/inactive.
Nada é instalado sobre a instalação congelada.

## 3. Runbook executável

Comandos marcados **[PC]** ou **[VM]**. Conferidos contra a CLI de
`src/macip_backup/cli.py` (subcomandos e argumentos reais). Variáveis de
área seguem o layout do laboratório; adapte nomes ao provisionamento.

### 3.0 Bloco de ambiente [PC]

```bash
umask 077
unset PYTHONPATH PYTHONHOME
AREA='/home/estagiario01/macip-piloto-mvp-linux'
export MACIP_HOME="$AREA/programa" MACIP_BIN="$AREA/bin"
export MACIP_CONFIG="$AREA/config" MACIP_STATE="$AREA/estado"
export MACIP_CACHE="$AREA/cache" MACIP_LOCK_DIR="$AREA/locks"
PY="$MACIP_HOME/current/bin/python"
CREDENTIALS="$AREA/agent.env"          # criado pelo operador, 0600
manual() { "$PY" -m macip_backup.manual --env-file "$CREDENTIALS" -- "$@"; }
```

Resultado esperado: variáveis exportadas, `manual` disponível.
Parada: `agent.env` inexistente → provisionar credenciais (3.4) antes.
Inconclusivo vs falha: `manual` ausente no wheel instalado é falha de
instalação; variável errada é falha de configuração do operador.

### 3.1 Preflight [PC]

```bash
"$PY" -m macip_backup.cli --version            # esperado: macip-backup 0.2.1
restic version                                # esperado: 0.18.x
systemctl --user is-system-running            # degraded aceitável
systemd-analyze --user verify /dev/null 2>&1 | head -1 || true
df -h "$AREA" | tail -1                        # margem ≥ proposta
ssh -V                                        # OpenSSH para destino SFTP
```

Espaço no destino **[VM]**:

```bash
df -h /home
getent passwd macipsftpmvp || echo "conta ainda não existe — provisionar"
```

Parada: restic ausente, systemd --user indisponível, espaço abaixo da
margem → não inicia. Inconclusivo: `systemctl --user` indisponível por
sessão sem lingering → decidir linger antes de continuar.

### 3.2 Propriedade e recusa de conflito [PC]

```bash
for n in macip-mvp-backup macip-mvp-poll macip-mvp-work macip-mvp-pull; do
  systemctl --user status "$n.timer" "$n.service" >/dev/null 2>&1 \
    && echo "CONFLITO: $n já existe" || true
done
ls "$AREA" 2>/dev/null && echo "área já existe — conferir conteúdo antes de usar"
```

Esperado: nenhum `CONFLITO`, área nova ou explicitamente aprovada.
Parada: recurso existente de outro dono → escolher outros nomes.

### 3.3 Instalação isolada de versão exata [PC]

Instalar o wheel **da versão escolhida** em área exclusiva (mesmo
procedimento `deploy/install.sh` do laboratório, prefixo próprio). Não
reutilizar `~/macip-homolog-recuperacao-ea74d5d`. Conferir:

```bash
"$PY" - <<'PY'
import json, sys
from pathlib import Path
m = json.loads(Path('<AREA>/programa/install.json').read_text())
print(m['commit'], m['version'])
PY
```

Esperado: commit e versão escolhidos registrados. Parada: wheel não
corresponde ao commit revisado → gerar artefato de commit exato
(`docs/candidata-linux.md`), nunca improvisar build.

### 3.4 Credenciais sem valores expostos [PC]

`agent.env` (0600, dono estagiario01) criado pelo operador, contendo
somente nomes de variáveis reais:

```bash
stat -c '%a %U' "$CREDENTIALS"   # exige 600 (ou 400) e dono correto
# conferir NOMES presentes, sem imprimir valores:
grep -c '^[A-Z_]*=' "$CREDENTIALS"
```

Variáveis necessárias: token da API (nome apontado por
`token_environment`), senha Restic (nome apontado por
`password_environment`), `SSL_CERT_FILE` se a CA for privada.
Parada: permissão aberta, dono errado ou parser `manual` recusando
formato → corrigir antes de qualquer execução.

### 3.5 Configurações [PC]

`$MACIP_CONFIG/central.json`:

```json
{"url":"https://<central-do-piloto>","agent_id":"<uuid-agente-piloto>",
 "token_environment":"<NOME_VAR_TOKEN>"}
```

Tarefa `$MACIP_CONFIG/tarefa-mvp.json` (rascunho local para conferência;
a publicação real é pela central — ver 3.6):

```json
{"id":"<task-uuid>","name":"Piloto MVP Linux","client":{"id":"<cliente>","name":"<nome>"},
 "device_id":"<device>","sources":["<AREA>/origem"],"exclusions":["*.tmp"],
 "destinations":[{"id":"sftp-mvp","name":"SFTP MVP","kind":"sftp",
   "sftp":{"host":"<host>","port":22,"user":"macipsftpmvp",
     "remote_path":"/repositories/mvp",
     "identity_file":"<AREA>/.ssh/macip-mvp_ed25519",
     "known_hosts_file":"<AREA>/.ssh/known_hosts",
     "connect_timeout_seconds":15},
   "password_environment":"<NOME_VAR_SENHA_RESTIC>",
   "retention":{"keep_last":7}}]}
```

Validação local **antes de qualquer efeito remoto**:

```bash
manual validate "$MACIP_CONFIG/tarefa-mvp.json"
```

Esperado: `validate` exit 0. Parada: `ConfigurationError` — corrigir o
arquivo; destino `kind:"sftp"` sem bloco `sftp` é recusado pelo parser.

### 3.6 Cadastro, publicação e aplicação [PC + portal]

1. Operador cadastra cliente/agente novos no portal e emite o token do
   agente (gravado em `agent.env` por nome de variável).
2. Publica a tarefa com destino SFTP e agenda `daily` — revisão 1.
3. No PC, **somente** reconciliação:

```bash
manual pull-tasks --central "$MACIP_CONFIG/central.json" --store "$MACIP_STATE/agent.db"
manual pull-status --central "$MACIP_CONFIG/central.json" --store "$MACIP_STATE/agent.db"
```

Esperado: revisão aplicada = publicada; `pull-status` sem `blocked`;
units gerenciadas com nomes exclusivos aparecem em
`~/.config/systemd/user/`. Parada: `blocked`/`auth` → conferir token;
divergência de revisão → não prosseguir. **Não** confirmar prematuramente:
confirmação é efeito do próprio `pull-tasks`, não de comando extra.

### 3.7 Backup inicial e verificação [PC]

Disparo manual da primeira execução (agenda já valida por si depois):

```bash
manual agent "$MACIP_CONFIG/tarefa-mvp.json" \
  --store "$MACIP_STATE/agent.db" --central "$MACIP_CONFIG/central.json"
manual sync --store "$MACIP_STATE/agent.db" --central "$MACIP_CONFIG/central.json" --limit 50
manual history --store "$MACIP_STATE/agent.db" --task "<task-uuid>"
manual check "$MACIP_CONFIG/tarefa-mvp.json" sftp-mvp --read-data
```

Esperado: execução `success`, envio `sent` na rodada de sync,
`check --read-data` exit 0. Parada: falha SFTP/autenticação → revisar
identidade/known_hosts/credencial; falha restic → log seguro do adapter.
`completed:1` do worker **não** é sucesso de backup — exigir `success`
no histórico.

### 3.8 Ativação explícita [PC]

Somente após 3.7 aprovado, o operador habilita **os recursos do piloto**:

- timer gerenciado da tarefa (instalado por `pull-tasks`; `enable --now`
  somente se a revisão aplicada veio com agenda e operador aprovar);
- `macip-mvp-poll.timer` / `macip-mvp-work.timer` do piloto (criados pelo
  operador com `EnvironmentFile`, comandos `recovery-poll`/`recovery-work`
  e nomes exclusivos — o produto **não** instala timers de recuperação
  automaticamente);
- opcional `schedule-pull` para reconciliação periódica:

```bash
manual schedule-pull --store "$MACIP_STATE/agent.db" \
  --central "$MACIP_CONFIG/central.json" --calendar "*-*-* *:15:00" --enable
systemctl --user list-timers 'macip-mvp-*' --no-pager
```

Parada: qualquer timer fora do namespace `macip-mvp-*` tocado → parar e
revisar. `inactive` ≠ `disabled`: registrar UnitFileState.

### 3.9 Comunicação e histórico central [PC + portal]

```bash
manual sync --store "$MACIP_STATE/agent.db" --central "$MACIP_CONFIG/central.json" --limit 50
manual history --store "$MACIP_STATE/agent.db" --outbox
curl --fail --silent --cacert '<ca-do-piloto>' 'https://<central>/readyz'
```

Esperado: `{"database":"ok"}` no readyz; outbox vazia após sync; cada
execução local visível no portal. Inconclusivo: readyz falha por túnel
fechado ≠ falha de backup — conferir execuções locais primeiro.

### 3.10 Restaurações deliberadas pelo portal [PC + portal]

Em ≥2 dias distintos:

1. Portal: consultar snapshots da tarefa, escolher ponto, solicitar
   restauração para destino autorizado (`restore_root` da política).
2. `recovery-poll` recebe a intenção; `recovery-work` executa;
   `recovery-poll` seguinte envia o resultado:

```bash
manual recovery-poll --central "$MACIP_CONFIG/central.json" --queue "$MACIP_STATE/recovery.db"
manual recovery-work --central "$MACIP_CONFIG/central.json" --queue "$MACIP_STATE/recovery.db" \
  --policy "$MACIP_CONFIG/recovery.json" --limit 1
manual recovery-poll --central "$MACIP_CONFIG/central.json" --queue "$MACIP_STATE/recovery.db"
```

3. Comparar conteúdo restaurado com a origem (diff -r ou sha256 por
   arquivo); exigir ausência de `*.tmp`.

Esperado: `succeeded`/`sent=1`, portal “Concluída pelo agente”, conteúdo
idêntico. “Solicitação já existente” **não** é defeito automático:
verificar a operação pendente na fila e a consulta associada antes de
classificar. `completed` do worker ≠ `succeeded` da operação.

### 3.11 Rotina diária [PC + portal]

Uma vez por dia, mesmo horário:

```bash
manual history --store "$MACIP_STATE/agent.db" --task "<task-uuid>" | tail -5
manual pull-status --central "$MACIP_CONFIG/central.json" --store "$MACIP_STATE/agent.db"
systemctl --user list-timers 'macip-mvp-*' --no-pager
df -h "$AREA" | tail -1
```

Registrar no modelo da seção 4. Ausência de erro na tela **não** conta:
confirmar que a execução esperada do dia existe no histórico/portal.

### 3.12 Diagnóstico e interrupção [PC]

- Falha de backup: histórico local → journal da tarefa (`tasks.d/`) →
  `systemctl --user status`/`journalctl --user -u <unit>`; classificar:
  falha real (restic/SFTP/config) vs inconclusiva (sem energia/rede/
  túnel).
- Fila recovery parada: `pull-status` e leitura `mode=ro` de recovery.db;
  rearmar somente após corrigir a causa (`pull-unblock`/`outbox-rearm`
  conforme o tipo de bloqueio).
- **Interromper o piloto**: `systemctl --user disable --now` nos timers
  `macip-mvp-*` do piloto e suspender a tarefa na central. Nada além do
  namespace `macip-mvp-*` é desabilitado.

### 3.13 Encerramento não destrutivo [PC]

```bash
systemctl --user disable --now macip-mvp-poll.timer macip-mvp-work.timer 2>/dev/null || true
# backup consistente dos bancos (inclui WAL), sem cp direto:
"$PY" - <<'PY'
import sqlite3, sys
from pathlib import Path
for name in ('agent.db','recovery.db'):
    src=sqlite3.connect((Path('<AREA>/estado')/name).resolve().as_uri()+'?mode=ro',uri=True)
    dst=sqlite3.connect(f'<AREA>/evidencias/{name}'); src.backup(dst); src.close(); dst.close()
print('backups consistentes gravados')
PY
```

Preservar: bancos, journals, units, logs, pastas restauradas e repositório
SFTP. Não apagar filas, WAL/SHM, locks, snapshots ou pastas parciais.
Não alterar SQLite para fabricar estados.

### Fora do roteiro principal (ensaio separado, se exigido)

SIGKILL do worker, queda de energia, abandono de `running`, exclusão de
repositório — ensaio futuro com preparação própria. Não fazem parte deste
aceite.

## 4. Registro diário e decisão

### Modelo por dia (D1–D7)

| Campo | Valor |
|---|---|
| Data / janela de observação | |
| Execuções de backup esperadas | |
| Execuções realizadas (`success/partial/failed`) | |
| Atraso até o registro (vs agenda) | |
| Arquivos/tamanho na origem | |
| Duração do backup | |
| Snapshot criado (prefixo) | |
| Envio ao central (sent/retry/blocked) | |
| Restauração do dia (se houver): op UUID, duração, comparação | |
| Espaço livre origem/destino | |
| Falhas + diagnóstico + recuperação | |
| Conclusão do dia | ok / divergente / inconclusivo |

### Critérios de decisão

- **Aprovado**: 7 dias completos; ≥6 backups agendados com `success`;
  toda execução local aparece na central; ≥2 restaurações pelo portal
  com conteúdo idêntico e exclusões respeitadas; nenhum resultado
  perdido; nenhuma falha sem diagnóstico.
- **Reprovado**: qualquer perda de dados, restauração divergente,
  ausência de backup não explicada > limite acordado, ou comportamento
  que exija correção antes de nova tentativa.
- **Inconclusivo**: indisponibilidade de infraestrutura (energia, rede,
  túnel, máquina desligada) cobrindo o período medido; evidência insuficiente
  para distinguir execução de falha. Registrar causa; piloto pode repetir
  os dias afetados.
- **Interrompido**: decisão explícita do operador ou defeito bloqueante;
  seguir 3.12/3.13 e preservar evidências.

A ausência de erros reportados **não** é prova: cada dia exige confirmação
positiva de execução e, nas restaurações, comparação de conteúdo.

### Lacuna de observabilidade identificada

O produto não emite alerta proativo de "backup esperado não ocorreu" —
a verificação é manual via `history`/portal no registro diário. Mínimo
proposto (não implementado nesta entrega): um comando ou consulta que
liste "última execução por tarefa + esperada pela agenda", ou campo na
central com o mesmo cálculo. Registrar como pendência de produto; o
registro diário acima cobre a lacuna durante o piloto.

## Limites desta preparação

Sem acesso a PC/VM/LAN, credenciais, contas ou bancos operacionais. Sem
início do piloto, sem novas funcionalidades, sem alteração do pacote
congelado. Comandos conferidos contra o código; a execução real depende
das decisões da seção 2.
