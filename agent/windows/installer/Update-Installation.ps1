#requires -Version 5.1
[CmdletBinding()]
param([ValidateSet('Preflight','Apply','Rollback','Recover','Verify')][string]$Action='Preflight',
 [string]$InstallRoot,[string]$DataRoot,[string]$Payload,[string]$ManifestHash,[string]$LegacyManifest,[string]$LegacyManifestHash,
 $Service=$null,[scriptblock]$ProcessProbe=$null)
$ErrorActionPreference='Stop'

function Read-ReleaseManifest([string]$Path,[string]$ExpectedHash) {
 if($ExpectedHash -notmatch '^[a-f0-9]{64}$' -or (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant() -cne $ExpectedHash){throw 'Integridade do manifesto recusada.'}
 $m=Get-Content -LiteralPath $Path -Raw -Encoding UTF8|ConvertFrom-Json
 if($m.format -ne 1 -or $m.commit -notmatch '^[a-f0-9]{40}$' -or $m.origin -cne 'Tomas5014/sistema-backup' -or $m.reader_min -ne 1 -or $m.reader_max -ne 1){throw 'Origem/contrato do pacote recusado.'}
 $allowed=@('Backup-MacipCliente.ps1','Central-Alerts.ps1','MacipBackup-Gui.ps1','Activation-Code.ps1','Monitoring-Task.ps1','Update-Installation.ps1','Desinstalar-Tarefa.ps1','restic.exe','perfil.example.json')
 foreach($property in $m.files.PSObject.Properties){
  if($allowed -cnotcontains $property.Name -or $property.Value -notmatch '^[a-f0-9]{64}$'){throw 'Arquivo de pacote nao permitido.'}
 }
 foreach($name in @('Backup-MacipCliente.ps1','MacipBackup-Gui.ps1','restic.exe')){if(-not $m.files.PSObject.Properties[$name]){throw 'Modulo obrigatorio ausente.'}}
 return $m
}
function Test-CodeFiles([string]$Directory,$Manifest) {
 foreach($file in $Manifest.files.PSObject.Properties){
  $path=Join-Path $Directory $file.Name
  $item=Get-Item -LiteralPath $path
  if($item.Attributes -band [IO.FileAttributes]::ReparsePoint){throw 'Link de codigo recusado.'}
  if($path.EndsWith('.ps1',[StringComparison]::OrdinalIgnoreCase)){
   $tokens=$null;$errors=$null;[void][Management.Automation.Language.Parser]::ParseFile($path,[ref]$tokens,[ref]$errors)
   if($errors.Count -gt 0){throw 'Modulo PowerShell invalido.'}
  }
  if((Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant() -cne $file.Value){throw 'Integridade de modulo recusada.'}
 }
}
function Write-UpdateJson([string]$Path,$Value) {
 $temp=$Path+'.'+[Guid]::NewGuid().ToString('N')
 try{[IO.File]::WriteAllText($temp,($Value|ConvertTo-Json -Depth 20),[Text.UTF8Encoding]::new($false));if(Test-Path -LiteralPath $Path){$backup=$temp+'.previous';[IO.File]::Replace($temp,$Path,$backup);[IO.File]::Delete($backup)}else{[IO.File]::Move($temp,$Path)}}finally{if(Test-Path -LiteralPath $temp){Remove-Item -LiteralPath $temp}}
}
function Test-DataReader([string]$Directory,$Manifest) {
 $format=Join-Path $Directory 'format.json';$v=1
 if(Test-Path -LiteralPath $format){$data=Get-Content -LiteralPath $format -Raw|ConvertFrom-Json;$v=$data.version;if($v -isnot [int] -and $v -isnot [long]){throw 'Versao de dados invalida.'}}
 $hasQueue=$false
 if(Test-Path -LiteralPath $Directory){$hasQueue=@(Get-ChildItem -LiteralPath $Directory -Directory -Filter 'monitoring-*').Count -gt 0 -or @(Get-ChildItem -LiteralPath $Directory -File -Filter 'event-*.json').Count -gt 0}
 if($hasQueue -and $Manifest.results_contract -ne 1){throw 'Versao incapaz de ler a fila de resultados atual.'}
 if($v -lt $Manifest.reader_min -or $v -gt $Manifest.reader_max){throw 'Versao incapaz de ler dados/fila atuais; reversao recusada.'}
}
function Get-TaskUpdateSignature([string]$Xml) {
 try{[xml]$document=$Xml;foreach($node in @($document.SelectNodes("//*[local-name()='Settings']/*[local-name()='Enabled']"))){[void]$node.ParentNode.RemoveChild($node)};return $document.OuterXml}catch{throw 'XML de tarefa inconclusivo.'}
}
function Get-OwnedUpdateTasks([string]$Code,[string]$Data,$Svc,[string]$Helper) {
 if(-not $Svc){$Svc=New-Object -ComObject Schedule.Service;$Svc.Connect()}
 $tasks=@()
 foreach($name in @('MACIP-Backup','MACIP-Backup-Sync')){
  $monitor=$name -ceq 'MACIP-Backup-Sync'
  $description=if($monitor){'MACIP Backup - envio central do piloto'}else{'MACIP Backup - agente de backup do piloto'}
  $entry=& {
   param($helper,$name,$description,$code,$data,$svc,$monitor)
   . $helper -Nome $name -DescricaoEsperada $description -PrefixoAcao $code -Svc $svc -Monitoramento:$monitor -SomenteVerificar
   $info=Get-TarefaInfo -Svc $svc
   if($info.Status -eq 'ausente'){return $null}
   if($info.Status -ne 'encontrada' -or -not(Test-Propriedade $info.Tarefa)){throw 'Tarefa alheia/inconclusiva; instalacao preservada.'}
   $t=$info.Tarefa
   if((Get-InstanciasAtivas $t) -ne 0 -or [int]$t.State -notin @(1,3)){throw 'Tarefa ativa/pendente; aguarde, nao sera encerrada.'}
   $arguments=[string]$t.Definition.Actions.Item(1).Arguments
   if($arguments -notmatch '-Config "([^"]+)"'){throw 'Referencia de configuracao inconclusiva.'}
   $config=[IO.Path]::GetFullPath($Matches[1]);$prefix=[IO.Path]::GetFullPath($data).TrimEnd('\','/')+[IO.Path]::DirectorySeparatorChar
   if(-not $config.StartsWith($prefix,[StringComparison]::OrdinalIgnoreCase) -or -not(Test-Path -LiteralPath $config -PathType Leaf)){throw 'Configuracao fora dos dados persistentes.'}
   return [pscustomobject]@{Name=$name;Task=$t;Enabled=[bool]$t.Enabled;Xml=[string]$t.Xml}
  } $Helper $name $description $Code $Data $Svc $monitor
  if($entry){$tasks+=,$entry}
 }
 return ,$tasks
}
function Assert-NoCodeProcesses([string]$Code,[scriptblock]$Probe) {
 if($Probe){if(& $Probe $Code){throw 'Arquivos/processos em uso; feche o aplicativo e aguarde.'};return}
 if($env:OS -ne 'Windows_NT'){throw 'Consulta real de processos Windows indisponivel.'}
 $prefix=[IO.Path]::GetFullPath($Code).TrimEnd('\')+'\'
 foreach($process in Get-CimInstance Win32_Process -ErrorAction Stop){
  if([int]$process.ProcessId -eq $PID){continue}
  if($process.ExecutablePath -and ([string]$process.ExecutablePath).StartsWith($prefix,[StringComparison]::OrdinalIgnoreCase)){throw 'Executavel em uso; aguarde.'}
  if($process.CommandLine -and [string]$process.CommandLine -match '(?i)-File\s+"([^"]+)"'){
   if(([string]$Matches[1]).StartsWith($prefix,[StringComparison]::OrdinalIgnoreCase)){throw 'Aplicativo/agente em uso; feche e aguarde.'}
  }
 }
}
function Get-DataShape([string]$Directory) {
 # Metadados apenas; nao segue junctions nem le segredos.
 if(-not(Test-Path -LiteralPath $Directory)){return ''}
 $pending=New-Object 'Collections.Generic.Queue[string]';$pending.Enqueue($Directory);$parts=@()
 while($pending.Count -gt 0){
  foreach($item in Get-ChildItem -LiteralPath $pending.Dequeue() -Force -ErrorAction Stop){
   if($item.Attributes -band [IO.FileAttributes]::ReparsePoint){throw 'Link nos dados persistentes recusado.'}
   if($item.PSIsContainer){$pending.Enqueue($item.FullName)}else{$parts+=,$item.FullName+'|'+$item.Length+'|'+$item.LastWriteTimeUtc.Ticks}
  }
 }
 return (($parts|Sort-Object) -join "`n")
}
function Set-InstalledUpdateVersion([string]$Code,$Manifest,[switch]$OnlyCheck) {
 if($env:OS -ne 'Windows_NT'){return}
 $key='HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\MACIP-Backup'
 if(Test-Path -LiteralPath $key){
  $registered=Get-ItemProperty -LiteralPath $key -ErrorAction Stop
  if(([IO.Path]::GetFullPath($registered.InstallLocation)).TrimEnd('\') -ine $Code.TrimEnd('\')){throw 'Registro pertence a outra instalacao.'}
  if(-not $OnlyCheck){Set-ItemProperty -LiteralPath $key -Name DisplayVersion -Value $Manifest.version;Set-ItemProperty -LiteralPath $key -Name DisplayName -Value ('MAC IP Backup '+$Manifest.version)}
 }
}
function Move-CodeDirectory([string]$From,[string]$To){[IO.Directory]::Move($From,$To)}
function Invoke-CodeUpdate {
 param([string]$Mode,[string]$Code,[string]$Data,[string]$Package,[string]$Hash,[string]$Legacy,$Svc,[scriptblock]$Probe,[string]$LegacyHash)
 $Code=[IO.Path]::GetFullPath($Code);$Data=[IO.Path]::GetFullPath($Data)
 if($Data -eq $Code -or $Data.StartsWith($Code+[IO.Path]::DirectorySeparatorChar,[StringComparison]::OrdinalIgnoreCase) -or $Code.StartsWith($Data+[IO.Path]::DirectorySeparatorChar,[StringComparison]::OrdinalIgnoreCase)){throw 'Codigo e dados precisam ser separados.'}
 $work=Join-Path ([IO.Path]::GetDirectoryName($Code)) '.MACIP-Backup-update'
 if(-not(Test-Path -LiteralPath $work)){New-Item -ItemType Directory -Path $work|Out-Null}
 $lock=[IO.File]::Open((Join-Path $work 'update.lock'),'OpenOrCreate','ReadWrite','None')
 $receiptPath=Join-Path $work 'current.json';$journalPath=Join-Path $work 'transaction.json';$handles=@();$tasks=@();$record=$null;$swapped=$false;$oldMoved=$false
 try{
  if($Mode -in @('Rollback','Recover')){
   $prior=Get-Content -LiteralPath $journalPath -Raw|ConvertFrom-Json
   if(-not $prior.previous -or -not(Test-Path -LiteralPath $prior.previous)){throw 'Codigo anterior indisponivel; arquivos preservados.'}
   if(-not ([IO.Path]::GetFullPath($prior.previous)).StartsWith($work+[IO.Path]::DirectorySeparatorChar,[StringComparison]::OrdinalIgnoreCase)){throw 'Caminho de recuperacao divergente.'}
   $Package=$prior.previous;$Hash=$prior.previous_manifest_hash
  }
  $manifestPath=Join-Path $Package 'MANIFEST.json';$m=Read-ReleaseManifest $manifestPath $Hash
  Test-CodeFiles $Package $m;Test-DataReader $Data $m
  Set-InstalledUpdateVersion $Code $m -OnlyCheck
  $helper=Join-Path $Package 'Desinstalar-Tarefa.ps1'
  $tasks=Get-OwnedUpdateTasks $Code $Data $Svc $helper
  if(@($tasks|Where-Object{$_.Name -ceq 'MACIP-Backup-Sync'}).Count -gt 0 -and $m.results_contract -ne 1){throw 'Versao incapaz de executar a sincronizacao existente.'}
  Assert-NoCodeProcesses $Code $Probe
  $before=Get-DataShape $Data
  $current=$null
  if(Test-Path -LiteralPath $Code){
   $installed=Join-Path $Code 'MANIFEST.json'
   if(-not(Test-Path -LiteralPath $installed)){
    if(-not $Legacy -or -not(Test-Path -LiteralPath $Legacy)){throw 'Instalacao sem manifesto: requer referencia legada aprovada.'}
    $installed=$Legacy
   }
   $previousHash=$LegacyHash
   if(Test-Path -LiteralPath $receiptPath){$receipt=Get-Content -LiteralPath $receiptPath -Raw|ConvertFrom-Json;if($receipt.install_root -cne $Code -or $receipt.data_root -cne $Data){throw 'Identidade dos diretorios instalados divergente.'};$previousHash=$receipt.manifest_hash}
   $current=Read-ReleaseManifest $installed $previousHash
   Test-CodeFiles $Code $current
  }
  if($Mode -eq 'Verify' -and (-not $current -or $previousHash -cne $Hash)){throw 'Versao instalada difere do pacote escolhido.'}
  if($Mode -in @('Preflight','Verify')){return [pscustomobject]@{state='verified';commit=$m.commit;tasks=$tasks.Count;data_version=1}}
  if($current -and $previousHash -ceq $Hash){return [pscustomobject]@{state='updated';commit=$m.commit;already_current=$true;recovery=$work}}
  $id=[Guid]::NewGuid().ToString('N');$next=Join-Path $work ('next-'+$id);$previous=Join-Path $work ('previous-'+$id)
  New-Item -ItemType Directory -Path $next|Out-Null
  foreach($file in $m.files.PSObject.Properties){Copy-Item -LiteralPath (Join-Path $Package $file.Name) -Destination (Join-Path $next $file.Name)}
  Copy-Item -LiteralPath $manifestPath -Destination (Join-Path $next 'MANIFEST.json')
  Test-CodeFiles $next $m
  $uninstaller=Join-Path $Code 'desinstalar.exe'
  $uninstallerHash=$null
  if(Test-Path -LiteralPath $uninstaller){$uninstallerHash=(Get-FileHash -LiteralPath $uninstaller -Algorithm SHA256).Hash.ToLowerInvariant()}
  $desiredUninstaller=if($Mode -in @('Rollback','Recover')){Join-Path $Package 'desinstalar.exe'}else{$uninstaller}
  if(Test-Path -LiteralPath $desiredUninstaller){
   if($Mode -in @('Rollback','Recover') -and (Get-FileHash -LiteralPath $desiredUninstaller -Algorithm SHA256).Hash.ToLowerInvariant() -cne $prior.previous_uninstaller_hash){throw 'Integridade do desinstalador anterior divergente.'}
   Copy-Item -LiteralPath $desiredUninstaller -Destination (Join-Path $next 'desinstalar.exe')
  }
  $saved=@($tasks|ForEach-Object{[pscustomobject]@{name=$_.Name;enabled=$_.Enabled;xml=$_.Xml}})
  $record=@{state='prepared';previous=$previous;previous_manifest_hash=$previousHash;previous_uninstaller_hash=$uninstallerHash;next=$next;commit=$m.commit;tasks=$saved}
  Write-UpdateJson $journalPath $record
  foreach($task in $tasks){$task.Task.Enabled=$false}
  $fresh=Get-OwnedUpdateTasks $Code $Data $Svc $helper
  foreach($task in $fresh){
   $original=@($tasks|Where-Object{$_.Name -ceq $task.Name})
   if($original.Count -ne 1 -or $task.Task.Enabled -or (Get-TaskUpdateSignature $task.Xml) -cne (Get-TaskUpdateSignature $original[0].Xml)){throw 'Tarefa mudou durante atualizacao; preservar arquivos.'}
  }
  Assert-NoCodeProcesses $Code $Probe
  # FILE_SHARE_DELETE permite rename enquanto nega novas leituras/escritas
  # desses arquivos. Um handle preexistente incompatível causa recusa.
  foreach($directory in @($Code,$next)){
   if(Test-Path -LiteralPath $directory){foreach($file in Get-ChildItem -LiteralPath $directory -File){
    $handles+=,[IO.File]::Open($file.FullName,'Open','ReadWrite',[IO.FileShare]::Delete)
   }}
  }
  if($current -and -not(Test-Path -LiteralPath (Join-Path $Code 'MANIFEST.json'))){
   # O manifesto legado fica no backup de codigo; nao nos dados DPAPI.
   Copy-Item -LiteralPath $Legacy -Destination (Join-Path $Code 'MANIFEST.json')
  }
  if(Test-Path -LiteralPath $Code){Move-CodeDirectory $Code $previous;$oldMoved=$true}
  Move-CodeDirectory $next $Code;$swapped=$true
  if((Get-DataShape $Data) -cne $before){throw 'Dados mudaram durante a janela; confira antes de retomar.'}
  Set-InstalledUpdateVersion $Code $m
  Write-UpdateJson $receiptPath @{manifest_hash=$Hash;commit=$m.commit;install_root=$Code;data_root=$Data}
  $controller=Join-Path $work 'Update-Installation.ps1'
  if(-not(Test-Path -LiteralPath $controller)){Copy-Item -LiteralPath (Join-Path $Package 'Update-Installation.ps1') -Destination $controller}
  $record.state='verified';Write-UpdateJson $journalPath $record
  return [pscustomobject]@{state='updated';commit=$m.commit;recovery=$work}
 }catch{
  if($swapped){try{Move-CodeDirectory $Code $next;$swapped=$false}catch{}}
  if($oldMoved -and -not(Test-Path -LiteralPath $Code)){try{Move-CodeDirectory $previous $Code;$oldMoved=$false}catch{}}
  if($record){$record.state='failed';Write-UpdateJson $journalPath $record}
  throw
 }finally{
  foreach($handle in $handles){$handle.Dispose()}
  $resumeFailed=$false
  foreach($entry in $tasks){
   try{
    $fresh=Get-OwnedUpdateTasks $Code $Data $Svc $helper
    $same=@($fresh|Where-Object{$_.Name -ceq $entry.Name -and (Get-TaskUpdateSignature $_.Xml) -ceq (Get-TaskUpdateSignature $entry.Xml)})
    if($same.Count -eq 1 -and (Test-Path -LiteralPath (Join-Path $Code 'Backup-MacipCliente.ps1'))){$same[0].Task.Enabled=$entry.Enabled;if([bool]$same[0].Task.Enabled -ne $entry.Enabled){throw 'Estado da agenda nao confirmado.'}}else{throw 'Propriedade da tarefa mudou.'}
   }catch{$resumeFailed=$true;Write-Warning 'Agenda preservada desabilitada: confira a recuperacao antes de retomar.'}
  }
  $lock.Dispose()
  if($resumeFailed){if($record){$record.state='needs-recovery';Write-UpdateJson $journalPath $record};throw 'Agenda nao foi confirmada; codigo preservado, consulte recuperacao.'}
 }
}
if($MyInvocation.InvocationName -ne '.'){
 try{Invoke-CodeUpdate $Action $InstallRoot $DataRoot $Payload $ManifestHash $LegacyManifest $Service $ProcessProbe $LegacyManifestHash|ConvertTo-Json -Compress;exit 0}
 catch{[Console]::Error.WriteLine('Atualizacao recusada/incompleta. Instalacao e dados preservados; consulte transaction.json na area protegida.');exit 1}
}
