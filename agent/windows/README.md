# Agente de backup Windows — MAC IP (piloto)

Primeira versão reutilizável: **um mesmo script** para qualquer cliente.
Tudo que muda de um cliente para outro está no `backup.json`, preenchido
pela MAC IP. Compatível com **Windows PowerShell 5.1**, Restic e OpenSSH
do Windows. Sem integração com o portal, sem retenção automática, sem
SMB/VSS/backup de aplicações, sem instalador — fora desta entrega.

Arquivos desta pasta:

- `Backup-MacipCliente.ps1` — o agente (modos `Preparar` e `Backup`).
- `backup.example.json` — modelo de configuração **sem segredos**.
- `tests/` — regressões (stub de restic e de ssh; nada real é acessado).

## backup.json

Campos ajustados por cliente (exemplo em `backup.example.json`):

| Campo | Dono | Regra |
|---|---|---|
| `origem` | operador | pasta em **volume local** do Windows, caminho absoluto com unidade (`D:\Dados`), existente e legível; caminho de rede recusado |
| `exclusoes` | operador | **lista** de padrões; `[]` = nenhuma exclusão implícita; cada padrão vira um `--exclude` separado. Texto isolado e `null` são recusados; em chave duplicada vale o **último** valor (comportamento do `ConvertFrom-Json`) |
| `repositorio` | operador | `sftp:usuario@host:/caminho` (porta 22) ou `sftp://usuario@host:porta//caminho` (porta própria; `//` = caminho absoluto) |
| `horario` | operador | `HH:mm`; mudar exige atualizar o disparador no Agendador |
| `repositorio_id` | MAC IP | ID Restic do repositório (64 hex); backup **recusado** se o destino divergir do preparo |
| `identidade_ssh` | MAC IP | caminho absoluto da chave privada exclusiva deste cliente |
| `known_hosts` | MAC IP | caminho absoluto do `known_hosts` aprovado |

O usuário SFTP é passado explicitamente ao ssh (`User` no `ssh_config`
gerado), junto de `Port`, `BatchMode`, `IdentitiesOnly`, chave exclusiva
e `StrictHostKeyChecking` — a identidade do servidor nunca é relaxada.

A senha Restic **nunca** vai no JSON: é digitada uma vez no `Preparar` e
gravada protegida por DPAPI (só abre na mesma conta Windows da mesma
máquina) em `restic.dpapi`, dentro do DiretorioEstado com ACL privada.

Limite real: `DiretorioEstado` **não pode conter espaços** (o
`sftp.command` do Restic não cita caminhos) — o `Preparar` recusa com
mensagem clara **antes** de gravar qualquer arquivo. Origem e exclusões
podem ter espaços e acentos normalmente.

## Quem faz o quê

**MAC IP (antes da visita):** provisiona o repositório SFTP (`init`
remoto), entrega `restic.exe`, a chave `*_ed25519` exclusiva, o
`known_hosts` aprovado, a senha Restic pelo cofre e o `backup.json`
preenchido (host/usuário/caminho, `repositorio_id`, horário).

**Administrador Windows (no cliente):**

1. Crie a conta dedicada (ex.: `macip-backup`) com **leitura na origem**
   e direito de **logon em lote** (SecPol → Atribuição de direitos →
   "Fazer logon como um trabalho em lotes").
2. Copie `Backup-MacipCliente.ps1`, `backup.json`, `restic.exe`,
   `identidade_ssh` e `known_hosts` para pastas **sem espaços**
   (ex.: `C:\MACIP\agente\`, `C:\MACIP\id\`).
3. Abra uma sessão **na conta dedicada** (a mesma que rodará a tarefa —
   o DPAPI só abre nela) e execute o preparo:

   ```powershell
   powershell -NoProfile -File C:\MACIP\agente\Backup-MacipCliente.ps1 `
     -Modo Preparar -Config C:\MACIP\agente\backup.json `
     -DiretorioEstado C:\MACIP\estado -ResticExe C:\MACIP\agente\restic.exe
   ```

   O preparo valida a configuração, os executáveis, a chave e o
   `known_hosts`; digita a senha uma vez (oculta); confirma que o
   repositório **existe e tem o ID aprovado** — nunca cria nem
   reinicializa repositório. **Recusa pasta de estado já existente**:
   novo preparo é sempre em pasta nova, e uma tentativa recusada não
   deixa artefatos nem toca o estado anterior. Ao final imprime **os
   campos exatos do Agendador** (programa, argumentos absolutos, conta
   e horário).
4. Cadastre a tarefa com os campos impressos — programa
   `powershell.exe`, argumentos `-NoProfile -NonInteractive -File
   "...ps1" -Modo Backup -Config "..." -DiretorioEstado "..."`,
   disparador **diário no `horario`**, conta dedicada, opção
   **"executar estando o usuário conectado ou não"** e **"não iniciar
   nova instância"** (impede sobreposição). Se a política de execução
   bloquear o script, o administrador decide na conta (ex.:
   `RemoteSigned` em `CurrentUser` ou assinatura) — o script não altera
   políticas.

   Equivalente por linha de comando (confira com os campos impressos;
   `schtasks /Create` **recusa** se o nome já existir — para regravar a
   tarefa do piloto, apague antes com `schtasks /Delete /TN
   "MACIP-Backup-Restic"`, somente essa tarefa):

   ```powershell
   schtasks /Create /TN "MACIP-Backup-Restic" /SC DAILY /ST 02:30 `
     /RU "macip-backup" /RP * /RL LIMITED `
     /TR "powershell -NoProfile -NonInteractive -File C:\MACIP\agente\Backup-MacipCliente.ps1 -Modo Backup -Config C:\MACIP\agente\backup.json -DiretorioEstado C:\MACIP\estado"
   ```

## Mudanças depois do preparo

- **Horário:** edite `horario` no `backup.json` e atualize o disparador
  (`/ST` no `schtasks /Change` ou a GUI). O script não é editado.
- **Origem ou exclusões:** edite o `backup.json`; o próximo backup já
  usa os novos valores (o vínculo é com destino e identidade, não com a
  origem).
- **Destino, usuário, porta, chave, known_hosts ou `ssh_config`:** o
  backup é **recusado** — o preparo registra caminho **e** SHA-256 da
  identidade e do `known_hosts`, além do SHA-256 do `ssh_config` gerado.
  Uma cópia com bytes idênticos **em outro caminho** também é recusada,
  pois o SSH continua lendo o caminho aprovado no preparo. Rode `-Modo
  Preparar` em **outra** pasta de estado (`-DiretorioEstado
  C:\MACIP\estado-novo`), confirme que o preparo concluiu com o ID novo
  e **só depois** aponte a tarefa para a pasta nova. O estado antigo
  permanece intacto para restaurações; o `ssh_config` nunca é regravado
  fora do preparo.

## Verificar

- `Get-ScheduledTaskInfo "MACIP-Backup-Restic"` → `LastTaskResult`
  (`0` = sucesso; `1`, `3`… = falha).
- `DiretorioEstado\ultimo-resultado.json` — `inicio_utc`, `fim_utc`,
  `conta`, `status` (`em_execucao`/`sucesso`/`falha`), `codigo_restic`
  e `snapshot`. **Falha de execução vs. sem execução nova:** confira
  `inicio_utc` — se o horário é anterior ao disparo esperado, a tarefa
  não rodou (problema de Agendador/conta, não de backup). O Restic só
  gera `status: sucesso` com código `0` **e** snapshot válido; código
  `3` (leitura incompleta) retorna erro ao Agendador.
- Localizar snapshots: `restic -r <repositorio> snapshots` com o mesmo
  `ssh_config` (a senha sai de `restic.dpapi` — comando no cofre).
- Restaurar em **pasta separada** (nunca sobre a origem):
  `restic -r <repo> restore <snapshot_id> --target C:\MACIP\restore-teste`.

## Interromper o piloto

Desabilite **somente** a tarefa do piloto —
`Disable-ScheduledTask -TaskName "MACIP-Backup-Restic"`. **Preserve os
snapshots**: não apague o repositório nem rode `forget`/`prune` durante
o piloto.

## Testes e limites da prova

`tests/run-windows-agent-tests.ps1` (pwsh ou PS 5.1) cobre: validação
de `backup.json` (incluindo `backup.example.json`), formas de
repositório canônica e com porta, exclusões vazia/preenchida/um item/
com espaço e acento, usuário+porta explícitos no `ssh_config`, destino
divergente recusado **antes** do `restic backup`, vínculo
repositório/ID/chave/known_hosts com o preparo, recusa de estado
existente com preservação byte a byte, limpeza de tentativa recusada,
JSON inválido após sucesso gerando falha nova, códigos 0/1/3, senha
ausente de argv/logs/resultado e duas configurações com o mesmo script.
Com `MACIP_RESTIC_REAL=<restic 0.18.1>`, uma seção extra executa o
**Restic real** com um ssh que só registra argumentos — prova de que o
endereço é aceito e usuário/porta chegam ao comando, sem conexão.

Fora do Windows a fronteira DPAPI é substituída nos testes acima.

## Interface gráfica (piloto)

`gui/MacipBackup-Gui.ps1` — WinForms em PS 5.1, sem dependência nova.
Dot-sourcing do próprio `Backup-MacipCliente.ps1`: a validação do
`backup.json`, o `ssh_config` restrito, a conferência do ID do
repositório e o `Preparar` (DPAPI na conta) são os mesmos do agente.

- **Ativar:** importa um `perfil.json` da MAC IP (repositório, ID
  esperado e `known_hosts` embutidos — dados, sem segredos; perfis com
  campo de senha/chave são recusados). Chave privada e senha Restic
  entram separadas pela tela. **Testar conexão** prova identidade do
  servidor + ID do repositório em pasta temporária, sem gravar estado;
  ativação só libera se os dois conferirem.
- **Configurar:** seletor de pasta do Windows, exclusões opcionais e
  horário diário. Salvar é **transacional em duas fases**: primeiro um
  conjunto novo (`config-N`, identidade e `backup.json`; `estado-N` novo
  só quando o perfil muda — DPAPI do estado anterior é reaproveitado se
  o destino é o mesmo, e nesse reuso o SID da sessão precisa ser o SID
  gravado no `preparo.json`) é montado e validado **sem tocar** no
  conjunto ativo; depois a tarefa `MACIP-Backup` é registrada e só então
  os metadados apontam para o conjunto novo. Cancelamento da janela de
  credencial, falha no preparo ou erro no COM descartam o conjunto novo
  e restauram a definição anterior da tarefa; se a reposição falhar ou
  não houver anterior, a tarefa **permanece apontando para arquivos
  preservados e funcionais** — a limpeza só acontece quando o Agendador
  confirma que nada referencia o conjunto. A tarefa usa
  `TASK_LOGON_PASSWORD` (**1**, não S4U) na mesma conta da sessão e só
  é alterada se estiver marcada como do aplicativo — tarefa homônima
  estranha é recusada. Salvar duas vezes atualiza a mesma tarefa, nunca
  duplica.
- **Operar:** pasta protegida, próxima execução e último resultado com
  classificação em sucesso/falha/incompleto/em execução/**sem resultado
  novo**/nunca executou — um resultado antigo não conta como prova da
  execução atual (inicio_utc × LastRunTime). *Executar agora* dispara a
  **mesma** tarefa validada por **SID**: a conta em
  `Definition.Principal.UserId` precisa resolver para o mesmo SID do
  `preparo.json` do estado que a tarefa usa (mesmo nome em outro
  domínio não equivale; SID ilegível recusa); *Pausar/Retomar* habilita
  a tarefa; execução em andamento bloqueia novo disparo.
- **Restaurar:** pontos via `restic snapshots`; arquivos via
  `restic ls --json` filtrados a **arquivos regulares** — a escolha vem
  da lista, nunca de texto livre. O destino é resolvido em **todos os
  componentes** (link/junction até em pasta pai, mesmo com a final
  inexistente) e precisa ser **vazio e fora da pasta protegida**; antes
  de listar/restaurar o destino e o ID do repositório são conferidos.
  `restic dump` traz **somente o arquivo escolhido** — nome com
  `[`/`]`/`*` é literal — gravado em **temporário exclusivo** com
  `stdout`/`stderr` drenados em paralelo e **tempo limite** (processo
  pendurado é morto); o arquivo só é **publicado no nome final depois
  de código 0** (`File.Move` não sobrescreve — colisão preserva o
  existente; falha remove só o parcial), e a tela só mostra sucesso
  quando o arquivo esperado existe no destino.
- **Conta:** a GUI roda na conta dedicada do backup — DPAPI, preparo e
  tarefa ficam na mesma identidade; se for aberta elevada em outra
  conta, a conta da tarefa registrada será a da sessão, e o aviso na
  aba 1 mostra qual é.

Instalador: `installer/macip-backup.nsi` + `installer/BUILD.md`
(`makensis /DVERSAO=…` gera `MacipBackupSetup-<versao>.exe`; verifica
OpenSSH e copia agente + GUI + `restic.exe` aprovado, sem segredos).
Dados ficam em `%ProgramData%\MacipBackup` (sem espaços) — o instalador
resolve o diretório comum com `SetShellVarContext all`, o **mesmo**
caminho da interface, e cria o atalho no Menu Iniciar de todos os
usuários (visível à conta dedicada do backup). Desinstalar delega a
decisão ao `Desinstalar-Tarefa.ps1` (**consulta estruturada** via
Schedule.Service: propriedade por marca+ação, desabilita primeiro,
confirma sem instâncias ativas/pendentes) — sem confirmação, executáveis
preservados e falha informada — e sempre preserva dados/estado.

Uso por script continua igual — a GUI não altera o agente.

## Homologação

**Homologação de laboratório (11/11/2026, 14h22 Brasília), relatada
pelo operador:** o Agendador disparou a tarefa em Windows PowerShell
5.1 **com a sessão encerrada** — Agendador e Restic retornaram `0` e o
agente registrou `sucesso`. Snapshot
`f0d283eed89cc54322dc22e2d1900c0ef8e3ba30f5ba9d9b41fffb80f3aa823a`,
`restic check --read-data` sem erros no servidor SFTP e restauração
independente da VM conferida por SHA-256
(`amostra.txt`, 43 bytes) com as exclusões respeitadas. O pacote
instalado na prova foi `macip-agente-windows-5a2f3f8.zip`, SHA-256
`4d9489626e6920b510b533c4f3e83215ef42e18e07439a6c1974f0f694945be0`.
Evidência registrada em `docs/piloto-windows-sftp.md` — é prova de
laboratório, não aceite de cliente nem de produção.
