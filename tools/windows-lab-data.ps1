#requires -Version 5.1
param(
    [Parameter(Mandatory=$true)]
    [ValidateSet('Create','CheckFolder','CheckFile')]
    [string]$Action,
    [string]$Area = 'C:\MACIP-Lab-Ficticio',
    [string]$Reference,
    [string]$Target
)
$ErrorActionPreference = 'Stop'

function Assert-NoLinkedParents([string]$Path) {
    $current = [IO.Path]::GetFullPath($Path)
    while ($current) {
        if ((Get-Item -LiteralPath $current -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw 'Link/junction recusado.'
        }
        $parent = Split-Path -Parent $current
        if ($parent -eq $current) { break }
        $current = $parent
    }
}

function Assert-RegularTree([string]$Root) {
    if (-not (Test-Path -LiteralPath $Root -PathType Container)) { throw 'Pasta ausente.' }
    Assert-NoLinkedParents $Root
    foreach ($item in @((Get-Item -LiteralPath $Root)) + @(Get-ChildItem -LiteralPath $Root -Force -Recurse)) {
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Link/junction recusado.' }
    }
}

function Read-Hashes([string]$Root) {
    Assert-RegularTree $Root
    $result = @{}
    foreach ($file in Get-ChildItem -LiteralPath $Root -File -Recurse -Force) {
        $relative = $file.FullName.Substring($Root.TrimEnd('\','/').Length + 1).Replace('\','/')
        $result[$relative] = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    }
    return $result
}

if ($Action -eq 'Create') {
    $Area = [IO.Path]::GetFullPath($Area)
    if (Test-Path -LiteralPath $Area) { throw 'Area ja existe. Escolha outra area ficticia nova.' }
    $parent = Split-Path -Parent $Area
    Assert-NoLinkedParents $parent
    $source = Join-Path $Area 'origem'
    New-Item -ItemType Directory -Path (Join-Path $source 'subpasta') | Out-Null
    [IO.File]::WriteAllText((Join-Path $source 'documento.txt'), "MACIP dados ficticios 0.1.3`r`n")
    [IO.File]::WriteAllBytes((Join-Path $source 'subpasta\binario.bin'), [byte[]](0..255))
    [IO.File]::WriteAllText((Join-Path $source 'ignorar.tmp'), 'EXCLUIR DO BACKUP')
    $all = Read-Hashes $source
    $all.Remove('ignorar.tmp')
    $record = [ordered]@{ source = $source; exclusion = '*.tmp'; files = $all }
    $json = $record | ConvertTo-Json -Depth 4
    # UTF-8 sem BOM, aceito pelo consumidor Linux.
    [IO.File]::WriteAllText((Join-Path $Area 'reference.json'), $json, [Text.UTF8Encoding]::new($false))
    Write-Output ('Origem: ' + $source)
    Write-Output ('Referencia: ' + (Join-Path $Area 'reference.json'))
    exit 0
}

if (-not $Reference -or -not $Target) { throw 'Informe Reference e Target.' }
$record = Get-Content -LiteralPath $Reference -Raw -Encoding UTF8 | ConvertFrom-Json
$expected = @{}
foreach ($entry in $record.files.PSObject.Properties) { $expected[$entry.Name] = $entry.Value }
if ($expected.Count -ne 2) { throw 'Referencia desta fixture deve conter dois arquivos.' }
if ($Action -eq 'CheckFile') {
    Assert-NoLinkedParents $Target
    $hash = (Get-FileHash -LiteralPath $Target -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($hash -cne $expected['documento.txt']) { throw 'SHA-256 do arquivo diverge.' }
} else {
    $Target = [IO.Path]::GetFullPath($Target).TrimEnd('\','/')
    $actual = Read-Hashes $Target
    if ($actual.Count -ne $expected.Count) { throw 'Conjunto de arquivos difere da referencia.' }
    foreach ($name in $expected.Keys) {
        if (-not $actual.ContainsKey($name) -or $actual[$name] -cne $expected[$name]) {
            throw ('Arquivo ausente ou alterado: ' + $name)
        }
    }
}
Write-Output 'CONFERENCIA SHA-256 APROVADA'
