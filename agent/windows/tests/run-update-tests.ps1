#requires -Version 5.1
# Filesystem real; Schedule.Service, processos Windows e DPAPI simulados.
$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot '../installer/Update-Installation.ps1')
# Nunca consulta/altera HKLM, mesmo quando esta suite roda na VM instalada.
function Set-InstalledUpdateVersion([string]$Code,$Manifest,[switch]$OnlyCheck) {}
$script:moveProbes=0
function Move-CodeDirectory([string]$From,[string]$To){
 # Falha tambem em Linux se o proprio atualizador ainda retiver seus guards.
 foreach($file in Get-ChildItem -LiteralPath $From -File){$h=[IO.File]::Open($file.FullName,'Open','ReadWrite','None');$h.Dispose()}
 $script:moveProbes++;[IO.Directory]::Move($From,$To)
}
function With-ForeignHandle([string]$Path,[string]$Share,[scriptblock]$Run){
 $ready=Join-Path $root ('ready-'+[Guid]::NewGuid().ToString('N'));$release=$ready+'.release'
 $quote={param($x)"'"+$x.Replace("'","''")+"'"}
 $source='$ErrorActionPreference="Stop";$h=[IO.File]::Open('+(& $quote $Path)+',"Open","Read",'+(& $quote $Share)+');try{[IO.File]::WriteAllText('+(& $quote $ready)+',"ready");$until=[DateTime]::UtcNow.AddSeconds(15);while(-not(Test-Path -LiteralPath '+(& $quote $release)+') -and [DateTime]::UtcNow -lt $until){Start-Sleep -Milliseconds 25}}finally{$h.Dispose()}'
 $encoded=[Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($source))
 $child=Start-Process -FilePath (Get-Process -Id $PID).Path -ArgumentList @('-NoProfile','-NonInteractive','-EncodedCommand',$encoded) -PassThru
 try{
  $until=[DateTime]::UtcNow.AddSeconds(5)
  while(-not(Test-Path -LiteralPath $ready) -and [DateTime]::UtcNow -lt $until -and -not $child.HasExited){Start-Sleep -Milliseconds 25}
  if(-not(Test-Path -LiteralPath $ready)){throw 'Processo de fixture nao confirmou handle.'}
  & $Run
  Assert (-not $child.HasExited) 'processo externo nao foi encerrado pelo atualizador'
 }finally{
  [IO.File]::WriteAllText($release,'release')
  if(-not $child.WaitForExit(17000)){throw 'Fixture excedeu prazo; nao apagar seus recursos.'}
  $child.Dispose();Remove-Item -LiteralPath $ready,$release -ErrorAction SilentlyContinue
 }
}
$checks=0
function Get-FixtureContent {return ((Get-ChildItem -LiteralPath $data -Recurse -File|Sort-Object FullName|ForEach-Object{$_.FullName+'|'+(Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash}) -join "`n")}
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
$contentBefore=Get-FixtureContent
$tasks=@{}
$folder=[pscustomobject]@{Tasks=$tasks}
$folder|Add-Member ScriptMethod GetTask {param($name)if(-not $this.Tasks.ContainsKey($name)){throw [Runtime.InteropServices.COMException]::new('absent',-2147024894)};return $this.Tasks[$name]}
$svc=[pscustomobject]@{Folder=$folder};$svc|Add-Member ScriptMethod GetFolder {param($name)$this.Folder}
try{
 $one=Package 'one' ('1'*40);$two=Package 'two' ('2'*40)
 $r=Invoke-CodeUpdate Apply $code $data $one.Path $one.Hash.ToUpperInvariant() $null $svc {$false}
 Assert ($r.state -eq 'updated') 'instalacao nova'
 Assert ((Get-FixtureContent) -ceq $contentBefore -and (Get-Content (Join-Path $code 'MANIFEST.json') -Raw|ConvertFrom-Json).commit -ceq ('1'*40)) 'instalacao inicial confere codigo e SHA dos dados ficticios'
 $work=Join-Path (Split-Path $code) '.MACIP-Backup-update';$receiptPath=Join-Path $work 'current.json';$journalPath=Join-Path $work 'transaction.json'
 Assert ((Get-Content $receiptPath -Raw|ConvertFrom-Json).manifest_hash -ceq $one.Hash) 'recibo grava hash normalizado'
 [IO.File]::WriteAllText((Join-Path $code 'desinstalar.exe'),'synthetic NSIS binary')
 $tasks['MACIP-Backup']=Task 'MACIP-Backup' $code $config $false
 $tasks['MACIP-Backup-Sync']=Task 'MACIP-Backup-Sync' $code $config $true
 $controller=Join-Path $work 'Update-Installation.ps1';[IO.File]::WriteAllText($controller,'# SYNTHETIC old controller');$oldControllerHash=(Get-FileHash $controller -Algorithm SHA256).Hash
 $shape=Get-DataShape $data;$bytes=[IO.File]::ReadAllBytes((Join-Path $data 'restic.dpapi'))
 $r=Invoke-CodeUpdate Apply $code $data $two.Path $two.Hash $null $svc {$false}
 Assert ($r.commit -ceq ('2'*40)) 'codigo novo confirmado'
 $controllerBackups=@(Get-ChildItem -LiteralPath $work -File -Filter 'controller-previous-*.ps1'|Where-Object{(Get-FileHash $_.FullName -Algorithm SHA256).Hash -ceq $oldControllerHash})
 Assert ((Get-FileHash $controller -Algorithm SHA256).Hash -ceq (Get-FileHash (Join-Path $PSScriptRoot '../installer/Update-Installation.ps1') -Algorithm SHA256).Hash -and $controllerBackups.Count -eq 1) 'controlador corrigido publicado atomicamente preservando anterior'
 Assert (-not $tasks['MACIP-Backup'].Enabled -and $tasks['MACIP-Backup-Sync'].Enabled) 'pausa e sincronizacao preservadas independentemente'
 Assert ((Get-DataShape $data) -ceq $shape -and (Get-FixtureContent) -ceq $contentBefore) 'configuracao e fila intactas'
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
 Assert ((Get-DataShape $data) -ceq $shape -and (Get-FixtureContent) -ceq $contentBefore) 'fila preservada na reversao'
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
 Assert ((Get-DataShape $data) -ceq $shape -and (Get-FixtureContent) -ceq $contentBefore) 'recusas preservam dados'
 Assert ((Get-Content (Join-Path $code 'MANIFEST.json') -Raw|ConvertFrom-Json).commit -ceq ('1'*40)) 'recusas preservam codigo ativo'
 $tasks['MACIP-Backup'].State=4
 try{Invoke-CodeUpdate Apply $code $data $two.Path $two.Hash $null $svc {$false};throw 'aceitou tarefa ativa'}catch{Assert ($_.Exception.Message -like 'Tarefa ativa*') 'tarefa ativa recusada'}
 $tasks['MACIP-Backup'].State=3
 $script:moves=0
 function Move-CodeDirectory([string]$From,[string]$To){$script:moves++;if($script:moves -eq 2){throw 'falha sintetica antes de publicar'};[IO.Directory]::Move($From,$To)}
 try{Invoke-CodeUpdate Apply $code $data $two.Path $two.Hash $null $svc {$false};throw 'aceitou falha'}catch{Assert ($_.Exception.Message -like 'falha sintetica*') 'falha durante troca propagada';Assert ((Get-CurrentUpdateDiagnostic $_) -ceq $journalPath) 'somente journal da tentativa atual e indicado'}
 Assert ((Get-Content (Join-Path $code 'MANIFEST.json') -Raw|ConvertFrom-Json).commit -ceq ('1'*40)) 'retorno automatico do codigo anterior'
 Assert (-not $tasks['MACIP-Backup'].Enabled -and $tasks['MACIP-Backup-Sync'].Enabled) 'pausa preservada apos falha parcial'
 Assert ((Get-DataShape $data) -ceq $shape -and (Get-FixtureContent) -ceq $contentBefore) 'dados preservados apos falha parcial'
 $failureRecord=Get-Content $journalPath -Raw|ConvertFrom-Json
 Assert ($failureRecord.attempt_id -ceq $script:UpdateAttemptId -and $failureRecord.failure.code -ceq 'directory-rename' -and $failureRecord.failure.stage -ceq 'publish-next') 'journal contem diagnostico sanitizado da tentativa atual'
 $oldJournal=[IO.File]::ReadAllText($journalPath)
 try{Invoke-CodeUpdate Apply $code $data $two.Path ('0'*64) $null $svc {$false};throw 'aceitou hash divergente'}catch{
  Assert ($_.Exception.Message -like 'Integridade do manifesto*') 'hash divergente recusado antes dos efeitos'
  Assert (-not (Get-CurrentUpdateDiagnostic $_)) 'falha inicial nao reutiliza diagnostico antigo'
 }
 Assert ([IO.File]::ReadAllText($journalPath) -ceq $oldJournal) 'falha inicial preserva journal de recuperacao anterior'
 try{Invoke-CodeUpdate Apply $code $data $null $two.Hash $null $svc {$false};throw 'aceitou pacote ausente'}catch{
  Assert ((Get-UpdateFailureInfo $_).stage -ceq 'package') 'falha inicial tem etapa propria'
  Assert (-not (Get-CurrentUpdateDiagnostic $_)) 'mesma categoria nunca reutiliza tentativa antiga'
 }
 Assert ([IO.File]::ReadAllText($journalPath) -ceq $oldJournal) 'journal preservado tambem em erro inicial'
 $script:UpdateStage='publish-next';$script:UpdateDiagnosticPath=$journalPath
 try{throw 'SYNTHETIC rename'}catch{Assert (-not(Get-CurrentUpdateDiagnostic $_)) 'mesma categoria de erro nao pode reutilizar tentativa antiga'}
 function Move-CodeDirectory([string]$From,[string]$To){foreach($file in Get-ChildItem -LiteralPath $From -File){$h=[IO.File]::Open($file.FullName,'Open','ReadWrite','None');$h.Dispose()};$script:moveProbes++;[IO.Directory]::Move($From,$To)}
 $r=Invoke-CodeUpdate Apply $code $data $two.Path $two.Hash $null $svc {$false}
 Assert ($r.state -eq 'updated') 'retomada apos falha parcial'
 $again=Invoke-CodeUpdate Apply $code $data $two.Path $two.Hash $null $svc {$false}
 Assert ($again.already_current) 'mesmo pacote nao troca novamente nem perde versao anterior'
 Assert ($script:moveProbes -ge 3) 'instalacao inicial atualizacao e rollback renomeiam sem handles filhos proprios'
 $stableReceipt=[IO.File]::ReadAllText($receiptPath)
 With-ForeignHandle (Join-Path $code 'restic.exe') 'Read' {
  try{Invoke-CodeUpdate Apply $code $data $one.Path $one.Hash $null $svc {$false};throw 'aceitou arquivo externo em uso'}catch{
   Assert ($_.Exception.Message -notlike 'aceitou*') 'arquivo aberto por outro processo recusa troca'
   $safe=Get-UpdateFailureInfo $_;Assert ($safe.code -ceq 'file-access' -and $safe.hresult -match '^0x[0-9A-F]{8}$') 'acesso negado tem tipo e HRESULT sanitizados'
  }
 }
 With-ForeignHandle (Join-Path $work 'update.lock') 'None' {
  try{Invoke-CodeUpdate Apply $code $data $one.Path $one.Hash $null $svc {$false};throw 'aceitou atualizador concorrente'}catch{Assert ((Get-UpdateFailureInfo $_).code -ceq 'update-busy') 'outro processo mantem exclusao entre atualizadores'}
 }
 Assert ([IO.File]::ReadAllText($receiptPath) -ceq $stableReceipt -and (Get-DataShape $data) -ceq $shape -and (Get-FixtureContent) -ceq $contentBefore -and -not $tasks['MACIP-Backup'].Enabled -and $tasks['MACIP-Backup-Sync'].Enabled) 'recusas externas preservam recibo fila dados e pausas'
 # Reproduz o layout relatado: primeiro rename falha, next fica, active permanece.
 function Move-CodeDirectory([string]$From,[string]$To){throw [IO.IOException]::new('SYNTHETIC_SECRET denied',-2147024891)}
 try{Invoke-CodeUpdate Apply $code $data $one.Path $one.Hash $null $svc {$false};throw 'aceitou primeira renomeacao'}catch{
  Assert ($_.Exception.GetBaseException().Message -ceq 'SYNTHETIC_SECRET denied') 'excecao principal de Move preservada'
  $safe=Get-UpdateFailureInfo $_;Assert ($safe.code -ceq 'directory-rename' -and $safe.stage -ceq 'move-active' -and $safe.exception_type -ceq 'System.IO.IOException' -and $safe.hresult -ceq '0x80070005') 'diagnostico desembrulha IOException e HRESULT'
  $wrapped=[Management.Automation.ErrorRecord]::new([Exception]::new('SYNTHETIC wrapper',$_.Exception),'synthetic',[Management.Automation.ErrorCategory]::InvalidOperation,$null)
  $script:UpdateStage='dispose-lock';$wrappedInfo=Get-UpdateFailureInfo $wrapped
  Assert ($wrappedInfo.stage -ceq 'move-active' -and $wrappedInfo.exception_type -ceq 'System.IO.IOException' -and $wrappedInfo.hresult -ceq '0x80070005') 'wrapper PowerShell nao perde etapa e excecao principais'
 }
 $stranded=Get-Content $journalPath -Raw|ConvertFrom-Json;$strandedJournal=[IO.File]::ReadAllText($journalPath)
 Assert ($stranded.state -ceq 'failed' -and -not(Test-Path $stranded.previous) -and (Test-Path $stranded.next) -and (Test-Path $code)) 'layout nativo relatado preservado sem apagar next'
 Assert (-not $strandedJournal.Contains('SYNTHETIC_SECRET')) 'diagnostico persistido nao expoe mensagem bruta'
 function Move-CodeDirectory([string]$From,[string]$To){foreach($file in Get-ChildItem -LiteralPath $From -File){$h=[IO.File]::Open($file.FullName,'Open','ReadWrite','None');$h.Dispose()};[IO.Directory]::Move($From,$To)}
 $retry=Invoke-CodeUpdate Apply $code $data $one.Path $one.Hash $null $svc {$false}
 $retried=Get-Content $journalPath -Raw|ConvertFrom-Json
 Assert ($retry.state -ceq 'updated' -and (Test-Path $stranded.next) -and [IO.File]::ReadAllText($retried.prior_journal) -ceq $strandedJournal -and $retried.next -cne $stranded.next) 'nova tentativa conserva next e journal antigos e usa GUID novo'
 Assert ((Get-DataShape $data) -ceq $shape -and (Get-FixtureContent) -ceq $contentBefore -and -not $tasks['MACIP-Backup'].Enabled -and $tasks['MACIP-Backup-Sync'].Enabled) 'retomada preserva fila dados e pausas individuais'
 # Falha entre renames e falha secundaria ao repor o antigo: nao mascarar principal.
 $script:moves=0
 function Move-CodeDirectory([string]$From,[string]$To){$script:moves++;if($script:moves -ge 2){throw [IO.IOException]::new(('SYNTHETIC failure '+$script:moves),-2147024891)};[IO.Directory]::Move($From,$To)}
 try{Invoke-CodeUpdate Apply $code $data $two.Path $two.Hash $null $svc {$false};throw 'aceitou falha dupla'}catch{Assert ($_.Exception.GetBaseException().Message -ceq 'SYNTHETIC failure 2') 'recuperacao e finally nao substituem excecao principal'}
 $broken=Get-Content $journalPath -Raw|ConvertFrom-Json;$brokenJournal=[IO.File]::ReadAllText($journalPath)
 Assert ($broken.state -ceq 'needs-recovery' -and $broken.failure.stage -ceq 'publish-next' -and @($broken.recovery_errors).Count -ge 2) 'erros secundarios ficam separados do erro principal'
 Assert (-not(Test-Path $code) -and (Test-Path $broken.previous) -and (Test-Path $broken.next) -and -not $tasks['MACIP-Backup-Sync'].Enabled) 'falha irrecuperada preserva ambas pastas e deixa tarefas pausadas'
 try{Invoke-CodeUpdate Apply $code $data $two.Path $two.Hash $null $svc {$false};throw 'instalou sobre codigo ausente'}catch{Assert ((Get-UpdateFailureInfo $_).code -ceq 'recovery-required') 'nova instalacao recusada sobre recuperacao pendente'}
 Assert ([IO.File]::ReadAllText($journalPath) -ceq $brokenJournal) 'recusa preserva journal de recuperacao'
 function Move-CodeDirectory([string]$From,[string]$To){[IO.Directory]::Move($From,$To)}
 $rescued=Invoke-CodeUpdate Recover $code $data $null $null $null $svc {$false}
 Assert ($rescued.commit -ceq ('1'*40) -and (Get-DataShape $data) -ceq $shape -and (Get-FixtureContent) -ceq $contentBefore -and -not $tasks['MACIP-Backup'].Enabled -and $tasks['MACIP-Backup-Sync'].Enabled) 'Recover repoe codigo fila e pausas originais pelo journal'
 # Falha apos recibo novo: voltar codigo tambem deve voltar a referencia do recibo.
 $script:realWrite=(Get-Command Write-UpdateJson).ScriptBlock
 function Write-UpdateJson([string]$Path,$Value){if($Path -ceq $journalPath -and $Value.state -eq 'verified'){throw [IO.IOException]::new('SYNTHETIC journal',-2147024891)};& $script:realWrite $Path $Value}
 try{Invoke-CodeUpdate Apply $code $data $two.Path $two.Hash $null $svc {$false};throw 'aceitou falha final'}catch{Assert ($_.Exception.GetBaseException().Message -ceq 'SYNTHETIC journal') 'erro final nao e mascarado pela reposicao'}
 Assert ((Get-Content $receiptPath -Raw|ConvertFrom-Json).manifest_hash -ceq $one.Hash -and (Get-Content (Join-Path $code 'MANIFEST.json') -Raw|ConvertFrom-Json).commit -ceq ('1'*40)) 'codigo e recibo anterior consistentes apos falha final'
 Assert ((Get-DataShape $data) -ceq $shape -and (Get-FixtureContent) -ceq $contentBefore -and -not $tasks['MACIP-Backup'].Enabled -and $tasks['MACIP-Backup-Sync'].Enabled) 'falha final preserva dados fila e pausas'
 function Write-UpdateJson([string]$Path,$Value){& $script:realWrite $Path $Value}
 $r=Invoke-CodeUpdate Apply $code $data $two.Path $two.Hash $null $svc {$false}
 Assert ($r.state -ceq 'updated') 'retomada apos falha final funciona'
 [IO.File]::AppendAllText((Join-Path $two.Path 'Backup-MacipCliente.ps1'),'# altered')
 try{Invoke-CodeUpdate Apply $code $data $two.Path $two.Hash $null $svc {$false};throw 'aceitou alteracao'}catch{Assert ($_.Exception.Message -like 'Integridade de modulo*') 'modulo alterado recusado'}
 "Atualizacao Windows: $checks verificacoes aprovadas (COM/DPAPI/processos simulados)."
}finally{Remove-Item -LiteralPath $root -Recurse -Force}
