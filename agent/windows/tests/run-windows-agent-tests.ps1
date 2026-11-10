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
        repositorio = 'sftp://agente@127.0.0.1:2222//backup/cliente'
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
eq $c.sftp_user 'agente' 'usuário extraído do repositório'
eq $c.sftp_host '127.0.0.1' 'host extraído do repositório'
eq $c.sftp_porta '2222' 'porta extraída da forma sftp://'
eq $c.sftp_caminho '/backup/cliente' 'caminho absoluto da forma //'
eq ($c.exclusoes -join '|') '*.tmp' 'exclusões carregadas'
$lab2 = New-Lab; Write-Config $lab2 @{ repositorio = 'sftp:agente@backup.macip:/dados/cliente' }
$c2 = Read-AgenteConfig $lab2.Config
eq $c2.sftp_host 'backup.macip' 'host da forma canônica'
eq $c2.sftp_porta '22' 'porta padrão 22 na forma canônica'
eq $c2.sftp_caminho '/dados/cliente' 'caminho da forma canônica'

lanca { $l = New-Lab; Write-Config $l @{ origem = 'dados' }; Read-AgenteConfig $l.Config } 'absoluto' 'origem relativa recusada'
lanca { $l = New-Lab; Write-Config $l @{ origem = (Join-Path $l.Raiz 'nao-existe') }; Read-AgenteConfig $l.Config } 'nao existe' 'origem inexistente recusada'
$labSemLeitura = New-Lab
if (-not $IsWindowsAlvo) {
    & chmod 000 $labSemLeitura.Origem
    lanca { Write-Config $labSemLeitura; Read-AgenteConfig $labSemLeitura.Config } 'sem leitura' 'origem sem leitura recusada'
    & chmod 755 $labSemLeitura.Origem
}
lanca { $l = New-Lab; Write-Config $l @{ exclusoes = @('*.tmp', '  ') }; Read-AgenteConfig $l.Config } 'vazio' 'exclusão vazia recusada'
$lTxt = New-Lab; Set-Content $lTxt.Config ('{"origem":"' + ($lTxt.Origem -replace '\\','\\') + '","exclusoes":"*.tmp","repositorio":"sftp:a@b:/c","horario":"02:30","repositorio_id":"' + ('b'*64) + '","identidade_ssh":"' + ($lTxt.Chave -replace '\\','\\') + '","known_hosts":"' + ($lTxt.Known -replace '\\','\\') + '"}') -Encoding UTF8
lanca { Read-AgenteConfig $lTxt.Config } 'lista' 'exclusões como texto isolado recusadas'
$lNull = New-Lab; Set-Content $lNull.Config ('{"origem":"' + ($lNull.Origem -replace '\\','\\') + '","exclusoes":null,"repositorio":"sftp:a@b:/c","horario":"02:30","repositorio_id":"' + ('b'*64) + '","identidade_ssh":"' + ($lNull.Chave -replace '\\','\\') + '","known_hosts":"' + ($lNull.Known -replace '\\','\\') + '"}') -Encoding UTF8
lanca { Read-AgenteConfig $lNull.Config } 'lista' 'exclusões null recusadas'
$lDup = New-Lab; Set-Content $lDup.Config ('{"origem":"' + ($lDup.Origem -replace '\\','\\') + '","exclusoes":[],"exclusoes":"*.tmp","repositorio":"sftp:a@b:/c","horario":"02:30","repositorio_id":"' + ('b'*64) + '","identidade_ssh":"' + ($lDup.Chave -replace '\\','\\') + '","known_hosts":"' + ($lDup.Known -replace '\\','\\') + '"}') -Encoding UTF8
lanca { Read-AgenteConfig $lDup.Config } 'lista' 'chave duplicada com último valor textual recusada'
lanca { $l = New-Lab; Write-Config $l @{ repositorio = 'local:/tmp/repo' }; Read-AgenteConfig $l.Config } 'sftp:' 'repositório não-SFTP recusado'
lanca { $l = New-Lab; Write-Config $l @{ repositorio = 'sftp:u@host' }; Read-AgenteConfig $l.Config } 'sftp:' 'repositório sem caminho recusado'
lanca { $l = New-Lab; Write-Config $l @{ repositorio = 'sftp:u@host:2222/caminho' }; Read-AgenteConfig $l.Config } 'sftp:' 'forma ambíguo :porta/caminho recusada'
lanca { $l = New-Lab; Write-Config $l @{ repositorio = 'sftp:u@host:99999/caminho' }; Read-AgenteConfig $l.Config } 'sftp' 'porta fora de faixa recusada'
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
$l1 = New-Lab; Write-Config $l1 @{ repositorio = 'sftp:c1@10.0.0.1:/repo-a'; horario = '02:30' }
$l2 = New-Lab; Write-Config $l2 @{ repositorio = 'sftp:c2@10.0.0.2:/repo-b'; horario = '03:45'; exclusoes = @('*.bak') }
Invoke-Preparar $l1 | Out-Null; Invoke-Preparar $l2 | Out-Null
$r1 = Invoke-Backup $l1; $r2 = Invoke-Backup $l2
eq $r1.status 'sucesso' 'config A: sucesso'
eq $r2.status 'sucesso' 'config B: sucesso'
eq ((@(Read-Args $l1) -match 'sftp:c1@10.0.0.1:/repo-a').Count -gt 0) $true 'config A usou repo A'
eq ((@(Read-Args $l2) -match 'sftp:c2@10.0.0.2:/repo-b').Count -gt 0) $true 'config B usou repo B'
eq (@(Get-Content (Join-Path $l2.StubDir 'last-args.txt')) -ceq '*.bak').Count 1 'config B aplicou sua exclusão'
eq (Test-Path (Join-Path $l1.Estado 'ultimo-resultado.json')) $true 'resultado A no estado A'
eq (Test-Path (Join-Path $l2.Estado 'ultimo-resultado.json')) $true 'resultado B no estado B'

Write-Host '== preparo: ssh_config, identidade e campos do Agendador =='
$lab = New-Lab; Write-Config $lab
$saida = @(Invoke-Preparar $lab 6>&1 | ForEach-Object { "$_" })
$ssh = Get-Content (Join-Path $lab.Estado 'ssh_config') -Raw
eq ($ssh -match 'User agente') $true 'ssh_config: usuário SFTP explícito'
eq ($ssh -match 'Port 2222') $true 'ssh_config: porta do repositório'
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
eq (Test-Path (Join-Path $lab.Estado 'ultimo-resultado.json')) $false 'sem estado verificável NÃO grava resultado'
eq (Test-Path $lab.Estado) $false 'sem preparo NÃO cria pasta de estado'
$lab = New-Lab; Write-Config $lab
Invoke-Preparar $lab | Out-Null
$p = Get-Content (Join-Path $lab.Estado 'preparo.json') -Raw | ConvertFrom-Json
$p.sid = 'SID-DIFERENTE'
$p | ConvertTo-Json | Set-Content (Join-Path $lab.Estado 'preparo.json') -Encoding UTF8
$r = Invoke-Backup $lab
eq $r.status 'falha' 'conta diferente do preparo falha'
eq ($r.erro -match 'Conta diferente') $true 'erro menciona a conta'

Write-Host '== exclusões de um item e backup.example.json =='
$lab = New-Lab; Write-Config $lab @{ exclusoes = @('*.bak') }
Invoke-Preparar $lab | Out-Null
$r = Invoke-Backup $lab
eq $r.status 'sucesso' 'lista de um padrão funciona'
$ex = Get-Content (Join-Path (Split-Path $AgenteScript) 'backup.example.json') -Raw | ConvertFrom-Json
eq (@($ex.PSObject.Properties.Name) -join ',') 'origem,exclusoes,repositorio,horario,repositorio_id,identidade_ssh,known_hosts' 'exemplo: campos exatos'
eq ([regex]::IsMatch([string]$ex.repositorio, '^sftp:[^@\s/]+@[^:/\s]+:/\S+$')) $true 'exemplo: repositório na forma canônica'
eq ($ex.repositorio_id -cmatch '^[0-9a-f]{64}$') $true 'exemplo: repositorio_id 64 hex'
eq (($ex | ConvertTo-Json) -notmatch 'senha|password') $true 'exemplo sem segredos'

Write-Host '== comando do README sem -DiretorioEstado =='
$lab = New-Lab; Write-Config $lab
$env:RESTIC_STUB_DIR = $lab.StubDir
Invoke-AgentePreparar -Config $lab.Config -ResticExe $Stub -SenhaRestic (ConvertTo-SecureString $script:senhaTeste -AsPlainText -Force) | Out-Null
$estadoPadrao = Join-Path $lab.Raiz 'macip-estado'
eq (Test-Path (Join-Path $estadoPadrao 'preparo.json')) $true 'sem -DiretorioEstado usa macip-estado ao lado da config'
$r = Invoke-AgenteBackup -Config $lab.Config
eq $r.status 'sucesso' 'backup no estado padrão é sucesso'

Write-Host '== preparo seguro: recusa, preservação e limpeza =='
$lab = New-Lab; Write-Config $lab
Invoke-Preparar $lab | Out-Null
$antes = Get-ChildItem $lab.Estado -Recurse -File | ForEach-Object { "$($_.Name)=$((Get-FileHash $_.FullName -Algorithm SHA256).Hash)" }
lanca { Invoke-Preparar $lab } 'ja existe' 'segundo preparo no mesmo estado recusado'
$depois = Get-ChildItem $lab.Estado -Recurse -File | ForEach-Object { "$($_.Name)=$((Get-FileHash $_.FullName -Algorithm SHA256).Hash)" }
eq ($antes -join '|') ($depois -join '|') 'estado anterior preservado byte a byte'
$lab = New-Lab; Write-Config $lab @{ repositorio_id = ('e' * 64) }
$env:RESTIC_STUB_DIR = $lab.StubDir
lanca { Invoke-Preparar $lab } 'divergente' 'preparo com ID divergente falha'
eq (Test-Path $lab.Estado) $false 'tentativa recusada não deixa pasta de estado'

Write-Host '== vínculo com o destino aprovado =='
$lab = New-Lab; Write-Config $lab
Invoke-Preparar $lab | Out-Null
foreach ($mutacao in @(
    @{ campo = 'repositorio'; valor = 'sftp:agente@9.9.9.9:/outro' },
    @{ campo = 'repositorio_id'; valor = ('f' * 64) })) {
    Write-Config $lab @{ ($mutacao.campo) = $mutacao.valor }
    Remove-Item (Join-Path $lab.StubDir 'args.log') -ErrorAction SilentlyContinue
    $r = Invoke-Backup $lab
    eq $r.status 'falha' "divergência de $($mutacao.campo) recusada"
    eq ($r.erro -match 'divergem do preparo') $true "erro orienta novo preparo ($($mutacao.campo))"
    eq @((Get-Content (Join-Path $lab.StubDir 'args.log') -ErrorAction SilentlyContinue) | Where-Object { $_ -ceq 'backup' }).Count 0 "backup não disparado ($($mutacao.campo))"
}
Write-Config $lab   # restaura config íntegra
'CONTEUDO-ADULTERADO' | Set-Content $lab.Known -Encoding Ascii
$r = Invoke-Backup $lab
eq ($r.erro -match 'divergem do preparo') $true 'known_hosts alterado recusado'
Set-Content $lab.Known '127.0.0.1 ssh-ed25519 AAAAFAKETESTE' -Encoding Ascii
'CHAVE-ADULTERADA' | Set-Content $lab.Chave -Encoding Ascii
$r = Invoke-Backup $lab
eq ($r.erro -match 'divergem do preparo') $true 'identidade alterada recusada'

Write-Host '== vínculo ssh efetivo: caminho, ssh_config e preparo legado =='
$lab = New-Lab; Write-Config $lab
Invoke-Preparar $lab | Out-Null
$outraChave = Join-Path $lab.Raiz 'outra\copia_ed25519'
New-Item -ItemType Directory (Split-Path $outraChave) -Force | Out-Null
Copy-Item $lab.Chave $outraChave   # bytes idênticos, caminho diferente
Write-Config $lab @{ identidade_ssh = $outraChave }
Remove-Item (Join-Path $lab.StubDir 'args.log') -ErrorAction SilentlyContinue
$r = Invoke-Backup $lab
eq $r.status 'falha' 'cópia idêntica em outro caminho recusada'
eq ($r.erro -match 'divergem do preparo') $true 'caminho registrado é o efetivo'
eq @((Get-Content (Join-Path $lab.StubDir 'args.log') -ErrorAction SilentlyContinue) | Where-Object { $_ -ceq 'backup' }).Count 0 'restic não chamado (caminho chave)'
$outraKh = Join-Path $lab.Raiz 'outra\known_hosts'
Copy-Item $lab.Known $outraKh
Write-Config $lab @{ known_hosts = $outraKh }
$r = Invoke-Backup $lab
eq ($r.erro -match 'divergem do preparo') $true 'known_hosts em outro caminho recusado'
Write-Config $lab   # config íntegra de novo
'    Compression yes' | Add-Content (Join-Path $lab.Estado 'ssh_config') -Encoding Ascii
$r = Invoke-Backup $lab
eq ($r.erro -match 'divergem do preparo') $true 'ssh_config adulterado recusado'
eq @((Get-Content (Join-Path $lab.StubDir 'args.log') -ErrorAction SilentlyContinue) | Where-Object { $_ -ceq 'backup' }).Count 0 'restic não chamado (ssh_config)'
Remove-Item (Join-Path $lab.Estado 'ssh_config')
$r = Invoke-Backup $lab
eq ($r.erro -match 'divergem do preparo') $true 'ssh_config ausente recusado'
$prep = Get-Content (Join-Path $lab.Estado 'preparo.json') -Raw | ConvertFrom-Json
foreach ($f in 'identidade_sha256', 'known_hosts_sha256', 'ssh_config_sha256') {
    $prep.PSObject.Properties.Remove($f)
}
$prep | ConvertTo-Json | Set-Content (Join-Path $lab.Estado 'preparo.json') -Encoding UTF8
$r = Invoke-Backup $lab
eq ($r.erro -match 'vinculo SSH completo') $true 'preparo legado sem hashes recusado'

Write-Host '== config íntegra e exclusão válida após preparo =='
$lab = New-Lab; Write-Config $lab
Invoke-Preparar $lab | Out-Null
Write-Config $lab @{ exclusoes = @('*.tmp', '*.bak'); horario = '04:15' }   # mesmos destino/identidade
$r = Invoke-Backup $lab
eq $r.status 'sucesso' 'exclusões e horário novos no mesmo destino: sucesso'
eq @((Get-Content (Join-Path $lab.StubDir 'last-args.txt')) -ceq '*.bak').Count 1 'nova exclusão aplicada'

Write-Host '== JSON inválido após sucesso =='
$lab = New-Lab; Write-Config $lab
Invoke-Preparar $lab | Out-Null
$r = Invoke-Backup $lab
eq $r.status 'sucesso' 'base: sucesso anterior'
Set-Content $lab.Config '{ quebrado' -Encoding UTF8
$r = Invoke-Backup $lab
eq $r.status 'falha' 'JSON quebrado vira falha'
$j = Read-Resultado $lab
eq $j.status 'falha' 'resultado novo substitui o sucesso antigo'
eq $j.snapshot $null 'falha não carrega snapshot antigo'

Write-Host '== disparo real do arquivo (-Modo) =='
$lab = New-Lab; Write-Config $lab
Invoke-Preparar $lab | Out-Null
'conteudo-invalido' | Set-Content (Join-Path $lab.Estado 'restic.dpapi') -Encoding Ascii -Force
foreach ($nome in 'restic', 'restic.exe') {
    Copy-Item $Stub (Join-Path $lab.Estado $nome) -Force
}
& (Get-Process -Id $PID).Path -NoProfile -NonInteractive -File $AgenteScript -Modo Backup -Config $lab.Config -DiretorioEstado $lab.Estado 2>$null | Out-Null
eq ($LASTEXITCODE -ne 0) $true 'falha de senha → saída com erro'
$j = Read-Resultado $lab
eq $j.status 'falha' 'falha nunca marcada como sucesso'
& (Get-Process -Id $PID).Path -NoProfile -NonInteractive -File $AgenteScript 2>$null | Out-Null
eq ($LASTEXITCODE -ne 0) $true 'sem -Modo → erro de uso'

Write-Host '== Restic REAL + ssh registrador (sem conexão) =='
$resticReal = $env:MACIP_RESTIC_REAL
$sshStub = Join-Path $PSScriptRoot 'ssh-stub.sh'
if (-not $resticReal -or -not (Test-Path -LiteralPath $resticReal -PathType Leaf) -or $IsWindowsAlvo) {
    Write-Host '  -- pulado (defina MACIP_RESTIC_REAL com o restic 0.18.1 de teste)'
}
else {
    $nomeAnterior = $script:ResticExeNome
    $script:ResticExeNome = 'restic'
    & chmod +x $sshStub 2>$null
    foreach ($repo in @('sftp://agente@10.20.30.40:2222//dados/cliente', 'sftp:agente@10.20.30.40:/dados/cliente')) {
        $lab = New-Lab; Write-Config $lab @{ repositorio = $repo }
        New-Item -ItemType Directory $lab.Estado | Out-Null
        Copy-Item $resticReal (Join-Path $lab.Estado $script:ResticExeNome)
        & chmod +x (Join-Path $lab.Estado $script:ResticExeNome) 2>$null
        Protect-ResticPassword -Senha (ConvertTo-SecureString 'x' -AsPlainText -Force) -Destino (Join-Path $lab.Estado 'restic.dpapi')
        $cfg = Read-AgenteConfig $lab.Config
        $cfg | Add-Member -NotePropertyName ssh_exe -NotePropertyValue $sshStub -Force
        $cfg | Add-Member -NotePropertyName ssh_config -NotePropertyValue (Write-SshConfig -Cfg $cfg -Estado $lab.Estado) -Force
        $env:SSH_STUB_LOG = Join-Path $lab.Raiz 'ssh-args.log'
        $resp = Invoke-Restic -Cfg $cfg -Estado $lab.Estado -Comando @('cat', 'config')
        $sshArgs = Get-Content $env:SSH_STUB_LOG -ErrorAction SilentlyContinue
        eq (@($sshArgs | Where-Object { $_ -ceq '10.20.30.40' }).Count -gt 0) $true "restic passou host p/ ssh ($repo)"
        eq (@($sshArgs | Where-Object { $_ -ceq 'sftp' }).Count -gt 0) $true "restic pediu subsystem sftp ($repo)"
        eq (@($sshArgs | Where-Object { $_ -ceq '-F' }).Count -gt 0) $true "ssh_config repassado ($repo)"
        $sshCfg = Get-Content $cfg.ssh_config -Raw
        eq ($sshCfg -match 'User agente') $true "usuário SFTP no comando ($repo)"
        $porta = if ($repo -match 'sftp://') { '2222' } else { '22' }
        eq ($sshCfg -match "Port $porta") $true "porta $porta no comando ($repo)"
    }
    $script:ResticExeNome = $nomeAnterior
}

Write-Host ("== resultado: {0} ok, {1} falhas ==" -f $script:ok, $script:falhas)
exit $(if ($script:falhas -gt 0) { 1 } else { 0 })
