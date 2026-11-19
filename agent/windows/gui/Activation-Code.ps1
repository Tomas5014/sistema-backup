#requires -Version 5.1
# Contrato MACIP activation v1. Somente dados publicos saem antes do resgate.
function Invoke-ActivationHttp {
    param([string]$Url, [object]$Body)
    $uri = [Uri]$Url
    if ($uri.Scheme -cne 'https' -or $uri.UserInfo -or $uri.Fragment) { throw 'Ativacao exige HTTPS confiavel.' }
    Add-Type -AssemblyName System.Net.Http
    $handler = [Net.Http.HttpClientHandler]::new()
    $handler.AllowAutoRedirect = $false
    $client = [Net.Http.HttpClient]::new($handler)
    $client.Timeout = [TimeSpan]::FromSeconds(30)
    try {
        $content = [Net.Http.StringContent]::new(($Body | ConvertTo-Json -Compress), [Text.Encoding]::UTF8, 'application/json')
        $response = $client.PostAsync($Url, $content).GetAwaiter().GetResult()
        try {
            if (-not $response.IsSuccessStatusCode) {
                switch ([int]$response.StatusCode) {
                    403 { throw 'Codigo invalido ou revogado.' }
                    410 { throw 'Codigo ou retomada vencidos.' }
                    409 { throw 'Ativacao pendente ou vinculada a outra chave. Confira a central.' }
                    429 { throw 'Muitas tentativas. Aguarde um minuto.' }
                    default { throw 'Falha de conexao ou resposta recusada na ativacao.' }
                }
            }
            return ($response.Content.ReadAsStringAsync().GetAwaiter().GetResult() | ConvertFrom-Json)
        } finally { $response.Dispose(); $content.Dispose() }
    } catch [Net.Http.HttpRequestException] {
        throw 'Falha de conexao HTTPS. Confira a rede, o endereco e a CA confiavel do laboratorio.'
    } finally { $client.Dispose(); $handler.Dispose() }
}

function New-ActivationAttempt {
    param([string]$Directory)
    New-Item -ItemType Directory -Path $Directory -ErrorAction Stop | Out-Null
    Protect-PastaEstado -Estado $Directory
    if (-not (Test-IsWindows)) { & chmod 700 $Directory }
    $key = Join-Path $Directory 'id_ed25519'
    $ssh = Get-SshExe
    $keygen = Join-Path (Split-Path -Parent $ssh) 'ssh-keygen.exe'
    if (-not (Test-IsWindows)) { $keygen = 'ssh-keygen' }
    $start = [Diagnostics.ProcessStartInfo]::new()
    $start.FileName = $keygen
    $start.Arguments = '-q -t ed25519 -N "" -C macip-activation -f "' + $key + '"'
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.RedirectStandardOutput = $true; $start.RedirectStandardError = $true
    $proc = [Diagnostics.Process]::Start($start)
    try {
        $out = $proc.StandardOutput.ReadToEndAsync(); $err = $proc.StandardError.ReadToEndAsync()
        if (-not $proc.WaitForExit(15000)) { $proc.Kill(); throw 'Geracao de chave excedeu o prazo.' }
        if ($proc.ExitCode -ne 0) { throw 'Nao foi possivel gerar a chave SSH.' }
    } finally { $proc.Dispose() }
    $public = ((Get-Content -LiteralPath ($key + '.pub') -Raw).Trim() -split '\s+')[0..1] -join ' '
    $bytes = New-Object byte[] 32
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    $secret = [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+','-').Replace('/','_')
    Protect-ResticPassword -Senha (ConvertTo-SecureString $secret -AsPlainText -Force) -Destino (Join-Path $Directory 'resume.dpapi')
    $attempt = [ordered]@{
        activation_id = [Guid]::NewGuid().ToString(); device_id = [Guid]::NewGuid().ToString()
        public_key = $public
    }
    $attempt | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $Directory 'attempt.json') -Encoding UTF8
    return [pscustomobject]$attempt
}

function Invoke-CodeActivation {
    param([string]$Code, [string]$Directory, [ValidateRange(0,120)][int]$WaitSeconds=90, [ValidateRange(1,10)][int]$PollSeconds=3)
    $uri = $null
    if (-not [Uri]::TryCreate($Code, [UriKind]::Absolute, [ref]$uri) -or
        $uri.Scheme -cne 'https' -or $uri.UserInfo -or $uri.Query -or
        $uri.Fragment -cnotmatch '^#macip-activation=([A-Za-z0-9_-]{43})$') {
        throw 'Codigo de ativacao invalido. Cole o codigo completo emitido pelo painel.'
    }
    $raw = $Matches[1]
    $central = $uri.GetLeftPart([UriPartial]::Path).TrimEnd('/')
    $identity = [Security.Cryptography.SHA256]::Create()
    try { $codeHash = [Convert]::ToBase64String($identity.ComputeHash([Text.Encoding]::UTF8.GetBytes($Code))) }
    finally { $identity.Dispose() }
    if (Test-Path -LiteralPath $Directory) {
        $attempt = Get-Content -LiteralPath (Join-Path $Directory 'attempt.json') -Raw | ConvertFrom-Json
        if ($attempt.code_hash -cne $codeHash) { throw 'Outra ativacao esta pendente. Retome com o mesmo codigo.' }
    } else {
        $attempt = New-ActivationAttempt $Directory
        $attempt | Add-Member NoteProperty code_hash $codeHash
        $attempt | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $Directory 'attempt.json') -Encoding UTF8
    }
    $secure = Read-ResticPassword -Caminho (Join-Path $Directory 'resume.dpapi')
    $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try { $secret = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) }
    $redeemed = Invoke-ActivationHttp ($central + '/api/v1/activations/redeem') @{
        activation_code = $raw; activation_id = $attempt.activation_id; device_id = $attempt.device_id
        public_key = $attempt.public_key; resume_secret = $secret
    }
    if ($redeemed.contract_version -ne 1 -or $redeemed.activation_id -cne $attempt.activation_id) {
        throw 'Contrato ou identidade de ativacao divergente.'
    }
    $wait=[Diagnostics.Stopwatch]::StartNew()
    do {
        try {
            $delivery = Invoke-ActivationHttp $redeemed.delivery_url @{
                activation_id = $attempt.activation_id; resume_secret = $secret
            }
        } catch {
            # A tentativa e a chave ficam duraveis; nenhuma configuracao ativa e substituida.
            if ($_.Exception.Message -like 'Falha de conexao*') {
                return [pscustomobject]@{ State='pending'; Reason='connection' }
            }
            throw
        }
        if ($delivery.state -ne 'awaiting_protection') { break }
        if ($wait.Elapsed.TotalSeconds -ge $WaitSeconds) {
            return [pscustomobject]@{ State='pending'; Reason='preparation' }
        }
        Start-Sleep -Milliseconds ([int][Math]::Min($PollSeconds*1000, [Math]::Max(1,($WaitSeconds-$wait.Elapsed.TotalSeconds)*1000)))
    } while ($true)
    if ($delivery.state -cne 'ready' -or $delivery.contract_version -ne 1 -or -not $delivery.restic_password) {
        throw 'Resposta de ativacao incompativel.'
    }
    $profilePath = Join-Path $Directory 'profile.json'
    [IO.File]::WriteAllText($profilePath, ($delivery.profile | ConvertTo-Json), [Text.UTF8Encoding]::new($false))
    $profile = Read-Perfil -Caminho $profilePath
    $password = ConvertTo-SecureString $delivery.restic_password -AsPlainText -Force
    Protect-ResticPassword -Senha $password -Destino (Join-Path $Directory 'restic.dpapi')
    $key = Join-Path $Directory 'id_ed25519'
    [void](Test-ConexaoDestino -Perfil $profile -ChavePath $key -SenhaRestic $password)
    $monitor = Invoke-ActivationHttp ($central + '/api/v1/activations/monitoring') @{
        activation_id = $attempt.activation_id; resume_secret = $secret
    }
    if ($monitor.contract_version -ne 1 -or -not $monitor.token) { throw 'Monitoramento da ativacao indisponivel.' }
    Protect-ResticPassword -Senha (ConvertTo-SecureString $monitor.token -AsPlainText -Force) -Destino (Join-Path $Directory 'monitoring.dpapi')
    Write-MonitorJson (Join-Path $Directory 'monitoring.json') @{
        central = ([Uri]$central).GetLeftPart([UriPartial]::Authority)
        installation_id = $monitor.installation_id
    }
    return [pscustomobject]@{ State = 'ready'; Profile = $profile; Key = $key; Password = $password }
}

function Remove-CodeActivation {
    param([string]$Directory)
    if (-not (Test-Path -LiteralPath $Directory)) { return }
    $allowed = @('id_ed25519','id_ed25519.pub','resume.dpapi','attempt.json','profile.json','restic.dpapi','monitoring.json','monitoring.dpapi')
    $items = @(Get-ChildItem -LiteralPath $Directory -Force)
    if ((Get-Item -LiteralPath $Directory).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Pasta de tentativa insegura.' }
    foreach ($item in $items) {
        if ($item.PSIsContainer -or $item.Name -notin $allowed -or
            ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'Conteudo inesperado na tentativa; limpeza recusada.' }
    }
    foreach ($item in $items) { Remove-Item -LiteralPath $item.FullName -Force }
    Remove-Item -LiteralPath $Directory
}
