#requires -Version 5.1
$ErrorActionPreference='Stop'
# Windows PowerShell 5.1 nao carrega ZipArchive implicitamente.
Add-Type -AssemblyName System.IO.Compression
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
function Write-FixtureZipEntry($Zip,[string]$Name,[byte[]]$Bytes,[switch]$Link){
 $stream=$null;$failure=$null
 try{
  $entry=$Zip.CreateEntry($Name);if($Link){$entry.ExternalAttributes=([int]40960 -shl 16)}
  $stream=$entry.Open();$stream.Write($Bytes,0,$Bytes.Length)
 }catch{$failure=$_;throw}finally{
  if($stream){try{$stream.Dispose()}catch{if(-not $failure){throw}}}
 }
}
function New-Zip([string]$Name,[string]$FileName='home/usuário/origem com espaços/ação.txt',[switch]$Corrupt,[string]$Extra='',[switch]$Link,[switch]$TwoFiles,[scriptblock]$ZipFactory){
 $path=Join-Path $root ($Name+'.zip')
 $operation=[Guid]::NewGuid().ToString();$bytes=[Text.Encoding]::UTF8.GetBytes("fictício`nsegunda linha`n")
 if($Name -ceq 'binario'){$bytes=[byte[]](@(0..255)*1024)}
 $sha=[Security.Cryptography.SHA256]::Create();try{$digest=([BitConverter]::ToString($sha.ComputeHash($bytes))).Replace('-','').ToLowerInvariant()}finally{$sha.Dispose()}
 $files=@{};$files[$FileName]=$digest
 $secondName='C/origem com espaços/binário.bin';$secondBytes=[byte[]](@(0..255)*16)
 $sha=[Security.Cryptography.SHA256]::Create();try{$secondDigest=([BitConverter]::ToString($sha.ComputeHash($secondBytes))).Replace('-','').ToLowerInvariant()}finally{$sha.Dispose()}
 if($TwoFiles){$files[$secondName]=$secondDigest}
 $meta=@{format=1;operation=$operation;installation=[Guid]::NewGuid().ToString();repository_a=('a'*64);repository_b=('b'*64);snapshot_a=('c'*64);snapshot_b=('d'*64);files=$files}
 $file=$null;$zip=$null;$primary=$null;$cleanup=$null
 try{
  $file=[IO.File]::Open($path,'CreateNew','ReadWrite','None')
  $zip=if($ZipFactory){& $ZipFactory $file}else{[IO.Compression.ZipArchive]::new($file,[IO.Compression.ZipArchiveMode]::Create,$true)}
  Write-FixtureZipEntry $zip 'RECOVERY.json' ([Text.UTF8Encoding]::new($false).GetBytes(($meta|ConvertTo-Json -Depth 10)))
  $data=if($Corrupt){[byte[]]@(0)}else{$bytes}
  Write-FixtureZipEntry $zip ('files/'+$FileName) $data -Link:$Link
  if($TwoFiles){Write-FixtureZipEntry $zip ('files/'+$secondName) $secondBytes}
  if($Extra){Write-FixtureZipEntry $zip $Extra ([byte[]]@())}
 }catch{$primary=$_;throw}finally{
  foreach($resource in @($zip,$file)){if($null -ne $resource){try{$resource.Dispose()}catch{if(-not $cleanup){$cleanup=$_}}}}
  if($cleanup -and -not $primary){throw $cleanup}
 }
 return [pscustomobject]@{Path=$path;Operation=$operation;Digest=$digest;Name=$FileName;SecondName=$secondName;SecondDigest=$secondDigest}
}
$suiteFailure=$null
try{
 # Falha de construtor e falha de Dispose nao podem ocultar a primeira causa.
 try{New-Zip 'constructor-fail' -ZipFactory {param($stream)throw 'SYNTHETIC constructor failure'}|Out-Null;throw 'aceitou construtor'}catch{Assert ($_.Exception.Message -ceq 'SYNTHETIC constructor failure') 'erro de construtor ZIP preservado'}
 $handle=[IO.File]::Open((Join-Path $root 'constructor-fail.zip'),'Open','ReadWrite','None');$handle.Dispose()
 Assert $true 'stream fechado apos falha do construtor'
 $fakeZip=[pscustomobject]@{};$fakeZip|Add-Member ScriptMethod CreateEntry {param($name)throw 'SYNTHETIC entry failure'};$fakeZip|Add-Member ScriptMethod Dispose {throw 'SYNTHETIC cleanup failure'}
 try{New-Zip 'cleanup-fail' -ZipFactory {param($stream)return $fakeZip}|Out-Null;throw 'aceitou entrada'}catch{Assert ($_.Exception.Message -like '*SYNTHETIC entry failure*' -and $_.Exception.Message -notlike '*cleanup failure*') 'Dispose nao mascara erro da entrada ZIP'}
 $handle=[IO.File]::Open((Join-Path $root 'cleanup-fail.zip'),'Open','ReadWrite','None');$handle.Dispose()
 Assert $true 'stream externo fechado mesmo com Dispose ZIP falho'
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
 # Fluxo exato usado pelo botao, com selecao real de configuracao pela GUI.
 $gui=Join-Path $PSScriptRoot '../gui/MacipBackup-Gui.ps1'
 $agent=Join-Path $PSScriptRoot '../Backup-MacipCliente.ps1'
 $appRoot=Join-Path $root 'app';New-Item -ItemType Directory $appRoot|Out-Null
 . $gui -BaseDir $appRoot -AgentePath $agent
 $tokens=$null;$errors=$null;$ast=[Management.Automation.Language.Parser]::ParseFile((Resolve-Path $gui).ProviderPath,[ref]$tokens,[ref]$errors)
 $handlers=@($ast.FindAll({param($node)$node -is [Management.Automation.Language.InvokeMemberExpressionAst] -and $node.Member.Value -ceq 'Add_Click' -and $node.Expression -is [Management.Automation.Language.VariableExpressionAst] -and $node.Expression.VariablePath.UserPath -ceq 'btRecuperacaoB'},$true))
 $tryBodies=@($handlers[0].Arguments[0].ScriptBlock.FindAll({param($node)$node -is [Management.Automation.Language.TryStatementAst]},$false))
 if($tryBodies.Count -ne 1){throw 'Handler de recuperacao inesperado'}
 $script:clickBody=[scriptblock]::Create($tryBodies[0].Extent.Text)
 function Click-RecoveryB([string]$Archive,[string]$Folder){
  & {
   param($body,$archivePath,$folderPath)
   $package=[pscustomobject]@{FileName=$archivePath};$parent=[pscustomobject]@{SelectedPath=$folderPath}
   $lblRecuperacaoB=[pscustomobject]@{ForeColor='DarkBlue';Text=''};$btRecuperacaoB=[pscustomobject]@{Enabled=$false}
   & $body
   return [pscustomobject]@{Color=$lblRecuperacaoB.ForeColor;Text=$lblRecuperacaoB.Text;Enabled=$btRecuperacaoB.Enabled}
  } $script:clickBody $Archive $Folder
 }
 $script:forbiddenCalls=0
 function Invoke-Restic {$script:forbiddenCalls++;throw 'A indisponivel na fixture'}
 function Write-SshConfig {$script:forbiddenCalls++;throw 'SSH proibido nesta recuperacao'}
 function Read-ResticPassword {$script:forbiddenCalls++;throw 'DPAPI proibido nesta recuperacao'}
 $source=Join-Path $root 'origem ação';$preservedSource=Join-Path $root 'origem-preservada'
 New-Item -ItemType Directory $source|Out-Null;[IO.File]::WriteAllText((Join-Path $source 'original.txt'),'preservado')
 [IO.Directory]::Move($source,$preservedSource)
 $configPath=Join-Path $appRoot 'backup.json';$key=Join-Path $appRoot 'chave';$known=Join-Path $appRoot 'known_hosts'
 [IO.File]::WriteAllText($key,'SYNTHETIC KEY');[IO.File]::WriteAllText($known,'SYNTHETIC HOST')
 foreach($name in @('restic.dpapi','event-pending.json','sync-pending.json')){[IO.File]::WriteAllText((Join-Path $appRoot $name),'preservar '+$name)}
 $configuration=[ordered]@{origem=$source;exclusoes=@('*.tmp');repositorio='sftp://fixture@127.0.0.1:1//repository';repositorio_id=('a'*64);horario='02:30';identidade_ssh=$key;known_hosts=$known}
 function Write-FixtureConfig {[IO.File]::WriteAllText($configPath,($configuration|ConvertTo-Json -Depth 5),[Text.UTF8Encoding]::new($false))}
 function Snapshot-App {return ((Get-ChildItem -LiteralPath $appRoot -Recurse -File|Sort-Object FullName|ForEach-Object{$_.FullName+'|'+(Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash+'|'+$_.LastWriteTimeUtc.Ticks}) -join "`n")}
 Write-FixtureConfig
 $tasks=@{'MACIP-Backup'=[pscustomobject]@{Enabled=$false;Definition=[pscustomobject]@{Actions=[pscustomobject]@{Action=[pscustomobject]@{Arguments=('-NoProfile -NonInteractive -File "'+$agent+'" -Modo Backup -Config "'+$configPath+'" -DiretorioEstado "'+$appRoot+'"')}}}};'MACIP-Backup-Sync'=[pscustomobject]@{Enabled=$true}}
 $tasks['MACIP-Backup'].Definition.Actions|Add-Member ScriptMethod Item {param($i)$this.Action}
 $backend=[pscustomobject]@{HasTask=$false;Tasks=$tasks}
 $backend|Add-Member ScriptMethod Conectar {}
 $backend|Add-Member ScriptMethod Get {param($name)if($this.HasTask -and $this.Tasks.ContainsKey($name)){return $this.Tasks[$name]};throw [Runtime.InteropServices.COMException]::new('absent',-2147024894)}
 $script:Backend=$backend
 Save-GuiMeta ([pscustomobject]@{config_atual=$configPath;estado_atual=$appRoot;conta_tarefa='fixture'})
 $appBefore=Snapshot-App;$tasksBefore=$tasks|ConvertTo-Json -Depth 10
 $clicked=New-Zip 'callback-origem-ausente' -TwoFiles
 $feedback=Click-RecoveryB $clicked.Path $parent
 Assert ($feedback.Color -ceq 'DarkGreen' -and $feedback.Enabled -and $feedback.Text -like '2 arquivos conferidos.*') 'callback real apos confirmacao informa sucesso e reabilita botao com origem ausente'
 foreach($pair in @(@($clicked.Name,$clicked.Digest),@($clicked.SecondName,$clicked.SecondDigest))){Assert ((Get-FileHash -LiteralPath (Join-Path (Join-Path (Join-Path $parent ('Recuperacao-'+$clicked.Operation)) 'arquivos') $pair[0]) -Algorithm SHA256).Hash.ToLowerInvariant() -ceq $pair[1]) 'SHA-256 do callback real confere'}
 $byButton=New-Zip 'botao-origem-ausente' -TwoFiles
 $result=Invoke-RecuperacaoB -Archive $byButton.Path -Parent $parent
 Assert ($result.State -ceq 'verified' -and $result.Files -eq 2) 'fluxo do botao importa dois arquivos com origem renomeada e A indisponivel'
 foreach($pair in @(@($byButton.Name,$byButton.Digest),@($byButton.SecondName,$byButton.SecondDigest))){Assert ((Get-FileHash -LiteralPath (Join-Path (Join-Path $result.Destination 'arquivos') $pair[0]) -Algorithm SHA256).Hash.ToLowerInvariant() -ceq $pair[1]) 'SHA-256 do fluxo da GUI confere'}
 Assert (-not(Test-Path -LiteralPath $source) -and (Get-Content (Join-Path $preservedSource 'original.txt') -Raw) -ceq 'preservado') 'origem continua ausente e copia preservada intacta'
 Assert ((Snapshot-App) -ceq $appBefore -and ($tasks|ConvertTo-Json -Depth 10) -ceq $tasksBefore) 'GUI preserva configuracao credenciais fila e pausas individuais'
 try{Read-AgenteConfig -Caminho $configPath|Out-Null;throw 'backup aceitou origem ausente'}catch{Assert ($_.Exception.Message -like 'origem nao existe*') 'backup ainda exige origem operacional'}
 # Tarefa e a fonte primaria, mesmo quando gui.json aponta uma configuracao antiga.
 $backend.HasTask=$true
 Save-GuiMeta ([pscustomobject]@{config_atual=(Join-Path $root 'config-antiga-ausente.json');estado_atual=$appRoot})
 foreach($relation in @('igual','pai')){
  $overlap=New-Zip ('botao-overlap-'+$relation);$target=Join-Path $parent ('Recuperacao-'+$overlap.Operation)
  $configuration.origem=if($relation -ceq 'igual'){$target}else{Join-Path $target 'sub/../sub/origem-antiga'}
  Write-FixtureConfig;$snapshot=Snapshot-App
  try{Invoke-RecuperacaoB -Archive $overlap.Path -Parent $parent|Out-Null;throw 'aceitou sobreposicao'}catch{Assert ($_.Exception.Message -like 'Destino sobreposto*') 'destino igual ou pai da origem ausente recusado pelo fluxo do botao'}
  Assert (-not(Test-Path -LiteralPath $target) -and (Snapshot-App) -ceq $snapshot) 'recusa ocorre sem criar pasta e sem mudar configuracao'
 }
 $configuration.origem=$source;Write-FixtureConfig
 # Configuracao ausente/malformada nao elimina silenciosamente as protecoes.
 $invalid=New-Zip 'botao-config-invalida';$invalidTarget=Join-Path $parent ('Recuperacao-'+$invalid.Operation)
 [IO.File]::WriteAllText($configPath,'{"origem":"SYNTHETIC_SECRET"')
 $snapshot=Snapshot-App
 $feedback=Click-RecoveryB $invalid.Path $parent
 Assert ($feedback.Color -ceq 'DarkRed' -and $feedback.Enabled -and $feedback.Text -like '*backup.json invalido*' -and $feedback.Text -notlike '*SYNTHETIC_SECRET*') 'callback mostra causa explicita sem vazar configuracao nem anunciar sucesso'
 try{Invoke-RecuperacaoB -Archive $invalid.Path -Parent $parent|Out-Null;throw 'aceitou JSON invalido'}catch{Assert ($_.Exception.Message -like 'backup.json invalido*' -and $_.Exception.Message -notlike '*SYNTHETIC_SECRET*') 'configuracao malformada recebe erro explicito sem conteudo bruto'}
 Assert (-not(Test-Path -LiteralPath $invalidTarget) -and (Snapshot-App) -ceq $snapshot) 'configuracao malformada nao e sobrescrita e nao publica pasta'
 $configuration.exclusoes='*.tmp';Write-FixtureConfig
 try{Invoke-RecuperacaoB -Archive $invalid.Path -Parent $parent|Out-Null;throw 'aceitou estrutura invalida'}catch{Assert ($_.Exception.Message -like 'exclusoes deve ser uma lista*') 'leitura estrutural nao afrouxa tipos da configuracao'}
 $configuration.exclusoes=@('*.tmp');Write-FixtureConfig
 # Credenciais locais tambem nao precisam estar disponiveis para extrair B.
 [IO.File]::Move($key,(Join-Path $root 'chave-preservada'));[IO.File]::Move($known,(Join-Path $root 'hosts-preservado'))
 $withoutKeys=New-Zip 'botao-sem-credenciais'
 $snapshot=Snapshot-App
 $result=Invoke-RecuperacaoB -Archive $withoutKeys.Path -Parent $parent
 Assert ($result.State -ceq 'verified' -and (Snapshot-App) -ceq $snapshot) 'importacao independe de credenciais de A sem regrava-las'
 # Links/junctions do destino continuam recusados no mesmo caminho da GUI.
 $linkParent=Join-Path $root 'destino-link';$linkType=if(Test-IsWindows){'Junction'}else{'SymbolicLink'}
 New-Item -ItemType $linkType -Path $linkParent -Target $parent|Out-Null
 try{try{Invoke-RecuperacaoB -Archive $invalid.Path -Parent $linkParent|Out-Null;throw 'aceitou link'}catch{Assert ($_.Exception.Message -like 'Caminho com link ou junction*') 'GUI recusa link ou junction no destino'}}finally{[IO.Directory]::Delete($linkParent)}
 $backend.HasTask=$false
 [IO.File]::WriteAllText($script:GuiMeta,'{"config_atual":')
 $snapshot=Snapshot-App
 try{Invoke-RecuperacaoB -Archive $invalid.Path -Parent $parent|Out-Null;throw 'ignorou metadata invalido'}catch{Assert ($_.Exception.Message -like 'Metadados da configuracao ativa invalidos*') 'metadata local invalido nao suprime protecao da origem'}
 Assert ((Snapshot-App) -ceq $snapshot -and ($tasks|ConvertTo-Json -Depth 10) -ceq $tasksBefore -and $script:forbiddenCalls -eq 0) 'nenhum acesso a A DPAPI ou mutacao das tarefas'
 # Confirma a ligacao do handler WinForms ao fluxo exercitado acima.
 $calls=@($handlers[0].Arguments[0].ScriptBlock.FindAll({param($node)$node -is [Management.Automation.Language.CommandAst] -and $node.GetCommandName() -ceq 'Invoke-RecuperacaoB'},$true))
 Assert ($errors.Count -eq 0 -and $handlers.Count -eq 1 -and $calls.Count -eq 1 -and $handlers[0].Extent.Text -notmatch 'Read-AgenteConfig') 'botao chama exatamente o fluxo de recuperacao testado'
 foreach($name in $before.Keys){Assert ((Get-FileHash -LiteralPath (Join-Path $active $name)).Hash -ceq $before[$name]) ('preservado '+$name)}
 Write-Output "$checks verificações de recuperação assistida aprovadas; filesystem/ZIP reais, sem DPAPI/COM/VM."
}catch{$suiteFailure=$_;throw}finally{
 try{Remove-Item -LiteralPath $root -Recurse -Force -ErrorAction Stop}catch{if(-not $suiteFailure){throw};Write-Warning 'Limpeza da fixture incompleta; erro principal preservado.' -WarningAction Continue}
}
