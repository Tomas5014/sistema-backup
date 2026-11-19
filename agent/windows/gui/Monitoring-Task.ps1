#requires -Version 5.1
# Segunda tarefa do MESMO aplicativo; nunca inicia backup e usa a conta DPAPI.
$script:MonitorTaskName='MACIP-Backup-Sync'
$script:MonitorTaskDescription='MACIP Backup - envio central do piloto'
$script:MonitorBackend=$null

function Get-MonitorBackend {
    if (-not $script:MonitorBackend) { $script:MonitorBackend=New-BackendCom }
    return $script:MonitorBackend
}

function Get-MonitorTaskInfo {
    $backend=Get-MonitorBackend
    return Get-TarefaInfo -Backend $backend -Name $script:MonitorTaskName
}

function Test-MonitorTaskOwner {
    param($Task)
    try {
        $def=$Task.Definition
        if ($def.RegistrationInfo.Description -cne $script:MonitorTaskDescription -or [int]$def.Actions.Count -ne 1) { return $false }
        $action=$def.Actions.Item(1)
        if ((ConvertTo-CaminhoNormal $action.Path) -ine (ConvertTo-CaminhoNormal $script:PsExeEsperado)) { return $false }
        if ([string]$action.Arguments -notmatch '^-NoProfile -NonInteractive -File "([^"]+)" -Modo Sincronizar -Config "([^"]+)"$') { return $false }
        $scriptPath=$Matches[1];$configPath=$Matches[2]
        $base=(ConvertTo-CaminhoNormal $script:BaseDir).TrimEnd('\')+'\'
        if (-not (ConvertTo-CaminhoNormal $configPath).StartsWith($base,[StringComparison]::OrdinalIgnoreCase)) { return $false }
        return (ConvertTo-CaminhoNormal $scriptPath) -ieq (ConvertTo-CaminhoNormal $script:AgentePath)
    } catch { return $false }
}

function Get-MonitorTaskPreflight {
    param([string]$Config)
    if (-not (Read-Monitor $Config) -and -not (Test-IsWindows) -and -not $script:MonitorBackend) { return $null }
    $info=Get-MonitorTaskInfo
    if ($info.Status -eq 'erro') { throw 'Consulta da tarefa de envio inconclusiva; nenhuma alteracao autorizada.' }
    if ($info.Status -eq 'encontrada') {
        if (-not (Test-MonitorTaskOwner $info.Tarefa)) {
            if (Read-Monitor $Config) { throw 'Tarefa de envio homonima nao pertence a esta instalacao; recusado.' }
            return $null # perfil sem monitoramento nunca toca uma tarefa alheia
        }
        $instances=$info.Tarefa.GetInstances(0)
        if ($null -eq $instances -or [int]$info.Tarefa.State -notin @(1,3) -or [int]$instances.Count -gt 0) { throw 'Envio ativo ou estado inconclusivo; aguarde antes de salvar.' }
        return [pscustomobject]@{Exists=$true;Xml=[string]$info.Tarefa.Xml;Task=$info.Tarefa}
    }
    return [pscustomobject]@{Exists=$false;Xml=$null;Task=$null}
}

function Register-MonitorTask {
    param([string]$Config,[string]$Conta,[string]$SenhaConta,$Previous)
    if (-not $Previous) { return }
    if (-not (Read-Monitor $Config)) {
        if ($Previous.Exists) { $Previous.Task.Enabled=$false }
        return
    }
    $backend=Get-MonitorBackend
    $def=$backend.NewDef($Previous.Task)
    $def.RegistrationInfo.Description=$script:MonitorTaskDescription
    $def.Settings.Enabled=$true
    $def.Settings.MultipleInstances=2
    $def.Settings.StartWhenAvailable=$true
    $def.Settings.DisallowStartIfOnBatteries=$false
    $def.Settings.StopIfGoingOnBatteries=$false
    $def.Settings.ExecutionTimeLimit='PT2M'
    $def.Triggers.Clear()
    $trigger=$def.Triggers.Create(1) # TIME_TRIGGER com repeticao sem data de fim
    $trigger.StartBoundary=[DateTime]::Now.AddMinutes(1).ToString('s')
    $trigger.Repetition.Interval='PT5M'
    $trigger.Enabled=$true
    while ($def.Actions.Count -gt 0) { $def.Actions.Remove(1) }
    $action=$def.Actions.Create(0)
    $action.Path=$script:PsExeEsperado
    $action.Arguments='-NoProfile -NonInteractive -File "{0}" -Modo Sincronizar -Config "{1}"' -f $script:AgentePath,$Config
    $def.Principal.UserId=$Conta
    $def.Principal.LogonType=$script:LogonPassword
    $def.Principal.RunLevel=0
    $flag=if($Previous.Exists){$script:TaskUpdate}else{$script:TaskCreate}
    $backend.Registrar($def,$Conta,$SenhaConta,$flag,$script:MonitorTaskName)
}

function Restore-MonitorTask {
    param($Previous,[string]$Config,[string]$Conta,[string]$SenhaConta)
    if (-not $Previous) { return }
    $info=Get-MonitorTaskInfo
    if ($info.Status -eq 'ausente' -and -not $Previous.Exists) { return }
    if ($info.Status -ne 'encontrada' -or -not (Test-MonitorTaskOwner $info.Tarefa)) { throw 'Rollback do envio inconclusivo; arquivos devem ser preservados.' }
    if ($Previous.Exists) { (Get-MonitorBackend).RegistrarXml($Previous.Xml,$Conta,$SenhaConta,$script:MonitorTaskName) }
    else {
        # Nao remove tarefa trocada nem arquivos referenciados. A nova fica desabilitada.
        $info.Tarefa.Enabled=$false
    }
}

function Test-MonitorUsesConfiguration {
    param([string]$Config)
    if (-not (Test-IsWindows) -and -not $script:MonitorBackend) { return $false }
    try {
        $info=Get-MonitorTaskInfo
        if ($info.Status -eq 'ausente') { return $false }
        if ($info.Status -ne 'encontrada') { return $true }
        if (-not (Test-MonitorTaskOwner $info.Tarefa)) { return $false }
        if ($info.Tarefa.Definition.Actions.Item(1).Arguments -notmatch '-Config "([^"]+)"$') { return $true }
        return (ConvertTo-CaminhoNormal $Matches[1]) -ieq (ConvertTo-CaminhoNormal $Config)
    } catch { return $true }
}
