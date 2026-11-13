# Piloto Windows + SFTP — roteiro do operador

Um operador deve conseguir seguir este roteiro do preparo ao
encerramento sem editar o agente. Escopo: **uma** máquina Windows, **uma**
pasta local não crítica, um repositório SFTP **exclusivo** na MAC IP e
**7 dias** de acompanhamento. Os backups atuais do cliente **permanecem
em operação** — o piloto não substitui nada.

Agente homologado em laboratório (ver seção Evidência): commit
`5a2f3f882fb11ecb33b34e5c63ad70c967d1d027`. O pacote **instalado** na
prova foi `macip-agente-windows-5a2f3f8.zip`, SHA-256
`4d9489626e6920b510b533c4f3e83215ef42e18e07439a6c1974f0f694945be0`.
(O arquivo `agente-windows-5a2f3f882fb1.zip`, SHA-256
`1320310ac065b641b21212ab22190a200289534f2348ea115a36954f82f4cff6`,
contém fontes do mesmo commit, mas é outro artefato e **não** foi o
instalado nessa prova — use sempre o nome e o hash do pacote de
instalação ao conferir o kit.)

Status do piloto: **não iniciado — depende da autorização registrada do
cliente e das duas provas pendentes (seções 6 e 7) executadas com dados
fictícios.**

## 1. Ficha do piloto (preencher ANTES da instalação)

| Campo | Valor | Exemplo (fictício) |
|---|---|---|
| Autorização do cliente (quem/data) | ______ | "João Silva, 10/11" |
| Máquina Windows | ______ | "SRV-ARQ-01, Windows Server 2019" |
| Pasta autorizada | ______ | "D:\Documentos\Setor" |
| Volume da pasta | ______ | "D: (disco local, NTFS)" |
| Exclusões aprovadas | ______ | "*.tmp, cache local\*.dat" |
| Horário do backup (HH:mm) | ______ | "02:30" |
| Janela de tolerância | ______ | "até 06:00 do dia seguinte" |
| Retenção pretendida | ______ | "manter tudo durante o piloto" |
| Responsável pelos alertas | ______ | "monitoramento@macip" |
| Meta de recuperação | ______ | "pasta restaurada em < 2h" |

**Regra:** a pasta precisa estar definida e autorizada **antes** da
instalação — o `backup.json` é preenchido com ela.

## 2. Evidência comprovada em laboratório

Homologação de laboratório **relatada pelo operador** para o agente
`5a2f3f8` — **não é** aceite de cliente nem validação de produção:

- 11/11/2026, 14h22 (Brasília): o Agendador disparou a tarefa em
  **Windows PowerShell 5.1**, com a sessão encerrada. Agendador e Restic
  retornaram `0`; `ultimo-resultado.json` registrou `status: sucesso`.
- Snapshot: `f0d283eed89cc54322dc22e2d1900c0ef8e3ba30f5ba9d9b41fffb80f3aa823a`.
- `restic check --read-data` no servidor SFTP: terminou **sem erros**.
- Restauração feita **sem depender da VM**: apenas `amostra.txt` (43
  bytes), SHA-256
  `18c4b63c718f062e9da0717912869c9364197fd75242e446021a334b64e3fc56`.
- Origem continha espaço e acento; as exclusões `ignorar.tmp` e
  `cache temporário.log` foram respeitadas (ausentes do snapshot).

## 3. Preparação pela MAC IP (antes da visita)

1. Provisionar repositório SFTP **exclusivo** do piloto (não reutilizar
   o do laboratório nem o de outro cliente): criar a conta SFTP restrita
   ao diretório do piloto, rodar `restic init` remotamente e anotar o
   `repositorio_id` (`restic cat config`).
2. Conferir capacidade (espaço ≥ volume estimado + margem) e
   conectividade (porta SFTP alcançável a partir da rede do cliente).
3. Conferir a **chave do servidor** contra o `known_hosts` que vai no
   kit — a entrega inclui a chave esperada, nunca a relaxada.
4. Custodiar a **senha Restic** no cofre da MAC IP; ela só entra na
   máquina do cliente via `Preparar` (DPAPI), nunca no JSON.
5. Gerar a chave SSH **exclusiva** do piloto e o `backup.json`
   preenchido (repositorio, repositorio_id, caminhos, horário).
6. Executar a **prova da cópia protegida** (seção 6) com dados fictícios
   e registrá-la — bloqueio para dados reais se não passar.
7. Executar a **prova de alertas** (seção 7) com dados fictícios —
   acompanhamento manual é a medida provisória enquanto a integração
   com o portal não é comprovada.

Kit entregue: `Backup-MacipCliente.ps1`, `backup.json`, `restic.exe`,
chave SSH, `known_hosts`, senha via cofre.

## 4. Instalação no Windows

O que o **administrador do cliente** faz **uma vez**; o resto o técnico
faz **pela interface**, sem terminal, JSON ou Agendador.

### 4a. Administrador (uma vez)

1. Criar a **conta dedicada** do backup (ex.: `macip-backup`) com
   leitura na pasta protegida e direito de **logon em lote** ("Fazer
   logon como trabalho em lotes"). Sem esse direito a tarefa não dispara
   com a sessão encerrada — a interface avisa se o registro falhar por
   permissão, mas **o cadastro por si só não comprova o agendamento**:
   a prova é o disparo real (item 6 abaixo).
2. Conferir SHA-256 de `MacipBackupSetup-<versao>.exe` (gerar por
   `agent/windows/installer/BUILD.md`) — abortar se ausente/divergente.
3. Executar o instalador **sem argumentos** — instala **sempre** em
   `%ProgramFiles%\MACIP-Backup` (protegido por ACL contra usuários
   comuns; `/D=` e pasta alternativa são recusados), registra o app em
   **Aplicativos instalados** e cria `%ProgramData%\MacipBackup` para
   dados. Entregar ao técnico: **perfil `.json` da MAC IP, chave SSH
   privada e senha Restic** (chave e senha por canal seguro).

### 4b. Técnico (pela interface, na conta dedicada)

1. Abrir **MAC IP Backup** no Menu Iniciar, **na conta dedicada** —
   preparo, senha DPAPI e tarefa ficam nessa conta.
2. **Aba 1 — Validar e continuar:** *Importar perfil*, *Chave privada*,
   *Senha Restic* → um clique só. Avança **somente** quando a identidade
   do servidor e o ID do repositório conferem; mudar perfil, chave ou
   senha depois exige nova validação.
3. **Aba 2 — Salvar e agendar:** só pasta, horário e exclusões. A senha
   Windows é pedida na janela nativa; salvar com a tarefa **pausada
   preserva a pausa** (só *Retomar* religa).
4. **Aba 3 — Operar:** mostra a **situação deste disparo** (o sucesso de
   outro disparo nunca conta como novo), o **último backup confirmado**
   com data/hora, a **próxima execução** e a **ação necessária**. O botão
   *Executar primeiro backup* dispara a mesma tarefa — "Disparo
   SOLICITADO" e a conclusão só aparece com resultado do disparo.
5. Prova sem sessão: encerrar a sessão antes do horário, religar e
   conferir *BACKUP CONCLUIDO* (resultado vinculado ao disparo) ou a
   ação indicada.
6. **Aba 4 — Restaurar:** ponto, arquivo (seleção literal), pasta
   separada e vazia — o original nunca é sobrescrito; vínculos
   SSH/destino são reconferidos antes de qualquer acesso.

### 4c. Pelo script (diagnóstico)

O fluxo por script abaixo segue válido **apenas para diagnóstico** — a
operação normal do piloto é a interface acima.

### 4b. Pelo script (alternativa/diagnóstico)

1. Conferir o pacote de instalação — **abortar** se o arquivo estiver
   ausente ou o hash divergir:

   ```powershell
   $zip = 'C:\MACIP\macip-agente-windows-5a2f3f8.zip'
   if (-not (Test-Path -LiteralPath $zip -PathType Leaf)) { throw "Pacote ausente: $zip" }
   $h = (Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash.ToLower()
   if ($h -cne '4d9489626e6920b510b533c4f3e83215ef42e18e07439a6c1974f0f694945be0') {
       throw "SHA-256 divergente: $h — abortar instalacao"
   }
   ```

2. Criar a **conta dedicada** (ex.: `macip-backup`) com leitura na pasta
   e direito de **logon em lote**; criar pastas sem espaço (ex.:
   `C:\MACIP\agente`, `C:\MACIP\id`, `C:\MACIP\estado`).
   Se faltar o direito de logon em lote à conta, **encaminhar ao
   administrador responsável** — a tarefa sem esse direito não dispara
   com a sessão encerrada.
3. **Operador MAC IP + administrador** preenchem o `backup.json` juntos:
   origem/exclusões/horário do cliente + campos MAC IP.
4. Na sessão da **conta dedicada**:

   ```powershell
   powershell -NoProfile -File C:\MACIP\agente\Backup-MacipCliente.ps1 `
     -Modo Preparar -Config C:\MACIP\agente\backup.json `
     -DiretorioEstado C:\MACIP\estado -ResticExe C:\MACIP\agente\restic.exe
   ```

   Recusas (config, credencial, destino divergente) abortam sem deixar
   pasta; sucesso imprime os campos exatos do Agendador — **guarde-os**.

5. Cadastrar a tarefa **nova** pela **interface do Agendador**
   (`taskschd.msc` → *Criar Tarefa*, não *Criar Tarefa Básica*), nome
   exclusivo, ex.: `MACIP-Backup-Piloto`:

   - **Geral:** conta = **a mesma do `Preparar`**; marcar **"Executar
     estando o usuário conectado ou não"**. Ao salvar, o Windows pede a
     senha da conta **somente na janela nativa** — nunca em linha de
     comando.
   - **Ações:** programa `powershell.exe` (caminho impresso pelo
     `Preparar`); colar os argumentos **exatamente** como impressos —
     conferir que o campo salvou o texto completo e **sem prefixo de
     lista** como `1. ` no início (erro comum de cópia múltipla).
   - **Disparadores:** **"Uma vez"**, alguns minutos no futuro.
   - Conferir `NextRunTime`:
     `Get-ScheduledTaskInfo -TaskName "MACIP-Backup-Piloto"` →
     `NextRunTime` coerente com o horário escolhido.

6. **Prova sem sessão:** encerrar a sessão do Windows, aguardar o
   horário, religar e conferir:

   - `Get-ScheduledTaskInfo` → `LastRunTime` ≈ horário previsto e
     `LastTaskResult = 0`.
   - `ultimo-resultado.json` **novo**: `inicio_utc`/`fim_utc` coerentes
     com aquele disparo, `status: sucesso`, `snapshot` válido.
   - Snapshot listado no destino e restauração em **pasta separada**
     conferida (ver procedimento da seção 5).

7. **Só depois de aprovado o disparo de prova:** editar o disparador na
   interface para **Diário** no `horario` aprovado e conferir
   `NextRunTime` diário. Mudanças futuras de horário seguem o mesmo
   caminho — `horario` no `backup.json` **e** o disparador devem ser
   atualizados juntos, sem editar o script.

## 5. Restauração na MAC IP (procedimento executável)

Feita **no servidor da MAC IP**, sem depender da máquina do cliente.
Exemplos fictícios — substituir pelos valores do piloto:

```bash
# Pré-condições: usuário MAC IP com acesso ao servidor SFTP, restic e
# jq instalados, ssh_config do piloto e senha no cofre.
REPO='sftp:piloto@backup.macip.example:/dados/piloto'      # do backup.json
ID_ESPERADO='<repositorio_id do backup.json, 64 hex>'
SSHCMD='ssh -F /etc/macip/piloto/ssh_config -s backup.macip.example sftp'

# Senha pelo procedimento do cofre — NUNCA impressa nem em argumentos:
export RESTIC_PASSWORD_COMMAND='gpg --decrypt /cofre/piloto/pw.gpg'

# 1. Confirmar que e o repositorio certo (o ID precisa bater):
REAL_ID=$(restic -r "$REPO" -o "sftp.command=$SSHCMD" cat config | jq -r .id)
[ "$REAL_ID" = "$ID_ESPERADO" ] || { echo "ID diverge: $REAL_ID"; exit 1; }

# 2. Integridade do repositorio:
restic -r "$REPO" -o "sftp.command=$SSHCMD" check --read-data

# 3. Localizar o snapshot exato e a subpasta da origem no catalogo:
SNAP='f0d283ee'  # prefixo do snapshot a restaurar
restic -r "$REPO" -o "sftp.command=$SSHCMD" snapshots | grep "$SNAP"
restic -r "$REPO" -o "sftp.command=$SSHCMD" ls "$SNAP" | grep -i 'dados exemplo'

# 4. Restaurar em DIRETORIO NOVO (nunca sobre a origem):
mkdir -p /tmp/restaura-piloto
restic -r "$REPO" -o "sftp.command=$SSHCMD" restore "$SNAP" \
    --target /tmp/restaura-piloto

# 5. Conferir manifesto de SHA-256 e as exclusoes:
find /tmp/restaura-piloto -type f -exec sha256sum {} + | sort \
    > /tmp/restaura-piloto.sha256
diff /tmp/restaura-piloto.sha256 /cofre/piloto/manifesto-origem.sha256
! grep -q 'ignorar.tmp\|cache tempor' /tmp/restaura-piloto.sha256
```

Cada arquivo restaurado deve bater o manifesto da origem; qualquer
arquivo excluído presente é falha do ensaio.

## 6. Prova pendente A — cópia protegida (dados fictícios)

Objetivo: provar que (a) a credencial do cliente **não** altera nem
apaga a cópia e (b) a MAC IP restaura **somente pela cópia**, sem o
repositório primário nem a máquina Windows. Executada **antes** de
qualquer dado real.

**Preflight:** capacidade da área protegida ≥ tamanho do repositório;
isolamento em **domínio de falha independente** (outra conta,
armazenamento ou local — não é isolamento apenas de permissão no mesmo
disco, que não sobrevive à perda dele); dados **sintéticos** apenas.

```bash
# 1. Repositorio sintetico + um backup:
mkdir -p /tmp/ensaio/origem && echo dado > /tmp/ensaio/origem/a.txt
restic -r /tmp/ensaio/repo-primario init            # senha de ensaio
restic -r /tmp/ensaio/repo-primario backup /tmp/ensaio/origem
# 2. Copia consistente no destino protegido (metodo conforme a
#    infraestrutura escolhida; ex.: sincronizacao file-level ou
#    'restic copy'). Apos copiar, a integridade da copia e exigida:
restic -r /protegido/repo-copia check --read-data
# 3. Sentinela descartavel na area protegida:
echo MACIP-SENTINELA > /protegido/sentinela.txt
```

**Teste de separação:** com a credencial equivalente à do cliente
(conta SFTP restrita ao diretório do piloto), tentar alterar e apagar
**somente** `/protegido/sentinela.txt` — as duas tentativas devem
falhar; a sentinela permanece intacta (hash conferido).

**Teste de restauração só pela cópia:** em um host da MAC IP, sem acesso
ao primário nem à máquina Windows: `restic -r /protegido/repo-copia
restore <snap> --target /tmp/ensaio/restore` → `sha256sum` dos arquivos
batem com a origem sintética.

**Aprovado se:** sentinela intacta + restauração pela cópia completa.
**Não conta como cópia:** acesso somente-append no próprio primário;
**não prova sobrevivência:** isolamento de permissão no mesmo disco.
Se o armazenamento da cópia ainda não foi escolhido: ⏳ **pendência de
decisão** — critérios a registrar: domínio de falha independente,
integridade verificável por `check --read-data`, e prazo de restauração
compatível com a meta da ficha. Não presumir tecnologia.

## 7. Prova pendente B — alertas (dados fictícios)

Verificar os dois avisos com dados **sintéticos**, **antes** dos dados
reais. A conferência manual na ficha é a medida provisória — a
integração Windows → portal **não está comprovada** e não se assume
cobertura automática.

| Caso | Como provocar | Quem confere | Prazo | Correlação ao disparo | Evidência de recebimento |
|---|---|---|---|---|---|
| Execução com falha/incompleto | origem de teste indisponível → `status: falha`, `codigo_restic` ≠ 0 | operador designado na ficha | até o fim da janela | `inicio_utc` do resultado ≈ `LastRunTime` do disparo | registro (print/ticket) assinado pelo responsável |
| Ausência de execução nova | disparo não acontece (tarefa desabilitada ou máquina desligada no ensaio) | idem | idem | `inicio_utc` do último resultado **anterior** ao esperado + `LastRunTime` sem o horário | idem |

Nota: um resultado **antigo ou ausente** não prova que a tarefa nunca
rodou — correlacionar sempre com `LastRunTime`/`NextRunTime` do
Agendador antes de classificar como ausência. O ensaio só fecha quando
o responsável **reconhece** o recebimento dos dois avisos.

## 8. Monitoramento — 7 dias

Ficha de acompanhamento (uma linha por disparo):

| Dia | Esperado | Disparou | Resultado | Snapshot | Duração | Espaço livre destino | Cópia protegida |
|---|---|---|---|---|---|---|---|
| 1–7 | 02:30 | ___ | sucesso/falha | ___ | ___ | ___ | íntegra/verificar |

- **Falha ou incompleto:** `LastTaskResult` ≠ 0 ou `ultimo-resultado.json`
  com `falha`/`codigo_restic=3` → investigar no dia.
- **Ausência de execução:** `inicio_utc` anterior ao disparo esperado ao
  fim da **janela de tolerância** → a tarefa não rodou (Agendador/conta,
  não Restic). Alerta distinto — ver regra de correlação na seção 7.
- **Snapshots:** todos preservados durante o piloto — sem
  `forget`/`prune`.

## 9. Aceite

Aprovação exige, registrado na ficha:

- Autorização do cliente registrada **antes** da instalação.
- Evidência dos **7 disparos** (tabela completa, sem execução ausente
  ou com explicação aceita).
- Prova da **cópia protegida** aprovada (seção 6) e evidência dos
  **dois alertas** entregues ao responsável (seção 7).
- Restauração de um **ponto recente** e de um **anterior**; pelo menos
  uma delas a partir da **cópia protegida**, sem depender da máquina
  original — SHA-256 comparado à origem.
- Comparar o tempo de restauração com a **meta de recuperação**;
  registrar quem aprova (nome/data do cliente + MAC IP). A aprovação
  do laboratório **não** libera produção.

## 10. Interrupção

Para interromper: desabilitar **somente** a tarefa do piloto —
`Disable-ScheduledTask -TaskName "MACIP-Backup-Piloto"`. **Preservar**
o repositório, os `ultimo-resultado.json`/logs e todos os snapshots —
o encerramento decide o destino dos dados, não o operador de plantão.
