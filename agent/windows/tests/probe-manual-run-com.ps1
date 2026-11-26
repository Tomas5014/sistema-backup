#requires -Version 5.1
# COM real apenas em pasta/tarefa exclusivas. Nenhuma tarefa MACIP instalada e consultada.
$ErrorActionPreference='Stop'
if($env:OS -ne 'Windows_NT' -or $PSVersionTable.PSVersion.Major -ne 5 -or $PSVersionTable.PSVersion.Minor -ne 1){throw 'Gate pendente: execute com Windows PowerShell 5.1; Linux/simulacao nao aprovam COM.'}
$identity=[Security.Principal.WindowsIdentity]::GetCurrent()
try{$account=$identity.Name;$sid=$identity.User.Value}finally{$identity.Dispose()}
$engine=(Get-Process -Id $PID).Path
$nonce=[Guid]::NewGuid().ToString('N');$folderName='MACIP-ManualRunProbe-'+$nonce;$probeTaskName='Probe'
$marker='MACIP disposable Run proof '+$nonce
$root=Join-Path ([IO.Path]::GetTempPath()) ('macip-manual-com-'+$nonce)
New-Item -ItemType Directory $root|Out-Null
$folder=$null;$createdFolder=$false;$createdTask=$false;$report=$null;$primary=$null;$cleanupFailure=$null
function Error-Metadata($Failure){$e=$Failure.Exception;while($e.InnerException){$e=$e.InnerException};return @{type=$e.GetType().FullName;hresult=('0x'+$e.HResult.ToString('X8'))}}
function Assert-Missing($Failure){$e=$Failure.Exception;while($e){if($e.HResult -eq -2147024894){return};$e=$e.InnerException};throw 'Nao foi possivel confirmar ausencia do recurso descartavel.'}
function Variant-Info($Value){
 $pointer=[Runtime.InteropServices.Marshal]::AllocCoTaskMem(32)
 for($i=0;$i -lt 32;$i++){[Runtime.InteropServices.Marshal]::WriteByte($pointer,$i,0)}
 try{
  [Runtime.InteropServices.Marshal]::GetNativeVariantForObject($Value,$pointer)
  return @{managed_type=$Value.GetType().FullName;dotnet_variant=('0x'+([Runtime.InteropServices.Marshal]::ReadInt16($pointer)).ToString('X4'))}
 }finally{[void][MacipRunProbe.Native]::VariantClear($pointer);[Runtime.InteropServices.Marshal]::FreeCoTaskMem($pointer)}
}
function Own-Task {
 $t=$folder.GetTask($probeTaskName)
 $principal=[string]$t.Definition.Principal.UserId
 $ownerSid=if($principal -match '^S-1-'){ $principal }else{ ([Security.Principal.NTAccount]$principal).Translate([Security.Principal.SecurityIdentifier]).Value }
 if($ownerSid -cne $sid -or [int]$t.Definition.Principal.LogonType -ne 3){throw 'Conta da prova mudou; nao alterar.'}
 if($t.Definition.RegistrationInfo.Description -cne $marker -or $t.Definition.Actions.Count -ne 1 -or $t.Definition.Actions.Item(1).Path -ine $engine -or $t.Definition.Actions.Item(1).Arguments -cne $arguments){throw 'Propriedade do recurso de prova mudou; nao alterar.'}
 return $t
}
function Wait-Idle {
 $until=[DateTime]::UtcNow.AddSeconds(20)
 do{$t=Own-Task;if($t.State -notin @(2,4) -and $t.GetInstances(0).Count -eq 0){return};Start-Sleep -Milliseconds 100}while([DateTime]::UtcNow -lt $until)
 throw 'Tarefa descartavel nao terminou no prazo.'
}
function Wait-Event($Before,[int]$Seconds=20,[string]$Trial){
 $until=[DateTime]::UtcNow.AddSeconds($Seconds)
 do{
  $new=@(Get-ChildItem -LiteralPath $state -Filter 'event-*.json'|Where-Object{$Before -cnotcontains $_.Name})
  if($new.Count -gt 1){throw 'Mais de uma execucao para um pedido; prova recusada.'}
  if($new.Count -eq 1){$result=Get-Content -LiteralPath $new[0].FullName -Raw -Encoding UTF8|ConvertFrom-Json;if($Trial -and $result.trial -cne $Trial){throw 'Resultado atrasado de outra tentativa; nao aceitar como prova.'};return $result}
  Start-Sleep -Milliseconds 100
 }while([DateTime]::UtcNow -lt $until)
 throw 'Sem evidencia nova da tarefa descartavel.'
}
try{
 Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
namespace MacipRunProbe { public static class Native {
 [DllImport("oleaut32.dll")] public static extern int VariantClear(IntPtr value);
}}
'@
 $gui=Join-Path $PSScriptRoot '../gui/MacipBackup-Gui.ps1';$agent=Join-Path $PSScriptRoot '../Backup-MacipCliente.ps1'
 . $gui -BaseDir (Join-Path $root 'app') -AgentePath $agent
 $state=Join-Path $root 'state';New-Item -ItemType Directory $state|Out-Null
 $config=Join-Path $root 'fixture.json';[IO.File]::WriteAllText($config,'{"synthetic":true}')
 $receiver=Join-Path $root 'Backup-MacipCliente.ps1'
 [IO.File]::WriteAllText($receiver,@'
param([string]$Modo,[string]$Config,[string]$DiretorioEstado,[string]$Disparo)
$ErrorActionPreference='Stop'
$id=[Security.Principal.WindowsIdentity]::GetCurrent()
try{$sid=$id.User.Value}finally{$id.Dispose()}
$trigger=if($Disparo -ceq 'manual'){'manual'}elseif(-not $Disparo -or $Disparo -ceq '$(Arg0)'){'scheduled'}else{'unexpected'}
$fixture=Get-Content -LiteralPath $Config -Raw -Encoding UTF8|ConvertFrom-Json
$result=@{synthetic=$true;trial=$fixture.trial;execution_id=[Guid]::NewGuid().ToString();sid=$sid;argument=$Disparo;trigger=$trigger;status='sucesso';codigo_restic=0;snapshot=('c'*64);inicio_utc=[DateTime]::UtcNow.ToString('o');fim_utc=[DateTime]::UtcNow.ToString('o')}
$json=$result|ConvertTo-Json
$event=Join-Path $DiretorioEstado ('event-'+$result.execution_id+'.json');$temp=$event+'.tmp'
[IO.File]::WriteAllText($temp,$json,[Text.UTF8Encoding]::new($false));[IO.File]::Move($temp,$event)
$last=Join-Path $DiretorioEstado 'ultimo-resultado.json';$temp=Join-Path $DiretorioEstado ($result.execution_id+'.tmp')
[IO.File]::WriteAllText($temp,$json,[Text.UTF8Encoding]::new($false))
if(Test-Path -LiteralPath $last){[IO.File]::Replace($temp,$last,$null)}else{[IO.File]::Move($temp,$last)}
'@,[Text.UTF8Encoding]::new($true))
 $script:AgentePath=$receiver;$script:PsExeEsperado=$engine;$script:TaskName=$probeTaskName;$script:TaskDescricao=$marker
 [IO.File]::WriteAllText((Join-Path $state 'preparo.json'),(@{sid=$sid}|ConvertTo-Json))
 $svc=New-Object -ComObject Schedule.Service;$svc.Connect();$schedulerRoot=$svc.GetFolder('\')
 try{[void]$svc.GetFolder('\'+$folderName);throw 'Pasta de prova ja existe; recusado.'}catch{Assert-Missing $_}
 [Console]::WriteLine(('Recursos exclusivos: \{0}\{1}; TEMP: {2}' -f $folderName,$probeTaskName,$root))
 $folder=$schedulerRoot.CreateFolder($folderName);$createdFolder=$true
 try{[void]$folder.GetTask($probeTaskName);throw 'Tarefa de prova ja existe; recusado.'}catch{Assert-Missing $_}
 $def=$svc.NewTask(0);$def.RegistrationInfo.Description=$marker
 $def.Principal.UserId=$account;$def.Principal.LogonType=3;$def.Principal.RunLevel=0
 $def.Settings.Enabled=$true;$def.Settings.AllowDemandStart=$true;$def.Settings.ExecutionTimeLimit='PT1M'
 $def.Settings.MultipleInstances=2;$def.Settings.DisallowStartIfOnBatteries=$false;$def.Settings.StopIfGoingOnBatteries=$false
 $action=$def.Actions.Create(0);$action.Path=$engine
 $arguments=Get-TaskActionArgs -Agente $receiver -Config $config -Estado $state;$action.Arguments=$arguments
 [void]$folder.RegisterTaskDefinition($probeTaskName,$def,2,$account,$null,3);$createdTask=$true
 $backend=[pscustomobject]@{Folder=$folder;Name=$probeTaskName;Marker=$marker}
 $backend|Add-Member ScriptMethod Conectar {}
 $backend|Add-Member ScriptMethod Get {param($name)if($name -cne $this.Name){throw 'Consulta fora da prova recusada'};$t=$this.Folder.GetTask($name);if($t.Definition.RegistrationInfo.Description -cne $this.Marker){throw 'Propriedade divergente'};return $t}
 $script:Backend=$backend
 $cases=@()
 foreach($kind in @('object-array','string-array','scalar','production-helper')){
  Wait-Idle;Start-Sleep -Seconds 2
  $current=Own-Task;$updated=$current.Definition
  $config=Join-Path $root ('fixture-'+$kind+'.json');[IO.File]::WriteAllText($config,(@{synthetic=$true;trial=$kind}|ConvertTo-Json))
  $arguments=Get-TaskActionArgs -Agente $receiver -Config $config -Estado $state;$updated.Actions.Item(1).Arguments=$arguments
  [void]$folder.RegisterTaskDefinition($probeTaskName,$updated,4,$account,$null,3)
  $before=@(Get-ChildItem -LiteralPath $state -Filter 'event-*.json'|ForEach-Object{$_.Name})
  $value=switch($kind){'object-array'{,@('manual')};'string-array'{,([string[]]@('manual'))};default{'manual'}}
  $variant=Variant-Info $value;$errorInfo=$null;$instance=$null;$event=$null
  $reference=Get-InfoTarefa;$old=Read-UltimoResultado -Estado $state;$pedido=New-PedidoDisparo -InfoTarefa $reference -Res $old
  try{
   $t=Own-Task
   $instance=switch($kind){'object-array'{$t.Run(@('manual'))};'string-array'{$t.Run([string[]]@('manual'))};'scalar'{$t.Run([string]'manual')};'production-helper'{Start-TarefaAgora}}
  }catch{$errorInfo=Error-Metadata $_}
  if($errorInfo){Wait-Idle;Start-Sleep -Seconds 1;$new=@(Get-ChildItem -LiteralPath $state -Filter 'event-*.json'|Where-Object{$before -cnotcontains $_.Name});if($new.Count){$event=Get-Content $new[0].FullName -Raw|ConvertFrom-Json}}
  else{$event=Wait-Event $before 20 $kind;Wait-Idle}
  $confirmed=$false
  if($event){$confirmed=($event.trial -ceq $kind -and $event.trigger -ceq 'manual' -and $event.sid -ceq $sid -and $event.synthetic -eq $true)}
  $ui=if($event){Get-TelaExecucao -InfoTarefa (Get-InfoTarefa) -Res $event -Pedido $pedido}else{$null}
  $cases+=@([pscustomobject]@{case=$kind;variant=$variant;error=$errorInfo;new_execution=$(if($event){$event.execution_id}else{$null});manual_same_sid=$confirmed;ui_state=$(if($ui){$ui.Classe}else{$null})})
 }
 # Disparo real por horario na MESMA tarefa, sem parametro manual.
 Wait-Idle;$before=@(Get-ChildItem -LiteralPath $state -Filter 'event-*.json'|ForEach-Object{$_.Name})
 $current=Own-Task;$scheduled=$current.Definition
 $config=Join-Path $root 'fixture-scheduled.json';[IO.File]::WriteAllText($config,(@{synthetic=$true;trial='scheduled'}|ConvertTo-Json))
 $arguments=Get-TaskActionArgs -Agente $receiver -Config $config -Estado $state;$scheduled.Actions.Item(1).Arguments=$arguments
 $trigger=$scheduled.Triggers.Create(1);$trigger.StartBoundary=[DateTime]::Now.AddSeconds(10).ToString('yyyy-MM-ddTHH:mm:ss');$trigger.EndBoundary=[DateTime]::Now.AddMinutes(1).ToString('yyyy-MM-ddTHH:mm:ss');$trigger.Enabled=$true
 [void]$folder.RegisterTaskDefinition($probeTaskName,$scheduled,4,$account,$null,3)
 $event=Wait-Event $before 55 'scheduled';Wait-Idle
 $scheduleOk=$event.trial -ceq 'scheduled' -and $event.trigger -ceq 'scheduled' -and $event.sid -ceq $sid
 $allEvents=@(Get-ChildItem -LiteralPath $state -Filter 'event-*.json'|ForEach-Object{Get-Content -LiteralPath $_.FullName -Raw -Encoding UTF8|ConvertFrom-Json})
 foreach($case in $cases){$effects=@($allEvents|Where-Object{$_.trial -ceq $case.case});$case|Add-Member NoteProperty total_effects $effects.Count}
 $scalar=@($cases|Where-Object{$_.case -ceq 'scalar'})[0];$helper=@($cases|Where-Object{$_.case -ceq 'production-helper'})[0];$legacy=@($cases|Where-Object{$_.case -ceq 'object-array'})[0]
 $report=[pscustomobject]@{native_windows=$true;powershell=$PSVersionTable.PSVersion.ToString();synthetic_task='\'+$folderName+'\'+$probeTaskName;cases=$cases;scheduled_same_sid=$scheduleOk;hypothesis=$(if($legacy.error -and $legacy.total_effects -eq 0 -and $scalar.manual_same_sid -and -not $scalar.error){'reproduced-in-this-environment'}else{'not-proven-in-this-environment'});cleanup='pending'}
 if($scalar.error -or $scalar.total_effects -ne 1 -or $helper.total_effects -ne 1 -or -not $scalar.manual_same_sid -or $helper.error -or -not $helper.manual_same_sid -or $helper.ui_state -cne 'concluido' -or -not $scheduleOk){throw 'Gate nativo nao aprovado: confira resultados da tarefa sintetica.'}
}catch{$primary=$_}finally{
 try{
  if($createdTask){$t=Own-Task;if($t.State -in @(2,4)){$t.Stop(0);Wait-Idle};$folder.DeleteTask($probeTaskName,0)}
  if($createdFolder){if($folder.GetTasks(1).Count -ne 0 -or $folder.GetFolders(0).Count -ne 0){throw 'Pasta recebeu outro recurso; nao remover'};$schedulerRoot.DeleteFolder($folderName,0)}
  Remove-Item -LiteralPath $root -Recurse -Force
  if($report){$report.cleanup='completed'}
 }catch{$cleanupFailure=Error-Metadata $_;if(-not $primary){$primary=$_}}
}
if($report){$report|Add-Member NoteProperty cleanup_error $cleanupFailure;$report|ConvertTo-Json -Depth 7}
if($primary){$safe=Error-Metadata $primary;throw ('Gate COM pendente/falhou: '+$safe.type+' '+$safe.hresult+'. Preserve a saida; nenhuma tarefa instalada foi usada.')}
