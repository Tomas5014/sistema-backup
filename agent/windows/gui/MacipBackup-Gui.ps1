#requires -Version 5.1
# MacipBackup-Gui.ps1 - interface grafica do agente Windows da MAC IP (piloto).
# WinForms sobre Windows PowerShell 5.1: sem dependencia nova  -  a mesma
# tecnologia do agente. Todas as strings em ASCII: o arquivo roda em PS 5.1
# sem BOM como o agente.
#
# Fluxo pensado para o tecnico: Ativar -> Configurar -> Operar -> Restaurar.
# Nenhuma etapa pede terminal, edicao de JSON ou o Agendador de Tarefas.
# O Agendador continua por tras: a tarefa MACIP-Backup roda com o app fechado,
# sem sessao aberta e apos reinicio.
#
# Reaproveitamento: o agente Backup-MacipCliente.ps1 e dot-sourced  -  validacao
# de backup.json, ssh_config restrito, Confirm-Destino, Invoke-Restic e
# Invoke-AgentePreparar sao os mesmos ja homologados em laboratorio.
[CmdletBinding()]
param(
    # Pasta base de dados do app (config + estado). Padrao: ProgramData\MacipBackup.
    # Precisa ser um caminho SEM espacos (limite do sftp.command do Restic).
    [string]$BaseDir,
    # Caminho do agente (instalador coloca ao lado; override so para teste)
    [string]$AgentePath
)

$ErrorActionPreference = 'Stop'

if (-not $BaseDir) { $BaseDir = Join-Path $env:ProgramData 'MacipBackup' }
if ($BaseDir -match '\s') {
    throw "BaseDir nao pode conter espacos (limite do sftp.command): '$BaseDir'"
}
if (-not $AgentePath) { $AgentePath = Join-Path $PSScriptRoot 'Backup-MacipCliente.ps1' }
if (-not (Test-Path -LiteralPath $AgentePath -PathType Leaf)) {
    throw "Agente nao encontrado ao lado da interface: $AgentePath"
}

$script:BaseDir    = [IO.Path]::GetFullPath($BaseDir)
$script:ConfigFile = Join-Path $script:BaseDir 'backup.json'
$script:GuiMeta    = Join-Path $script:BaseDir 'gui.json'
$script:TaskName   = 'MACIP-Backup'
$script:AgentePath = $AgentePath
$script:LogonPassword = 2   # TASK_LOGON_PASSWORD: token completo (rede + DPAPI)

. $AgentePath   # reusa Read-AgenteConfig, Write-SshConfig, Invoke-Restic, etc.

# ---------- dados do app ----------

function Read-GuiMeta {
    if (Test-Path -LiteralPath $script:GuiMeta -PathType Leaf) {
        try { return (Get-Content -LiteralPath $script:GuiMeta -Raw | ConvertFrom-Json) }
        catch { }
    }
    return [pscustomobject]@{ estado_atual = $null; conta_tarefa = $null }
}

function Save-GuiMeta {
    param([pscustomobject]$Meta)
    if (-not (Test-Path -LiteralPath $script:BaseDir)) { New-Item -ItemType Directory -Path $script:BaseDir | Out-Null }
    $tmp = "$($script:GuiMeta).tmp"
    $Meta | ConvertTo-Json | Set-Content -LiteralPath $tmp -Encoding UTF8
    Move-Item -LiteralPath $tmp -Destination $script:GuiMeta -Force
}

# ---------- perfil (dados, sem segredos) ----------

$script:PerfilCampos = @('nome', 'repositorio', 'repositorio_id', 'known_hosts', 'horario_sugerido')
$script:PadroesSegredo = '(?i)(senha|password|private|secret|-----BEGIN|identidade)'

function Read-Perfil {
    param([string]$Caminho)
    if (-not (Test-Path -LiteralPath $Caminho -PathType Leaf)) {
        throw "Perfil nao encontrado: $Caminho"
    }
    $abs = (Resolve-Path -LiteralPath $Caminho).ProviderPath
    try { $p = Get-Content -LiteralPath $abs -Raw -Encoding UTF8 | ConvertFrom-Json }
    catch { throw "Perfil nao e um JSON valido: $($_.Exception.Message)" }
    if ($null -eq $p -or $p -isnot [pscustomobject]) { throw 'Perfil deve ser um objeto JSON.' }
    foreach ($c in 'repositorio', 'repositorio_id', 'known_hosts') {
        if (-not $p.PSObject.Properties[$c] -or -not $p.$c) {
            throw "Perfil incompleto: campo obrigatorio ausente ou vazio: $c"
        }
    }
    foreach ($c in @($p.PSObject.Properties.Name)) {
        if ($script:PerfilCampos -cnotcontains $c) {
            throw "Perfil com campo desconhecido '$c'  -  perfis sao gerados pela MAC IP e nao devem ser editados."
        }
        if ([string]$p.$c -match $script:PadroesSegredo) {
            throw "Perfil contem dado que parece segredo no campo '$c'. Senha e chave privada nunca viajam no perfil."
        }
    }
    # repositorio nas formas canonicas do agente (validacao detalhada no backup.json depois)
    $repo = [string]$p.repositorio
    $m1 = [regex]::Match($repo, '^sftp:([^@\s/]+)@([^:/\s]+):(/{1,2}\S+)$')
    $m2 = [regex]::Match($repo, '^sftp://([^@\s/]+)@([^:/\s]+)(?::(\d{1,5}))?//(\S+)$')
    if (-not $m1.Success -and -not ($m2.Success -and $m2.Groups[4].Success)) {
        throw "Perfil com repositorio fora das formas canonicas: '$repo'"
    }
    if ([string]$p.repositorio_id -cnotmatch '^[0-9a-f]{64}$') {
        throw 'Perfil com repositorio_id invalido (esperado ID Restic de 64 hex).'
    }
    if ([string]$p.known_hosts -notmatch '^\S+\s+(ssh-|ecdsa-)') {
        throw 'Perfil com known_hosts invalido (esperado "<host> <tipo> <chave>").'
    }
    $r = [ordered]@{
        caminho = $abs
        nome = [string]$p.nome
        repositorio = $repo
        repositorio_id = [string]$p.repositorio_id
        known_hosts = [string]$p.known_hosts
        horario_sugerido = [string]$p.horario_sugerido
    }
    if ($r.horario_sugerido -and $r.horario_sugerido -cnotmatch '^([01][0-9]|2[0-3]):[0-5][0-9]$') {
        throw "horario_sugerido invalido no perfil: '$($r.horario_sugerido)'"
    }
    return [pscustomobject]$r
}

function New-BackupJsonObj {
    param([pscustomobject]$Perfil, [string]$Origem, [object[]]$Exclusoes,
          [string]$Horario, [string]$IdentidadeSsh, [string]$KnownHostsFile)
    return [ordered]@{
        origem         = $Origem
        exclusoes      = @($Exclusoes)
        repositorio    = $Perfil.repositorio
        horario        = $Horario
        repositorio_id = $Perfil.repositorio_id
        identidade_ssh = $IdentidadeSsh
        known_hosts    = $KnownHostsFile
    }
}

function Save-ConfigAtomico {
    # Grava backup.json de forma atomica e NUNCA apaga a config valida anterior:
    # valida pelo proprio parser do agente antes de substituir.
    param([hashtable]$CfgObj)
    if (-not (Test-Path -LiteralPath $script:BaseDir)) { New-Item -ItemType Directory -Path $script:BaseDir | Out-Null }
    $tmp = "$($script:ConfigFile).tmp"
    ($CfgObj | ConvertTo-Json -Depth 4) | Set-Content -LiteralPath $tmp -Encoding UTF8
    try {
        [void](Read-AgenteConfig -Caminho $tmp)   # recusa invalida ANTES de tocar a anterior
    } catch {
        Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
        throw "Configuracao recusada: $($_.Exception.Message)  -  a configuracao anterior continua valendo."
    }
    Move-Item -LiteralPath $tmp -Destination $script:ConfigFile -Force
}

# ---------- tarefa agendada (COM Schedule.Service, somente Windows) ----------

function Get-TarefaMacip {
    if (-not (Test-IsWindows)) { return $null }
    $svc = New-Object -ComObject Schedule.Service
    $svc.Connect()
    try { return $svc.GetFolder('\').GetTask($script:TaskName) } catch { return $null }
}

function Get-TaskActionArgs {
    param([string]$Agente, [string]$Config, [string]$Estado)
    return '-NoProfile -NonInteractive -File "{0}" -Modo Backup -Config "{1}" -DiretorioEstado "{2}"' -f $Agente, $Config, $Estado
}

function Save-TarefaMacip {
    # Idempotente: existe a tarefa -> atualiza SOMENTE o disparador e a acao;
    # nao existe -> cria. Nunca gera uma segunda tarefa. Conta e senha sao da
    # janela de credencial nativa (TASK_LOGON_PASSWORD: roda sem sessao e o
    # token completo permite rede e DPAPI da conta).
    param([string]$Horario, [string]$Conta, [string]$SenhaConta, [string]$Estado)
    if ($Horario -cnotmatch '^([01][0-9]|2[0-3]):[0-5][0-9]$') { throw "Horario invalido: $Horario" }
    if (-not (Test-IsWindows)) { throw 'Agendador disponivel apenas no Windows.' }
    $svc = New-Object -ComObject Schedule.Service
    $svc.Connect()
    $def = $null; $tarefa = $null
    try { $tarefa = $svc.GetFolder('\').GetTask($script:TaskName) } catch { }
    if ($tarefa) { $def = $tarefa.Definition } else { $def = $svc.NewTask(0) }

    $def.Settings.Enabled = $true
    $def.Settings.MultipleInstances = 2            # IGNORE_NEW: nunca duas execucoes
    $def.Settings.DisallowStartIfOnBatteries = $false
    $def.Settings.StopIfGoingOnBatteries = $false
    $def.Settings.ExecutionTimeLimit = 'PT0S'      # sem limite de duracao
    $def.Triggers.Clear()
    $trg = $def.Triggers.Create(2)                 # DAILY
    $hoje = Get-Date
    $trg.StartBoundary = (Get-Date -Year $hoje.Year -Month $hoje.Month -Day $hoje.Day `
        -Hour ([int]$Horario.Substring(0,2)) -Minute ([int]$Horario.Substring(3,2)) -Second 0).ToString('s')
    $trg.DaysInterval = 1
    $trg.Enabled = $true

    $ps = Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $def.Actions.Clear()
    $act = $def.Actions.Create(0)
    $act.Path = $ps
    $act.Arguments = Get-TaskActionArgs -Agente $script:AgentePath -Config $script:ConfigFile -Estado $Estado

    $prin = $def.Principal
    $prin.UserId = $Conta
    $prin.LogonType = $script:LogonPassword
    $prin.RunLevel = 0                             # LIMITADO: sem elevacao

    # 6 = TASK_CREATE_OR_UPDATE: mesma tarefa, nunca duplicada.
    $svc.GetFolder('\').RegisterTaskDefinition($script:TaskName, $def, 6, $Conta, $SenhaConta, $script:LogonPassword, $null) | Out-Null
}

function Set-TarefaHabilitada {
    param([bool]$On)
    $t = Get-TarefaMacip
    if (-not $t) { throw 'Tarefa MACIP-Backup nao existe  -  salve a configuracao primeiro.' }
    $t.Enabled = $On
}

function Start-TarefaAgora {
    $t = Get-TarefaMacip
    if (-not $t) { throw 'Tarefa MACIP-Backup nao existe  -  salve a configuracao primeiro.' }
    if ($t.State -eq 4) { throw 'Ja ha uma execucao em andamento.' }   # TASK_STATE_RUNNING
    $t.Run($null) | Out-Null
}

function Get-InfoTarefa {
    $t = Get-TarefaMacip
    if (-not $t) { return $null }
    return [pscustomobject]@{
        Habilitada = [bool]$t.Enabled
        Estado     = [int]$t.State          # 4 = em execucao
        Ultima     = [string]$t.LastRunTime
        Proxima    = [string]$t.NextRunTime
        Codigo     = [int]$t.LastTaskResult
    }
}

# ---------- ultimo resultado / classificacao na tela ----------

function Read-UltimoResultado {
    param([string]$Estado)
    $f = Join-Path $Estado 'ultimo-resultado.json'
    if (-not (Test-Path -LiteralPath $f -PathType Leaf)) { return $null }
    try { return (Get-Content -LiteralPath $f -Raw | ConvertFrom-Json) } catch { return $null }
}

function Classify-Resultado {
    # Nunca trata resultado antigo como prova da execucao atual: so e "novo"
    # quando inicio_utc pertence ao disparo informado (LastRunTime da tarefa).
    param($Res, $InfoTarefa)
    if ($InfoTarefa -and $InfoTarefa.Estado -eq 4) { return 'em_execucao' }
    if (-not $Res) { return 'nunca_executou' }
    $novo = $true
    if ($InfoTarefa -and $InfoTarefa.Ultima) {
        try {
            $ultima = [DateTimeOffset]::Parse($InfoTarefa.Ultima)
            $inicio = [DateTimeOffset]::Parse([string]$Res.inicio_utc)
            if ($inicio -lt $ultima.AddMinutes(-2)) { $novo = $false }  # resultado anterior ao ultimo disparo
        } catch { }
    }
    if (-not $novo) { return 'sem_resultado_novo' }
    if ([string]$Res.status -eq 'sucesso') { return 'sucesso' }
    if ($null -ne $Res.codigo_restic -and [int]$Res.codigo_restic -eq 3) { return 'incompleto' }
    return 'falha'
}

$script:RotulosStatus = @{
    sucesso            = 'SUCESSO  -  backup concluido neste disparo'
    falha              = 'FALHA  -  execucao terminou com erro'
    incompleto         = 'INCOMPLETO  -  Restic codigo 3 (nem todo arquivo lido)'
    em_execucao        = 'EM EXECUCAO  -  backup rodando agora'
    sem_resultado_novo = 'SEM RESULTADO NOVO  -  o ultimo resultado e de um disparo anterior'
    nunca_executou     = 'NUNCA EXECUTOU  -  ainda nao ha resultado registrado'
    sem_tarefa         = 'SEM TAREFA  -  configure e salve primeiro'
}

# ---------- restauracao ----------

function Get-CfgOperacional {
    param([string]$Estado)
    $cfg = Read-AgenteConfig -Caminho $script:ConfigFile
    $cfg | Add-Member -NotePropertyName ssh_exe -NotePropertyValue (Get-SshExe) -Force
    $cfg | Add-Member -NotePropertyName ssh_config -NotePropertyValue ((Join-Path $Estado 'ssh_config').Replace('\', '/')) -Force
    return $cfg
}

function Get-Snapshots {
    param([string]$Estado)
    $cfg = Get-CfgOperacional -Estado $Estado
    $r = Invoke-Restic -Cfg $cfg -Estado $Estado -Comando @('snapshots', '--json')
    if ($r.Codigo -ne 0) { throw "Falha ao listar pontos de restauracao (restic $($r.Codigo))." }
    $snaps = @($r.Linhas | ForEach-Object { try { $_ | ConvertFrom-Json } catch { } } |
        Where-Object { $_.id } | ForEach-Object { $_ })
    return $snaps
}

function Get-ArquivosSnapshot {
    param([string]$Estado, [string]$Snapshot)
    $cfg = Get-CfgOperacional -Estado $Estado
    $r = Invoke-Restic -Cfg $cfg -Estado $Estado -Comando @('ls', $Snapshot)
    if ($r.Codigo -ne 0) { throw "Falha ao listar arquivos do ponto (restic $($r.Codigo))." }
    return @($r.Linhas | Where-Object { $_ -and $_.Trim() -ne '' })
}

function Restore-Arquivo {
    # Piloto: restaura SOMENTE em pasta separada  -  nunca sobre a origem nem em
    # pasta que ja tenha conteudo.
    param([string]$Estado, [string]$Snapshot, [string]$Arquivo, [string]$Destino, [string]$Origem)
    if (-not $Destino) { throw 'Escolha a pasta de destino da restauracao.' }
    $destAbs = [IO.Path]::GetFullPath($Destino)
    $origAbs = if ($Origem) { [IO.Path]::GetFullPath($Origem) } else { $null }
    if ($origAbs -and $destAbs.TrimEnd('\','/') -ieq $origAbs.TrimEnd('\','/')) {
        throw 'A restauracao do piloto nao pode ser feita sobre a pasta protegida.'
    }
    if (Test-Path -LiteralPath $destAbs) {
        if ((Get-ChildItem -LiteralPath $destAbs -Force | Measure-Object).Count -gt 0) {
            throw "Destino precisa estar vazio (restauracao nunca sobrescreve): $destAbs"
        }
    } else { New-Item -ItemType Directory -Path $destAbs -Force | Out-Null }
    $cfg = Get-CfgOperacional -Estado $Estado
    $r = Invoke-Restic -Cfg $cfg -Estado $Estado -Comando @('restore', $Snapshot, '--target', $destAbs, '--include', $Arquivo)
    if ($r.Codigo -ne 0) { throw "Falha na restauracao (restic $($r.Codigo))." }
    return $destAbs
}

# ---------- pre-requisitos ----------

function Get-PreRequisitos {
    $falta = @()
    if (Test-IsWindows) {
        $ssh = Join-Path $env:WINDIR 'System32\OpenSSH\ssh.exe'
        if (-not (Test-Path -LiteralPath $ssh -PathType Leaf)) {
            $falta += "Cliente OpenSSH ausente ($ssh). Ative em Configuracoes > Recursos opcionais > Cliente OpenSSH."
        }
    }
    $restic = Join-Path $script:BaseDir 'restic.exe'
    if (-not (Test-Path -LiteralPath $restic -PathType Leaf)) {
        $restic = Join-Path $PSScriptRoot 'restic.exe'
    }
    if (-not (Test-Path -LiteralPath $restic -PathType Leaf)) {
        $falta += 'restic.exe ausente  -  o instalador da MAC IP coloca o binario aprovado na pasta do aplicativo.'
    }
    return [pscustomobject]@{ Ok = ($falta.Count -eq 0); Faltas = $falta; ResticExe = $restic }
}

# ---------- ativacao ----------

function Test-ConexaoDestino {
    # Prova a identidade do servidor e o ID do repositorio SEM gravar estado:
    # pasta temporaria descartavel com known_hosts + chave + ssh_config.
    param([pscustomobject]$Perfil, [string]$ChavePath, [securestring]$SenhaRestic)
    if (-not (Test-Path -LiteralPath $ChavePath -PathType Leaf)) { throw "Chave privada nao encontrada: $ChavePath" }
    $tmp = Join-Path ([IO.Path]::GetTempPath()) ('macip-teste-' + [Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $tmp | Out-Null
    try {
        $kh = Join-Path $tmp 'known_hosts'
        [IO.File]::WriteAllText($kh, ($Perfil.known_hosts -join "`n") + "`n")
        $chave = Join-Path $tmp 'id'
        Copy-Item -LiteralPath $ChavePath -Destination $chave
        if (-not (Test-IsWindows)) { & chmod 600 $chave 2>$null | Out-Null }
        $cfgObj = [ordered]@{ repositorio = $Perfil.repositorio; sftp_host = ''; sftp_user = ''; sftp_porta = '22' }
        $m1 = [regex]::Match($Perfil.repositorio, '^sftp:([^@\s/]+)@([^:/\s]+):')
        $m2 = [regex]::Match($Perfil.repositorio, '^sftp://([^@\s/]+)@([^:/\s]+)(?::(\d{1,5}))?//')
        if ($m1.Success) { $cfgObj.sftp_user = $m1.Groups[1].Value; $cfgObj.sftp_host = $m1.Groups[2].Value }
        else { $cfgObj.sftp_user = $m2.Groups[1].Value; $cfgObj.sftp_host = $m2.Groups[2].Value
               if ($m2.Groups[3].Success) { $cfgObj.sftp_porta = $m2.Groups[3].Value } }
        $cfg = [pscustomobject]@{
            repositorio = $Perfil.repositorio; repositorio_id = $Perfil.repositorio_id
            sftp_user = $cfgObj.sftp_user; sftp_host = $cfgObj.sftp_host; sftp_porta = $cfgObj.sftp_porta
            identidade_ssh = $chave; known_hosts = $kh
            ssh_exe = (Get-SshExe); ssh_config = $null
        }
        $cfg.ssh_config = Write-SshConfig -Cfg $cfg -Estado $tmp
        foreach ($src in @((Join-Path $script:BaseDir 'restic.exe'), (Join-Path $PSScriptRoot 'restic.exe'))) {
            if (Test-Path -LiteralPath $src -PathType Leaf) {
                Copy-Item -LiteralPath $src -Destination (Join-Path $tmp $script:ResticExeNome) -Force
                break
            }
        }
        if (-not (Test-IsWindows)) { & chmod +x (Join-Path $tmp $script:ResticExeNome) 2>$null | Out-Null }
        # restic precisa da senha no processo: usa restic.dpapi da pasta tmp (DPAPI da conta atual)
        Protect-ResticPassword -Senha $SenhaRestic -Destino (Join-Path $tmp 'restic.dpapi')
        $id = Confirm-Destino -Cfg $cfg -Estado $tmp
        return $id
    } finally {
        Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
    }
}

function Complete-Ativacao {
    # Grava identidade + known_hosts + senha (DPAPI da conta atual) e o backup.json.
    # Retorna a pasta de estado usada (nova  -  Preparar recusa sobrescrever).
    param([pscustomobject]$Perfil, [string]$ChavePath, [securestring]$SenhaRestic,
          [string]$Origem, [object[]]$Exclusoes, [string]$Horario)
    New-Item -ItemType Directory -Path $script:BaseDir -Force | Out-Null
    $idDir = Join-Path $script:BaseDir 'id'
    New-Item -ItemType Directory -Path $idDir -Force | Out-Null
    $chave = Join-Path $idDir 'id_cliente'
    Copy-Item -LiteralPath $ChavePath -Destination $chave -Force
    if (-not (Test-IsWindows)) { & chmod 600 $chave 2>$null | Out-Null }
    $kh = Join-Path $script:BaseDir 'known_hosts'
    [IO.File]::WriteAllText($kh, ($Perfil.known_hosts -join "`n") + "`n")

    Save-ConfigAtomico -CfgObj (New-BackupJsonObj -Perfil $Perfil -Origem $Origem -Exclusoes $Exclusoes `
        -Horario $Horario -IdentidadeSsh $chave -KnownHostsFile $kh)

    $meta = Read-GuiMeta
    $n = 1
    if ($meta.estado_atual -and $meta.estado_atual -match 'estado-(\d+)') { $n = [int]$Matches[1] + 1 }
    $estado = Join-Path $script:BaseDir ("estado-$n")
    $restic = (Get-PreRequisitos).ResticExe
    Invoke-AgentePreparar -Config $script:ConfigFile -DiretorioEstado $estado -ResticExe $restic -SenhaRestic $SenhaRestic | Out-Null
    $meta.estado_atual = $estado
    $meta.conta_tarefa = (Get-IdentidadeAtual).Nome
    Save-GuiMeta -Meta $meta
    return $estado
}

# =================== INTERFACE ===================

function Show-Gui {
    Add-Type -AssemblyName System.Windows.Forms
    Add-Type -AssemblyName System.Drawing
    $W = 'System.Windows.Forms'

    $form = New-Object "$W.Form"
    $form.Text = 'MAC IP Backup'
    $form.Size = New-Object System.Drawing.Size(560, 470)
    $form.StartPosition = 'CenterScreen'
    $form.MaximizeBox = $false
    $form.FormBorderStyle = 'FixedSingle'

    $tabs = New-Object "$W.TabControl"
    $tabs.Dock = 'Fill'
    $form.Controls.Add($tabs)

    function New-Tab([string]$t) {
        $p = New-Object "$W.TabPage"; $p.Text = $t; $tabs.TabPages.Add($p); return $p
    }
    function New-Label([System.Windows.Forms.Control]$p, [string]$t, [int]$x, [int]$y, [int]$w = 500) {
        $l = New-Object "$W.Label"; $l.Text = $t; $l.Location = New-Object System.Drawing.Point($x, $y)
        $l.Size = New-Object System.Drawing.Size($w, 20); $p.Controls.Add($l); return $l
    }
    function New-Button([System.Windows.Forms.Control]$p, [string]$t, [int]$x, [int]$y, [int]$w = 130) {
        $b = New-Object "$W.Button"; $b.Text = $t; $b.Location = New-Object System.Drawing.Point($x, $y)
        $b.Size = New-Object System.Drawing.Size($w, 28); $p.Controls.Add($b); return $b
    }
    function New-TextBox([System.Windows.Forms.Control]$p, [string]$t, [int]$x, [int]$y, [int]$w = 380) {
        $tb = New-Object "$W.TextBox"; $tb.Text = $t; $tb.Location = New-Object System.Drawing.Point($x, $y)
        $tb.Size = New-Object System.Drawing.Size($w, 24); $p.Controls.Add($tb); return $tb
    }

    $pre = Get-PreRequisitos
    if (-not $pre.Ok) {
        [void][System.Windows.Forms.MessageBox]::Show($form,
            ("Pre-requisitos faltando:`n`n" + ($pre.Faltas -join "`n")),
            'MAC IP Backup', 'OK', 'Warning')
    }

    $conta = (Get-IdentidadeAtual).Nome
    $meta = Read-GuiMeta

    # ----- Tab 1: Ativacao -----
    $t1 = New-Tab '1. Ativar'
    New-Label $t1 "Conta Windows em uso: $conta" 10 12 | Out-Null
    New-Label $t1 'A conta acima sera a conta da tarefa de backup (o preparo e a senha ficam vinculados a ela).' 10 34 | Out-Null
    $tbPerfil = New-TextBox $t1 '' 10 66 380
    $tbPerfil.ReadOnly = $true
    $script:perfilAtual = $null
    $btPerfil = New-Button $t1 'Importar perfil...' 400 64
    $tbChave = New-TextBox $t1 '' 10 106 380
    $tbChave.ReadOnly = $true
    $btChave = New-Button $t1 'Chave privada...' 400 104
    New-Label $t1 'Senha Restic (fornecida pela MAC IP  -  nunca vai para o perfil nem para o JSON):' 10 142 | Out-Null
    $tbSenha = New-TextBox $t1 '' 10 164 240
    $tbSenha.UseSystemPasswordChar = $true
    $lblAtv = New-Label $t1 '' 10 230 520
    $btTestar = New-Button $t1 'Testar conexao' 10 196
    $btAtivar = New-Button $t1 'Ativar' 150 196
    $btAtivar.Enabled = $false

    $btPerfil.Add_Click({
        $dlg = New-Object "$W.OpenFileDialog"
        $dlg.Filter = 'Perfil MAC IP (*.json)|*.json'
        if ($dlg.ShowDialog() -eq 'OK') {
            try {
                $script:perfilAtual = Read-Perfil -Caminho $dlg.FileName
                $tbPerfil.Text = $dlg.FileName
                $lblAtv.ForeColor = 'DarkGreen'
                $lblAtv.Text = "Perfil OK: $($script:perfilAtual.repositorio)"
            } catch {
                $script:perfilAtual = $null; $tbPerfil.Text = ''
                $lblAtv.ForeColor = 'DarkRed'; $lblAtv.Text = $_.Exception.Message
            }
        }
    })
    $btChave.Add_Click({
        $dlg = New-Object "$W.OpenFileDialog"
        $dlg.Title = 'Chave privada SSH fornecida pela MAC IP'
        if ($dlg.ShowDialog() -eq 'OK') { $tbChave.Text = $dlg.FileName }
    })
    $btTestar.Add_Click({
        $btTestar.Enabled = $false; $lblAtv.ForeColor = 'DarkBlue'; $lblAtv.Text = 'Testando...'
        $form.Refresh()
        try {
            if (-not $script:perfilAtual) { throw 'Importe o perfil primeiro.' }
            if (-not $tbChave.Text) { throw 'Escolha o arquivo da chave privada.' }
            if (-not $tbSenha.Text) { throw 'Informe a senha Restic.' }
            $ss = ConvertTo-SecureString $tbSenha.Text -AsPlainText -Force
            $id = Test-ConexaoDestino -Perfil $script:perfilAtual -ChavePath $tbChave.Text -SenhaRestic $ss
            $lblAtv.ForeColor = 'DarkGreen'
            $lblAtv.Text = "Conexao OK  -  identidade do servidor e ID do repositorio conferem ($($id.Substring(0,12))...)."
            $btAtivar.Enabled = $true
        } catch {
            $lblAtv.ForeColor = 'DarkRed'; $lblAtv.Text = "Recusado: $($_.Exception.Message)"
            $btAtivar.Enabled = $false
        } finally { $btTestar.Enabled = $true }
    })
    $btAtivar.Add_Click({
        $tabs.SelectedTab = $t2
        [void][System.Windows.Forms.MessageBox]::Show($form,
            'Ativacao validada. Agora escolha a pasta, as exclusoes e o horario na aba Configurar.',
            'MAC IP Backup', 'OK', 'Information')
    })

    # ----- Tab 2: Configurar -----
    $t2 = New-Tab '2. Configurar'
    New-Label $t2 'Pasta protegida:' 10 14 | Out-Null
    $tbOrigem = New-TextBox $t2 '' 10 36 380
    $btOrigem = New-Button $t2 'Escolher pasta...' 400 34
    New-Label $t2 'Exclusoes (opcional, uma por linha  -  ex.: *.tmp):' 10 70 | Out-Null
    $tbExcl = New-Object "$W.TextBox"
    $tbExcl.Multiline = $true; $tbExcl.ScrollBars = 'Vertical'
    $tbExcl.Location = New-Object System.Drawing.Point(10, 92)
    $tbExcl.Size = New-Object System.Drawing.Size(520, 80)
    $t2.Controls.Add($tbExcl)
    New-Label $t2 'Horario diario (HH:mm):' 10 184 | Out-Null
    $tbHora = New-TextBox $t2 '02:30' 150 182 80
    $lblCfg = New-Label $t2 '' 10 250 520
    $btSalvar = New-Button $t2 'Salvar e agendar' 10 216 160

    $btOrigem.Add_Click({
        $dlg = New-Object "$W.FolderBrowserDialog"
        $dlg.Description = 'Escolha a pasta local a proteger'
        if ($dlg.ShowDialog() -eq 'OK') { $tbOrigem.Text = $dlg.SelectedPath }
    })

    # Preenche com a config existente, se houver (reabrir o app mostra o atual).
    if (Test-Path -LiteralPath $script:ConfigFile -PathType Leaf) {
        try {
            $cfgAtual = Read-AgenteConfig -Caminho $script:ConfigFile
            $tbOrigem.Text = $cfgAtual.origem
            $tbExcl.Lines = $cfgAtual.exclusoes
            $tbHora.Text = $cfgAtual.horario
        } catch { }
    }

    $btSalvar.Add_Click({
        $btSalvar.Enabled = $false; $lblCfg.ForeColor = 'DarkBlue'; $lblCfg.Text = 'Salvando...'
        $form.Refresh()
        try {
            if (-not $script:perfilAtual) {
                # reabertura: perfil ja usado antes  -  remonta do backup.json atual
                if (-not (Test-Path -LiteralPath $script:ConfigFile)) { throw 'Ative um perfil na aba 1 primeiro.' }
            }
            $excl = @($tbExcl.Lines | Where-Object { $_ -and $_.Trim() -ne '' })
            if (-not $tbOrigem.Text) { throw 'Escolha a pasta a proteger.' }
            if ($tbHora.Text -cnotmatch '^([01][0-9]|2[0-3]):[0-5][0-9]$') { throw 'Horario invalido (use HH:mm).' }

            $estado = $meta.estado_atual
            $perfil = $script:perfilAtual
            if ($perfil) {
                # ativacao nova ou troca de perfil: roda o preparo completo em estado NOVO
                $estado = Complete-Ativacao -Perfil $perfil -ChavePath $tbChave.Text `
                    -SenhaRestic (ConvertTo-SecureString $tbSenha.Text -AsPlainText -Force) `
                    -Origem $tbOrigem.Text -Exclusoes $excl -Horario $tbHora.Text
                $meta = Read-GuiMeta
            } else {
                # ajuste de pasta/exclusoes/horario no MESMO destino: so o JSON muda
                if (-not $estado -or -not (Test-Path -LiteralPath (Join-Path $estado 'preparo.json') -PathType Leaf)) {
                    throw 'Sem preparo concluido nesta maquina  -  conclua a ativacao na aba 1 primeiro.'
                }
                $cfg = Read-AgenteConfig -Caminho $script:ConfigFile
                $prep = Get-Content -LiteralPath (Join-Path $estado 'preparo.json') -Raw | ConvertFrom-Json
                $cfgObj = [ordered]@{
                    origem = $tbOrigem.Text; exclusoes = $excl; repositorio = $cfg.repositorio
                    horario = $tbHora.Text; repositorio_id = $cfg.repositorio_id
                    identidade_ssh = $prep.identidade_ssh; known_hosts = $prep.known_hosts
                }
                Save-ConfigAtomico -CfgObj $cfgObj
            }
            # Tarefa: criar ou atualizar  -  pede a senha Windows da conta na janela nativa
            $cred = $host.UI.PromptForCredential('MAC IP Backup',
                "Senha do Windows da conta $conta (a tarefa roda mesmo sem sessao aberta)",
                $conta, '')
            if (-not $cred) { throw 'Agendamento cancelado pelo operador.' }
            Save-TarefaMacip -Horario $tbHora.Text -Conta $conta `
                -SenhaConta $cred.GetNetworkCredential().Password -Estado $estado
            $script:perfilAtual = $null
            $lblCfg.ForeColor = 'DarkGreen'
            $lblCfg.Text = "Salvo. Tarefa '$($script:TaskName)' agendada para $($tbHora.Text) na conta $conta."
        } catch {
            $lblCfg.ForeColor = 'DarkRed'; $lblCfg.Text = "Erro: $($_.Exception.Message)"
        } finally { $btSalvar.Enabled = $true }
    })

    # ----- Tab 3: Operar -----
    $t3 = New-Tab '3. Operar'
    $lblPasta = New-Label $t3 'Pasta protegida:  - ' 10 14
    $lblStatus = New-Label $t3 'Situacao:  - ' 10 40
    $lblStatus.Font = New-Object System.Drawing.Font($lblStatus.Font, 'Bold')
    $lblProx = New-Label $t3 'Proxima execucao:  - ' 10 66
    $lblUlt = New-Label $t3 'Ultimo resultado:  - ' 10 92
    $btAgora = New-Button $t3 'Executar agora' 10 130 130
    $btPausa = New-Button $t3 'Pausar' 150 130 90
    $btRetomar = New-Button $t3 'Retomar' 250 130 90
    $btAtual = New-Button $t3 'Atualizar' 400 130 90
    $lblOp = New-Label $t3 '' 10 170 520

    $script:AtualizarTela = {
        try {
            $info = Get-InfoTarefa
            $meta = Read-GuiMeta
            if (Test-Path -LiteralPath $script:ConfigFile -PathType Leaf) {
                try { $lblPasta.Text = 'Pasta protegida: ' + (Read-AgenteConfig -Caminho $script:ConfigFile).origem } catch { }
            }
            if (-not $info) { $lblStatus.Text = 'Situacao: ' + $script:RotulosStatus.sem_tarefa; $lblProx.Text = 'Proxima execucao:  - '; $lblUlt.Text = 'Ultimo resultado:  - '; return }
            $estado = $meta.estado_atual
            $res = if ($estado) { Read-UltimoResultado -Estado $estado } else { $null }
            $cls = Classify-Resultado -Res $res -InfoTarefa $info
            $cor = switch ($cls) {
                'sucesso' { 'DarkGreen' } 'em_execucao' { 'DarkBlue' } 'incompleto' { 'DarkOrange' }
                default { 'DarkRed' }
            }
            $lblStatus.ForeColor = $cor
            $lblStatus.Text = 'Situacao: ' + $script:RotulosStatus[$cls]
            $lblProx.Text = 'Proxima execucao: ' + $(if ($info.Habilitada) { $info.Proxima } else { 'PAUSADO' })
            $lblUlt.Text = 'Ultimo resultado: ' + $(if ($res) {
                "$($res.status) em $($res.fim_utc) (restic $($res.codigo_restic), snapshot $($res.snapshot))"
            } else { ' - ' })
            $btPausa.Enabled = $info.Habilitada
            $btRetomar.Enabled = -not $info.Habilitada
            $btAgora.Enabled = ($info.Estado -ne 4)
        } catch { $lblStatus.Text = 'Situacao: erro ao ler  -  ' + $_.Exception.Message }
    }
    $btAgora.Add_Click({ try { Start-TarefaAgora; $lblOp.ForeColor='DarkGreen'; $lblOp.Text = 'Disparo manual enviado  -  a mesma tarefa agendada executa agora.' } catch { $lblOp.ForeColor='DarkRed'; $lblOp.Text = $_.Exception.Message }; & $script:AtualizarTela })
    $btPausa.Add_Click({ try { Set-TarefaHabilitada $false; $lblOp.Text = 'Tarefa pausada  -  o disparo agendado nao roda ate retomar.' } catch { $lblOp.Text = $_.Exception.Message }; & $script:AtualizarTela })
    $btRetomar.Add_Click({ try { Set-TarefaHabilitada $true; $lblOp.Text = 'Tarefa retomada.' } catch { $lblOp.Text = $_.Exception.Message }; & $script:AtualizarTela })
    $btAtual.Add_Click({ & $script:AtualizarTela })

    # ----- Tab 4: Restaurar -----
    $t4 = New-Tab '4. Restaurar'
    New-Label $t4 'Ponto de restauracao:' 10 14 | Out-Null
    $cbSnap = New-Object "$W.ComboBox"
    $cbSnap.DropDownStyle = 'DropDownList'; $cbSnap.Location = New-Object System.Drawing.Point(10, 36)
    $cbSnap.Size = New-Object System.Drawing.Size(520, 24); $t4.Controls.Add($cbSnap)
    New-Label $t4 'Arquivo no ponto escolhido:' 10 70 | Out-Null
    $cbArq = New-Object "$W.ComboBox"
    $cbArq.Location = New-Object System.Drawing.Point(10, 92)
    $cbArq.Size = New-Object System.Drawing.Size(520, 24); $t4.Controls.Add($cbArq)
    $tbDest = New-TextBox $t4 '' 10 136 380
    $btDest = New-Button $t4 'Pasta de destino...' 400 134
    $lblRes = New-Label $t4 '' 10 210 520
    $btRest = New-Button $t4 'Restaurar arquivo' 10 170 150
    $btSnaps = New-Button $t4 'Carregar pontos' 10 170 130
    $btRest.Location = New-Object System.Drawing.Point(150, 170)

    $btSnaps.Add_Click({
        $btSnaps.Enabled = $false; $lblRes.ForeColor = 'DarkBlue'; $lblRes.Text = 'Listando pontos...'; $form.Refresh()
        try {
            $meta = Read-GuiMeta
            if (-not $meta.estado_atual) { throw 'Sem preparo concluido  -  ative e configure primeiro.' }
            $snaps = Get-Snapshots -Estado $meta.estado_atual
            $script:snaps = @($snaps)
            $cbSnap.Items.Clear()
            foreach ($s in $snaps) { [void]$cbSnap.Items.Add(('{0}  -  {1}' -f $s.short_id, $s.time)) }
            if ($cbSnap.Items.Count -gt 0) { $cbSnap.SelectedIndex = $cbSnap.Items.Count - 1 }
            $lblRes.ForeColor = 'DarkGreen'; $lblRes.Text = "$($cbSnap.Items.Count) ponto(s) disponivel(is)."
        } catch { $lblRes.ForeColor = 'DarkRed'; $lblRes.Text = $_.Exception.Message }
        finally { $btSnaps.Enabled = $true }
    })
    $cbSnap.Add_SelectedIndexChanged({
        try {
            $cbArq.Items.Clear()
            $meta = Read-GuiMeta
            $snap = $script:snaps[$cbSnap.SelectedIndex]
            foreach ($a in (Get-ArquivosSnapshot -Estado $meta.estado_atual -Snapshot $snap.id)) { [void]$cbArq.Items.Add($a) }
            if ($cbArq.Items.Count -gt 0) { $cbArq.SelectedIndex = 0 }
        } catch { $lblRes.ForeColor = 'DarkRed'; $lblRes.Text = $_.Exception.Message }
    })
    $btDest.Add_Click({
        $dlg = New-Object "$W.FolderBrowserDialog"
        $dlg.Description = 'Pasta SEPARADA para receber o arquivo restaurado'
        if ($dlg.ShowDialog() -eq 'OK') { $tbDest.Text = $dlg.SelectedPath }
    })
    $btRest.Add_Click({
        $btRest.Enabled = $false; $lblRes.ForeColor = 'DarkBlue'; $lblRes.Text = 'Restaurando...'; $form.Refresh()
        try {
            $meta = Read-GuiMeta
            $snap = $script:snaps[$cbSnap.SelectedIndex]
            $cfg = Read-AgenteConfig -Caminho $script:ConfigFile
            $dest = Restore-Arquivo -Estado $meta.estado_atual -Snapshot $snap.id `
                -Arquivo $cbArq.Text -Destino $tbDest.Text -Origem $cfg.origem
            $lblRes.ForeColor = 'DarkGreen'
            $lblRes.Text = "Restaurado com sucesso em: $dest (o arquivo original NAO foi sobrescrito)."
        } catch { $lblRes.ForeColor = 'DarkRed'; $lblRes.Text = $_.Exception.Message }
        finally { $btRest.Enabled = $true }
    })

    $tabs.Add_SelectedIndexChanged({ if ($tabs.SelectedTab -eq $t3) { & $script:AtualizarTela } })
    & $script:AtualizarTela
    [void]$form.ShowDialog()
}

if ($MyInvocation.InvocationName -eq '.') { return }

if (-not (Test-IsWindows)) {
    Write-Error 'A interface grafica requer Windows (Agendador de Tarefas + WinForms).'
    exit 2
}
Show-Gui
