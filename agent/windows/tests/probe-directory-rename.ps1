#requires -Version 5.1
# Somente arquivos ficticios em TEMP. Nao consulta instalacao, tarefas ou dados reais.
param([switch]$AllowNonWindows)
$ErrorActionPreference='Stop'
$native=$env:OS -eq 'Windows_NT'
if($native -and ($PSVersionTable.PSVersion.Major -ne 5 -or $PSVersionTable.PSVersion.Minor -ne 1)){throw 'Execute a prova nativa com Windows PowerShell 5.1.'}
if(-not $native -and -not $AllowNonWindows){throw 'Prova nativa exige Windows PowerShell 5.1 em NTFS. Linux nao confirma a hipotese.'}
$root=Join-Path ([IO.Path]::GetTempPath()) ('macip-rename-probe-'+[Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory $root|Out-Null
function ErrorInfo($Failure){$e=$Failure.Exception;while($e.InnerException){$e=$e.InnerException};return @{exception_type=$e.GetType().FullName;hresult=('0x'+$e.HResult.ToString('X8'))}}
function Trial([string]$Name,[bool]$OpenActive,[bool]$OpenNext,[bool]$DisposeBeforeMove){
 $area=Join-Path $root $Name;New-Item -ItemType Directory $area|Out-Null
 $code=Join-Path $area 'Code';$next=Join-Path $area 'next';$previous=Join-Path $area 'previous'
 foreach($dir in @($code,$next)){New-Item -ItemType Directory $dir|Out-Null;[IO.File]::WriteAllText((Join-Path $dir 'module.ps1'),'# synthetic')}
 $handles=@();$stage='open';$failure=$null;$moved=$false;$sharing=if($DisposeBeforeMove){'None'}else{'Delete'}
 try{
  foreach($dir in @($code,$next)){
   if(($dir -ceq $code -and $OpenActive) -or ($dir -ceq $next -and $OpenNext)){$handles+=,[IO.File]::Open((Join-Path $dir 'module.ps1'),'Open','ReadWrite',([IO.FileShare]$sharing))}
  }
  if($DisposeBeforeMove){foreach($h in $handles){$h.Dispose()};$handles=@()}
  $stage='move-active';[IO.Directory]::Move($code,$previous)
  $stage='publish-next';[IO.Directory]::Move($next,$code);$moved=$true
 }catch{$failure=ErrorInfo $_}finally{foreach($h in $handles){$h.Dispose()}}
 $afterClose=$true
 try{if(-not(Test-Path $previous)){[IO.Directory]::Move($code,$previous)};if(Test-Path $next){[IO.Directory]::Move($next,$code)}}catch{$afterClose=$false}
 return [pscustomobject]@{case=$Name;file_share=$sharing;handles_held_at_move=$(if($DisposeBeforeMove){0}else{$handles.Count});moved_with_original_handles=$moved;failed_stage=$(if($failure){$stage}else{$null});failure=$failure;completed_after_close=$afterClose}
}
try{
 $volume=[IO.DriveInfo]::new([IO.Path]::GetPathRoot($root)).DriveFormat
 if($native -and $volume -ine 'NTFS'){throw 'Prova nativa requer TEMP em volume NTFS.'}
 $rows=@((Trial 'no-handles' $false $false $false),(Trial 'legacy-both-open' $true $true $false),(Trial 'legacy-next-open' $false $true $false),(Trial 'probe-disposed' $true $true $true))
 [pscustomobject]@{native_windows=$native;powershell=$PSVersionTable.PSVersion.ToString();volume=$volume;cases=$rows;hypothesis=$(if(-not $native){'not-proven-on-Linux'}elseif(@($rows|Where-Object{$_.case -like 'legacy-*' -and $_.failed_stage -in @('move-active','publish-next') -and -not $_.moved_with_original_handles -and $_.completed_after_close}).Count){'reproduced-in-this-environment'}else{'not-reproduced-in-this-environment'})}|ConvertTo-Json -Depth 6
 if(@($rows|Where-Object{$_.case -in @('no-handles','probe-disposed') -and -not $_.moved_with_original_handles}).Count){throw 'Controle sem handles abertos falhou; nao instalar.'}
}finally{Remove-Item -LiteralPath $root -Recurse -Force}
