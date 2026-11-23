#requires -Version 5.1
$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot '../gui/Assisted-Recovery.ps1')
$checks=0
function Assert($Value,$Message){if(-not $Value){throw $Message};$script:checks++}
function Reject([scriptblock]$Work,[string]$Message){$failed=$false;try{& $Work|Out-Null}catch{$failed=$true};Assert $failed $Message}
$root=Join-Path ([IO.Path]::GetTempPath()) ('macip-assisted-'+[Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $root|Out-Null
$parent=Join-Path $root 'pasta nova ação';New-Item -ItemType Directory $parent|Out-Null
$active=Join-Path $root 'dados ativos';New-Item -ItemType Directory $active|Out-Null
foreach($name in @('backup.json','restic.dpapi','fila.json','pausa-backup','pausa-sync')){[IO.File]::WriteAllText((Join-Path $active $name),'preservar '+$name)}
$before=@{};Get-ChildItem -LiteralPath $active|ForEach-Object{$before[$_.Name]=(Get-FileHash -LiteralPath $_.FullName).Hash}
function New-Zip([string]$Name,[string]$FileName='home/usuário/origem com espaços/ação.txt',[switch]$Corrupt,[string]$Extra='',[switch]$Link){
 $path=Join-Path $root ($Name+'.zip')
 $operation=[Guid]::NewGuid().ToString();$bytes=[Text.Encoding]::UTF8.GetBytes("fictício`nsegunda linha`n")
 if($Name -ceq 'binario'){$bytes=[byte[]](@(0..255)*1024)}
 $sha=[Security.Cryptography.SHA256]::Create();try{$digest=([BitConverter]::ToString($sha.ComputeHash($bytes))).Replace('-','').ToLowerInvariant()}finally{$sha.Dispose()}
 $files=@{};$files[$FileName]=$digest
 $meta=@{format=1;operation=$operation;installation=[Guid]::NewGuid().ToString();repository_a=('a'*64);repository_b=('b'*64);snapshot_a=('c'*64);snapshot_b=('d'*64);files=$files}
 $file=[IO.File]::Open($path,'CreateNew','ReadWrite','None');$zip=[IO.Compression.ZipArchive]::new($file,[IO.Compression.ZipArchiveMode]::Create,$true)
 try{
  $entry=$zip.CreateEntry('RECOVERY.json');$writer=[IO.StreamWriter]::new($entry.Open(),[Text.UTF8Encoding]::new($false));try{$writer.Write(($meta|ConvertTo-Json -Depth 10))}finally{$writer.Dispose()}
  $entry=$zip.CreateEntry('files/'+$FileName);if($Link){$entry.ExternalAttributes=([int]40960 -shl 16)}
  $stream=$entry.Open();try{if($Corrupt){$stream.WriteByte(0)}else{$stream.Write($bytes,0,$bytes.Length)}}finally{$stream.Dispose()}
  if($Extra){$e=$zip.CreateEntry($Extra);$s=$e.Open();$s.Dispose()}
 }finally{$zip.Dispose();$file.Dispose()}
 return [pscustomobject]@{Path=$path;Operation=$operation;Digest=$digest;Name=$FileName}
}
try{
 $valid=New-Zip 'valido';$r=Import-AssistedRecovery -Archive $valid.Path -Parent $parent -ProtectedRoots @($active)
 Assert ($r.State -ceq 'verified' -and $r.Files -eq 1) 'recuperação verificada'
 $recovered=Join-Path (Join-Path $r.Destination 'arquivos') ($valid.Name.Replace('/',[IO.Path]::DirectorySeparatorChar))
 Assert ((Get-FileHash -LiteralPath $recovered -Algorithm SHA256).Hash.ToLowerInvariant() -ceq $valid.Digest) 'conteúdo, acentos e layout absoluto Restic'
 Assert (Test-Path -LiteralPath (Join-Path $r.Destination 'conferencia.json')) 'comprovante local'
 Reject {Import-AssistedRecovery -Archive $valid.Path -Parent $parent} 'não sobrescreve operação anterior'
 Assert (-not(Test-Path -LiteralPath (Join-Path $r.Destination 'INCOMPLETA.txt'))) 'recusa não altera pasta já conferida'
 $bad=New-Zip 'alterado' -Corrupt
 Reject {Import-AssistedRecovery -Archive $bad.Path -Parent $parent} 'hash adulterado recusado'
 Assert (-not(Test-Path -LiteralPath (Join-Path $parent ('Recuperacao-'+$bad.Operation)))) 'sem publicação de hash inválido'
 foreach($name in @('../escape.txt','C:/escape.txt','diretório/../escape','caminho//arquivo','NUL.txt','nome.','foo\arquivo')){
  $bad=New-Zip ([Guid]::NewGuid().ToString('N')) -FileName $name
  Reject {Import-AssistedRecovery -Archive $bad.Path -Parent $parent} ('caminho recusado '+$name)
 }
 $extra=New-Zip 'extra' -Extra 'files/indevido.tmp'
 Reject {Import-AssistedRecovery -Archive $extra.Path -Parent $parent} 'extra fora do manifesto recusado'
 $link=New-Zip 'link' -Link
 Reject {Import-AssistedRecovery -Archive $link.Path -Parent $parent} 'symlink no ZIP recusado'
 $case=New-Zip 'case' -FileName 'Teste.txt' -Extra 'files/teste.txt'
 Reject {Import-AssistedRecovery -Archive $case.Path -Parent $parent} 'colisão de nomes Windows recusada'
 $overlap=New-Zip 'overlap'
 Reject {Import-AssistedRecovery -Archive $overlap.Path -Parent $active -ProtectedRoots @($active)} 'dados ativos protegidos'
 $locked=New-Zip 'lock';$lockPath=Join-Path $parent ('Recuperacao-'+$locked.Operation+'.lock')
 $held=[IO.File]::Open($lockPath,'CreateNew','ReadWrite','None')
 try{Reject {Import-AssistedRecovery -Archive $locked.Path -Parent $parent} 'concorrência recusada';Assert (Test-Path -LiteralPath $lockPath) 'lock alheio preservado'}finally{$held.Dispose();[IO.File]::Delete($lockPath)}
 $newer=New-Zip 'historico' -FileName 'antigo/removido.txt'
 $other=Import-AssistedRecovery -Archive $newer.Path -Parent $parent
 Assert (Test-Path -LiteralPath (Join-Path $other.Destination 'arquivos/antigo/removido.txt')) 'ponto histórico independe da origem atual'
 $binary=New-Zip 'binario' -FileName 'C/origem com espaços/binário.bin'
 $r=Import-AssistedRecovery -Archive $binary.Path -Parent $parent
 Assert ((Get-FileHash -LiteralPath (Join-Path $r.Destination 'arquivos/C/origem com espaços/binário.bin') -Algorithm SHA256).Hash.ToLowerInvariant() -ceq $binary.Digest) 'binário multi-bloco e raiz Windows canonicalizada'
 $memory=[IO.MemoryStream]::new([byte[]](1,2,3,4))
 try{Reject {Get-RecoveryStreamHash $memory 1} 'tamanho real limitado mesmo com tamanho declarado menor'}finally{$memory.Dispose()}
 foreach($name in $before.Keys){Assert ((Get-FileHash -LiteralPath (Join-Path $active $name)).Hash -ceq $before[$name]) ('preservado '+$name)}
 Write-Output "$checks verificações de recuperação assistida aprovadas; filesystem/ZIP reais, sem DPAPI/COM/VM."
}finally{Remove-Item -LiteralPath $root -Recurse -Force}
