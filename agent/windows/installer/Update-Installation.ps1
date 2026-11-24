#requires -Version 5.1
[CmdletBinding()]
param([ValidateSet('Preflight','Apply','Rollback','Recover','Verify')][string]$Action='Preflight',
 [string]$InstallRoot,[string]$DataRoot,[string]$Payload,[string]$ManifestHash,[string]$LegacyManifest,[string]$LegacyManifestHash,
 $Service=$null,[scriptblock]$ProcessProbe=$null)
$ErrorActionPreference='Stop'
$script:UpdateControllerSource=$PSCommandPath

function ConvertTo-ReleaseHash($Value) {
 if($Value -isnot [string] -or $Value.Length -ne 64 -or $Value -cnotmatch '^[a-fA-F0-9]{64}$'){throw 'Formato SHA-256 invalido.'}
 return $Value.ToLowerInvariant()
}
function Get-ReleaseFiles {
 # Contrato unico do gerador e leitor. NSIS deve instalar/remover os mesmos nomes.
 return [ordered]@{
  'Backup-MacipCliente.ps1'='Backup-MacipCliente.ps1'
  'Central-Alerts.ps1'='Central-Alerts.ps1'
  'MacipBackup-Gui.ps1'='gui/MacipBackup-Gui.ps1'
  'Activation-Code.ps1'='gui/Activation-Code.ps1'
  'Assisted-Recovery.ps1'='gui/Assisted-Recovery.ps1'
  'Monitoring-Task.ps1'='gui/Monitoring-Task.ps1'
  'Update-Installation.ps1'='installer/Update-Installation.ps1'
  'Desinstalar-Tarefa.ps1'='installer/Desinstalar-Tarefa.ps1'
  'perfil.example.json'='gui/perfil.example.json'
  'restic.exe'=$null
 }
}
function Get-UpdateFailureInfo($Failure) {
 $cachedException=$Failure.Exception
 while($cachedException){
  $cached=$cachedException.Data['MACIP.UpdateFailure']
  if($cached -and $cached.attempt_id -ceq $script:UpdateAttemptId){return $cached}
  $cachedException=$cachedException.InnerException
 }
 # Nunca devolve Exception.Message, caminhos de entrada ou dados do pacote.
 $code='unconfirmed';$cause='Nao foi possivel confirmar a atualizacao.';$action='Confira acesso ao pacote e aos diretorios protegidos; preserve os arquivos e solicite suporte.'
 switch -Regex ($Failure.Exception.Message) {
  '^Formato SHA-256' {$code='hash-format';$cause='SHA-256 invalido.';$action='Use exatamente 64 caracteres hexadecimais do artefato aprovado.';break}
  '^Integridade do manifesto' {$code='manifest-integrity';$cause='O manifesto nao corresponde ao SHA-256 aprovado.';$action='Confira o hash informado e obtenha novamente o mesmo pacote aprovado.';break}
  '^Arquivo de pacote' {$code='package-file';$cause='Manifesto contem arquivo nao permitido.';$action='Recompile pelo gerador e NSIS da mesma versao aprovada, sem arquivos adicionais.';break}
  '^Modulo obrigatorio|^Modulo PowerShell|^Integridade de modulo|^Origem/contrato' {$code='package-integrity';$cause='Contrato ou conteudo do pacote recusado.';$action='Confira commit, bytes Git e hashes; nao gere referencia a partir da instalacao desconhecida.';break}
  '^Instalacao sem manifesto' {$code='legacy-reference';$cause='Falta a referencia legada aprovada.';$action='Recompile com LEGACY-MANIFEST.json e seu SHA-256, gerados do commit e Restic originais aprovados.';break}
  '^Tarefa ativa|^Arquivos/processos|^Executavel em uso|^Aplicativo/agente em uso' {$code='in-use';$cause='Aplicativo ou tarefa ainda em uso.';$action='Feche a interface, aguarde as tarefas terminarem e repita; nao force o encerramento.';break}
  '^Tarefa alheia|^Tarefa mudou|^Propriedade da tarefa|^XML de tarefa|^Referencia de configuracao|^Configuracao fora' {$code='task-identity';$cause='Propriedade ou configuracao da tarefa nao confirmada.';$action='Preserve a tarefa e confira sua instalacao com o administrador.';break}
  '^Versao incapaz|^Versao de dados|^Versao instalada' {$code='incompatible';$cause='Versao, dados ou fila incompativeis.';$action='Use um pacote compativel; nao altere dados ou numeros de versao para forcar a troca.';break}
  '^Agenda nao foi confirmada' {$code='schedule-unconfirmed';$cause='Retomada da agenda nao confirmada.';$action='Preserve os arquivos e confira a recuperacao antes de retomar a agenda.';break}
  '^Codigo anterior indisponivel|^Caminho de recuperacao|^Integridade do desinstalador' {$code='recovery-reference';$cause='Referencia de recuperacao indisponivel ou divergente.';$action='Preserve as pastas de codigo e o journal; solicite recuperacao assistida.';break}
  '^Codigo e dados|^Identidade dos diretorios|^Registro pertence|^Link de codigo|^Link nos dados|^Dados mudaram' {$code='layout-unconfirmed';$cause='Layout ou estado da instalacao nao confirmado.';$action='Preserve codigo e dados; confira diretorios, links e operacoes em andamento com o administrador.';break}
 }
 if($code -eq 'unconfirmed' -and $Failure.Exception -is [IO.IOException]){$code='file-access';$cause='Arquivo ausente, bloqueado ou acesso inconclusivo.';$action='Confira o pacote, permissoes e arquivos em uso; nao apague dados ou tarefas.'}
 $e=$Failure.Exception;while($e.InnerException){$e=$e.InnerException}
 if($code -eq 'unconfirmed' -and ($e -is [IO.IOException] -or $e -is [UnauthorizedAccessException])){$code='file-access';$cause='Acesso ao arquivo ou diretorio recusado.';$action='Feche o aplicativo e aguarde trabalhos; preserve as pastas e confira o diagnostico.'}
 $stage=$script:UpdateStage
 if($stage -notin @('preflight','update-lock','package','tasks','active-code','stage-next','archive-journal','pause-tasks','probe-access','move-active','publish-next','verify-active','controller','receipt','journal','resume-tasks','dispose-lock','recover-unpublish','recover-previous','recover-receipt','recover-version')){$stage='preflight'}
 if($stage -in @('move-active','publish-next','recover-unpublish','recover-previous')){$code='directory-rename';$cause='Troca de diretorios nao concluida.';$action='Preserve codigo, next e journal; aguarde arquivos em uso e use o procedimento de retomada.'}
 if($stage -eq 'update-lock'){$code='update-busy';$cause='Exclusao entre atualizadores nao obtida.';$action='Aguarde a outra tentativa terminar; nao remova o arquivo de lock.'}
 if($Failure.Exception.Message -like 'Recuperacao anterior pendente*'){$code='recovery-required';$cause='A tentativa anterior requer recuperacao antes de nova troca.';$action='Preserve as pastas e use o controlador corrigido com o journal existente; nao reinstale sobre codigo ausente.'}
 $type=$e.GetType().FullName;if($type -cnotmatch '^System\.[A-Za-z0-9_.]+$'){$type='System.Exception'}
 return [pscustomobject]@{code=$code;cause=$cause;action=$action;stage=$stage;exception_type=$type;hresult=('0x'+$e.HResult.ToString('X8'));attempt_id=$script:UpdateAttemptId}
}
function Save-UpdateFailure([string]$Path,$Record,$Failure) {
 if(-not $Record){return}
 try {
  $Record.attempt_id=$script:UpdateAttemptId;$Record.failure=Get-UpdateFailureInfo $Failure
  Write-UpdateJson $Path $Record
  $saved=Get-Content -LiteralPath $Path -Raw -Encoding UTF8|ConvertFrom-Json
  if($saved.attempt_id -ceq $script:UpdateAttemptId -and $saved.failure.code -ceq $Record.failure.code){$script:UpdateDiagnosticPath=$Path}
 } catch {} # O diagnostico secundario nao substitui a falha principal.
}
function Get-CurrentUpdateDiagnostic($Failure) {
 if(-not $script:UpdateDiagnosticPath){return $null}
 try {
  $saved=Get-Content -LiteralPath $script:UpdateDiagnosticPath -Raw -Encoding UTF8|ConvertFrom-Json
  $safe=Get-UpdateFailureInfo $Failure
  if($saved.attempt_id -ceq $script:UpdateAttemptId -and $saved.failure.code -ceq $safe.code){return $script:UpdateDiagnosticPath}
 } catch {}
 return $null
}
function Read-ReleaseManifest([string]$Path,[string]$ExpectedHash) {
 $ExpectedHash=ConvertTo-ReleaseHash $ExpectedHash
 if((Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant() -cne $ExpectedHash){throw 'Integridade do manifesto recusada.'}
 $m=Get-Content -LiteralPath $Path -Raw -Encoding UTF8|ConvertFrom-Json
 if($m.format -ne 1 -or $m.commit -notmatch '^[a-f0-9]{40}$' -or $m.origin -cne 'Tomas5014/sistema-backup' -or $m.reader_min -ne 1 -or $m.reader_max -ne 1){throw 'Origem/contrato do pacote recusado.'}
 $allowed=@((Get-ReleaseFiles).Keys)
 foreach($property in $m.files.PSObject.Properties){
  if($allowed -cnotcontains $property.Name){throw 'Arquivo de pacote nao permitido.'}
  $property.Value=ConvertTo-ReleaseHash $property.Value
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
  if((Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant() -cne (ConvertTo-ReleaseHash $file.Value)){throw 'Integridade de modulo recusada.'}
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
   $parameters=@{Nome=$name;DescricaoEsperada=$description;PrefixoAcao=$code;Svc=$svc;SomenteVerificar=$true}
   $supportsMonitoring=(Get-Command $helper -ErrorAction Stop).Parameters.ContainsKey('Monitoramento')
   if($supportsMonitoring){$parameters.Monitoramento=$monitor}
   . $helper @parameters
   $info=Get-TarefaInfo -Svc $svc
   if($info.Status -eq 'ausente'){return $null}
   if($monitor -and -not $supportsMonitoring){throw 'Versao incapaz de executar a sincronizacao existente.'}
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
function Test-CodeFileAccess([string[]]$Directories) {
 # Sondagem exclusiva preservada. Nenhum handle filho atravessa Directory.Move.
 $handles=@();$failure=$null;$disposeFailure=$null
 try {
  foreach($directory in $Directories){
   if(Test-Path -LiteralPath $directory){foreach($file in Get-ChildItem -LiteralPath $directory -File){
    $handles+=,[IO.File]::Open($file.FullName,'Open','ReadWrite',[IO.FileShare]::None)
   }}
  }
 }catch{$failure=$_;throw}finally{
  foreach($handle in $handles){try{$handle.Dispose()}catch{if(-not $disposeFailure){$disposeFailure=$_}}}
  if($disposeFailure -and -not $failure){throw $disposeFailure}
 }
}
function Archive-UpdateJournal([string]$Work) {
 $journal=Join-Path $Work 'transaction.json'
 if(-not(Test-Path -LiteralPath $journal)){return $null}
 if((Get-Item -LiteralPath $journal).Attributes -band [IO.FileAttributes]::ReparsePoint){throw 'Caminho de recuperacao divergente.'}
 $history=Join-Path $Work 'history'
 if(-not(Test-Path -LiteralPath $history)){New-Item -ItemType Directory $history|Out-Null}
 if((Get-Item -LiteralPath $history).Attributes -band [IO.FileAttributes]::ReparsePoint){throw 'Caminho de recuperacao divergente.'}
 $archive=Join-Path $history ('journal-'+[Guid]::NewGuid().ToString('N')+'.json')
 [IO.File]::Copy($journal,$archive,$false)
 if((Get-FileHash $journal -Algorithm SHA256).Hash -cne (Get-FileHash $archive -Algorithm SHA256).Hash){throw 'Caminho de recuperacao divergente.'}
 return $archive
}
function Install-UpdateController([string]$Work) {
 $target=Join-Path $Work 'Update-Installation.ps1'
 if(Test-Path -LiteralPath $target){if((Get-FileHash $target -Algorithm SHA256).Hash -ceq (Get-FileHash $script:UpdateControllerSource -Algorithm SHA256).Hash){return}}
 $temp=Join-Path $Work ('controller-'+[Guid]::NewGuid().ToString('N')+'.ps1')
 [IO.File]::Copy($script:UpdateControllerSource,$temp,$false)
 if(Test-Path -LiteralPath $target){[IO.File]::Replace($temp,$target,(Join-Path $Work ('controller-previous-'+[Guid]::NewGuid().ToString('N')+'.ps1')))}else{[IO.File]::Move($temp,$target)}
}
function Add-UpdateRecoveryFailure($Record,$Failure,[string]$Stage) {
 $script:UpdateStage=$Stage
 $safe=Get-UpdateFailureInfo $Failure
 $Record.recovery_errors=@($Record.recovery_errors)+@($safe)
}
function Move-CodeDirectory([string]$From,[string]$To){[IO.Directory]::Move($From,$To)}
function Invoke-CodeUpdate {
 param([string]$Mode,[string]$Code,[string]$Data,[string]$Package,[string]$Hash,[string]$Legacy,$Svc,[scriptblock]$Probe,[string]$LegacyHash)
 $script:UpdateAttemptId=[Guid]::NewGuid().ToString('N');$script:UpdateDiagnosticPath=$null;$script:UpdateStage='preflight'
 $Code=[IO.Path]::GetFullPath($Code);$Data=[IO.Path]::GetFullPath($Data)
 if($Data -eq $Code -or $Data.StartsWith($Code+[IO.Path]::DirectorySeparatorChar,[StringComparison]::OrdinalIgnoreCase) -or $Code.StartsWith($Data+[IO.Path]::DirectorySeparatorChar,[StringComparison]::OrdinalIgnoreCase)){throw 'Codigo e dados precisam ser separados.'}
 $work=Join-Path ([IO.Path]::GetDirectoryName($Code)) '.MACIP-Backup-update'
 if(-not(Test-Path -LiteralPath $work)){New-Item -ItemType Directory -Path $work|Out-Null}
 $script:UpdateStage='update-lock'
 $lock=[IO.File]::Open((Join-Path $work 'update.lock'),'OpenOrCreate','ReadWrite','None')
 $receiptPath=Join-Path $work 'current.json';$journalPath=Join-Path $work 'transaction.json';$tasks=@();$record=$null;$swapped=$false;$oldMoved=$false;$primaryError=$null;$oldReceipt=$null;$receiptChanged=$false;$versionChanged=$false;$current=$null;$previousHash=$null
 try{
  $script:UpdateStage='package'
  $priorJournal=$null
  if(Test-Path -LiteralPath $journalPath){$priorJournal=Get-Content -LiteralPath $journalPath -Raw -Encoding UTF8|ConvertFrom-Json}
  if($Mode -notin @('Rollback','Recover') -and $priorJournal -and ($priorJournal.state -eq 'needs-recovery' -or ($priorJournal.state -in @('failed','prepared') -and -not(Test-Path -LiteralPath $Code)))){throw 'Recuperacao anterior pendente; nao instalar sobre codigo ausente.'}
  if($Mode -in @('Rollback','Recover')){
   $prior=Get-Content -LiteralPath $journalPath -Raw|ConvertFrom-Json
   if(-not $prior.previous -or -not(Test-Path -LiteralPath $prior.previous)){throw 'Codigo anterior indisponivel; arquivos preservados.'}
   if(-not ([IO.Path]::GetFullPath($prior.previous)).StartsWith($work+[IO.Path]::DirectorySeparatorChar,[StringComparison]::OrdinalIgnoreCase)){throw 'Caminho de recuperacao divergente.'}
   $Package=$prior.previous;$Hash=$prior.previous_manifest_hash
  }
  $Hash=ConvertTo-ReleaseHash $Hash
  $manifestPath=Join-Path $Package 'MANIFEST.json';$m=Read-ReleaseManifest $manifestPath $Hash
  Test-CodeFiles $Package $m;Test-DataReader $Data $m
  Set-InstalledUpdateVersion $Code $m -OnlyCheck
  $helper=Join-Path $Package 'Desinstalar-Tarefa.ps1'
  $script:UpdateStage='tasks'
  $tasks=Get-OwnedUpdateTasks $Code $Data $Svc $helper
  if($Mode -eq 'Recover' -and $prior -and $prior.state -in @('failed','prepared','needs-recovery')){
   if(@($prior.tasks).Count -ne $tasks.Count){throw 'Tarefa mudou durante atualizacao; preservar arquivos.'}
   foreach($task in $tasks){
    $savedTask=@($prior.tasks|Where-Object{$_.name -ceq $task.Name})
    if($savedTask.Count -ne 1 -or (Get-TaskUpdateSignature $savedTask[0].xml) -cne (Get-TaskUpdateSignature $task.Xml)){throw 'Tarefa mudou durante atualizacao; preservar arquivos.'}
    $task.Enabled=[bool]$savedTask[0].enabled
   }
  }
  if(@($tasks|Where-Object{$_.Name -ceq 'MACIP-Backup-Sync'}).Count -gt 0 -and $m.results_contract -ne 1){throw 'Versao incapaz de executar a sincronizacao existente.'}
  Assert-NoCodeProcesses $Code $Probe
  $before=Get-DataShape $Data
  $script:UpdateStage='active-code'
  $current=$null
  if(Test-Path -LiteralPath $Code){
   $installed=Join-Path $Code 'MANIFEST.json'
   if(-not(Test-Path -LiteralPath $installed)){
    if(-not $Legacy -or -not(Test-Path -LiteralPath $Legacy)){throw 'Instalacao sem manifesto: requer referencia legada aprovada.'}
    $installed=$Legacy
   }
   $previousHash=$LegacyHash
   if(Test-Path -LiteralPath $receiptPath){$receipt=Get-Content -LiteralPath $receiptPath -Raw|ConvertFrom-Json;if($receipt.install_root -cne $Code -or $receipt.data_root -cne $Data){throw 'Identidade dos diretorios instalados divergente.'};$previousHash=$receipt.manifest_hash;$oldReceipt=$receipt}
   $previousHash=ConvertTo-ReleaseHash $previousHash
   $current=Read-ReleaseManifest $installed $previousHash
   Test-CodeFiles $Code $current
  }
  if($Mode -eq 'Verify' -and (-not $current -or $previousHash -cne $Hash)){throw 'Versao instalada difere do pacote escolhido.'}
  if($Mode -in @('Preflight','Verify')){return [pscustomobject]@{state='verified';commit=$m.commit;tasks=$tasks.Count;data_version=1}}
  if($current -and $previousHash -ceq $Hash){return [pscustomobject]@{state='updated';commit=$m.commit;already_current=$true;recovery=$work}}
  $script:UpdateStage='stage-next'
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
   if($Mode -in @('Rollback','Recover') -and (Get-FileHash -LiteralPath $desiredUninstaller -Algorithm SHA256).Hash.ToLowerInvariant() -cne (ConvertTo-ReleaseHash $prior.previous_uninstaller_hash)){throw 'Integridade do desinstalador anterior divergente.'}
   Copy-Item -LiteralPath $desiredUninstaller -Destination (Join-Path $next 'desinstalar.exe')
  }
  $saved=@($tasks|ForEach-Object{[pscustomobject]@{name=$_.Name;enabled=$_.Enabled;xml=$_.Xml}})
  # Nunca reutiliza/apaga next-* de uma tentativa anterior. Guarda o journal
  # byte a byte antes de publicar a nova transacao, sob o mesmo lock.
  $script:UpdateStage='archive-journal';$archived=Archive-UpdateJournal $work
  $record=@{attempt_id=$script:UpdateAttemptId;state='prepared';stage='pause-tasks';previous=$previous;previous_manifest_hash=$previousHash;previous_uninstaller_hash=$uninstallerHash;previous_receipt=$oldReceipt;next=$next;commit=$m.commit;tasks=$saved;prior_journal=$archived;recovery_errors=@()}
  Write-UpdateJson $journalPath $record
  $script:UpdateStage='pause-tasks'
  foreach($task in $tasks){$task.Task.Enabled=$false}
  $fresh=Get-OwnedUpdateTasks $Code $Data $Svc $helper
  if($fresh.Count -ne $tasks.Count){throw 'Tarefa mudou durante atualizacao; preservar arquivos.'}
  foreach($task in $fresh){
   $original=@($tasks|Where-Object{$_.Name -ceq $task.Name})
   if($original.Count -ne 1 -or $task.Task.Enabled -or (Get-TaskUpdateSignature $task.Xml) -cne (Get-TaskUpdateSignature $original[0].Xml)){throw 'Tarefa mudou durante atualizacao; preservar arquivos.'}
  }
  Assert-NoCodeProcesses $Code $Probe
  $script:UpdateStage='probe-access'
  Test-CodeFileAccess @($Code,$next)
  # As sondagens ja foram fechadas. A exclusao do atualizador permanece fora
  # das pastas; tarefas seguem pausadas. Reconfere integridade antes/depois.
  Test-CodeFiles $next $m
  if($current){Test-CodeFiles $Code $current}
  Assert-NoCodeProcesses $Code $Probe
  if($current -and -not(Test-Path -LiteralPath (Join-Path $Code 'MANIFEST.json'))){
   # O manifesto legado fica no backup de codigo; nao nos dados DPAPI.
   Copy-Item -LiteralPath $Legacy -Destination (Join-Path $Code 'MANIFEST.json')
  }
  if(Test-Path -LiteralPath $Code){
   $script:UpdateStage='move-active';$record.stage=$script:UpdateStage;Write-UpdateJson $journalPath $record
   Move-CodeDirectory $Code $previous;$oldMoved=$true
  }
  $script:UpdateStage='publish-next';$record.stage=$script:UpdateStage;Write-UpdateJson $journalPath $record
  Move-CodeDirectory $next $Code;$swapped=$true
  $script:UpdateStage='verify-active';$record.stage=$script:UpdateStage
  Test-CodeFiles $Code $m
  if((Get-DataShape $Data) -cne $before){throw 'Dados mudaram durante a janela; confira antes de retomar.'}
  $script:UpdateStage='controller';Install-UpdateController $work
  $script:UpdateStage='receipt';$versionChanged=$true;Set-InstalledUpdateVersion $Code $m
  $receiptChanged=$true
  Write-UpdateJson $receiptPath @{manifest_hash=$Hash;commit=$m.commit;install_root=$Code;data_root=$Data}
  $script:UpdateStage='journal';$record.state='verified';$record.stage='verified';Write-UpdateJson $journalPath $record
  return [pscustomobject]@{state='updated';commit=$m.commit;recovery=$work}
 }catch{
  $primaryError=$_
  $primaryError.Exception.Data['MACIP.UpdateFailure']=Get-UpdateFailureInfo $primaryError
  if($swapped){try{$script:UpdateStage='recover-unpublish';Move-CodeDirectory $Code $next;$swapped=$false}catch{Add-UpdateRecoveryFailure $record $_ 'recover-unpublish'}}
  if($oldMoved -and -not(Test-Path -LiteralPath $Code)){try{$script:UpdateStage='recover-previous';Move-CodeDirectory $previous $Code;$oldMoved=$false}catch{Add-UpdateRecoveryFailure $record $_ 'recover-previous'}}
  if(-not $swapped -and -not $oldMoved){
   if($receiptChanged){try{
    $script:UpdateStage='recover-receipt'
    if($oldReceipt){Write-UpdateJson $receiptPath $oldReceipt}elseif(Test-Path -LiteralPath $receiptPath){[IO.File]::Move($receiptPath,(Join-Path $work ('receipt-failed-'+$script:UpdateAttemptId+'.json')))}
   }catch{Add-UpdateRecoveryFailure $record $_ 'recover-receipt'}}
   if($versionChanged -and $current){try{$script:UpdateStage='recover-version';Set-InstalledUpdateVersion $Code $current}catch{Add-UpdateRecoveryFailure $record $_ 'recover-version'}}
  }
  if($record){$record.state=if($oldMoved -or $swapped -or $record.recovery_errors.Count){'needs-recovery'}else{'failed'};Save-UpdateFailure $journalPath $record $primaryError}
  throw
 }finally{
  $cleanupError=$null
  foreach($entry in $tasks){
   try{
    $script:UpdateStage='resume-tasks'
    if($primaryError -and ($oldMoved -or $swapped)){throw 'Agenda nao foi confirmada; codigo preservado, consulte recuperacao.'}
    $fresh=Get-OwnedUpdateTasks $Code $Data $Svc $helper
    $same=@($fresh|Where-Object{$_.Name -ceq $entry.Name -and (Get-TaskUpdateSignature $_.Xml) -ceq (Get-TaskUpdateSignature $entry.Xml)})
    if($same.Count -eq 1 -and (Test-Path -LiteralPath (Join-Path $Code 'Backup-MacipCliente.ps1'))){$same[0].Task.Enabled=$entry.Enabled;if([bool]$same[0].Task.Enabled -ne $entry.Enabled){throw 'Estado da agenda nao confirmado.'}}else{throw 'Propriedade da tarefa mudou.'}
   }catch{
    if(-not $cleanupError){$cleanupError=$_;$cleanupError.Exception.Data['MACIP.UpdateFailure']=Get-UpdateFailureInfo $cleanupError}
    if($record){Add-UpdateRecoveryFailure $record $_ 'resume-tasks'}
    Write-Warning 'Agenda nao confirmada; preserve codigo, dados e journal antes de retomar.' -WarningAction Continue
   }
  }
  # Grava erros secundarios enquanto ainda possui o lock; nao troca a causa principal.
  if($cleanupError -and $record){$record.state='needs-recovery';$toSave=if($primaryError){$primaryError}else{$cleanupError};Save-UpdateFailure $journalPath $record $toSave}
  try{$script:UpdateStage='dispose-lock';$lock.Dispose()}catch{
   if(-not $cleanupError){$cleanupError=$_;$cleanupError.Exception.Data['MACIP.UpdateFailure']=Get-UpdateFailureInfo $cleanupError}
   if($record){Add-UpdateRecoveryFailure $record $_ 'dispose-lock';$record.state='needs-recovery';$toSave=if($primaryError){$primaryError}else{$cleanupError};Save-UpdateFailure $journalPath $record $toSave}
  }
  if($cleanupError -and -not $primaryError){throw $cleanupError}
 }
}
if($MyInvocation.InvocationName -ne '.'){
 try{Invoke-CodeUpdate $Action $InstallRoot $DataRoot $Payload $ManifestHash $LegacyManifest $Service $ProcessProbe $LegacyManifestHash|ConvertTo-Json -Compress;exit 0}
 catch{
  $safe=Get-UpdateFailureInfo $_
  [Console]::Error.WriteLine(('Atualizacao recusada/incompleta. [{0}] Causa: {1} Acao: {2}' -f $safe.code,$safe.cause,$safe.action))
  [Console]::Error.WriteLine(('Etapa: {0}; tipo: {1}; HRESULT: {2}' -f $safe.stage,$safe.exception_type,$safe.hresult))
  $diagnostic=Get-CurrentUpdateDiagnostic $_
  if($diagnostic){[Console]::Error.WriteLine(('Diagnostico desta tentativa: {0}' -f $diagnostic))}
  exit 1
 }
}
