#requires -Version 5.1
# Prova com restic REAL (0.18.x) da restauracao da pasta protegida:
# repositorio LOCAL descartavel — nao usa SFTP nem toca o laboratorio.
# Separa o que e REAL (ls --json, id:subpasta, --verify, restore) do
# que continua simulado na suite principal (stub).
# Uso:  pwsh -File run-restic-real-tests.ps1 [-Restic /caminho/restic]
param([string]$Restic = '')

$ErrorActionPreference = 'Stop'
$script:ok = 0; $script:er = 0; $script:skip = 0
function eq($real, $esperado, [string]$nome) {
    if ("$real" -ceq "$esperado") { $script:ok++; Write-Host " ok  $nome" }
    else { $script:er++; Write-Host " FALHOU  $nome  (esperado='$esperado' real='$real')" }
}
function ok($cond, [string]$nome) { eq ([bool]$cond) $true $nome }
function throws($sb, [string]$nome) {
    try { & $sb | Out-Null; $script:er++; Write-Host " FALHOU  $nome (nao lancou erro)" }
    catch { $script:ok++; Write-Host " ok  $nome" }
}
function pula([string]$nome) { $script:skip++; Write-Host " pulou $nome" }

if (-not $Restic) {
    $cmd = Get-Command restic -ErrorAction SilentlyContinue
    if ($cmd) { $Restic = $cmd.Source }
}
if (-not $Restic -or -not (Test-Path -LiteralPath $Restic -PathType Leaf)) {
    pula 'restic real indisponivel — prova adiada (suite simulada segue valendo)'
    Write-Host "`nRESULTADO: $script:ok ok, $script:er falha(s), $script:skip pulado(s)"
    exit 0
}
& $Restic version | Out-Null

$tmp = Join-Path ([IO.Path]::GetTempPath()) ('rest-real-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory $tmp -Force | Out-Null
try {
    # ---- GUI dot-sourced (mesma carga da suite simulada) ----
    $gui = Join-Path $PSScriptRoot '..\gui\MacipBackup-Gui.ps1'
    $agente = Join-Path $PSScriptRoot '..\Backup-MacipCliente.ps1'
    . $agente
    . $gui -BaseDir (Join-Path $tmp 'base') -AgentePath $agente
    $script:ResticExeNome = 'restic'
    $script:SshExe = 'ssh'
    $script:senhaTeste = 'senha-lab-x'

    # ---- origem aninhada + fixtures (espacos, acentos, subpasta) ----
    $origem = Join-Path $tmp 'aninhado/Pasta protegida'
    New-Item -ItemType Directory (Join-Path $origem 'sub pasta') -Force | Out-Null
    'VERSAO-ANTIGA' | Set-Content -LiteralPath (Join-Path $origem 'alterado.txt') -Encoding Ascii -NoNewline
    'REMOVIDO-DEPOIS' | Set-Content -LiteralPath (Join-Path $origem 'removido.txt') -Encoding Ascii -NoNewline
    'INTERNO' | Set-Content -LiteralPath (Join-Path $origem 'sub pasta/interno.txt') -Encoding Ascii -NoNewline
    'ACENTOS' | Set-Content -LiteralPath (Join-Path $origem 'nome com espaços e ação.txt') -Encoding Ascii -NoNewline
    'NAO-ENTRA' | Set-Content -LiteralPath (Join-Path $origem 'excluido.tmp') -Encoding Ascii -NoNewline
    # manifesto "guardado antes do backup": caminho relativo -> sha256
    $manifesto = @{}
    Get-ChildItem -LiteralPath $origem -Recurse -File | Where-Object { $_.Name -notlike '*.tmp' } | ForEach-Object {
        $rel = $_.FullName.Substring($origem.Length).TrimStart('\','/')
        $manifesto[($rel -replace '\\','/')] = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLower() }
    ok ($manifesto.Count -eq 4) 'real: manifesto pre-backup com 4 arquivos (excluido fora)'

    # ---- repositorio local descartavel: dois pontos reais ----
    $repo = Join-Path $tmp 'repo'
    $env:RESTIC_PASSWORD = $script:senhaTeste
    & $Restic init --repo $repo 2>$null | Out-Null
    ok (Test-Path -LiteralPath (Join-Path $repo 'config')) 'real: repo local inicializado'
    & $Restic --repo $repo backup $origem --exclude '*.tmp' 2>$null | Out-Null
    $snaps = & $Restic --repo $repo snapshots --json | ConvertFrom-Json
    $snapA = [string]$snaps[0].id
    ok ($snapA.Length -eq 64) 'real: ponto A gravado (ID completo)'
    # entre os pontos: alterado muda, removido sai da origem
    'VERSAO-NOVA' | Set-Content -LiteralPath (Join-Path $origem 'alterado.txt') -Encoding Ascii -NoNewline
    Remove-Item -LiteralPath (Join-Path $origem 'removido.txt') -Force
    & $Restic --repo $repo backup $origem --exclude '*.tmp' 2>$null | Out-Null
    $snaps = & $Restic --repo $repo snapshots --json | ConvertFrom-Json
    ok (@($snaps).Count -eq 2) 'real: ponto B gravado — restaurar o ANTIGO'

    # ---- estado + config operacional (vinculos como o preparo real) ----
    $estado = Join-Path $tmp 'estado'; New-Item -ItemType Directory $estado | Out-Null
    # o pipeline da GUI executa o "restic" via `pwsh -File` fora do
    # Windows — o wrapper repassa ao binario real. O backup.json exige
    # 'sftp:user@host:/x'; o wrapper traduz o --repo para o repo LOCAL
    # e descarta o '-o sftp.command' — o transporte SFTP e a unica parte
    # simulada; cat/ls/restore/--verify sao o restic 0.18.1 de verdade.
    @"
`$a = [System.Collections.Generic.List[string]]::new()
for (`$i = 0; `$i -lt `$args.Count; `$i++) {
    if (`$args[`$i] -eq '--repo') { `$a += '--repo'; `$a += '$repo'; `$i++ }
    elseif (`$args[`$i] -eq '-o') { `$i++ }
    else { `$a += `$args[`$i] }
}
& '$Restic' `$a
exit `$LASTEXITCODE
"@ | Set-Content -LiteralPath (Join-Path $estado 'restic') -Encoding Ascii
    # restic.dpapi no formato do agente (ConvertFrom-SecureString)
    ConvertFrom-SecureString -SecureString (ConvertTo-SecureString $script:senhaTeste -AsPlainText -Force) |
        Set-Content -LiteralPath (Join-Path $estado 'restic.dpapi') -Encoding Ascii
    $ident = Join-Path $estado 'id_lab'; 'k' | Set-Content $ident -Encoding Ascii -NoNewline
    $kh = Join-Path $estado 'known_hosts'; 'h' | Set-Content $kh -Encoding Ascii -NoNewline
    $sshCfg = Join-Path $estado 'ssh_config'; 'c' | Set-Content $sshCfg -Encoding Ascii -NoNewline
    $repoId = [string]((& $Restic --repo $repo cat config | ConvertFrom-Json).id)
    ok ($repoId -match '^[0-9a-f]{64}$') 'real: repositorio_id lido do repo'
    $repoCfg = 'sftp:lab@127.0.0.1:/repo'   # forma exigida pelo backup.json
    @{
        sid = (Get-IdentidadeAtual).Sid; repositorio = $repoCfg; repositorio_id = $repoId
        identidade_ssh = $ident; identidade_sha256 = (Get-HashArquivo $ident)
        known_hosts = $kh; known_hosts_sha256 = (Get-HashArquivo $kh)
        ssh_config_sha256 = (Get-HashArquivo $sshCfg)
    } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $estado 'preparo.json') -Encoding Ascii
    $cfgPath = Join-Path $tmp 'backup.json'
    @{
        origem = $origem; exclusoes = @('*.tmp'); repositorio = $repoCfg
        repositorio_id = $repoId; identidade_ssh = $ident; known_hosts = $kh
        horario = '02:30'
    } | ConvertTo-Json | Set-Content -LiteralPath $cfgPath -Encoding Ascii

    # ---- 1) catalogo REAL pelo leitor do aplicativo ----
    $info = Get-RaizSnapshot -Cfg (Get-CfgOperacional -Estado $estado -Config $cfgPath) `
        -Estado $estado -Snapshot $snapA -PathsMeta @($origem)
    eq $info.Origem $origem 'real: origem registrada devolvida (tecnico/destino)'
    eq $info.Raiz $origem 'real: raiz interna confirmada exata no catalogo'
    ok (@($info.Esperados.Keys | Where-Object { $_ -notlike '*/*' -and $_ -ne '' }).Count -ge 2) 'real: conteudo direto sob a raiz no esperado'
    ok (-not ($info.Esperados.Keys | Where-Object { $_ -match '^(tmp|aninhado)' })) 'real: ancestrais aceitos e FORA do conjunto esperado'
    ok ($info.Esperados.Contains('sub pasta') -and $info.Esperados['sub pasta'] -eq 'dir') 'real: subpasta esperada como dir'

    # ---- 2) restauracao REAL do ponto ANTIGO, fim a fim ----
    $dest = Join-Path $tmp 'restaurado-antigo'
    $res = Restore-PastaCompleta -Estado $estado -Config $cfgPath `
        -Snapshot $snapA -PathsMeta @($origem) -Destino $dest -Origem $origem
    eq ([IO.Path]::GetFullPath($dest)) ([IO.Path]::GetFullPath($res.Destino)) 'real: publicada no destino pedido'
    eq $res.Raiz $origem 'real: raiz registrada no ponto devolvida'
    # manifesto do ponto A: todo caminho relativo com o MESMO sha-256
    $faltaHash = @()
    foreach ($rel in $manifesto.Keys) {
        $alvoRel = Join-Path $dest ($rel -replace '/', [IO.Path]::DirectorySeparatorChar)
        if (-not (Test-Path -LiteralPath $alvoRel -PathType Leaf)) { $faltaHash += $rel; continue }
        if ((Get-FileHash -LiteralPath $alvoRel -Algorithm SHA256).Hash.ToLower() -cne $manifesto[$rel]) { $faltaHash += $rel }
    }
    eq ($faltaHash -join ',') '' 'real: manifesto completo — caminhos relativos e SHA-256 iguais'
    eq (Get-Content -LiteralPath (Join-Path $dest 'alterado.txt') -Raw) 'VERSAO-ANTIGA' 'real: alterado volta com o conteudo do ponto A'
    ok (Test-Path -LiteralPath (Join-Path $dest 'removido.txt') -PathType Leaf) 'real: removido da origem volta'
    ok (Test-Path -LiteralPath (Join-Path $dest 'sub pasta/interno.txt') -PathType Leaf) 'real: subpasta com espaco preservada'
    ok (Test-Path -LiteralPath (Join-Path $dest 'nome com espaços e ação.txt') -PathType Leaf) 'real: nome com espacos e acentos preservado'
    ok (-not (Test-Path -LiteralPath (Join-Path $dest 'excluido.tmp'))) 'real: excluido continua ausente'
    ok (-not (Test-Path -LiteralPath (Join-Path $dest 'aninhado')) -and -not (Test-Path -LiteralPath (Join-Path $dest 'tmp'))) 'real: sem prefixos extras — hierarquia relativa certa'
    eq (Get-Content -LiteralPath (Join-Path $origem 'alterado.txt') -Raw) 'VERSAO-NOVA' 'real: origem preservada (versao nova intacta)'
    ok (-not (Test-Path -LiteralPath (Join-Path $origem 'removido.txt'))) 'real: remocao na origem preservada'

    # ---- 3) destino ja existente + recusa de raiz alheia ----
    throws { Restore-PastaCompleta -Estado $estado -Config $cfgPath -Snapshot $snapA -PathsMeta @($origem) -Destino $dest -Origem $origem } 'real: destino existente recusado'
    throws { Get-RaizSnapshot -Cfg (Get-CfgOperacional -Estado $estado -Config $cfgPath) -Estado $estado -Snapshot $snapA -PathsMeta @('/raiz/alheia') } 'real: raiz nao confirmada no catalogo -> recusado'

    Write-Host "`n== separacao de prova: catalogo ls/restore/--verify = RESTIC REAL =="
    Write-Host "== transporte SFTP/conta Windows/PS 5.1 = seguem simulados =="
} finally {
    Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item Env:RESTIC_PASSWORD -ErrorAction SilentlyContinue
}
Write-Host "`nRESULTADO: $script:ok ok, $script:er falha(s), $script:skip pulado(s)"
if ($script:er -gt 0) { exit 1 }
