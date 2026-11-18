#requires -Version 5.1
$ErrorActionPreference='Stop'
$root=Join-Path ([IO.Path]::GetTempPath()) ('central-alerts-'+[Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory $root | Out-Null
$checks=0
function Assert($value,$message) { if (-not $value) { throw $message }; $script:checks++ }
try {
    . (Join-Path $PSScriptRoot '../Backup-MacipCliente.ps1')
    $config=Join-Path $root 'backup.json'
    [IO.File]::WriteAllText($config,'{}')
    Assert ((Sync-MonitorQueue $config) -eq 'Monitoramento nao configurado') 'perfil sem vinculo continua sem monitoramento'
    $queue=Join-Path $root 'queue'; New-Item -ItemType Directory $queue | Out-Null
    $m=@{central='https://central.lab';installation_id=[Guid]::NewGuid().ToString();queue=$queue}
    Write-MonitorJson (Join-Path $root 'monitoring.json') $m
    $script:sent=@(); $script:offline=$true
    function Invoke-MonitorHttp($Monitor,$Path,$Body) {
        Assert ($Monitor.central -ceq 'https://central.lab') 'central vinculada preservada'
        if ($script:offline) { throw 'central indisponivel simulada' }
        $script:sent += [pscustomobject]@{ Path=$Path; Body=$Body }
        if ($script:lost) { $script:lost=$false; throw 'resposta perdida depois de aceitar' }
    }
    Set-MonitorSchedule $config '10:00' $false
    $first=(Get-Content (Join-Path $queue 'schedule-current.json') -Raw | ConvertFrom-Json).schedule_id
    Set-MonitorSchedule $config '10:00' $true
    try { Sync-MonitorQueue $config; throw 'aceitou offline' } catch { Assert ($_.Exception.Message -like 'central indisponivel*') 'offline sem apagar agenda' }
    Assert (@(Get-ChildItem $queue -Filter 'schedule-*.json' | Where-Object Name -cmatch '^schedule-[0-9]{8}\.json$').Count -eq 2) 'cadeia de agenda preservada offline'
    $start=[DateTime]::UtcNow.AddMinutes(-1).ToString('o')
    $pending=Start-MonitorResult $config ('a'*64) 'manual' $start
    Complete-MonitorResult $pending @{fim_utc=[DateTime]::UtcNow.ToString('o');status='sucesso';codigo_restic=0;snapshot=('b'*64)}
    $file=@(Get-ChildItem $queue -Filter 'event-*.json')[0]
    $event=Get-Content $file.FullName -Raw | ConvertFrom-Json
    Assert ($event.trigger -eq 'manual' -and -not $event.occurrence) 'manual nao satisfaz agenda'
    Assert ($event.PSObject.Properties.Name -notcontains 'origem') 'nenhum nome de arquivo enviado'
    Assert ($event.PSObject.Properties.Name -notcontains 'senha') 'nenhum segredo no resultado'
    Assert ($event.status -eq 'success') 'sucesso preservado offline'
    $script:offline=$false; $script:lost=$true
    try { Sync-MonitorQueue $config } catch { }
    Assert (@(Get-ChildItem $queue -Filter 'schedule-*.json' | Where-Object Name -cmatch '^schedule-[0-9]{8}\.json$').Count -eq 2) 'perda de resposta conserva mensagem'
    [void](Sync-MonitorQueue $config)
    Assert (@(Get-ChildItem $queue -Filter 'event-*.json').Count -eq 0) 'remove apenas depois da confirmacao'
    $updates=@($script:sent | Where-Object Path -eq 'schedule')
    Assert ($updates[0].Body.schedule_id -eq $updates[1].Body.schedule_id) 'reenvio tem identidade estavel'
    Assert ($updates[-1].Body.previous_schedule_id -eq $first) 'cadeia CAS preservada'
    Set-MonitorSchedule $config '10:00' $false
    $scheduled=Start-MonitorResult $config ('a'*64) '' $start
    Assert ($scheduled.Body.trigger -eq 'scheduled' -and $scheduled.Body.occurrence) 'disparo agendado tem ocorrencia'
    [void](Sync-MonitorQueue $config)
    Assert (Test-Path $scheduled.Path) 'nao converte processo com lock ativo em interrompido'
    $scheduled.Lock.Dispose() # simula processo abandonado, sem completar
    [void](Sync-MonitorQueue $config)
    $interrupted=@($script:sent | Where-Object {$_.Path -eq 'executions' -and $_.Body.status -eq 'interrupted'})
    Assert ($interrupted.Count -eq 1) 'abandono com lock livre envia interrupted'
    Assert (-not (Test-Path $scheduled.Path)) 'abandono nao repete efeito'
    $literal=Start-MonitorResult $config ('a'*64) '$(Arg0)' $start
    Assert ($literal.Body.trigger -eq 'scheduled') 'marcador literal do Agendador tambem e reconhecido'
    Complete-MonitorResult $literal @{fim_utc=[DateTime]::UtcNow.ToString('o');status='sucesso';codigo_restic=0;snapshot=('b'*64)}
    [void](Sync-MonitorQueue $config)
    $failed=Start-MonitorResult $config ('a'*64) 'manual' $start
    Complete-MonitorResult $failed @{fim_utc=[DateTime]::UtcNow.ToString('o');status='falha';codigo_restic=3;snapshot=$null}
    $script:offline=$true
    try { Sync-MonitorQueue $config } catch { }
    $queued=Get-Content (@(Get-ChildItem $queue -Filter 'event-*.json')[0].FullName) -Raw | ConvertFrom-Json
    Assert ($queued.status -eq 'partial' -and $queued.result_code -eq 'incomplete') 'incompleto identificado'
    # Entrada publica real em processo separado, sem GUI; Restic simulado por arquivo de teste.
    $cli=Join-Path $root 'cli'; New-Item -ItemType Directory $cli | Out-Null
    $source=Join-Path $cli 'origem'; New-Item -ItemType Directory $source | Out-Null
    $stub=Join-Path $cli 'stub'; New-Item -ItemType Directory $stub | Out-Null
    [IO.File]::WriteAllText((Join-Path $stub 'id'),('a'*64))
    [IO.File]::WriteAllText((Join-Path $stub 'code'),'0')
    [IO.File]::WriteAllText((Join-Path $stub 'snapshot'),('b'*64))
    $env:RESTIC_STUB_DIR=$stub
    $script:ResticExeNome='restic'
    $key=Join-Path $cli 'key'; [IO.File]::WriteAllText($key,'synthetic-key')
    $hosts=Join-Path $cli 'known_hosts'; [IO.File]::WriteAllText($hosts,'127.0.0.1 ssh-ed25519 AAAATEST')
    $cfg=Join-Path $cli 'backup.json'
    Write-MonitorJson $cfg @{origem=$source;exclusoes=@('*.tmp');repositorio='sftp://lab@127.0.0.1:2222//repository';horario='10:00';repositorio_id=('a'*64);identidade_ssh=$key;known_hosts=$hosts}
    $state=Join-Path $cli 'estado'
    $restic=Join-Path $cli 'restic'
    [IO.File]::WriteAllText($restic, @'
#!/usr/bin/python3
import sys,json
if 'version' in sys.argv: print('restic 0.18.1 fixture')
elif 'cat' in sys.argv: print(json.dumps({'id':'a'*64}))
else: print(json.dumps({'message_type':'summary','snapshot_id':'b'*64}))
'@)
    & chmod +x $restic
    Invoke-AgentePreparar $cfg $state $restic (ConvertTo-SecureString 'synthetic-password' -AsPlainText -Force) | Out-Null
    # Linux SecureString NAO e DPAPI Windows. Nao usar este fixture para dados reais.
    $queue2=Join-Path $cli 'queue'; New-Item -ItemType Directory $queue2 | Out-Null
    Write-MonitorJson (Join-Path $cli 'monitoring.json') @{central='https://127.0.0.1:1';installation_id=[Guid]::NewGuid().ToString();queue=$queue2}
    Protect-ResticPassword (ConvertTo-SecureString 'synthetic-token' -AsPlainText -Force) (Join-Path $cli 'monitoring.dpapi')
    Set-MonitorSchedule $cfg '10:00' $false
    # O processo filho usa o mesmo agente publico, com um shim de teste apenas para o nome Restic.
    $runner=(Resolve-Path (Join-Path $PSScriptRoot '../Backup-MacipCliente.ps1')).Path
    $pwsh=(Get-Process -Id $PID).Path
    $output=& $pwsh -NoProfile -File $runner -Modo Backup -Config $cfg -DiretorioEstado $state -Disparo '' 2>&1
    Assert ($LASTEXITCODE -eq 0) ('entrada publica preserva sucesso offline: '+($output -join ' '))
    $record=Get-Content (@(Get-ChildItem $queue2 -Filter 'event-*.json')[0].FullName) -Raw | ConvertFrom-Json
    Assert ($record.status -eq 'success' -and $record.trigger -eq 'scheduled') ('processo sem GUI registra disparo agendado: '+($record | ConvertTo-Json -Compress))
    $prior=$record.execution_id
    $output=& $pwsh -NoProfile -File $runner -Modo Sincronizar -Config $cfg 2>&1
    Assert ($LASTEXITCODE -ne 0) 'reenvio publico offline recusa sem perder resultado'
    $record=Get-Content (@(Get-ChildItem $queue2 -Filter 'event-*.json')[0].FullName) -Raw | ConvertFrom-Json
    Assert ($record.execution_id -eq $prior) 'reenvio publico nao repete backup'

    Write-Host "$checks verificacoes aprovadas; HTTP e DPAPI simulados; arquivos e locks reais"
} finally { Remove-Item -LiteralPath $root -Recurse -Force }
