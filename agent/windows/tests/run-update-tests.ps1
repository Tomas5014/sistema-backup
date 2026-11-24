#requires -Version 5.1
# Filesystem real; Schedule.Service, processos Windows e DPAPI simulados.
$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot '../installer/Update-Installation.ps1')
$checks=0
function Assert($Value,$Message){if(-not $Value){throw $Message};$script:checks++}
$root=Join-Path ([IO.Path]::GetTempPath()) ('macip-update-'+[Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory $root|Out-Null
function Package($Name,$Commit){
 $p=Join-Path $root $Name;New-Item -ItemType Directory $p|Out-Null
 foreach($file in @('Backup-MacipCliente.ps1','MacipBackup-Gui.ps1','restic.exe')){[IO.File]::WriteAllText((Join-Path $p $file),"# synthetic $Commit")}
 Copy-Item (Join-Path $PSScriptRoot '../installer/Desinstalar-Tarefa.ps1') (Join-Path $p 'Desinstalar-Tarefa.ps1')
 Copy-Item (Join-Path $PSScriptRoot '../installer/Update-Installation.ps1') (Join-Path $p 'Update-Installation.ps1')
 $files=@{};Get-ChildItem $p -File|ForEach-Object{$files[$_.Name]=(Get-FileHash $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()}
 $m=@{format=1;origin='Tomas5014/sistema-backup';version=$Name;commit=$Commit;reader_min=1;reader_max=1;results_contract=1;files=$files}
 Write-UpdateJson (Join-Path $p 'MANIFEST.json') $m
 return [pscustomobject]@{Path=$p;Hash=(Get-FileHash (Join-Path $p 'MANIFEST.json') -Algorithm SHA256).Hash.ToLowerInvariant()}
}
function Task($Name,$Code,$Config,$Enabled){
 $monitor=$Name -eq 'MACIP-Backup-Sync';$description=if($monitor){'MACIP Backup - envio central do piloto'}else{'MACIP Backup - agente de backup do piloto'}
 $arguments=if($monitor){'-NoProfile -NonInteractive -File "'+(Join-Path $Code 'Backup-MacipCliente.ps1')+'" -Modo Sincronizar -Config "'+$Config+'"'}else{'-NoProfile -NonInteractive -File "'+(Join-Path $Code 'Backup-MacipCliente.ps1')+'" -Modo Backup -Config "'+$Config+'" -DiretorioEstado "'+(Split-Path $Config)+'"'}
 $actions=[pscustomobject]@{Count=1;Action=[pscustomobject]@{Path='C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe';Arguments=$arguments}}
 $actions|Add-Member ScriptMethod Item {param($n)$this.Action}
 $t=[pscustomobject]@{Enabled=$Enabled;State=3;Definition=[pscustomobject]@{RegistrationInfo=[pscustomobject]@{Description=$description};Actions=$actions}}
 $t|Add-Member ScriptProperty Xml { '<Task><Settings><Enabled>'+([bool]$this.Enabled).ToString().ToLowerInvariant()+'</Enabled></Settings><Name>owned</Name></Task>' }
 $t|Add-Member ScriptMethod GetInstances {param($x)[pscustomobject]@{Count=0}}
 return $t
}
$code=Join-Path $root 'Program Files/Code';$data=Join-Path $root 'data';New-Item -ItemType Directory -Path $data|Out-Null
$config=Join-Path $data 'backup.json';[IO.File]::WriteAllText($config,'{"synthetic":true}')
[IO.File]::WriteAllBytes((Join-Path $data 'restic.dpapi'),[byte[]](1,2,3,0,255))
[IO.File]::WriteAllText((Join-Path $data 'event-pending.json'),'synthetic pending UUID')
$tasks=@{}
$folder=[pscustomobject]@{Tasks=$tasks}
$folder|Add-Member ScriptMethod GetTask {param($name)if(-not $this.Tasks.ContainsKey($name)){throw [Runtime.InteropServices.COMException]::new('absent',-2147024894)};return $this.Tasks[$name]}
$svc=[pscustomobject]@{Folder=$folder};$svc|Add-Member ScriptMethod GetFolder {param($name)$this.Folder}
try{
 $one=Package 'one' ('1'*40);$two=Package 'two' ('2'*40)
 $r=Invoke-CodeUpdate Apply $code $data $one.Path $one.Hash.ToUpperInvariant() $null $svc {$false}
 Assert ($r.state -eq 'updated') 'instalacao nova'
 $work=Join-Path (Split-Path $code) '.MACIP-Backup-update';$receiptPath=Join-Path $work 'current.json';$journalPath=Join-Path $work 'transaction.json'
 Assert ((Get-Content $receiptPath -Raw|ConvertFrom-Json).manifest_hash -ceq $one.Hash) 'recibo grava hash normalizado'
 [IO.File]::WriteAllText((Join-Path $code 'desinstalar.exe'),'synthetic NSIS binary')
 $tasks['MACIP-Backup']=Task 'MACIP-Backup' $code $config $false
 $tasks['MACIP-Backup-Sync']=Task 'MACIP-Backup-Sync' $code $config $true
 $shape=Get-DataShape $data;$bytes=[IO.File]::ReadAllBytes((Join-Path $data 'restic.dpapi'))
 $r=Invoke-CodeUpdate Apply $code $data $two.Path $two.Hash $null $svc {$false}
 Assert ($r.commit -ceq ('2'*40)) 'codigo novo confirmado'
 Assert (-not $tasks['MACIP-Backup'].Enabled -and $tasks['MACIP-Backup-Sync'].Enabled) 'pausa e sincronizacao preservadas independentemente'
 Assert ((Get-DataShape $data) -ceq $shape) 'configuracao e fila intactas'
 Assert ([Convert]::ToBase64String([IO.File]::ReadAllBytes((Join-Path $data 'restic.dpapi'))) -ceq [Convert]::ToBase64String($bytes)) 'DPAPI sintetico preservado byte a byte'
 $receipt=Get-Content $receiptPath -Raw|ConvertFrom-Json;$receipt.manifest_hash=$receipt.manifest_hash.ToUpperInvariant();Write-UpdateJson $receiptPath $receipt
 $verified=Invoke-CodeUpdate Verify $code $data $two.Path $two.Hash.ToLowerInvariant() $null $svc {$false}
 Assert ($verified.state -eq 'verified') 'verify aceita recibo maiusculo e argumento minusculo'
 $same=Invoke-CodeUpdate Apply $code $data $two.Path $two.Hash.ToUpperInvariant() $null $svc {$false}
 Assert ($same.already_current) 'apply reconhece hashes equivalentes sem nova troca'
 $prior=Get-Content $journalPath -Raw|ConvertFrom-Json
 $prior.previous_manifest_hash=$prior.previous_manifest_hash.ToUpperInvariant();$prior.previous_uninstaller_hash=$prior.previous_uninstaller_hash.ToUpperInvariant();Write-UpdateJson $journalPath $prior
 $noQueue=Package 'no-queue' ('5'*40)
 $old=Get-Content (Join-Path $noQueue.Path 'MANIFEST.json') -Raw|ConvertFrom-Json;$old.results_contract=0
 Write-UpdateJson (Join-Path $noQueue.Path 'MANIFEST.json') $old
 $oldHash=(Get-FileHash (Join-Path $noQueue.Path 'MANIFEST.json') -Algorithm SHA256).Hash.ToLowerInvariant()
 try{Invoke-CodeUpdate Apply $code $data $noQueue.Path $oldHash $null $svc {$false};throw 'aceitou leitor sem fila'}catch{Assert ($_.Exception.Message -like 'Versao incapaz*') 'leitor sem resultados recusado'}
 $r=Invoke-CodeUpdate Rollback $code $data $null $null $null $svc {$false}
 Assert ($r.commit -ceq ('1'*40)) 'reversao compativel com hashes maiusculos'
 Assert ((Get-Content $receiptPath -Raw|ConvertFrom-Json).manifest_hash -ceq $one.Hash) 'rollback grava recibo normalizado'
 Assert ((Get-Content (Join-Path $code 'desinstalar.exe') -Raw) -ceq 'synthetic NSIS binary') 'desinstalador preservado na reversao'
 Assert ((Get-DataShape $data) -ceq $shape) 'fila preservada na reversao'
 Write-UpdateJson (Join-Path $data 'format.json') @{version=2}
 try{Invoke-CodeUpdate Rollback $code $data $null $null $null $svc {$false};throw 'aceitou formato futuro'}catch{Assert ($_.Exception.Message -like 'Versao incapaz*') 'formato futuro recusado'}
 Remove-Item (Join-Path $data 'format.json')
 $tasks['MACIP-Backup'].Definition.RegistrationInfo.Description='alheia'
 try{Invoke-CodeUpdate Apply $code $data $two.Path $two.Hash $null $svc {$false};throw 'aceitou tarefa alheia'}catch{Assert ($_.Exception.Message -like 'Tarefa alheia*') 'tarefa alheia recusada'}
 Assert (-not $tasks['MACIP-Backup'].Enabled) 'alheia intocada'
 $tasks['MACIP-Backup'].Definition.RegistrationInfo.Description='MACIP Backup - agente de backup do piloto'
 try{Invoke-CodeUpdate Apply $code $data $two.Path $two.Hash $null $svc {$true};throw 'aceitou processo'}catch{Assert ($_.Exception.Message -like 'Arquivos/processos*') 'processo em uso recusado'}
 $lock=[IO.File]::Open((Join-Path $code 'restic.exe'),'Open','ReadWrite','None')
 try{try{Invoke-CodeUpdate Apply $code $data $two.Path $two.Hash $null $svc {$false};throw 'aceitou arquivo em uso'}catch{Assert ($_.Exception.Message -notlike 'aceitou*') 'arquivo bloqueado recusa troca'}}finally{$lock.Dispose()}
 Assert ((Get-DataShape $data) -ceq $shape) 'recusas preservam dados'
 Assert ((Get-Content (Join-Path $code 'MANIFEST.json') -Raw|ConvertFrom-Json).commit -ceq ('1'*40)) 'recusas preservam codigo ativo'
 $tasks['MACIP-Backup'].State=4
 try{Invoke-CodeUpdate Apply $code $data $two.Path $two.Hash $null $svc {$false};throw 'aceitou tarefa ativa'}catch{Assert ($_.Exception.Message -like 'Tarefa ativa*') 'tarefa ativa recusada'}
 $tasks['MACIP-Backup'].State=3
 $script:moves=0
 function Move-CodeDirectory([string]$From,[string]$To){$script:moves++;if($script:moves -eq 2){throw 'falha sintetica antes de publicar'};[IO.Directory]::Move($From,$To)}
 try{Invoke-CodeUpdate Apply $code $data $two.Path $two.Hash $null $svc {$false};throw 'aceitou falha'}catch{Assert ($_.Exception.Message -like 'falha sintetica*') 'falha durante troca propagada';Assert ((Get-CurrentUpdateDiagnostic $_) -ceq $journalPath) 'somente journal da tentativa atual e indicado'}
 Assert ((Get-Content (Join-Path $code 'MANIFEST.json') -Raw|ConvertFrom-Json).commit -ceq ('1'*40)) 'retorno automatico do codigo anterior'
 Assert (-not $tasks['MACIP-Backup'].Enabled -and $tasks['MACIP-Backup-Sync'].Enabled) 'pausa preservada apos falha parcial'
 Assert ((Get-DataShape $data) -ceq $shape) 'dados preservados apos falha parcial'
 $failureRecord=Get-Content $journalPath -Raw|ConvertFrom-Json
 Assert ($failureRecord.attempt_id -ceq $script:UpdateAttemptId -and $failureRecord.failure.code -ceq 'unconfirmed') 'journal contem diagnostico sanitizado da tentativa atual'
 $oldJournal=[IO.File]::ReadAllText($journalPath)
 try{Invoke-CodeUpdate Apply $code $data $two.Path ('0'*64) $null $svc {$false};throw 'aceitou hash divergente'}catch{
  Assert ($_.Exception.Message -like 'Integridade do manifesto*') 'hash divergente recusado antes dos efeitos'
  Assert (-not (Get-CurrentUpdateDiagnostic $_)) 'falha inicial nao reutiliza diagnostico antigo'
 }
 Assert ([IO.File]::ReadAllText($journalPath) -ceq $oldJournal) 'falha inicial preserva journal de recuperacao anterior'
 try{Invoke-CodeUpdate Apply $code $data $null $two.Hash $null $svc {$false};throw 'aceitou pacote ausente'}catch{
  Assert ((Get-UpdateFailureInfo $_).code -ceq $failureRecord.failure.code) 'nova falha pode ter a mesma categoria do diagnostico anterior'
  Assert (-not (Get-CurrentUpdateDiagnostic $_)) 'mesma categoria nunca reutiliza tentativa antiga'
 }
 Assert ([IO.File]::ReadAllText($journalPath) -ceq $oldJournal) 'journal preservado tambem em erro inicial de mesma categoria'
 function Move-CodeDirectory([string]$From,[string]$To){[IO.Directory]::Move($From,$To)}
 $r=Invoke-CodeUpdate Apply $code $data $two.Path $two.Hash $null $svc {$false}
 Assert ($r.state -eq 'updated') 'retomada apos falha parcial'
 $again=Invoke-CodeUpdate Apply $code $data $two.Path $two.Hash $null $svc {$false}
 Assert ($again.already_current) 'mesmo pacote nao troca novamente nem perde versao anterior'
 [IO.File]::AppendAllText((Join-Path $two.Path 'Backup-MacipCliente.ps1'),'# altered')
 try{Invoke-CodeUpdate Apply $code $data $two.Path $two.Hash $null $svc {$false};throw 'aceitou alteracao'}catch{Assert ($_.Exception.Message -like 'Integridade de modulo*') 'modulo alterado recusado'}
 "Atualizacao Windows: $checks verificacoes aprovadas (COM/DPAPI/processos simulados)."
}finally{Remove-Item -LiteralPath $root -Recurse -Force}
