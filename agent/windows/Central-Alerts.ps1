#requires -Version 5.1
# Telemetria Windows v1. Credencial DPAPI separada do perfil e da senha Restic.
function Invoke-MonitorHttp {
    param($Monitor, [string]$Path, $Body)
    $uri = [Uri]$Monitor.central
    if ($uri.Scheme -cne 'https' -or $uri.UserInfo -or $uri.Query -or $uri.Fragment -or
        $uri.AbsolutePath -ne '/') { throw 'Central autorizada deve ser origem HTTPS.' }
    $secure = Read-ResticPassword -Caminho $Monitor.token_file
    $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try { $token = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) }
    Add-Type -AssemblyName System.Net.Http
    $handler = [Net.Http.HttpClientHandler]::new(); $handler.AllowAutoRedirect = $false
    $http = [Net.Http.HttpClient]::new($handler); $http.Timeout = [TimeSpan]::FromSeconds(10)
    try {
        $http.DefaultRequestHeaders.Authorization = [Net.Http.Headers.AuthenticationHeaderValue]::new('Bearer', $token)
        $content = [Net.Http.StringContent]::new(($Body | ConvertTo-Json -Depth 6 -Compress), [Text.Encoding]::UTF8, 'application/json')
        try {
            $response = $http.PostAsync($uri.GetLeftPart([UriPartial]::Authority) + '/api/v1/windows-results/' + $Path, $content).GetAwaiter().GetResult()
            try {
                if (-not $response.IsSuccessStatusCode) { throw 'Central recusou o resultado; fila preservada.' }
            } finally { $response.Dispose() }
        } finally { $content.Dispose() }
    } finally { $http.Dispose(); $handler.Dispose(); $token = $null }
}

function Write-MonitorJson {
    param([string]$Path, $Value)
    $tmp = $Path + '.' + [Guid]::NewGuid().ToString() + '.tmp'
    $bytes = [Text.UTF8Encoding]::new($false).GetBytes(($Value | ConvertTo-Json -Depth 7 -Compress))
    try {
        $stream = [IO.File]::Open($tmp, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
        try { $stream.Write($bytes, 0, $bytes.Length); $stream.Flush($true) } finally { $stream.Dispose() }
        if ([IO.File]::Exists($Path)) { [IO.File]::Replace($tmp, $Path, [NullString]::Value) }
        else { [IO.File]::Move($tmp, $Path) }
    } finally { if (Test-Path -LiteralPath $tmp) { Remove-Item -LiteralPath $tmp -Force } }
}

function Read-Monitor {
    param([string]$Config)
    $path = Join-Path ([IO.Path]::GetDirectoryName([IO.Path]::GetFullPath($Config))) 'monitoring.json'
    if (-not (Test-Path -LiteralPath $path)) { return $null }
    $monitor = Get-Content -LiteralPath $path -Raw | ConvertFrom-Json
    $monitor | Add-Member NoteProperty token_file (Join-Path ([IO.Path]::GetDirectoryName([IO.Path]::GetFullPath($Config))) 'monitoring.dpapi') -Force
    return $monitor
}

function Get-MonitorZone {
    # Windows PowerShell 5.1 nao possui conversor IANA. Lista explicita do laboratorio.
    $id = [TimeZoneInfo]::Local.Id
    switch ($id) {
        'E. South America Standard Time' { return 'America/Sao_Paulo' }
        'UTC' { return 'UTC' }
        'Etc/UTC' { return 'UTC' }
        'America/Sao_Paulo' { return $id }
        default { throw 'Fuso local ainda nao homologado para monitoramento. Configure UTC ou America/Sao_Paulo no laboratorio.' }
    }
}

function Set-MonitorSchedule {
    param([string]$Config, [string]$Horario, [bool]$Paused)
    $monitor = Read-Monitor $Config
    if (-not $monitor) { return }
    $zone = Get-MonitorZone
    $lock = [IO.File]::Open((Join-Path $monitor.queue 'queue.lock'), 'OpenOrCreate', 'ReadWrite', 'None')
    try {
        $currentPath = Join-Path $monitor.queue 'schedule-current.json'
        $previous = $null
        if (Test-Path -LiteralPath $currentPath) { $previous = Get-Content -LiteralPath $currentPath -Raw | ConvertFrom-Json }
        $body = [ordered]@{
            previous_schedule_id = $(if ($previous) { $previous.schedule_id } else { $null })
            schedule_id = [Guid]::NewGuid().ToString()
            schedule = @{ kind = 'daily'; timezone = $zone; hour = [int]$Horario.Split(':')[0]; minute = [int]$Horario.Split(':')[1]; tolerance_seconds = 1800; copy_seconds = 86400 }
            paused = $Paused
        }
        # Ordem persistida, sem depender da ordenacao de UUID ou do relogio.
        $number = if ($previous) { [int]$previous.sequence + 1 } else { 1 }
        Write-MonitorJson (Join-Path $monitor.queue ('schedule-{0:D8}.json' -f $number)) $body
        Write-MonitorJson $currentPath @{ schedule_id = $body.schedule_id; schedule = $body.schedule; paused = $Paused; sequence = $number }
    } finally { $lock.Dispose() }
}

function Sync-MonitorQueue {
    param([string]$Config)
    $monitor = Read-Monitor $Config
    if (-not $monitor) { return 'Monitoramento nao configurado' }
    $lock = [IO.File]::Open((Join-Path $monitor.queue 'queue.lock'), 'OpenOrCreate', 'ReadWrite', 'None')
    try {
        $messages = @(Get-ChildItem -LiteralPath $monitor.queue -Filter 'schedule-*.json' | Where-Object Name -cmatch '^schedule-[0-9]{8}\.json$' | Sort-Object Name)
        foreach ($message in $messages) {
            Invoke-MonitorHttp $monitor 'schedule' (Get-Content -LiteralPath $message.FullName -Raw | ConvertFrom-Json)
            Remove-Item -LiteralPath $message.FullName
        }
        # Abandono confirmado por lock de execucao livre, nunca apenas por idade/PID.
        $backupLock = $null
        try { $backupLock = [IO.File]::Open((Join-Path $monitor.queue 'backup.lock'), 'OpenOrCreate', 'ReadWrite', 'None') } catch { }
        if ($backupLock) {
            try {
                foreach ($orphan in @(Get-ChildItem -LiteralPath $monitor.queue -Filter 'event-*.running')) {
                    $terminal = $orphan.FullName.Replace('.running','.json')
                    if (-not (Test-Path -LiteralPath $terminal)) {
                        $body = Get-Content -LiteralPath $orphan.FullName -Raw | ConvertFrom-Json
                        $body.finished_at = [DateTime]::UtcNow.ToString('o')
                        Write-MonitorJson $terminal $body
                    }
                    Remove-Item -LiteralPath $orphan.FullName
                }
            } finally { $backupLock.Dispose() }
        }
        $events = @(Get-ChildItem -LiteralPath $monitor.queue -Filter 'event-*.json' | Sort-Object Name | Select-Object -First 50)
        foreach ($event in $events) {
            # Resultados em andamento usam .running; somente terminais entram aqui.
            Invoke-MonitorHttp $monitor 'executions' (Get-Content -LiteralPath $event.FullName -Raw | ConvertFrom-Json)
            Remove-Item -LiteralPath $event.FullName
        }
        return 'Monitoramento sincronizado'
    } finally { $lock.Dispose() }
}

function Start-MonitorResult {
    param([string]$Config, [string]$Repository, [string]$Disparo, [string]$Started)
    $monitor = Read-Monitor $Config
    if (-not $monitor) { return $null }
    $id = [Guid]::NewGuid().ToString()
    $scheduled = ($Disparo -ceq '' -or $Disparo -ceq '$(Arg0)')
    $body = [ordered]@{ execution_id = $id; schedule_id = $null; trigger = 'manual'; occurrence = $null
        started_at = $Started; finished_at = $Started; status = 'interrupted'; result_code = 'interrupted'
        repository_id = $Repository; snapshot_id = $null }
    $current = Join-Path $monitor.queue 'schedule-current.json'
    if ($scheduled -and (Test-Path -LiteralPath $current)) {
        $schedule = Get-Content -LiteralPath $current -Raw | ConvertFrom-Json
        $sameZone = $false
        try { $sameZone = ((Get-MonitorZone) -ceq $schedule.schedule.timezone) } catch { }
        if (-not $sameZone) { Write-Warning 'Fuso local mudou; resultado nao confirma agenda. Publique a agenda novamente.' }
        if (-not $schedule.paused -and $sameZone) {
            # Agenda diaria no fuso local conferido ao salvar. Nunca usa recebimento como ocorrencia.
            $local = [DateTimeOffset]::Parse($Started).ToLocalTime()
            $due = $local.Date.AddHours([int]$schedule.schedule.hour).AddMinutes([int]$schedule.schedule.minute)
            if ($due -gt $local.DateTime) { $due = $due.AddDays(-1) }
            $body.trigger = 'scheduled'; $body.schedule_id = $schedule.schedule_id
            $body.occurrence = ([DateTimeOffset]$due).UtcDateTime.ToString('o')
        }
    }
    $path = Join-Path $monitor.queue ('event-' + $id + '.running')
    $executionLock = [IO.File]::Open((Join-Path $monitor.queue 'backup.lock'), 'OpenOrCreate', 'ReadWrite', 'None')
    try { Write-MonitorJson $path $body } catch { $executionLock.Dispose(); throw }
    return [pscustomobject]@{ Body = $body; Path = $path; Lock = $executionLock }
}

function Complete-MonitorResult {
    param($Pending, $Result)
    if (-not $Pending) { return }
    $body = $Pending.Body
    $body.finished_at = $Result.fim_utc
    $body.status = if ($Result.status -eq 'sucesso') { 'success' } elseif ($Result.codigo_restic -eq 3) { 'partial' } else { 'failed' }
    $body.result_code = if ($body.status -eq 'success') { 'ok' } elseif ($body.status -eq 'partial') { 'incomplete' } elseif ($null -eq $Result.codigo_restic) { 'validation_failed' } else { 'restic_failed' }
    $body.snapshot_id = $Result.snapshot
    try {
        Write-MonitorJson ($Pending.Path.Replace('.running','.json')) $body
        Remove-Item -LiteralPath $Pending.Path -Force
    } finally { $Pending.Lock.Dispose() }
}
