#requires -Version 5.1
[CmdletBinding()]
param([Parameter(Mandatory)][ValidatePattern('^[a-f0-9]{40}$')][string]$Commit,
 [Parameter(Mandatory)][string]$Version,[Parameter(Mandatory)][string]$Restic,
 [Parameter(Mandatory)][ValidatePattern('^[a-f0-9]{64}$')][string]$ResticHash,
 [Parameter(Mandatory)][string]$Output,[switch]$Legacy)
$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot 'Update-Installation.ps1')
$ResticHash=ConvertTo-ReleaseHash $ResticHash
if(Test-Path -LiteralPath $Output){throw 'Saida ja existe.'}
if((Get-FileHash -LiteralPath $Restic -Algorithm SHA256).Hash.ToLowerInvariant() -cne $ResticHash){throw 'Restic nao corresponde ao payload aprovado.'}
if($Legacy -and $Commit -notin @('97de94ba563feeccb24fabea11d12abada67e247','7d62a800325146d1357f8832b1df5c4c3b2eff8d')){throw 'Referencia legada nao permitida.'}
$repo=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../../..'))
$temp=Join-Path ([IO.Path]::GetTempPath()) ('macip-build-'+[Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $temp|Out-Null
try{
 $archive=Join-Path $temp 'source.zip'
 & git -C $repo -c core.autocrlf=false -c core.eol=lf archive --format=zip "--output=$archive" $Commit agent/windows
 if($LASTEXITCODE -ne 0){throw 'Commit nao disponivel.'}
 Expand-Archive -LiteralPath $archive -DestinationPath (Join-Path $temp 'source')
 New-Item -ItemType Directory -Path $Output|Out-Null
 $base=Join-Path $temp 'source/agent/windows'
 $files=Get-ReleaseFiles
 $hashes=[ordered]@{}
 foreach($entry in $files.GetEnumerator()){
  if($entry.Key -ceq 'restic.exe' -or ($Legacy -and $entry.Key -cin @('Update-Installation.ps1','Assisted-Recovery.ps1'))){continue}
  $relative=$entry.Value
  $from=Join-Path $base $relative
  if(-not(Test-Path -LiteralPath $from)){if($Legacy){continue};throw 'Modulo obrigatorio ausente no commit.'}
  # Confere bytes crus contra o blob aprovado, mesmo sob configuracao Git Windows.
  $blob=& git -C $repo rev-parse ($Commit+':agent/windows/'+$relative)
  if($LASTEXITCODE -ne 0){throw 'Blob aprovado indisponivel.'}
  $exported=& git -C $repo hash-object --no-filters -- $from
  if($LASTEXITCODE -ne 0 -or $blob -cne $exported){throw 'Bytes exportados divergem do commit aprovado.'}
  $to=Join-Path $Output $entry.Key;Copy-Item -LiteralPath $from -Destination $to
  $hashes[[IO.Path]::GetFileName($relative)]=(Get-FileHash -LiteralPath $to -Algorithm SHA256).Hash.ToLowerInvariant()
 }
 Copy-Item -LiteralPath $Restic -Destination (Join-Path $Output 'restic.exe');$hashes['restic.exe']=$ResticHash
 $manifest=[ordered]@{format=1;origin='Tomas5014/sistema-backup';version=$Version;commit=$Commit;reader_min=1;reader_max=1;activation_contract=1;results_contract=$(if($Commit -ceq '7d62a800325146d1357f8832b1df5c4c3b2eff8d'){0}else{1});files=$hashes}
 $path=Join-Path $Output 'MANIFEST.json'
 [IO.File]::WriteAllText($path,($manifest|ConvertTo-Json -Depth 10),[Text.UTF8Encoding]::new($false))
 [pscustomobject]@{commit=$Commit;manifest_sha256=(Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant();installer_sha256=$null;installer_built=$false}|ConvertTo-Json
}finally{Remove-Item -LiteralPath $temp -Recurse -Force}
