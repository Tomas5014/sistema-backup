param()
$ErrorActionPreference = 'Stop'
$root = Join-Path ([IO.Path]::GetTempPath()) ('activation-test-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory $root | Out-Null
$checks = 0
function Assert($condition, [string]$message) {
    if (-not $condition) { throw $message }
    $script:checks++
}
try {
    . (Join-Path $PSScriptRoot '../gui/MacipBackup-Gui.ps1') -BaseDir $root -AgentePath (Join-Path $PSScriptRoot '../Backup-MacipCliente.ps1')
    $httpOriginal = ${function:Invoke-ActivationHttp}
    try { & $httpOriginal -Url 'http://localhost/v1/activate' -Body @{}; throw 'HTTP aceito' }
    catch { Assert ($_.Exception.Message -eq 'Ativacao exige HTTPS confiavel.') 'HTTP recusado antes da rede' }
    $active = Join-Path $root 'gui.json'
    [IO.File]::WriteAllText($active, '{"config_atual":"conjunto-anterior"}')
    # DPAPI simulado exclusivamente nesta prova Linux.
    function Protect-ResticPassword([securestring]$Senha, [string]$Destino) {
        $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Senha)
        try { $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr) }
        finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) }
        [IO.File]::WriteAllText($Destino, [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($plain)))
    }
    function Read-ResticPassword([string]$Caminho) {
        ConvertTo-SecureString ([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String([IO.File]::ReadAllText($Caminho)))) -AsPlainText -Force
    }
    function Get-SshExe { '/usr/bin/ssh' }
    function Test-ConexaoDestino { param($Perfil,$ChavePath,$SenhaRestic) $script:validated++; return $Perfil.repositorio_id }
    $script:validated = 0
    $script:requests = @()
    $script:ready = $false
    $script:loseResponse = $true
    function Invoke-ActivationHttp {
        param($Url,$Body)
        $script:requests += $Body
        if ($Url -like '*/monitoring') { return [pscustomobject]@{ contract_version=1; installation_id=[Guid]::NewGuid().ToString(); token='synthetic-monitoring-token' } }
        if ($Url -like '*/redeem') {
            if ($script:loseResponse) { $script:loseResponse = $false; throw 'resposta perdida' }
            return [pscustomobject]@{ contract_version=1; activation_id=$Body.activation_id; delivery_url='https://a.lab/v1/activate' }
        }
        if (-not $script:ready) { return [pscustomobject]@{ state='awaiting_protection' } }
        return [pscustomobject]@{ contract_version=1; state='ready'; restic_password='sintetica-123'; profile=[ordered]@{
            nome='Cliente ficticio'; repositorio='sftp://conta@a.lab//repository'; repositorio_id=('a'*64)
            known_hosts='a.lab ssh-ed25519 AAAAx'; horario_sugerido='22:30'
        }}
    }
    $code = 'https://central.lab#macip-activation=' + ('a'*43)
    $directory = Join-Path $root 'attempt'
    try { Invoke-CodeActivation -Code $code -Directory $directory; throw 'nao recusou perda' }
    catch { Assert ($_.Exception.Message -eq 'resposta perdida') 'perda de resposta propagada' }
    $keyHash = (Get-FileHash -LiteralPath (Join-Path $directory 'id_ed25519')).Hash
    $pending = Invoke-CodeActivation -Code $code -Directory $directory
    Assert ($pending.State -eq 'pending') 'B pendente nao libera'
    Assert ($script:validated -eq 0) 'nao valida antes de B'
    Assert ($script:requests[0].activation_id -eq $script:requests[1].activation_id) 'retomada mantem operacao'
    Assert ($script:requests[0].public_key -eq $script:requests[1].public_key) 'retomada mantem chave'
    Assert (-not ($script:requests[0].Keys -contains 'private_key')) 'chave privada nao enviada'
    $script:ready = $true
    $completed = Invoke-CodeActivation -Code $code -Directory $directory
    Assert ($completed.State -eq 'ready') 'codigo pronto libera'
    Assert ($script:validated -eq 1) 'SFTP e ID exigidos antes de avancar'
    Assert ((Get-FileHash -LiteralPath (Join-Path $directory 'id_ed25519')).Hash -eq $keyHash) 'chave original preservada'
    Assert ([IO.File]::ReadAllText($active) -ceq '{"config_atual":"conjunto-anterior"}') 'configuracao ativa preservada'
    $before = $script:requests.Count
    try { Invoke-CodeActivation -Code ($code.Replace('aaa','bbb')) -Directory $directory; throw 'aceitou codigo trocado' }
    catch { Assert ($_.Exception.Message -like 'Outra ativacao*') 'codigo diferente recusado' }
    Assert ($script:requests.Count -eq $before) 'recusa ocorre antes de enviar'
    [IO.File]::WriteAllText((Join-Path $directory 'alheio.txt'), 'preservar')
    try { Remove-CodeActivation $directory; throw 'limpeza indevida' }
    catch { Assert ($_.Exception.Message -like 'Conteudo inesperado*') 'limpeza recusa arquivo alheio' }
    Remove-Item -LiteralPath (Join-Path $directory 'alheio.txt')
    Remove-CodeActivation $directory
    Assert (-not (Test-Path $directory)) 'tentativa descartada'
    Assert ([IO.File]::ReadAllText($active) -ceq '{"config_atual":"conjunto-anterior"}') 'descarte preserva configuracao ativa'
    Write-Host "$checks verificacoes de ativacao aprovadas (rede, DPAPI e SFTP simulados)"
} finally { Remove-Item -LiteralPath $root -Recurse -Force }
