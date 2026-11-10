#requires -Version 5.1
# Backup-MacipCliente.ps1 — agente de backup Windows da MAC IP (piloto).
# Um mesmo script serve a qualquer cliente: tudo que muda vem do backup.json.
# Modos: Preparar (interativo, na sessao da conta da tarefa) e Backup
# (sem prompts, disparado pelo Agendador). Nunca executa "restic init".
[CmdletBinding()]
param(
    [ValidateSet('Preparar', 'Backup')]
    [string]$Modo,
    [string]$Config,
    [string]$DiretorioEstado,
    [string]$ResticExe,
    [securestring]$SenhaRestic
)

$ErrorActionPreference = 'Stop'

if (-not $script:ResticExeNome) {
    $script:ResticExeNome = $(if ($env:OS -eq 'Windows_NT') { 'restic.exe' } else { 'restic' })
}

$script:CamposConhecidos = @(
    'origem', 'exclusoes', 'repositorio', 'horario',
    'repositorio_id', 'identidade_ssh', 'known_hosts'
)

function Test-IsWindows {
    return ($env:OS -eq 'Windows_NT')
}

function Get-IdentidadeAtual {
    if (Test-IsWindows) {
        $id = [Security.Principal.WindowsIdentity]::GetCurrent()
        return [pscustomobject]@{ Nome = $id.Name; Sid = $id.User.Value }
    }
    return [pscustomobject]@{ Nome = $env:USER; Sid = "posix:$env:USER" }
}

function Get-CaminhoEstado {
    param([string]$ConfigAbs)
    $dir = Split-Path -LiteralPath $ConfigAbs -Parent
    return (Join-Path $dir 'macip-estado')
}

function Read-AgenteConfig {
    param([string]$Caminho)
    if (-not $Caminho) { throw 'Informe -Config com o caminho do backup.json.' }
    if (-not (Test-Path -LiteralPath $Caminho -PathType Leaf)) {
        throw "backup.json nao encontrado: $Caminho"
    }
    $abs = (Resolve-Path -LiteralPath $Caminho).ProviderPath
    try { $cfg = Get-Content -LiteralPath $abs -Raw -Encoding UTF8 | ConvertFrom-Json }
    catch { throw "backup.json invalido ($abs): $($_.Exception.Message)" }
    if ($null -eq $cfg -or $cfg -isnot [pscustomobject]) {
        throw "backup.json deve ser um objeto JSON: $abs"
    }

    $extras = @($cfg.PSObject.Properties.Name | Where-Object {
        $script:CamposConhecidos -cnotcontains $_
    })
    if ($extras.Count -gt 0) {
        throw ("backup.json: campo(s) desconhecido(s) '{0}'. Validos: {1}" -f
            ($extras -join "', '"), ($script:CamposConhecidos -join ', '))
    }
    foreach ($campo in $script:CamposConhecidos) {
        if (-not $cfg.PSObject.Properties[$campo]) {
            throw "backup.json: campo obrigatorio ausente: $campo"
        }
    }

    $r = [ordered]@{ caminho_config = $abs }

    $origem = [string]$cfg.origem
    if (-not [IO.Path]::IsPathRooted($origem)) {
        throw "origem deve ser um caminho local absoluto: '$origem'"
    }
    if (-not (Test-Path -LiteralPath $origem -PathType Container)) {
        throw "origem nao existe ou nao e pasta: '$origem'"
    }
    try { [void][IO.Directory]::EnumerateFileSystemEntries($origem).GetEnumerator().MoveNext() }
    catch { throw "origem sem leitura para esta conta: '$origem'" }
    $r.origem = $origem

    # PS 5.1 desempacota lista JSON de um item; normaliza escalar ou lista.
    $padroes = @()
    if ($null -ne $cfg.exclusoes) {
        $padroes = @($cfg.exclusoes | ForEach-Object {
            if ($_ -isnot [string] -or $_.Trim() -eq '') {
                throw 'exclusoes: padrao vazio ou nao textual recusado'
            }
            $_
        })
    }
    $r.exclusoes = $padroes

    $repo = [string]$cfg.repositorio
    $m = [regex]::Match($repo, '^sftp:([^@\s]+)@([^:/\s]+)(?::(\d{1,5}))?(/\S*)$')
    if (-not $m.Success -or $m.Groups[1].Value -eq '' -or $m.Groups[4].Value.Length -lt 2) {
        throw "repositorio deve ser sftp:usuario@servidor[:porta]/caminho: '$repo'"
    }
    if ($m.Groups[3].Success -and [int]$m.Groups[3].Value -gt 65535) {
        throw "repositorio com porta invalida: '$repo'"
    }
    $r.repositorio = $repo
    $r.sftp_host = $m.Groups[2].Value
    $r.sftp_porta = $(if ($m.Groups[3].Success) { $m.Groups[3].Value } else { '22' })

    if ([string]$cfg.horario -cnotmatch '^([01][0-9]|2[0-3]):[0-5][0-9]$') {
        throw "horario deve ser HH:mm (00:00-23:59): '$($cfg.horario)'"
    }
    $r.horario = [string]$cfg.horario

    if ([string]$cfg.repositorio_id -cnotmatch '^[0-9a-f]{64}$') {
        throw 'repositorio_id deve ser o ID Restic (64 hex) fornecido pela MAC IP'
    }
    $r.repositorio_id = [string]$cfg.repositorio_id

    foreach ($c in 'identidade_ssh', 'known_hosts') {
        $p = [string]$cfg.$c
        if (-not [IO.Path]::IsPathRooted($p)) { throw "$c deve ser caminho absoluto: '$p'" }
        if (-not (Test-Path -LiteralPath $p -PathType Leaf)) { throw "$c nao encontrado: '$p'" }
        $r[$c] = (Resolve-Path -LiteralPath $p).ProviderPath
    }

    return [pscustomobject]$r
}

function Get-SshExe {
    if (Test-IsWindows) {
        $p = Join-Path $env:WINDIR 'System32\OpenSSH\ssh.exe'
        if (-not (Test-Path -LiteralPath $p -PathType Leaf)) {
            throw "OpenSSH nao encontrado: $p (instale o recurso opcional 'Cliente OpenSSH')"
        }
        if ($p -match '\s') { throw "Caminho do OpenSSH com espacos nao suportado: $p" }
        return $p.Replace('\', '/')
    }
    $ssh = Get-Command ssh -ErrorAction SilentlyContinue
    if (-not $ssh) { throw 'OpenSSH nao encontrado no PATH' }
    return $ssh.Source
}

function Write-SshConfig {
    param([pscustomobject]$Cfg, [string]$Estado)
    if ($Estado -match '\s') {
        throw "DiretorioEstado nao pode conter espacos (limite do sftp.command do Restic): '$Estado'"
    }
    $alvo = Join-Path $Estado 'ssh_config'
    $kh = $Cfg.known_hosts.Replace('\', '/')
    $chave = $Cfg.identidade_ssh.Replace('\', '/')
    $cfgSsh = $alvo.Replace('\', '/')
    @"
Host $($Cfg.sftp_host)
    Port $($Cfg.sftp_porta)
    BatchMode yes
    IdentityAgent none
    IdentitiesOnly yes
    IdentityFile "$chave"
    StrictHostKeyChecking yes
    UserKnownHostsFile "$kh"
    GlobalKnownHostsFile none
    UpdateHostKeys no
    ConnectTimeout 15
"@ | Set-Content -LiteralPath $alvo -Encoding Ascii
    return $cfgSsh
}

function Protect-ResticPassword {
    param([securestring]$Senha, [string]$Destino)
    $blob = ConvertFrom-SecureString -SecureString $Senha
    Set-Content -LiteralPath $Destino -Value $blob -Encoding Ascii
}

function Read-ResticPassword {
    param([string]$Caminho)
    return ConvertTo-SecureString -String ((Get-Content -LiteralPath $Caminho -Raw).Trim())
}

function Invoke-Restic {
    param([pscustomobject]$Cfg, [string]$Estado, [string[]]$Comando)
    $exe = Join-Path $Estado $script:ResticExeNome
    $senha = Read-ResticPassword -Caminho (Join-Path $Estado 'restic.dpapi')
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($senha)
    $anterior = [Environment]::GetEnvironmentVariable('RESTIC_PASSWORD', 'Process')
    $eap = $ErrorActionPreference
    try {
        $env:RESTIC_PASSWORD = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
        Remove-Item Env:RESTIC_PASSWORD_FILE, Env:RESTIC_PASSWORD_COMMAND -ErrorAction SilentlyContinue
        $ErrorActionPreference = 'Continue'
        $sftpCmd = 'sftp.command={0} -F {1} -s {2} sftp' -f $Cfg.ssh_exe, $Cfg.ssh_config, $Cfg.sftp_host
        $saida = @(& $exe --repo $Cfg.repositorio --cache-dir (Join-Path $Estado 'cache') -o $sftpCmd @Comando 2>&1)
        $codigo = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $eap
        [Environment]::SetEnvironmentVariable('RESTIC_PASSWORD', $anterior, 'Process')
        if ($null -eq $anterior) { Remove-Item Env:RESTIC_PASSWORD -ErrorAction SilentlyContinue }
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
        if ($senha) { $senha.Dispose() }
    }
    return [pscustomobject]@{ Codigo = $codigo; Linhas = @($saida | ForEach-Object { $_.ToString() }) }
}

function Confirm-Destino {
    param([pscustomobject]$Cfg, [string]$Estado)
    $resp = Invoke-Restic -Cfg $Cfg -Estado $Estado -Comando @('cat', 'config')
    if ($resp.Codigo -ne 0) {
        throw ('Repositorio inacessivel ou identidade recusada (restic codigo {0}). ' +
            'Sem repositorio valido o backup nao e disparado.') -f $resp.Codigo
    }
    $linha = $resp.Linhas | Where-Object { $_ -match '"id"' } | Select-Object -First 1
    $id = $null
    if ($linha) { try { $id = ($linha | ConvertFrom-Json).id } catch { } }
    if (-not $id -and $linha -match '"id"\s*:\s*"([0-9a-f]{64})"') { $id = $Matches[1] }
    if (-not $id) { throw 'Repositorio respondeu sem ID verificavel; backup recusado.' }
    if ($id -cne $Cfg.repositorio_id) {
        throw ('Destino divergente do aprovado pela MAC IP ' +
            '(repositorio_id nao confere); backup recusado ANTES de iniciar.')
    }
    return $id
}

function Read-SnapshotId {
    param([string[]]$Linhas)
    $resumo = $Linhas | Where-Object { $_ -match '"message_type"\s*:\s*"summary"' } |
        Select-Object -Last 1
    if (-not $resumo) { return $null }
    try { $sid = ($resumo | ConvertFrom-Json).snapshot_id } catch { return $null }
    if ([string]$sid -cmatch '^[0-9a-f]{64}$') { return $sid }
    return $null
}

function Write-Resultado {
    param([string]$Estado, [hashtable]$Dados)
    if (-not (Test-Path -LiteralPath $Estado -PathType Container)) {
        New-Item -ItemType Directory -Path $Estado -Force | Out-Null
    }
    $Dados | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $Estado 'ultimo-resultado.json') -Encoding UTF8
}

function Protect-PastaEstado {
    param([string]$Estado)
    if (-not (Test-IsWindows)) { return }
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $acl = [Security.AccessControl.DirectorySecurity]::new()
    $acl.SetAccessRuleProtection($true, $false)
    $regra = [Security.AccessControl.FileSystemAccessRule]::new(
        $id.User, 'FullControl', 'ContainerInherit, ObjectInherit', 'None', 'Allow')
    $acl.AddAccessRule($regra)
    [IO.Directory]::SetAccessControl($Estado, $acl)
}

function Resolve-Contexto {
    param([string]$Config, [string]$DiretorioEstado)
    $cfg = Read-AgenteConfig -Caminho $Config
    $estado = if ($DiretorioEstado) { [IO.Path]::GetFullPath($DiretorioEstado) } else { Get-CaminhoEstado $cfg.caminho_config }
    if ($estado -match '\s') {
        throw "DiretorioEstado nao pode conter espacos (limite do sftp.command do Restic): '$estado'"
    }
    return [pscustomobject]@{ Cfg = $cfg; Estado = $estado }
}

function Invoke-AgentePreparar {
    param([string]$Config, [string]$DiretorioEstado, [string]$ResticExe, [securestring]$SenhaRestic)
    $ctx = Resolve-Contexto -Config $Config -DiretorioEstado $DiretorioEstado
    $cfg = $ctx.Cfg; $estado = $ctx.Estado
    $ident = Get-IdentidadeAtual

    if (-not $ResticExe -or -not (Test-Path -LiteralPath $ResticExe -PathType Leaf)) {
        throw 'Informe -ResticExe com o restic.exe fornecido pela MAC IP.'
    }
    $ResticExe = (Resolve-Path -LiteralPath $ResticExe).ProviderPath
    $ver = & $ResticExe version 2>&1
    if ($LASTEXITCODE -ne 0 -or ($ver -join ' ') -cnotmatch '^restic \d+\.\d+\.\d+') {
        throw "Restic nao respondeu a 'version': $ResticExe"
    }
    $ssh = Get-SshExe

    if (-not $SenhaRestic) {
        $SenhaRestic = Read-Host 'Senha Restic fornecida pela MAC IP' -AsSecureString
    }
    if ($SenhaRestic.Length -lt 1) { $SenhaRestic.Dispose(); throw 'Senha vazia recusada.' }

    $jaExiste = Test-Path -LiteralPath $estado -PathType Container
    New-Item -ItemType Directory -Path $estado -Force | Out-Null
    if (-not $jaExiste) { Protect-PastaEstado -Estado $estado }

    $exeDestino = Join-Path $estado $script:ResticExeNome
    Copy-Item -LiteralPath $ResticExe -Destination $exeDestino -Force
    if (-not (Test-IsWindows)) { & chmod +x $exeDestino 2>$null | Out-Null }

    try {
        Protect-ResticPassword -Senha $SenhaRestic -Destino (Join-Path $estado 'restic.dpapi')
    }
    finally { $SenhaRestic.Dispose() }

    $cfg | Add-Member -NotePropertyName ssh_exe -NotePropertyValue $ssh -Force
    $cfg | Add-Member -NotePropertyName ssh_config -NotePropertyValue (Write-SshConfig -Cfg $cfg -Estado $estado) -Force

    $repoId = Confirm-Destino -Cfg $cfg -Estado $estado

    $scriptAbs = $PSCommandPath
    [ordered]@{
        preparado_utc  = [DateTime]::UtcNow.ToString('o')
        conta          = $ident.Nome
        sid            = $ident.Sid
        config         = $cfg.caminho_config
        script         = $scriptAbs
        repositorio    = $cfg.repositorio
        repositorio_id = $repoId
        restic_versao  = ($ver -join ' ').Trim()
    } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $estado 'preparo.json') -Encoding UTF8

    if (Test-IsWindows) {
        $ps = Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe'
    } else { $ps = 'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe' }
    Write-Host 'PREPARACAO OK'
    Write-Host '=== Campos para o Agendador de Tarefas ==='
    Write-Host "Programa........: $ps"
    Write-Host ('Argumentos......: -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{0}" -Modo Backup -Config "{1}" -DiretorioEstado "{2}"' -f $scriptAbs, $cfg.caminho_config, $estado)
    Write-Host "Conta da tarefa.: $($ident.Nome) (mesma conta desta sessao — a senha DPAPI so abre nela)"
    Write-Host "Disparador......: Diario as $($cfg.horario) (de backup.json)"
    Write-Host 'Opcoes..........: executar com a sessao encerrada; nao iniciar nova instancia se ja estiver em execucao'
    Write-Host "Resultado.......: $(Join-Path $estado 'ultimo-resultado.json')"
}

function Invoke-AgenteBackup {
    param([string]$Config, [string]$DiretorioEstado)
    $estado = $null
    $res = [ordered]@{
        inicio_utc    = [DateTime]::UtcNow.ToString('o')
        fim_utc       = $null
        conta         = (Get-IdentidadeAtual).Nome
        status        = 'em_execucao'
        codigo_restic = $null
        snapshot      = $null
    }
    try {
        $ctx = Resolve-Contexto -Config $Config -DiretorioEstado $DiretorioEstado
        $cfg = $ctx.Cfg; $estado = $ctx.Estado
        $preparo = Join-Path $estado 'preparo.json'
        if (-not (Test-Path -LiteralPath $preparo -PathType Leaf)) {
            throw 'Preparacao nao concluida (execute -Modo Preparar na conta da tarefa).'
        }
        $prep = Get-Content -LiteralPath $preparo -Raw | ConvertFrom-Json
        if ($prep.sid -cne (Get-IdentidadeAtual).Sid) {
            throw 'Conta diferente da usada no preparo (a senha DPAPI so abre na conta original).'
        }
        foreach ($f in 'restic.dpapi', 'ssh_config') {
            if (-not (Test-Path -LiteralPath (Join-Path $estado $f) -PathType Leaf)) {
                throw "Arquivo do preparo ausente: $f"
            }
        }
        $exe = Join-Path $estado $script:ResticExeNome
        if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) { throw 'restic do preparo ausente.' }
        $cfg | Add-Member -NotePropertyName ssh_exe -NotePropertyValue (Get-SshExe) -Force
        $cfg | Add-Member -NotePropertyName ssh_config -NotePropertyValue ((Join-Path $estado 'ssh_config').Replace('\', '/')) -Force

        Write-Resultado -Estado $estado -Dados $res

        [void](Confirm-Destino -Cfg $cfg -Estado $estado)

        $argsRestic = @('backup', '--json', '--tag', 'macip-agente-windows')
        foreach ($padrao in $cfg.exclusoes) { $argsRestic += @('--exclude', $padrao) }
        $argsRestic += $cfg.origem
        $backup = Invoke-Restic -Cfg $cfg -Estado $estado -Comando $argsRestic
        $res.codigo_restic = $backup.Codigo
        $backup.Linhas | Set-Content -LiteralPath (Join-Path $estado 'ultimo-restic.log') -Encoding UTF8

        if ($backup.Codigo -eq 3) { throw 'Restic codigo 3: backup incompleto (nem todo arquivo lido).' }
        if ($backup.Codigo -ne 0) { throw "Restic backup falhou (codigo $($backup.Codigo))." }
        $snapshot = Read-SnapshotId -Linhas $backup.Linhas
        if (-not $snapshot) { throw 'Restic 0 mas sem ID de snapshot valido; nao marcado como sucesso.' }
        $res.snapshot = $snapshot
        $res.status = 'sucesso'
    }
    catch {
        $res.status = 'falha'
        $res.erro = $_.Exception.Message
    }
    finally {
        $res.fim_utc = [DateTime]::UtcNow.ToString('o')
        if ($estado) { Write-Resultado -Estado $estado -Dados $res }
    }
    return [pscustomobject]$res
}

if ($MyInvocation.InvocationName -eq '.') { return }

if (-not $Modo) {
    Write-Error 'Uso: -Modo Preparar|Backup -Config <backup.json> [-DiretorioEstado <pasta>] [-ResticExe <restic.exe>]'
    exit 2
}

if ($Modo -eq 'Preparar') {
    Invoke-AgentePreparar -Config $Config -DiretorioEstado $DiretorioEstado -ResticExe $ResticExe -SenhaRestic $SenhaRestic
    exit 0
}

$r = Invoke-AgenteBackup -Config $Config -DiretorioEstado $DiretorioEstado
if ($r.status -eq 'sucesso') {
    Write-Host "BACKUP OK: $($r.snapshot)"
    exit 0
}
Write-Error -ErrorAction Continue $r.erro
exit $(if ($null -ne $r.codigo_restic -and $r.codigo_restic -ne 0) { $r.codigo_restic } else { 1 })
