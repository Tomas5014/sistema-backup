#requires -Version 5.1
# Regressões do agente Windows da MAC IP — roda em pwsh (Linux) e em
# Windows PowerShell 5.1. Fora do Windows apenas a fronteira DPAPI
# (Protect/Read-ResticPassword) é substituída por base64 local: a prova
# real de DPAPI + Agendador + OpenSSH Windows fica para a VM descartável.
param(
    [string]$AgenteScript = (Join-Path $PSScriptRoot '..\Backup-MacipCliente.ps1'),
    [string]$Stub = (Join-Path $PSScriptRoot 'restic-stub.ps1')
)
$ErrorActionPreference = 'Stop'
$script:ok = 0; $script:falhas = 0
function ok($n) { $script:ok++; Write-Host "  ok  $n" }
function falha($n, $d) { $script:falhas++; Write-Host "  FALHA  $n -- $d" }
function eq($obtido, $esperado, $n) {
    if ($obtido -ceq $esperado) { ok $n } else { falha $n "esperado '$esperado', obtido '$obtido'" }
}
function lanca([scriptblock]$sb, $frag, $n) {
    try { & $sb | Out-Null; falha $n "nao lancou erro" }
    catch { if ($_.Exception.Message -match [regex]::Escape($frag)) { ok $n }
            else { falha $n "erro divergente: $($_.Exception.Message)" } }
}

. $AgenteScript   # funções do agente; a guarda interna evita a execução

$IsWindowsAlvo = Test-IsWindows
if (-not $IsWindowsAlvo) {
    # Fronteira DPAPI substituída (somente teste; nada muda no agente).
    function Protect-ResticPassword([securestring]$Senha, [string]$Destino) {
        $b = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Senha)
        try { [IO.File]::WriteAllText($Destino, [Convert]::ToBase64String(
            [Text.Encoding]::UTF8.GetBytes([Runtime.InteropServices.Marshal]::PtrToStringBSTR($b)))) }
        finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($b) }
    }
    function Read-ResticPassword([string]$Caminho) {
        $pl = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String((Get-Content -LiteralPath $Caminho -Raw).Trim()))
        ConvertTo-SecureString -String $pl -AsPlainText -Force
    }
}
$script:ResticExeNome = 'restic-stub.ps1'

$script:senhaTeste = 'senha-sintetica-teste'
function New-Lab {
    $raiz = Join-Path ([IO.Path]::GetTempPath()) ("macipwin-" + [guid]::NewGuid().ToString('N'))
    $origem = Join-Path $raiz 'origem ação'   # espaço + acento de propósito
    $idpasta = Join-Path $raiz 'id'
    New-Item -ItemType Directory $origem -Force | Out-Null
    New-Item -ItemType Directory $idpasta -Force | Out-Null
    'dado 1' | Set-Content (Join-Path $origem 'dados ação.txt') -Encoding UTF8
    'ignorar' | Set-Content (Join-Path $origem 'lixo.tmp') -Encoding UTF8
    'CHAVE-FALSA-TESTE' | Set-Content (Join-Path $idpasta 'agente_ed25519') -Encoding Ascii
    '127.0.0.1 ssh-ed25519 AAAAFAKETESTE' | Set-Content (Join-Path $idpasta 'known_hosts') -Encoding Ascii
    $stubDir = Join-Path $raiz 'stub'
    New-Item -ItemType Directory $stubDir -Force | Out-Null
    ('b' * 64) | Set-Content (Join-Path $stubDir 'id') -Encoding Ascii -NoNewline
    '0' | Set-Content (Join-Path $stubDir 'code') -Encoding Ascii -NoNewline
    ('c' * 64) | Set-Content (Join-Path $stubDir 'snapshot') -Encoding Ascii -NoNewline
    return [pscustomobject]@{
        Raiz = $raiz; Origem = $origem; StubDir = $stubDir
        Chave = (Join-Path $idpasta 'agente_ed25519'); Known = (Join-Path $idpasta 'known_hosts')
        Estado = (Join-Path $raiz 'estado'); Config = (Join-Path $raiz 'backup.json')
    }
}
function Write-Config($lab, [hashtable]$over = @{}, [string[]]$remover = @()) {
    $cfg = [ordered]@{
        origem = $lab.Origem; exclusoes = @('*.tmp')
        repositorio = 'sftp:agente@127.0.0.1:2222/backup/cliente'
        horario = '02:30'; repositorio_id = ('b' * 64)
        identidade_ssh = $lab.Chave; known_hosts = $lab.Known
    }
    foreach ($k in $over.Keys) { $cfg[$k] = $over[$k] }
    foreach ($k in $remover) { $cfg.Remove($k) }
    ($cfg | ConvertTo-Json) | Set-Content -LiteralPath $lab.Config -Encoding UTF8
}
function Invoke-Preparar($lab, $config = $lab.Config) {
    $env:RESTIC_STUB_DIR = $lab.StubDir
    Invoke-AgentePreparar -Config $config -DiretorioEstado $lab.Estado -ResticExe $Stub `
        -SenhaRestic (ConvertTo-SecureString $script:senhaTeste -AsPlainText -Force)
}
function Invoke-Backup($lab) {
    $env:RESTIC_STUB_DIR = $lab.StubDir
    Invoke-AgenteBackup -Config $lab.Config -DiretorioEstado $lab.Estado
}
function Read-Resultado($lab) {
    Get-Content -LiteralPath (Join-Path $lab.Estado 'ultimo-resultado.json') -Raw | ConvertFrom-Json
}
function Read-Args($lab) { Get-Content -LiteralPath (Join-Path $lab.StubDir 'args.log') -Raw }

Write-Host '== backup.json: validação =='
$lab = New-Lab; Write-Config $lab
$c = Read-AgenteConfig $lab.Config
eq $c.sftp_host '127.0.0.1' 'host extraído do repositório'
eq $c.sftp_porta '2222' 'porta extraída do repositório'
eq ($c.exclusoes -join '|') '*.tmp' 'exclusões carregadas'

lanca { $l = New-Lab; Write-Config $l @{ origem = 'dados' }; Read-AgenteConfig $l.Config } 'absoluto' 'origem relativa recusada'
lanca { $l = New-Lab; Write-Config $l @{ origem = (Join-Path $l.Raiz 'nao-existe') }; Read-AgenteConfig $l.Config } 'nao existe' 'origem inexistente recusada'
$labSemLeitura = New-Lab
if (-not $IsWindowsAlvo) {
    & chmod 000 $labSemLeitura.Origem
    lanca { Write-Config $labSemLeitura; Read-AgenteConfig $labSemLeitura.Config } 'sem leitura' 'origem sem leitura recusada'
    & chmod 755 $labSemLeitura.Origem
}
lanca { $l = New-Lab; Write-Config $l @{ exclusoes = @('*.tmp', '  ') }; Read-AgenteConfig $l.Config } 'vazio' 'exclusão vazia recusada'
lanca { $l = New-Lab; Write-Config $l @{ repositorio = 'local:/tmp/repo' }; Read-AgenteConfig $l.Config } 'sftp:' 'repositório não-SFTP recusado'
lanca { $l = New-Lab; Write-Config $l @{ repositorio = 'sftp:u@host' }; Read-AgenteConfig $l.Config } 'sftp:' 'repositório sem caminho recusado'
lanca { $l = New-Lab; Write-Config $l @{ horario = '25:00' }; Read-AgenteConfig $l.Config } 'HH:mm' 'horário 25:00 recusado'
lanca { $l = New-Lab; Write-Config $l @{ horario = '2:30' }; Read-AgenteConfig $l.Config } 'HH:mm' 'horário sem zero recusado'
lanca { $l = New-Lab; Write-Config $l @{ repositorio_id = 'abc' }; Read-AgenteConfig $l.Config } '64 hex' 'repositorio_id curto recusado'
lanca { $l = New-Lab; Write-Config $l @{ identidade_ssh = (Join-Path $l.Raiz 'sem-chave') }; Read-AgenteConfig $l.Config } 'nao encontrado' 'identidade_ssh ausente recusada'
lanca { $l = New-Lab; Write-Config $l @{ campo_extra = 1 }; Read-AgenteConfig $l.Config } 'desconhecido' 'campo desconhecido recusado'
lanca { $l = New-Lab; Write-Config $l @{} 'horario'; Read-AgenteConfig $l.Config } 'ausente' 'campo obrigatório ausente recusado'
lanca { $l = New-Lab; Set-Content $l.Config '{ quebrado' -Encoding UTF8; Read-AgenteConfig $l.Config } 'invalido' 'JSON quebrado recusado'
$lEspaco = New-Lab; Write-Config $lEspaco
$alvoEspaco = Join-Path $lEspaco.Raiz 'com espaco'
lanca { Resolve-Contexto -Config $lEspaco.Config -DiretorioEstado $alvoEspaco } 'espacos' 'estado com espaço recusado'

Write-Host '== exclusões: lista vazia e padrões com espaço/acento =='
$lab = New-Lab; Write-Config $lab @{ exclusoes = @() }
Invoke-Preparar $lab | Out-Null
Remove-Item (Join-Path $lab.StubDir 'args.log') -ErrorAction SilentlyContinue
$r = Invoke-Backup $lab
eq $r.status 'sucesso' 'backup sem exclusões é sucesso'
$linhas = Get-Content (Join-Path $lab.StubDir 'last-args.txt')
eq @($linhas | Where-Object { $_ -eq '--exclude' }).Count 0 'nenhum --exclude com lista vazia'

$lab = New-Lab; Write-Config $lab @{ exclusoes = @('*.tmp', 'cache local\*.dat', 'relatório*') }
Invoke-Preparar $lab | Out-Null
$r = Invoke-Backup $lab
eq $r.status 'sucesso' 'backup com exclusões é sucesso'
$linhas = Get-Content (Join-Path $lab.StubDir 'last-args.txt')
$idx = [array]::IndexOf($linhas, '--exclude')
eq $linhas[$idx + 1] '*.tmp' 'padrão 1 como --exclude separado'
eq (($linhas -join '|') -match [regex]::Escape('cache local\*.dat')) $true 'padrão com espaço preservado'
eq @($linhas | Where-Object { $_ -ceq 'relatório*' }).Count 1 'padrão acentuado preservado'

Write-Host '== destino divergente recusado antes do backup =='
$lab = New-Lab; Write-Config $lab
Invoke-Preparar $lab | Out-Null
('d' * 64) | Set-Content (Join-Path $lab.StubDir 'id') -Encoding Ascii -NoNewline
Remove-Item (Join-Path $lab.StubDir 'args.log') -ErrorAction SilentlyContinue
$r = Invoke-Backup $lab
eq $r.status 'falha' 'destino divergente falha'
eq ($r.erro -match 'divergente') $true 'erro menciona divergência'
$logArgs = Get-Content (Join-Path $lab.StubDir 'args.log')
eq @($logArgs | Where-Object { $_ -ceq 'backup' }).Count 0 'restic backup NÃO foi chamado'
eq (@($logArgs | Where-Object { $_ -ceq 'cat' }).Count -gt 0) $true 'identidade do destino consultada'

Write-Host '== códigos Restic 0, 1, 3 =='
foreach ($caso in @(@(0, 'sucesso'), @(1, 'falha'), @(3, 'falha'))) {
    $lab = New-Lab; Write-Config $lab
    Invoke-Preparar $lab | Out-Null
    [string]$caso[0] | Set-Content (Join-Path $lab.StubDir 'code') -Encoding Ascii -NoNewline
    $r = Invoke-Backup $lab
    eq $r.status $caso[1] "rc=$($caso[0]) → $($caso[1])"
    eq $r.codigo_restic $caso[0] "rc=$($caso[0]) gravado no resultado"
}
$lab = New-Lab; Write-Config $lab
Invoke-Preparar $lab | Out-Null
Remove-Item (Join-Path $lab.StubDir 'snapshot')
$r = Invoke-Backup $lab
eq $r.status 'falha' 'rc=0 sem snapshot válido NÃO é sucesso'
eq $r.snapshot $null 'snapshot nulo quando ausente'

Write-Host '== ultimo-resultado.json =='
$lab = New-Lab; Write-Config $lab
Invoke-Preparar $lab | Out-Null
$r = Invoke-Backup $lab
$j = Read-Resultado $lab
eq $j.status 'sucesso' 'resultado: sucesso'
eq $j.snapshot ('c' * 64) 'resultado: snapshot_id'
eq $j.codigo_restic 0 'resultado: codigo_restic'
$bruto = Get-Content (Join-Path $lab.Estado 'ultimo-resultado.json') -Raw
eq ($bruto -match '"inicio_utc"\s*:\s*"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}') $true 'resultado: inicio_utc UTC'
eq ($bruto -match '"fim_utc"\s*:\s*"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}') $true 'resultado: fim_utc UTC'
eq ([string]::IsNullOrEmpty($j.conta)) $false 'resultado: conta registrada'
eq (Test-Path (Join-Path $lab.Estado 'ultimo-restic.log')) $true 'log do restic gravado'

Write-Host '== senha: nunca em argv/log/resultado; env restaurada =='
$conteudo = (Read-Args $lab) + (Get-Content (Join-Path $lab.Estado 'ultimo-resultado.json') -Raw) +
            (Get-Content (Join-Path $lab.Estado 'ultimo-restic.log') -Raw)
eq ($conteudo -match [regex]::Escape($script:senhaTeste)) $false 'senha ausente de argv/resultado/log'
eq ((Get-Content (Join-Path $lab.StubDir 'env.log') -Raw) -match 'RESTIC_PASSWORD=DEFINIDA') $true 'senha presente só no processo'
eq ([Environment]::GetEnvironmentVariable('RESTIC_PASSWORD', 'Process')) $null 'RESTIC_PASSWORD restaurada'

Write-Host '== mesmo script, duas configurações =='
$l1 = New-Lab; Write-Config $l1 @{ repositorio = 'sftp:c1@10.0.0.1/repo-a'; horario = '02:30' }
$l2 = New-Lab; Write-Config $l2 @{ repositorio = 'sftp:c2@10.0.0.2/repo-b'; horario = '03:45'; exclusoes = @('*.bak') }
Invoke-Preparar $l1 | Out-Null; Invoke-Preparar $l2 | Out-Null
$r1 = Invoke-Backup $l1; $r2 = Invoke-Backup $l2
eq $r1.status 'sucesso' 'config A: sucesso'
eq $r2.status 'sucesso' 'config B: sucesso'
eq ((@(Read-Args $l1) -match 'sftp:c1@10.0.0.1/repo-a').Count -gt 0) $true 'config A usou repo A'
eq ((@(Read-Args $l2) -match 'sftp:c2@10.0.0.2/repo-b').Count -gt 0) $true 'config B usou repo B'
eq (@(Get-Content (Join-Path $l2.StubDir 'last-args.txt')) -ceq '*.bak').Count 1 'config B aplicou sua exclusão'
eq (Test-Path (Join-Path $l1.Estado 'ultimo-resultado.json')) $true 'resultado A no estado A'
eq (Test-Path (Join-Path $l2.Estado 'ultimo-resultado.json')) $true 'resultado B no estado B'

Write-Host '== preparo: ssh_config, identidade e campos do Agendador =='
$lab = New-Lab; Write-Config $lab
$saida = @(Invoke-Preparar $lab 6>&1 | ForEach-Object { "$_" })
$ssh = Get-Content (Join-Path $lab.Estado 'ssh_config') -Raw
eq ($ssh -match 'BatchMode yes') $true 'ssh_config: BatchMode'
eq ($ssh -match 'StrictHostKeyChecking yes') $true 'ssh_config: verificação estrita'
eq ($ssh -match 'IdentitiesOnly yes') $true 'ssh_config: chave exclusiva'
eq ($ssh -match [regex]::Escape($lab.Chave.Replace('\', '/'))) $true 'ssh_config: IdentityFile da config'
eq ($ssh -match [regex]::Escape($lab.Known.Replace('\', '/'))) $true 'ssh_config: known_hosts aprovado'
eq ($ssh -match 'UpdateHostKeys no') $true 'ssh_config: sem atualização de chave'
eq (($saida -join "`n") -match 'Modo Backup') $true 'Agendador: argumentos impressos'
eq (($saida -join "`n") -match 'Diario as 02:30') $true 'Agendador: horário de backup.json'
eq (($saida -join "`n") -match [regex]::Escape($lab.Estado)) $true 'Agendador: DiretorioEstado absoluto'
eq (Test-Path (Join-Path $lab.Estado 'preparo.json')) $true 'preparo.json gravado'
eq ((Get-Content (Join-Path $lab.Estado 'preparo.json') -Raw | ConvertFrom-Json).repositorio_id) ('b' * 64) 'preparo.json: ID conferido gravado'
eq (Test-Path (Join-Path $lab.Estado 'restic.dpapi')) $true 'senha protegida gravada'
eq ((Get-Content (Join-Path $lab.Estado 'restic.dpapi') -Raw) -match [regex]::Escape($script:senhaTeste)) $false 'senha não está em texto puro'

Write-Host '== recusas de contexto no Backup =='
$lab = New-Lab; Write-Config $lab
$r = Invoke-Backup $lab
eq $r.status 'falha' 'backup sem preparo falha'
eq ($r.erro -match 'Preparacao nao concluida') $true 'erro orienta o preparo'
eq (Test-Path (Join-Path $lab.Estado 'ultimo-resultado.json')) $true 'falha registrada mesmo sem preparo'
$lab = New-Lab; Write-Config $lab
Invoke-Preparar $lab | Out-Null
$p = Get-Content (Join-Path $lab.Estado 'preparo.json') -Raw | ConvertFrom-Json
$p.sid = 'SID-DIFERENTE'
$p | ConvertTo-Json | Set-Content (Join-Path $lab.Estado 'preparo.json') -Encoding UTF8
$r = Invoke-Backup $lab
eq $r.status 'falha' 'conta diferente do preparo falha'
eq ($r.erro -match 'Conta diferente') $true 'erro menciona a conta'

Write-Host '== disparo real do arquivo (-Modo) =='
$lab = New-Lab; Write-Config $lab
New-Item -ItemType Directory $lab.Estado -Force | Out-Null
[ordered]@{ preparado_utc = 'x'; conta = 'x'; sid = (Get-IdentidadeAtual).Sid; config = $lab.Config; script = 'x'; repositorio = 'x'; repositorio_id = 'x'; restic_versao = 'x' } |
    ConvertTo-Json | Set-Content (Join-Path $lab.Estado 'preparo.json') -Encoding UTF8
'conteudo-invalido' | Set-Content (Join-Path $lab.Estado 'restic.dpapi') -Encoding Ascii
foreach ($nome in 'restic-stub.ps1', 'restic', 'restic.exe') {
    Copy-Item $Stub (Join-Path $lab.Estado $nome) -Force
}
& (Get-Process -Id $PID).Path -NoProfile -NonInteractive -File $AgenteScript -Modo Backup -Config $lab.Config -DiretorioEstado $lab.Estado 2>$null | Out-Null
eq ($LASTEXITCODE -ne 0) $true 'falha de senha → saída com erro'
$j = Read-Resultado $lab
eq $j.status 'falha' 'falha nunca marcada como sucesso'
& (Get-Process -Id $PID).Path -NoProfile -NonInteractive -File $AgenteScript 2>$null | Out-Null
eq ($LASTEXITCODE -ne 0) $true 'sem -Modo → erro de uso'

Write-Host ("== resultado: {0} ok, {1} falhas ==" -f $script:ok, $script:falhas)
exit $(if ($script:falhas -gt 0) { 1 } else { 0 })
