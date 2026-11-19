# Candidata de automação do laboratório Windows

Branch: `feature/windows-backup-lab-automation`, derivada de
`7a5c3acf6b192c4e4cadad445c6ef90c0a02319a`. Use a central
`feature/backup-lab-automation`, migração 13. Os SHAs compatíveis são
fixados em `docs/lab-automation-pins.json` na branch central. Não faça
merge das linhas. A referência Windows 0.1.3 e seu instalador ficam intactos.

Funcionalidades do piloto ficam congeladas após esta entrega. A candidata
proposta é **0.1.4-dev2-lab**; build e homologação na VM ainda são gates.
Não é autorização para usar dados reais.

## Técnico: sem comandos por instalação

1. Operador autorizado cadastra o cliente fictício e clica Preparar
   instalação no painel. A preparação e prova de B são automáticas.
2. Após liberação, o operador gera o código e o entrega pelo canal privado
   do laboratório. O administrador já configurou a confiança TLS da VM.
3. Na aba Ativar, cole o código. A chave privada nasce na conta Windows.
   A nova prova de B é automática; o aplicativo aguarda até 90 segundos,
   com mensagens e possibilidade de retomar o mesmo código e a mesma chave.
4. Após validar identidade SFTP e repositório, siga para Pasta e horário.
   Escolha pasta fictícia, exclusões e horário. Salve e confirme a conta
   pela janela nativa. A senha Windows não passa pelo painel.
5. São registradas duas tarefas na mesma conta: **MACIP-Backup** e
   **MACIP-Backup-Sync**. A segunda executa somente Sincronizar a cada
   cinco minutos, com GUI fechada e sem novo backup. Retoma disparo perdido
   quando disponível, sem paralelismo e com limite de dois minutos da tarefa.
6. Aba Situação mostra Monitoramento sincronizado/Envio pendente e o último
   envio confirmado. Fila vazia não inventa contato novo. Falha de envio
   não muda o resultado do backup; UUID/conteúdo são preservados.
7. Acompanhe no painel backup, cópia protegida e prazo separadamente.
   Cada ponto confirmado em A é copiado automaticamente. Prova seed não
   confirma a cópia de um ponto Windows posterior. Alertas ficam no
   capturador Mailpit do laboratório e são reconhecidos no painel.

Importar perfil permanece alternativa. Perfil sem vínculo mostra
**Monitoramento não configurado** e não exige uma central. A troca para
esse fluxo não apaga fila anterior ou credenciais; confirme a suspensão
recebida pela central antes de abandonar uma instalação monitorada.

## Publicação e segurança local

A tarefa de envio usa Windows PowerShell 5.1, logon por senha na conta
DPAPI, `MultipleInstances=IGNORE_NEW`, repetição PT5M e ExecutionTimeLimit
PT2M. O agente limita cada tentativa a 20 mensagens/45 segundos e cada
requisição HTTPS a até 10 segundos. `sync.lock` impede transmissores
concorrentes; `queue.lock` permite persistir pausa/revisão local sem esperar
uma requisição de rede. A central reconhece agenda e resultado por identidade.

Publicação recusa tarefa alheia, processo ativo ou consulta inconclusiva
antes de alterar o backup. Falha do segundo registro tenta restaurar a
definição anterior. Arquivo ainda referenciado por qualquer tarefa é
preservado. O desinstalador confere e para as duas tarefas antes de remover
executáveis. Não remove fila, DPAPI, configurações ou recuperações.

Pausar backup não suspende sincronização. Alterações diretas no Agendador
fora do aplicativo não são reconciliadas automaticamente. Limites atuais
de fuso monitorado: UTC e America/Sao_Paulo. Falta de espaço/permissão
para persistir resultado ou agenda impede o aceite do piloto.

## Build e aceite

No Windows de build, use o commit fixado, NSIS 3.x e o `restic.exe`
Windows aprovado em `agent/windows/installer/payload/restic.exe`.
Confira seu SHA-256 aprovado antes de compilar:

```powershell
$ErrorActionPreference='Stop'
git rev-parse HEAD
$TrackedChanges=git status --porcelain --untracked-files=no
if($LASTEXITCODE -ne 0 -or $TrackedChanges){throw 'Checkout versionado alterado'}
Set-Location agent\windows\installer
$ApprovedSha256=Read-Host 'SHA-256 aprovado do restic.exe Windows'
if($ApprovedSha256 -cnotmatch '^[0-9a-fA-F]{64}$'){throw 'Hash aprovado invalido'}
if((Get-FileHash .\payload\restic.exe -Algorithm SHA256).Hash -ine $ApprovedSha256){throw 'Payload divergente'}
makensis /DVERSAO=0.1.4-dev2-lab macip-backup.nsi
if($LASTEXITCODE -ne 0){throw 'Build falhou'}
Get-FileHash .\MacipBackupSetup-0.1.4-dev2-lab.exe -Algorithm SHA256
```

Não sobrescreva o 0.1.3. Registre SHA Git, versão/ID do payload, política
assinatura aplicada e SHA-256 do novo instalador. NSIS ou payload aprovado
ausente significa build pendente. O Restic Linux dos testes não é payload.

Testes no Linux usam PowerShell 7.4.6, COM/DPAPI/WinForms simulados e
Restic/SFTP reais em prova separada. Não homologam o Windows. Execute
`run-lab-automation-tests.ps1`, `run-activation-tests.ps1`,
`run-central-alerts-tests.ps1`, `run-gui-tests.ps1` e
`run-windows-agent-tests.ps1` para as regressões pertinentes.

VM pendente: cliques, certificado confiável, DPAPI/COM reais, cancelamento
da confirmação nativa, tarefa sem sessão, sincronização periódica sem
novo backup, restauração de arquivo/pasta e recuperação do ponto Windows
somente por B com A e origem inacessíveis. Roteiro integrado completo na
central: `docs/operacao-automatica-laboratorio.md`.
