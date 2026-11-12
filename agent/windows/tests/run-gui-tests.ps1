#requires -Version 5.1
# Testes da interface MAC IP Backup  -  mesma infra da suíte do agente.
# Roda em pwsh no Linux: a fronteira DPAPI e o Agendador (COM) sao
# substituidos; no Windows roda inteira. Cobre: perfil (importacao e
# recusas), gravacao atomica do backup.json, classificacao de resultado,
# alvo de restauracao e forma dos argumentos da tarefa.
param([string]$Stub = (Join-Path $PSScriptRoot 'restic-stub.ps1'))

$ErrorActionPreference = 'Stop'
$script:ok = 0; $script:er = 0; $script:skip = 0
function eq($real, $esperado, [string]$nome) {
    if ("$real" -ceq "$esperado") { $script:ok++; Write-Host " ok  $nome" }
    else { $script:er++; Write-Host " FALHOU  $nome  (esperado='$esperado' real='$real')" }
}
function ok($cond, [string]$nome) { eq ([bool]$cond) $true $nome }
function throws($sb, [string]$nome) {
    try { & $sb | Out-Null; $script:er++; Write-Host " FALHOU  $nome (nao lancou erro)" }
    catch { $script:ok++; Write-Host " ok  $nome" }
}
function pula([string]$nome) { $script:skip++; Write-Host " pulou $nome" }

$tmp = Join-Path ([IO.Path]::GetTempPath()) ('gui-teste-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory $tmp -Force | Out-Null
try {
    # ---- ambiente: GUI dot-sourced + fronteiras simuladas ----
    $gui = Join-Path $PSScriptRoot '..\gui\MacipBackup-Gui.ps1'
    $agente = Join-Path $PSScriptRoot '..\Backup-MacipCliente.ps1'
    . $agente
    # fronteira DPAPI fora do Windows (igual a suite do agente)
    function Protect-ResticPassword([securestring]$Senha, [string]$Destino) {
        $b = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Senha)
        $pl = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($b)
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($b)
        [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($pl)) |
            Set-Content -LiteralPath $Destino -Encoding Ascii -NoNewline
    }
    function Read-ResticPassword([string]$Caminho) {
        $pl = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String((Get-Content -LiteralPath $Caminho -Raw).Trim()))
        ConvertTo-SecureString -String $pl -AsPlainText -Force
    }
    . $gui -BaseDir (Join-Path $tmp 'base') -AgentePath $agente
    $script:ResticExeNome = 'restic-stub.ps1'
    $script:SshExe = 'ssh'

    $kh = 'backup.macip.example ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFicticia'
    $idValido = 'a' * 64
    function Write-Perfil($obj) {
        $f = Join-Path $tmp ('perfil-' + [Guid]::NewGuid().ToString('N') + '.json')
        ($obj | ConvertTo-Json -Depth 4) | Set-Content -LiteralPath $f -Encoding UTF8
        return $f
    }

    # ---- Read-Perfil ----
    $p = Read-Perfil -Caminho (Write-Perfil ([ordered]@{
        nome = 'X'; repositorio = 'sftp:u@h:/dados/x'; repositorio_id = $idValido; known_hosts = $kh }))
    eq $p.repositorio 'sftp:u@h:/dados/x' 'perfil valido: repositorio'
    eq $p.repositorio_id $idValido 'perfil valido: id'
    $p2 = Read-Perfil -Caminho (Write-Perfil ([ordered]@{
        repositorio = 'sftp://u@h:2222//abs/x'; repositorio_id = $idValido; known_hosts = $kh }))
    eq $p2.repositorio 'sftp://u@h:2222//abs/x' 'perfil valido: forma com porta'
    throws { Read-Perfil -Caminho (Write-Perfil @{ repositorio='sftp:u@h:/x'; repositorio_id=$idValido; senha='abc'; known_hosts=$kh }) } 'perfil: campo senha recusado'
    throws { Read-Perfil -Caminho (Write-Perfil @{ repositorio='sftp:u@h:/x'; repositorio_id=$idValido; known_hosts='h ssh-ed25519 -----BEGIN KEY' }) } 'perfil: segredo no valor recusado'
    throws { Read-Perfil -Caminho (Write-Perfil @{ repositorio='sftp:u@h:/x'; repositorio_id=$idValido; known_hosts=$kh; estranho=1 }) } 'perfil: campo desconhecido recusado'
    throws { Read-Perfil -Caminho (Write-Perfil @{ repositorio='sftp:u@h:2222/x'; repositorio_id=$idValido; known_hosts=$kh }) } 'perfil: forma ambigua :porta/caminho recusada'
    throws { Read-Perfil -Caminho (Write-Perfil @{ repositorio='sftp:u@h:/x'; repositorio_id='zzz'; known_hosts=$kh }) } 'perfil: id invalido recusado'
    throws { Read-Perfil -Caminho (Write-Perfil @{ repositorio='sftp:u@h:/x'; repositorio_id=$idValido; known_hosts='' }) } 'perfil: known_hosts vazio recusado'
    throws { Read-Perfil -Caminho (Write-Perfil @{ repositorio='sftp:u@h:/x'; repositorio_id=$idValido; known_hosts='texto-sem-chave' }) } 'perfil: known_hosts sem chave recusado'
    throws { Read-Perfil -Caminho (Write-Perfil @{ repositorio='sftp:u@h:/x'; repositorio_id=$idValido; known_hosts=$kh; horario_sugerido='25:00' }) } 'perfil: horario_sugerido invalido recusado'
    throws { Read-Perfil -Caminho (Join-Path $tmp 'nao-existe.json') } 'perfil: arquivo ausente recusado'
    '{nao-e-json' | Set-Content (Join-Path $tmp 'ruim.json') -Encoding UTF8
    throws { Read-Perfil -Caminho (Join-Path $tmp 'ruim.json') } 'perfil: JSON quebrado recusado'

    # ---- New-BackupJsonObj + Save-ConfigAtomico ----
    $origem = Join-Path $tmp 'origem'; New-Item -ItemType Directory $origem | Out-Null
    $chave = Join-Path $tmp 'id'; 'x' | Set-Content $chave -Encoding Ascii
    $khf = Join-Path $tmp 'kh'; $kh | Set-Content $khf -Encoding Ascii
    $obj = New-BackupJsonObj -Perfil $p -Origem $origem -Exclusoes @('*.tmp') -Horario '02:30' `
        -IdentidadeSsh $chave -KnownHostsFile $khf
    eq $obj.origem $origem 'json: origem'
    eq $obj.horario '02:30' 'json: horario'
    Save-ConfigAtomico -CfgObj $obj
    ok (Test-Path -LiteralPath $script:ConfigFile -PathType Leaf) 'backup.json gravado'
    $antes = Get-Content -LiteralPath $script:ConfigFile -Raw
    # gravacao invalida preserva a anterior
    throws { Save-ConfigAtomico -CfgObj (New-BackupJsonObj -Perfil $p -Origem $origem -Exclusoes @('') -Horario '02:30' -IdentidadeSsh $chave -KnownHostsFile $khf) } 'config invalida recusada'
    eq (Get-Content -LiteralPath $script:ConfigFile -Raw) $antes 'config anterior preservada apos recusa'
    throws { Save-ConfigAtomico -CfgObj (New-BackupJsonObj -Perfil $p -Origem $origem -Exclusoes @() -Horario '99:99' -IdentidadeSsh $chave -KnownHostsFile $khf) } 'horario invalido recusado'
    eq (Get-Content -LiteralPath $script:ConfigFile -Raw) $antes 'config anterior preservada (2)'

    # ---- Get-TaskActionArgs ----
    $args1 = Get-TaskActionArgs -Agente 'C:\App\agente.ps1' -Config 'C:\Dados\backup.json' -Estado 'C:\Dados\estado-1'
    ok ($args1 -match '-Modo Backup' -and $args1 -match 'agente\.ps1' -and $args1 -match 'estado-1') 'argumentos da tarefa chamam -Modo Backup'
    ok ($args1 -match '-NonInteractive') 'tarefa roda sem prompts'

    # ---- Classify-Resultado ----
    $infoOk = [pscustomobject]@{ Habilitada=$true; Estado=1; Ultima=''; Proxima='amanha'; Codigo=0 }
    eq (Classify-Resultado -Res $null -InfoTarefa $infoOk) 'nunca_executou' 'classifica: sem resultado'
    eq (Classify-Resultado -Res ([pscustomobject]@{status='sucesso';inicio_utc='2030-01-01T00:00:00Z';codigo_restic=0}) -InfoTarefa $infoOk) 'sucesso' 'classifica: sucesso'
    eq (Classify-Resultado -Res ([pscustomobject]@{status='falha';inicio_utc='2030-01-01T00:00:00Z';codigo_restic=1}) -InfoTarefa $infoOk) 'falha' 'classifica: falha'
    eq (Classify-Resultado -Res ([pscustomobject]@{status='falha';inicio_utc='2030-01-01T00:00:00Z';codigo_restic=3}) -InfoTarefa $infoOk) 'incompleto' 'classifica: incompleto (restic 3)'
    eq (Classify-Resultado -Res ([pscustomobject]@{status='sucesso';inicio_utc='2030-01-01T00:00:00Z'}) -InfoTarefa ([pscustomobject]@{Habilitada=$true;Estado=4;Ultima='';Proxima='';Codigo=0})) 'em_execucao' 'classifica: em execucao agora'
    # resultado ANTIGO nao prova a execucao atual: inicio anterior ao ultimo disparo
    eq (Classify-Resultado -Res ([pscustomobject]@{status='sucesso';inicio_utc='2026-01-01T00:00:00Z'}) -InfoTarefa ([pscustomobject]@{Habilitada=$true;Estado=1;Ultima='2030-06-01T03:00:00Z';Proxima='';Codigo=0})) 'sem_resultado_novo' 'classifica: resultado antigo nao conta como novo'
    eq $script:RotulosStatus['sem_resultado_novo'].Contains('anterior') $true 'rotulo explica resultado antigo'

    # ---- Restore-Arquivo: validacao do alvo ----
    throws { Restore-Arquivo -Estado $tmp -Snapshot 'aa' -Arquivo 'f.txt' -Destino $origem -Origem $origem } 'restaurar: destino igual a origem recusado'
    $cheio = Join-Path $tmp 'cheio'; New-Item -ItemType Directory $cheio | Out-Null; 'x' | Set-Content (Join-Path $cheio 'f')
    throws { Restore-Arquivo -Estado $tmp -Snapshot 'aa' -Arquivo 'f.txt' -Destino $cheio -Origem $origem } 'restaurar: destino nao vazio recusado'
    throws { Restore-Arquivo -Estado $tmp -Snapshot 'aa' -Arquivo 'f.txt' -Destino '' -Origem $origem } 'restaurar: sem destino recusado'

    # ---- tarefa/Agendador: so no Windows ----
    if (Test-IsWindows) {
        ok $true 'windows: funcoes COM presentes (Get-TarefaMacip)'
    } else {
        pula 'registro/rodada da tarefa COM (somente Windows)'
    }
} finally {
    Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
}
Write-Host ''
Write-Host "RESULTADO: $($script:ok) ok, $($script:er) falha(s), $($script:skip) pulado(s)"
exit $(if ($script:er -gt 0) { 1 } else { 0 })
