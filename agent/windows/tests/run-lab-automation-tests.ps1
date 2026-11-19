#requires -Version 5.1
# COM, DPAPI e HTTP simulados; definicoes e controle de arquivos reais em pasta exclusiva.
$ErrorActionPreference='Stop'
$root=Join-Path ([IO.Path]::GetTempPath()) ('auto-monitor-'+[Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory $root | Out-Null
$checks=0
function Assert($value,$message){if(-not $value){throw $message};$script:checks++}
function Collection($factory){
    $c=[pscustomobject]@{items=[Collections.ArrayList]::new();factory=$factory}
    $c|Add-Member ScriptProperty Count {$this.items.Count}
    $c|Add-Member ScriptMethod Clear {$this.items.Clear()}
    $c|Add-Member ScriptMethod Remove {param($n)$this.items.RemoveAt($n-1)}
    $c|Add-Member ScriptMethod Item {param($n)return $this.items[$n-1]}
    $c|Add-Member ScriptMethod Create {param($kind)$obj=& $this.factory $kind;[void]$this.items.Add($obj);return $obj}
    return $c
}
function Definition {
    [pscustomobject]@{
        RegistrationInfo=[pscustomobject]@{Description=''}
        Settings=[pscustomobject]@{Enabled=$false;MultipleInstances=0;StartWhenAvailable=$false;DisallowStartIfOnBatteries=$true;StopIfGoingOnBatteries=$true;ExecutionTimeLimit=''}
        Principal=[pscustomobject]@{UserId='';LogonType=0;RunLevel=0}
        Triggers=(Collection {param($kind)[pscustomobject]@{Kind=$kind;StartBoundary='';Repetition=[pscustomobject]@{Interval=''};DaysInterval=0;Enabled=$false}})
        Actions=(Collection {param($kind)[pscustomobject]@{Path='';Arguments=''}})
    }
}
function Backend {
    $b=[pscustomobject]@{task=$null;count=0;fail=$false;name=$null;history=@{}}
    $b|Add-Member ScriptMethod Conectar {}
    $b|Add-Member ScriptMethod Get {param($name)
        if(-not $this.task){throw [Runtime.InteropServices.COMException]::new('ausente',-2147024894)}
        return $this.task}
    $b|Add-Member ScriptMethod NewDef {param($old)return (Definition)}
    $b|Add-Member ScriptMethod Registrar {param($def,$account,$password,$flag,$name)
        if($this.fail){throw 'registro recusado simulado'}
        $this.count++;$this.name=$name
        $xml=[Guid]::NewGuid().ToString()
        $this.task=[pscustomobject]@{Definition=$def;Enabled=$true;State=3;Xml=$xml}
        $this.task|Add-Member ScriptMethod GetInstances {param($x)return [pscustomobject]@{Count=0}}
        $this.history[$xml]=$this.task
    }
    $b|Add-Member ScriptMethod RegistrarXml {param($xml,$account,$password,$name)$this.name=$name;$this.task=$this.history[$xml]}
    $b|Add-Member ScriptMethod XmlAtual {if($this.task){return $this.task.Xml};return $null}
    return $b
}
try {
    . (Join-Path $PSScriptRoot '../gui/MacipBackup-Gui.ps1') -BaseDir $root -AgentePath (Join-Path $PSScriptRoot '../Backup-MacipCliente.ps1')
    $cfg=Join-Path $root 'backup.json';[IO.File]::WriteAllText($cfg,'{}')
    $queue=Join-Path $root 'queue';New-Item -ItemType Directory $queue|Out-Null
    Write-MonitorJson (Join-Path $root 'monitoring.json') @{central='https://central.lab';installation_id=[Guid]::NewGuid().ToString();queue=$queue}
    $script:MonitorBackend=Backend
    $previous=Get-MonitorTaskPreflight $cfg
    Assert (-not $previous.Exists) 'ausencia confirmada da tarefa de envio'
    Register-MonitorTask $cfg 'LAB\operador' 'synthetic' $previous
    $task=$script:MonitorBackend.task
    Assert ($script:MonitorBackend.name -ceq 'MACIP-Backup-Sync') 'nome exclusivo de envio'
    Assert (Test-MonitorTaskOwner $task) 'propriedade completa reconhecida'
    Assert ($task.Definition.Actions.Item(1).Arguments -match '-Modo Sincronizar -Config') 'sincronizacao nao dispara backup'
    Assert ($task.Definition.Principal.UserId -ceq 'LAB\operador' -and $task.Definition.Principal.LogonType -eq 1) 'mesma conta com logon por senha'
    Assert ($task.Definition.Triggers.Item(1).Repetition.Interval -ceq 'PT5M') 'periodicidade de cinco minutos'
    Assert ($task.Definition.Settings.MultipleInstances -eq 2 -and $task.Definition.Settings.ExecutionTimeLimit -ceq 'PT2M') 'limite e exclusao de instancia'
    Assert (Test-MonitorUsesConfiguration $cfg) 'limpeza preserva configuracao referenciada pelo envio'
    $previous=Get-MonitorTaskPreflight $cfg
    Register-MonitorTask $cfg 'LAB\operador' 'synthetic' $previous
    Assert ($script:MonitorBackend.count -eq 2) 'atualizacao reutiliza a mesma tarefa'
    $script:MonitorBackend.task.Definition.RegistrationInfo.Description='tarefa alheia'
    try{Get-MonitorTaskPreflight $cfg;throw 'aceitou tarefa alheia'}catch{Assert ($_.Exception.Message -like 'Tarefa de envio homonima*') 'colisao recusada antes de alterar backup'}
    Assert ($script:MonitorBackend.count -eq 2) 'colisao nao registra tarefa'
    $script:MonitorBackend.task.Definition.RegistrationInfo.Description=$script:MonitorTaskDescription
    $script:MonitorBackend.task.Definition.Actions.Item(1).Path='C:\Temp\powershell.exe'
    Assert (-not (Test-MonitorTaskOwner $script:MonitorBackend.task)) 'executavel semelhante nao vale'
    $script:MonitorBackend.task.Definition.Actions.Item(1).Path=$script:PsExeEsperado
    $script:MonitorBackend.task.Definition.Actions.Item(1).Arguments+=' -Extra x'
    Assert (-not (Test-MonitorTaskOwner $script:MonitorBackend.task)) 'argumento livre recusado'
    # Limites e locks reais da fila, HTTP simulado.
    $script:sent=0
    function Invoke-MonitorHttp {param($Monitor,$Path,$Body,$TimeoutSeconds)$script:sent++}
    Set-MonitorSchedule $cfg '10:00' $false
    Set-MonitorSchedule $cfg '11:00' $false
    [void](Sync-MonitorQueue $cfg -MaxMessages 1 -MaxSeconds 5)
    Assert ($script:sent -eq 1 -and (Get-MonitorStatus $cfg).state -eq 'pending') 'quantidade limitada conserva pendentes'
    [void](Sync-MonitorQueue $cfg)
    Assert ((Get-MonitorStatus $cfg).state -eq 'synchronized') 'estado sincronizado so apos confirmacao'
    $success=(Get-MonitorStatus $cfg).last_success
    [void](Sync-MonitorQueue $cfg)
    Assert ((Get-MonitorStatus $cfg).last_success -ceq $success) 'fila vazia nao inventa contato novo'
    $lock=[IO.File]::Open((Join-Path $queue 'sync.lock'),'OpenOrCreate','ReadWrite','None')
    try{
        Assert ((Sync-MonitorQueue $cfg) -ceq 'Envio ja em andamento') 'sincronizacao concorrente nao envia'
        Set-MonitorSchedule $cfg '11:00' $true
        Assert ((Get-MonitorStatus $cfg).pending -gt 0) 'pausa persiste mesmo enquanto outro processo envia'
    }finally{$lock.Dispose()}
    # Entrada publica da GUI: publica as DUAS tarefas e reverte se o segundo registro falhar.
    $script:Backend=Backend;$script:MonitorBackend=Backend
    $state=Join-Path $root 'estado';New-Item -ItemType Directory $state|Out-Null
    $built=[pscustomobject]@{Config=$cfg;ConfigDir=$root;Estado=$state;EstadoNovo=$false;Horario='10:00';Publicado=$false}
    Publish-Configuracao $built 'LAB\operador' 'synthetic'
    Assert ($script:Backend.count -eq 1 -and $script:MonitorBackend.count -eq 1) 'GUI publica backup e envio na mesma conta'
    Assert ($script:MonitorBackend.task.Definition.Principal.UserId -ceq $script:Backend.task.Definition.Principal.UserId) 'GUI conserva a identidade entre as tarefas'
    $oldBackup=$script:Backend.task.Definition.Actions.Item(1).Arguments
    $oldSync=$script:MonitorBackend.task.Definition.Actions.Item(1).Arguments
    $newDir=Join-Path $root 'config nova';New-Item -ItemType Directory $newDir|Out-Null
    $newConfig=Join-Path $newDir 'backup.json';[IO.File]::WriteAllText($newConfig,'{}')
    Copy-Item (Join-Path $root 'monitoring.json') (Join-Path $newDir 'monitoring.json')
    $newBuilt=[pscustomobject]@{Config=$newConfig;ConfigDir=$newDir;Estado=$state;EstadoNovo=$false;Horario='11:00';Publicado=$false}
    $script:MonitorBackend.fail=$true
    try{Publish-Configuracao $newBuilt 'LAB\operador' 'synthetic';throw 'aceitou falha'}catch{Assert ($_.Exception.Message -like 'Falha ao publicar*') 'falha do envio cancela publicacao conjunta'}
    Assert ($script:Backend.task.Definition.Actions.Item(1).Arguments -ceq $oldBackup) 'rollback restaura tarefa ativa anterior'
    Assert ($script:MonitorBackend.task.Definition.Actions.Item(1).Arguments -ceq $oldSync) 'rollback conserva tarefa de envio anterior'
    Assert (Test-Path $cfg) 'rollback preserva a configuracao ativa'
    $script:MonitorBackend.fail=$false
    $script:MonitorBackend.task.State=4
    $count=$script:Backend.count
    try{Publish-Configuracao $built 'LAB\operador' 'synthetic';throw 'aceitou ativo'}catch{Assert ($_.Exception.Message -like 'Envio ativo*') 'publicacao recusa sincronizacao ativa antes de tocar backup'}
    Assert ($script:Backend.count -eq $count) 'recusa deixa tarefa ativa intacta'
    $script:MonitorBackend.task.State=3
    Remove-Item (Join-Path $root 'monitoring.json')
    Assert ((Get-MonitorStatus $cfg).state -eq 'unconfigured') 'perfil sem monitoramento continua funcionando'
    Write-Host "$checks verificacoes de automacao Windows aprovadas; COM/HTTP/DPAPI simulados"
} finally {Remove-Item -LiteralPath $root -Recurse -Force}
