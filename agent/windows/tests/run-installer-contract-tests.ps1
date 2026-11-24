#requires -Version 5.1
# Git/manifestos/bytes/filesystem reais; payload Restic sintetico, tarefas/processos simulados.
param([ValidatePattern('^[a-f0-9]{40}$')][string]$Commit='0381ba7b7695125655c437912119fc7579dd2e09')
$ErrorActionPreference='Stop'
$repo=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../../..'))
$updater=Join-Path $repo 'agent/windows/installer/Update-Installation.ps1'
$generator=Join-Path $repo 'agent/windows/installer/New-ReleaseManifest.ps1'
. $updater
# Nao consulta/altera registro da VM: somente o filesystem e nativo nesta suite.
function Set-InstalledUpdateVersion([string]$Code,$Manifest,[switch]$OnlyCheck) {}
$checks=0
function Assert($Value,$Message){if(-not $Value){throw $Message};$script:checks++}
function Refuses([scriptblock]$Run,[string]$Message){$failed=$false;try{& $Run|Out-Null}catch{$failed=$true};Assert $failed $Message}
$root=Join-Path ([IO.Path]::GetTempPath()) ('macip-installer-contract-'+[Guid]::NewGuid().ToString('N')+' com espacos')
New-Item -ItemType Directory $root|Out-Null
$gitEnvironment=@{}
foreach($name in @('GIT_CONFIG_COUNT','GIT_CONFIG_KEY_0','GIT_CONFIG_VALUE_0','GIT_CONFIG_KEY_1','GIT_CONFIG_VALUE_1')){$gitEnvironment[$name]=[Environment]::GetEnvironmentVariable($name,'Process')}
try {
 # Reproduz configuracao comum no Git Windows sem alterar configuracao do host.
 $env:GIT_CONFIG_COUNT='2';$env:GIT_CONFIG_KEY_0='core.autocrlf';$env:GIT_CONFIG_VALUE_0='true';$env:GIT_CONFIG_KEY_1='core.eol';$env:GIT_CONFIG_VALUE_1='crlf'
 $restic=Join-Path $root 'restic.exe';[IO.File]::WriteAllText($restic,'SYNTHETIC RESTIC - NOT A WINDOWS EXECUTABLE')
 $resticHash=(Get-FileHash $restic -Algorithm SHA256).Hash
 $payload=Join-Path $root 'candidate'
 & $generator -Commit $Commit -Version '0.1.4-dev4-lab' -Restic $restic -ResticHash $resticHash -Output $payload|Out-Null
 $path=Join-Path $payload 'MANIFEST.json';$hash=(Get-FileHash $path -Algorithm SHA256).Hash
 $m=Read-ReleaseManifest $path $hash
 Test-CodeFiles $payload $m
 $contract=Get-ReleaseFiles
 Assert ($m.files.PSObject.Properties.Name.Count -eq $contract.Count) 'manifesto real contem todos os modulos'
 Assert ($m.files.PSObject.Properties.Name -ccontains 'Assisted-Recovery.ps1') 'modulo Assisted-Recovery aceito'
 foreach($entry in $contract.GetEnumerator()){
  if($entry.Key -ceq 'restic.exe'){continue}
  $blob=& git -C $repo rev-parse ($Commit+':agent/windows/'+$entry.Value)
  if($LASTEXITCODE -ne 0){throw 'blob nao disponivel'}
  $exported=& git -C $repo hash-object --no-filters -- (Join-Path $payload $entry.Key)
  Assert ($LASTEXITCODE -eq 0 -and $blob -ceq $exported) ('bytes exatos do blob: '+$entry.Key)
 }
 $nsi=Get-Content (Join-Path $repo 'agent/windows/installer/macip-backup.nsi') -Raw
 $installed=@([regex]::Matches($nsi,'(?m)^\s*File "payload\\([^"\r\n]+)"')|ForEach-Object{$_.Groups[1].Value}|Where-Object{$_ -cne 'MANIFEST.json'})
 Assert (($installed|Sort-Object) -join '|' -ceq (($contract.Keys|Sort-Object) -join '|')) 'NSIS e gerador usam exatamente o mesmo contrato'
 foreach($name in $contract.Keys){Assert ($nsi.Contains('Delete "$INSTDIR\'+$name+'"')) ('NSIS remove modulo contratado: '+$name)}
 Test-CodeFiles $payload (Read-ReleaseManifest $path $hash.ToLowerInvariant())
 Assert $true 'manifest hash minusculo aceito'
 foreach($file in $m.files.PSObject.Properties){$file.Value=$file.Value.ToUpperInvariant()}
 $upperPath=Join-Path $root 'UPPER.json';Write-UpdateJson $upperPath $m
 $upper=Read-ReleaseManifest $upperPath (Get-FileHash $upperPath -Algorithm SHA256).Hash
 Test-CodeFiles $payload $upper
 Assert (@($upper.files.PSObject.Properties|Where-Object{$_.Value -cne $_.Value.ToLowerInvariant()}).Count -eq 0) 'hashes dos arquivos normalizados'
 Refuses {Read-ReleaseManifest $path ('0'*64)} 'hash divergente recusado'
 foreach($bad in @('',('a'*63),('a'*65),('a'*63+'z'),(('a'*64)+"`n"))){Refuses {Read-ReleaseManifest $path $bad} 'formato hash invalido recusado'}
 $m.files|Add-Member NoteProperty 'unexpected.ps1' ('a'*64)
 $extra=Join-Path $root 'EXTRA.json';Write-UpdateJson $extra $m
 Refuses {Read-ReleaseManifest $extra (Get-FileHash $extra -Algorithm SHA256).Hash} 'arquivo adicional nao permitido recusado'
 Refuses {& $generator -Commit $Commit -Version 'test' -Restic $restic -ResticHash ('0'*64) -Output (Join-Path $root 'bad-restic')} 'gerador recusa Restic divergente'
 # A referencia legada vem exclusivamente do commit 0.1.3 aprovado.
 $legacy=Join-Path $root 'legacy'
 & $generator -Commit '7d62a800325146d1357f8832b1df5c4c3b2eff8d' -Version '0.1.3' -Restic $restic -ResticHash $resticHash -Output $legacy -Legacy|Out-Null
 $legacyManifest=Join-Path $legacy 'MANIFEST.json';$legacyHash=(Get-FileHash $legacyManifest -Algorithm SHA256).Hash
 $code=Join-Path $root 'Program Files/Code';New-Item -ItemType Directory $code -Force|Out-Null
 Get-ChildItem $legacy -File|Where-Object{$_.Name -cne 'MANIFEST.json'}|Copy-Item -Destination $code
 [IO.File]::WriteAllText((Join-Path $code 'desinstalar.exe'),'SYNTHETIC NSIS')
 $data=Join-Path $root 'data';New-Item -ItemType Directory $data|Out-Null
 $config=Join-Path $data 'backup.json';[IO.File]::WriteAllText($config,'{"synthetic":true}')
 [IO.File]::WriteAllBytes((Join-Path $data 'restic.dpapi'),[byte[]](0,255,1,2))
 $actions=[pscustomobject]@{Count=1;Action=[pscustomobject]@{Path='C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe';Arguments=('-NoProfile -NonInteractive -File "'+(Join-Path $code 'Backup-MacipCliente.ps1')+'" -Modo Backup -Config "'+$config+'" -DiretorioEstado "'+$data+'"')}}
 $actions|Add-Member ScriptMethod Item {param($i)$this.Action}
 $task=[pscustomobject]@{Enabled=$false;State=3;Definition=[pscustomobject]@{RegistrationInfo=[pscustomobject]@{Description='MACIP Backup - agente de backup do piloto'};Actions=$actions}}
 $task|Add-Member ScriptProperty Xml {'<Task><Settings><Enabled>'+([bool]$this.Enabled).ToString().ToLowerInvariant()+'</Enabled></Settings></Task>'}
 $task|Add-Member ScriptMethod GetInstances {param($n)[pscustomobject]@{Count=0}}
 $folder=[pscustomobject]@{Task=$task;Sync=$null};$folder|Add-Member ScriptMethod GetTask {param($name)if($name -ceq 'MACIP-Backup'){return $this.Task};if($name -ceq 'MACIP-Backup-Sync' -and $this.Sync){return $this.Sync};throw [Runtime.InteropServices.COMException]::new('absent',-2147024894)}
 $svc=[pscustomobject]@{Folder=$folder};$svc|Add-Member ScriptMethod GetFolder {param($name)$this.Folder}
 Assert (-not(Test-Path (Join-Path $code 'MANIFEST.json'))) 'instalacao legada inicia sem manifesto'
 $shape=Get-DataShape $data
 $legacyEntry=Join-Path $code 'Backup-MacipCliente.ps1';$approvedBytes=[IO.File]::ReadAllBytes($legacyEntry)
 [IO.File]::AppendAllText($legacyEntry,'# SYNTHETIC DIVERGENCE')
 Refuses {Invoke-CodeUpdate Apply $code $data $payload $hash $legacyManifest $svc {$false} $legacyHash} 'legado com conteudo divergente nao pode ser adotado'
 Assert ((Get-DataShape $data) -ceq $shape -and -not $task.Enabled -and -not(Test-Path (Join-Path $code 'MANIFEST.json'))) 'recusa legada nao muda dados tarefa ou identidade'
 [IO.File]::WriteAllBytes($legacyEntry,$approvedBytes)
 $result=Invoke-CodeUpdate Apply $code $data $payload $hash $legacyManifest $svc {$false} $legacyHash
 Assert ($result.state -ceq 'updated' -and $result.commit -ceq $Commit) 'legado com bytes aprovados atualizado com hashes uppercase'
 Assert ((Get-DataShape $data) -ceq $shape -and -not $task.Enabled) 'dados e pausa legados preservados'
 $work=Join-Path (Split-Path $code) '.MACIP-Backup-update';$receipt=Join-Path $work 'current.json';$journal=Join-Path $work 'transaction.json'
 Assert ((Get-Content $receipt -Raw|ConvertFrom-Json).manifest_hash -ceq $hash.ToLowerInvariant()) 'recibo legado usa hash canonico'
 $oldJournal=[IO.File]::ReadAllText($journal);$oldCode=(Get-FileHash (Join-Path $code 'Backup-MacipCliente.ps1') -Algorithm SHA256).Hash
 $engine=(Get-Process -Id $PID).Path
 foreach($bad in @('SYNTHETIC_SECRET',('0'*64))){
  $oldPreference=$ErrorActionPreference;$ErrorActionPreference='Continue'
  try{$output=@(& $engine -NoProfile -File $updater -Action Preflight -InstallRoot $code -DataRoot $data -Payload $payload -ManifestHash $bad 2>&1);$status=$LASTEXITCODE}finally{$ErrorActionPreference=$oldPreference}
  $text=$output|Out-String
  Assert ($status -ne 0 -and $text.Contains('Causa:') -and $text.Contains('Acao:')) 'entrada publica informa causa e proximo passo'
  $expected=if($bad -ceq 'SYNTHETIC_SECRET'){'hash-format'}else{'manifest-integrity'}
  Assert ($text.Contains('['+$expected+']')) 'causa corresponde a tentativa atual'
  Assert (-not $text.Contains('transaction.json') -and -not $text.Contains('SYNTHETIC_SECRET')) 'nenhum journal antigo ou entrada secreta expostos'
  Assert ([IO.File]::ReadAllText($journal) -ceq $oldJournal -and (Get-DataShape $data) -ceq $shape -and -not $task.Enabled -and (Get-FileHash (Join-Path $code 'Backup-MacipCliente.ps1') -Algorithm SHA256).Hash -ceq $oldCode) 'falha inicial preserva journal codigo dados e tarefas'
 }
 $prior=Get-Content $journal -Raw|ConvertFrom-Json
 $prior.previous_manifest_hash=$prior.previous_manifest_hash.ToUpperInvariant();$prior.previous_uninstaller_hash=$prior.previous_uninstaller_hash.ToUpperInvariant();Write-UpdateJson $journal $prior
 $folder.Sync=[pscustomobject]@{synthetic=$true}
 Refuses {Invoke-CodeUpdate Recover $code $data $null $null $null $svc {$false}} 'helper legado nao ignora tarefa de sincronizacao existente'
 Assert ((Get-Content (Join-Path $code 'MANIFEST.json') -Raw|ConvertFrom-Json).commit -ceq $Commit -and (Get-DataShape $data) -ceq $shape -and -not $task.Enabled) 'recusa de sincronizacao legada preserva instalacao'
 $folder.Sync=$null
 $recovered=Invoke-CodeUpdate Recover $code $data $null $null $null $svc {$false}
 Assert ($recovered.commit -ceq '7d62a800325146d1357f8832b1df5c4c3b2eff8d') 'recover aceita hashes uppercase e retorna codigo legado aprovado'
 Assert ((Get-DataShape $data) -ceq $shape -and -not $task.Enabled) 'recover preserva dados e pausa'
 'Contrato instalador: '+$checks+' verificacoes aprovadas; Git/filesystem reais, Restic sintetico e COM/processos simulados.'
} finally {
 foreach($name in $gitEnvironment.Keys){[Environment]::SetEnvironmentVariable($name,$gitEnvironment[$name],'Process')}
 Remove-Item -LiteralPath $root -Recurse -Force
}
