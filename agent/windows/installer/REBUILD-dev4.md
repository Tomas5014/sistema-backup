# Recompilar a correção do instalador 0.1.4-dev4-lab

**Disparo manual:** antes do aceite, execute o [gate COM isolado e teste ZIP](MANUAL-RUN.md) com o SHA novo aprovado. Simulação Linux não confirma o marshaling do Windows PowerShell 5.1.

**Gate obrigatório antes de outro instalador:** execute a [prova de troca de diretórios](DIRECTORY-SWAP.md) em Windows PowerShell 5.1/NTFS com o novo SHA aprovado. A falha nativa de rename da base 87158ac ainda exige essa conferência.

Este roteiro substitui os exemplos incompletos de build dev4 anteriores.
Somente laboratório. Preserve o instalador 0.1.3 e o build dev4 anterior.
Não houve alteração na Central ou nos formatos de configuração/fila.

## Evidência e alcance

O operador relatou compilação real com **NSIS 3.11** do commit
`0381ba7b7695125655c437912119fc7579dd2e09`, seguida de recusa na atualização
0.1.3. Relatou também comparação SHA-256 de todos os arquivos instalados com a
referência `7d62a800325146d1357f8832b1df5c4c3b2eff8d`, após desabilitar conversão
Git de quebras de linha. Esta evidência é do operador, não uma execução nativa
nesta sessão. A correção ainda exige recompilação e repetição na VM.

O leitor e o gerador compartilham a lista exata de módulos, incluindo
`Assisted-Recovery.ps1`; testes conferem instalação/remoção dessa lista no NSIS.
Arquivos inesperados no manifesto permanecem recusados. SHA-256 aceita ambos
os casos hexadecimais, exige exatamente 64 caracteres e é normalizado antes de
comparações e gravação de recibo/journal. Conteúdo diferente continua recusado.

## Administrador — preparar e compilar no Windows

Use Windows PowerShell 5.1, Git, NSIS 3.11 e dois artefatos Restic confiáveis:
o aprovado para a candidata e o que compôs a instalação 0.1.3 (podem ser o
mesmo somente se isso tiver sido aprovado/conferido). Os hashes esperados vêm
da referência confiável; não use o executável instalado desconhecido para
inventar sua própria referência. Nenhum comando pede senha ou chave.

Execute a partir de um clone Windows com o commit final desta correção já
obtido. Informe o **SHA completo aprovado do relatório da entrega**, não apenas
um nome de branch. O checkout de build é novo; não reaproveite payload anterior.

```powershell
$ErrorActionPreference = 'Stop'
$repo = (Get-Location).Path
$commit = Read-Host 'SHA completo aprovado desta correcao Windows'
if ($commit -cnotmatch '^[a-f0-9]{40}$') { throw 'SHA invalido' }
$resolved = & git -C $repo rev-parse --verify "$commit^{commit}"
if ($LASTEXITCODE -ne 0 -or $resolved -cne $commit) { throw 'Commit indisponivel' }
$build = Join-Path $env:TEMP ('macip-dev4-build-' + [Guid]::NewGuid().ToString('N'))
& git -C $repo -c core.autocrlf=false -c core.eol=lf worktree add --detach $build $commit
if ($LASTEXITCODE -ne 0) { throw 'Checkout de build falhou' }
$installer = Join-Path $build 'agent\windows\installer'
$generator = Join-Path $installer 'New-ReleaseManifest.ps1'
$payload = Join-Path $installer 'payload'
if (Test-Path -LiteralPath $payload) { throw 'Payload precisa ser novo' }
$restic = Read-Host 'Caminho do restic.exe aprovado para a candidata'
$resticHash = Read-Host 'SHA-256 aprovado desse Restic'
$candidateText = & $generator -Commit $commit -Version '0.1.4-dev4-lab' `
  -Restic $restic -ResticHash $resticHash -Output $payload
$candidate = $candidateText | ConvertFrom-Json
$manifestHash = $candidate.manifest_sha256
$legacyRestic = Read-Host 'Caminho do Restic original aprovado da 0.1.3'
$legacyResticHash = Read-Host 'SHA-256 aprovado desse Restic original'
$legacy = Join-Path $env:TEMP ('macip-legacy-reference-' + [Guid]::NewGuid().ToString('N'))
& $generator -Commit '7d62a800325146d1357f8832b1df5c4c3b2eff8d' `
  -Version '0.1.3' -Restic $legacyRestic -ResticHash $legacyResticHash `
  -Output $legacy -Legacy
Copy-Item -LiteralPath (Join-Path $legacy 'MANIFEST.json') `
  -Destination (Join-Path $payload 'LEGACY-MANIFEST.json')
$legacyHash = (Get-FileHash -LiteralPath (Join-Path $payload 'LEGACY-MANIFEST.json') -Algorithm SHA256).Hash
$nsis = (Get-Command makensis.exe -ErrorAction Stop).Source
Push-Location $installer
try {
  & $nsis /VERSION
  if ($LASTEXITCODE -ne 0) { throw 'NSIS indisponivel' }
  & $nsis '/DVERSAO=0.1.4-dev4-lab' "/DMANIFEST_SHA256=$manifestHash" `
    "/DLEGACY_SHA256=$legacyHash" 'macip-backup.nsi'
  if ($LASTEXITCODE -ne 0) { throw 'Compilacao NSIS falhou' }
  Get-FileHash -LiteralPath '.\MacipBackupSetup-0.1.4-dev4-lab.exe' -Algorithm SHA256
} finally { Pop-Location }
```

O gerador usa `git -c core.autocrlf=false -c core.eol=lf archive` e verifica
cada arquivo exportado com `git hash-object --no-filters` contra o blob do
commit. Isso evita depender de `core.autocrlf` global/local do Git Windows.
Não é necessário alterar a configuração global nem reescrever a instalação.
Se atributos/filtros ainda produzirem bytes divergentes, o build recusa em vez
de aceitar uma referência transformada. Nem working tree sujo nem arquivos
instalados são fonte do manifesto. Registre commit, versão, NSIS, hashes de
Restic, MANIFEST/LEGACY-MANIFEST e instalador; guarde os artefatos anteriores.

## Conferir e repetir a prova nativa

As suítes abaixo usam diretórios temporários e tarefas/processos simulados;
não alteram tarefas reais. Execute no checkout de build:

```powershell
& (Join-Path $build 'agent\windows\tests\run-installer-contract-tests.ps1') -Commit $commit
& (Join-Path $build 'agent\windows\tests\run-update-tests.ps1')
```

Na VM autorizada, preserve a evidência da 0.1.3, configurações, DPAPI, chave,
fila, pausas individuais e tarefas. Feche o aplicativo e aguarde trabalhos.
Execute **o novo instalador conferido**, com UAC. Confira versão, arquivos,
recibo, tarefas e pausas; depois backup/monitoramento e restauração previstos
no roteiro integrado. Nenhuma prova Linux substitui esta atualização real.
A reversão para 0.1.3 recusa dados/fila ou tarefa de sincronização incompatíveis;
não apague fila nem altere marcador de formato para forçá-la.

## Diagnóstico e recuperação

Falhas iniciais mostram causa sanitizada e ação concreta, por exemplo
`[manifest-integrity]` ou `[legacy-reference]`. Não expõem valores recebidos,
conteúdo de arquivos ou exceções brutas. O journal anterior de recuperação não
é sobrescrito antes de preparar a nova transação. Somente após gravar e reler
um diagnóstico com identificador da tentativa atual o erro indica seu caminho.
Um arquivo antigo, ausente ou não gravável nunca é anunciado como diagnóstico
atual. A falha secundária ao gravar diagnóstico não substitui a causa principal.

Se a troca chegou a ser iniciada, preserve código anterior e journal e siga
[UPDATE-ROLLBACK.md](../UPDATE-ROLLBACK.md). Não desinstale, apague dados ou
reative tarefas sem conferir o resultado. Depois de guardar os artefatos e
as evidências, remova somente o worktree e os diretórios temporários criados
por esta compilação. Não use limpeza ampla na instalação ou no repositório.
