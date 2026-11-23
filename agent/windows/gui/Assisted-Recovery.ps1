#requires -Version 5.1
# Dados de recuperação verificados, nunca configuração ou credenciais de B.
function Assert-RecoveryPlainPath([string]$Path) {
    $current=[IO.Path]::GetFullPath($Path)
    while($current){
        if(Test-Path -LiteralPath $current){
            $item=Get-Item -LiteralPath $current -Force
            if(($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0){throw 'Caminho com link ou junction recusado.'}
        }
        $parent=[IO.Path]::GetDirectoryName($current)
        if($parent -ceq $current){break};$current=$parent
    }
}
function Assert-RecoveryName([string]$Name) {
    if(-not $Name -or $Name.StartsWith('/') -or $Name.Contains('\') -or $Name -match '[:\x00-\x1f]'){throw 'Caminho incompatível no pacote.'}
    foreach($part in $Name.Split('/')){
        if(-not $part -or $part -in @('.','..') -or $part -match '[<>"|?*]' -or $part -match '[. ]$' -or $part -match '^(?i:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)'){throw 'Nome incompatível com Windows no pacote.'}
    }
}
function Get-RecoveryStreamHash($Stream,[long]$ExpectedLength=-1) {
    $sha=[Security.Cryptography.SHA256]::Create();$buffer=New-Object byte[] 65536;$total=0L
    try{
        while(($read=$Stream.Read($buffer,0,$buffer.Length)) -gt 0){
            $total+=$read
            if($total -gt 1GB -or ($ExpectedLength -ge 0 -and $total -gt $ExpectedLength)){throw 'Tamanho real do arquivo excede o permitido.'}
            [void]$sha.TransformBlock($buffer,0,$read,$buffer,0)
        }
        if($ExpectedLength -ge 0 -and $total -ne $ExpectedLength){throw 'Tamanho real diverge do pacote.'}
        [void]$sha.TransformFinalBlock([byte[]]@(),0,0)
        return ([BitConverter]::ToString($sha.Hash)).Replace('-','').ToLowerInvariant()
    }finally{$sha.Dispose()}
}
function Import-AssistedRecovery {
    [CmdletBinding()]
    param([Parameter(Mandatory)][string]$Archive,[Parameter(Mandatory)][string]$Parent,[string[]]$ProtectedRoots=@())
    Add-Type -AssemblyName System.IO.Compression
    $Archive=[IO.Path]::GetFullPath($Archive);$Parent=[IO.Path]::GetFullPath($Parent)
    Assert-RecoveryPlainPath $Archive;Assert-RecoveryPlainPath $Parent
    if(-not(Test-Path -LiteralPath $Parent -PathType Container)){throw 'Escolha uma pasta existente para receber a recuperação.'}
    $stream=[IO.File]::Open($Archive,'Open','Read','Read')
    $zip=$null;$reservation=$null;$reservationPath=$null;$destination=$null;$created=$false
    try{
        $zip=[IO.Compression.ZipArchive]::new($stream,[IO.Compression.ZipArchiveMode]::Read,$true)
        if($zip.Entries.Count -lt 2 -or $zip.Entries.Count -gt 1001){throw 'Quantidade de arquivos incompatível com o laboratório.'}
        $entries=@{};$total=0L
        foreach($entry in $zip.Entries){
            $name=[string]$entry.FullName;Assert-RecoveryName $name
            if($entries.ContainsKey($name)){throw 'Nome duplicado ou ambíguo no pacote.'}
            $mode=($entry.ExternalAttributes -shr 16) -band 61440
            if($mode -ne 0 -and $mode -ne 32768){throw 'Links ou arquivos especiais recusados.'}
            $total+=$entry.Length
            if($total -gt (1GB+2MB)){throw 'Pacote supera o limite de 1 GiB do laboratório.'}
            $entries[$name]=$entry
        }
        if(-not $entries.ContainsKey('RECOVERY.json') -or $entries['RECOVERY.json'].Length -gt 2MB){throw 'Comprovante de recuperação ausente ou grande demais.'}
        $metaStream=$entries['RECOVERY.json'].Open()
        $reader=[IO.StreamReader]::new($metaStream,[Text.UTF8Encoding]::new($false,$true))
        try{$meta=$reader.ReadToEnd()|ConvertFrom-Json}finally{$reader.Dispose()}
        if(($meta.format -isnot [int] -and $meta.format -isnot [long]) -or $meta.format -ne 1 -or [string]$meta.operation -cnotmatch '^[a-f0-9-]{36}$' -or [string]$meta.installation -cnotmatch '^[a-f0-9-]{36}$' -or [string]$meta.snapshot_b -cnotmatch '^[a-f0-9]{64}$' -or [string]$meta.repository_b -cnotmatch '^[a-f0-9]{64}$' -or [string]$meta.repository_a -cnotmatch '^[a-f0-9]{64}$' -or [string]$meta.snapshot_a -cnotmatch '^[a-f0-9]{64}$'){throw 'Identidade ou versão de recuperação inválida.'}
        [void][Guid]::Parse([string]$meta.operation);[void][Guid]::Parse([string]$meta.installation)
        $files=@($meta.files.PSObject.Properties)
        if($files.Count -lt 1 -or $files.Count -gt 1000 -or $zip.Entries.Count -ne $files.Count+1){throw 'Manifesto não corresponde aos arquivos.'}
        $seen=@{}
        foreach($file in $files){
            Assert-RecoveryName $file.Name
            if($seen.ContainsKey($file.Name) -or [string]$file.Value -cnotmatch '^[a-f0-9]{64}$'){throw 'Manifesto ambíguo ou hash inválido.'}
            $seen[$file.Name]=$true
            $name='files/'+$file.Name
            if(-not $entries.ContainsKey($name) -or $entries[$name].FullName -cne $name){throw 'Arquivo do manifesto ausente ou divergente.'}
            $inputStream=$entries[$name].Open()
            try{if((Get-RecoveryStreamHash $inputStream $entries[$name].Length) -cne $file.Value){throw 'Conferência SHA-256 falhou; nada foi importado.'}}finally{$inputStream.Dispose()}
        }
        $destination=Join-Path $Parent ('Recuperacao-'+$meta.operation)
        foreach($protected in $ProtectedRoots){
            if(-not $protected){continue};$root=[IO.Path]::GetFullPath($protected).TrimEnd([IO.Path]::DirectorySeparatorChar)+[IO.Path]::DirectorySeparatorChar
            $target=$destination.TrimEnd([IO.Path]::DirectorySeparatorChar)+[IO.Path]::DirectorySeparatorChar
            if($target.StartsWith($root,[StringComparison]::OrdinalIgnoreCase) -or $root.StartsWith($target,[StringComparison]::OrdinalIgnoreCase)){throw 'Destino sobreposto à origem ou aos dados do aplicativo.'}
        }
        if(Test-Path -LiteralPath $destination){throw 'A pasta da operação já existe; não será reutilizada nem sobrescrita.'}
        $reservationPath=$destination+'.lock'
        $reservation=[IO.File]::Open($reservationPath,'CreateNew','ReadWrite','None')
        if(Test-Path -LiteralPath $destination){throw 'A pasta da operação já existe.'}
        [void][IO.Directory]::CreateDirectory($destination);$created=$true
        foreach($file in $files){
            $relative=([string]$file.Name).Replace('/',[IO.Path]::DirectorySeparatorChar)
            $target=Join-Path (Join-Path $destination 'arquivos') $relative
            [void][IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($target))
            Assert-RecoveryPlainPath $target
            $inputStream=$entries['files/'+$file.Name].Open();$output=$null
            try{$output=[IO.File]::Open($target,'CreateNew','Write','None');$inputStream.CopyTo($output)}finally{if($output){$output.Dispose()};$inputStream.Dispose()}
            $actual=[IO.File]::OpenRead($target)
            try{if((Get-RecoveryStreamHash $actual $entries['files/'+$file.Name].Length) -cne $file.Value){throw 'Conferência final falhou; pasta parcial preservada.'}}finally{$actual.Dispose()}
        }
        [IO.File]::WriteAllText((Join-Path $destination 'conferencia.json'),($meta|ConvertTo-Json -Depth 12),[Text.UTF8Encoding]::new($false))
        return [pscustomobject]@{Destination=$destination;Files=$files.Count;SnapshotB=[string]$meta.snapshot_b;State='verified'}
    }catch{
        if($created -and $destination -and (Test-Path -LiteralPath $destination -PathType Container)){
            # Não elimina arquivos parciais e nunca toca a configuração ativa.
            [IO.File]::WriteAllText((Join-Path $destination 'INCOMPLETA.txt'),'Recuperação não concluída. Preserve esta pasta e solicite orientação MAC IP.')
        }
        throw
    }finally{
        if($zip){$zip.Dispose()};$stream.Dispose()
        if($reservation){$reservation.Dispose();if(Test-Path -LiteralPath $reservationPath){[IO.File]::Delete($reservationPath)}}
    }
}
