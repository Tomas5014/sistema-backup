# Disparo manual dev4 — comprovação COM antes do aceite

Correção baseada em `ff714cd244bfade1a38264371e326ca89ebd3792`, em branch
Windows separada. Preserve a Central, instalações, tarefas e dados existentes.
Versão continua 0.1.4-dev4-lab; contratos e formatos persistentes não mudaram.

## Evidência e hipótese

O operador relatou Windows PowerShell 5.1, tarefa configurada, execução agendada
sem sessão já aprovada, A disponível e origem existente. Após Retomar agenda →
Executar backup agora, a GUI mostrou "O valor não recai no intervalo esperado".
O último backup permaneceu 24/11/2026 14:52, snapshot abreviado 4fb7dd6a42a8;
a repetição com ff714cd não resolveu. Isso não prova uma nova execução e não
foi reproduzido nativamente por este agente.

A [API IRegisteredTask.Run](https://learn.microsoft.com/en-us/windows/win32/api/taskschd/nf-taskschd-iregisteredtask-run)
aceita um BSTR único para Arg0 ou uma matriz de BSTRs. O valor anterior
`@('manual')` tem tipo gerenciado Object[], enquanto a correção usa
`[string]'manual'`. A [documentação de marshaling COM](https://learn.microsoft.com/en-us/dotnet/standard/native-interop/type-marshaling)
descreve String como BSTR e String[] como SAFEARRAY de BSTRs. Esse mecanismo
justifica usar a forma escalar explícita, mas não comprova o VARIANT efetivamente
recebido pelo serviço na VM. A causa nativa depende da comparação abaixo.

Não há fallback para Run(null), RunEx ou nova execução por outro processo.
Existe uma única chamada à mesma tarefa. Conta/principal, SID do preparo,
propriedade e concorrência continuam verificados; não se regrava a agenda.

## Gate COM descartável no Windows PowerShell 5.1

Use clone/checkout novo do **SHA completo desta correção**, informado no relatório.
Não execute o helper a partir da pasta instalada. Ele usa tarefa sintética em
pasta exclusiva `MACIP-ManualRunProbe-UUID`, com nome Probe, registra esses nomes,
confirma ausência antes da criação e confere propriedade antes de atualizar,
parar ou remover. Não consulta MACIP-Backup/MACIP-Backup-Sync instaladas.

A ação só grava evidências fictícias em TEMP; não executa backup ou Restic.
Usa a conta interativa atual (logon INTERACTIVE_TOKEN), sem pedir senha. Política
local pode exigir autorização administrativa para criar a pasta de tarefas;
ausência dessa infraestrutura é recusa/pendência, nunca aprovação.

```powershell
$ErrorActionPreference = 'Stop'
$repo = (Get-Location).Path
$commit = Read-Host 'SHA completo aprovado da correcao de disparo manual'
if ($commit -cnotmatch '^[a-f0-9]{40}$') { throw 'SHA invalido' }
$resolved = & git -C $repo rev-parse --verify "$commit^{commit}"
if ($LASTEXITCODE -ne 0 -or $resolved -cne $commit) { throw 'Commit indisponivel' }
$check = Join-Path $env:TEMP ('macip-run-check-' + [Guid]::NewGuid().ToString('N'))
& git -C $repo -c core.autocrlf=false -c core.eol=lf worktree add --detach $check $commit
if ($LASTEXITCODE -ne 0) { throw 'Checkout falhou' }
$ps51 = Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe'
if ([Environment]::Is64BitOperatingSystem -and -not [Environment]::Is64BitProcess) {
  $ps51 = Join-Path $env:WINDIR 'Sysnative\WindowsPowerShell\v1.0\powershell.exe'
}
& $ps51 -NoProfile -ExecutionPolicy Bypass -File (Join-Path $check 'agent\windows\tests\probe-manual-run-com.ps1') |
  Tee-Object -FilePath (Join-Path $check 'manual-com-native.txt')
if ($LASTEXITCODE -ne 0) { throw 'Gate COM falhou/pendente; preservar saida, nao aprovar' }
& $ps51 -NoProfile -ExecutionPolicy Bypass -File (Join-Path $check 'agent\windows\tests\run-assisted-tests.ps1')
if ($LASTEXITCODE -ne 0) { throw 'Teste ZIP falhou; preservar causa inicial' }
```

Guarde saída completa e exit code. O helper mede o VARIANT produzido pelo
marshaler .NET para cada valor e testa a chamada real com Object[], String[] e
string. Essa medição .NET é complementar; o resultado de Run é a prova da API.
Executa também Start-TarefaAgora real com backend restrito à tarefa da prova,
valida SID/Arg0, resultado novo e confirmação do modelo de tela. Cada tentativa
usa configuração imutável identificada; resultado atrasado de outra tentativa
não é aceito. Evidências são UUIDs novos, não só LastTaskResult=0.

Um trigger real por horário na mesma tarefa confirma scheduled sem parâmetro
manual. Os arquivos têm snapshot **sintético**, claramente marcado; não são
pontos de backup ou prova de SFTP. Prazos são limitados; ao final, remove somente
seus recursos, reconferindo identidade. Se propriedade/limpeza ficarem inconclusivas,
preserva a área e reporta falha. Não remova tarefas ou pastas por glob.

`hypothesis=reproduced-in-this-environment` exige erro da forma Object[] **sem
efeito**, além de sucesso da string escalar. A auditoria final conta também
efeitos atrasados por tentativa. Se Object[] funcionar, não declarar a hipótese
comprovada. String escalar e helper precisam produzir exatamente uma execução
manual da mesma conta; trigger precisa produzir scheduled; cleanup deve terminar.

Este gate não prova conta PASSWORD/execução sem sessão do produto, DPAPI, SFTP
ou WinForms. Não fornece autorização para alterar a instalação/laboratório.
Se interrompido abruptamente, preserve os nomes exclusivos impressos e investigue
somente esses recursos; não use comandos amplos de limpeza.

## Tela e teste ZIP

Recusa de Run passa a ser uma resolução explícita na linha de execução atual.
O último backup bem-sucedido permanece separado e intacto; não significa sucesso
do pedido recusado. A atualização periódica conserva essa recusa até nova
evidência. Uma chamada aceita segue aguardando; só resultado vinculado à execução
nova pode concluir. A referência é capturada antes da chamada ao Agendador.

run-assisted-tests.ps1 carrega System.IO.Compression antes de New-Zip. Construtor,
entrada, ZIP e arquivo têm fechamento protegido: erro secundário de Dispose ou
limpeza não substitui a causa principal. Há falhas controladas para verificar
isso; não equivalem a teste de disco cheio/antivírus em toda VM.

A suíte GUI usa COM simulado em todos os sistemas; o bloco legado que tentava
registrar MACIP-Backup real foi substituído por skip e indicação deste helper.
Somente o helper acima executa a prova COM isolada.

## Recompilação e aceite integrado

Depois de guardar os gates nativos, siga [REBUILD-dev4.md](REBUILD-dev4.md) usando
o mesmo SHA aprovado, NSIS 3.11, payload novo do Git e Restic/referências aprovadas.
Preserve instaladores anteriores e registre SHA-256 real do exe. Gates anteriores
de atualização e recuperação B continuam válidos no seu alcance.

Na janela autorizada da VM, instalar a correção, conferir conta, configuração,
fila, pausas e tarefas. Com A e origem disponíveis, usar Situação → Executar
backup agora. Conferir aguardando/em execução e só depois resultado novo,
horário/UUID/snapshot novos e distinção manual. Recusa não pode virar sucesso
antigo. Conferir também scheduled sem sessão no aceite integrado, separadamente.
Não retomar pausas ou mudar tarefa para contornar erro. Nenhum teste Linux
aprova este aceite nativo; não houve acesso à VM ou laboratório nesta sessão.
