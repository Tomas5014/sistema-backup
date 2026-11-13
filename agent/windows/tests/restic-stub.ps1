# Simula restic.exe para os testes. Registra argv e ambiente, responde
# conforme arquivos em $env:RESTIC_STUB_DIR:
#   version (se presente, saida de "restic version")
#   id        ID que "cat config" devolve
#   code      codigo de saida de "backup"
#   snapshot  snapshot_id do resumo JSON (ausente = backup sem resumo)
$d = $env:RESTIC_STUB_DIR
$logArgs = Join-Path $d 'args.log'
[IO.File]::WriteAllLines((Join-Path $d 'last-args.txt'), [string[]]$args)
[IO.File]::AppendAllText($logArgs, ([string]::Join("`n", [string[]]$args) + "`n---`n"))
$senha = if ($env:RESTIC_PASSWORD) { 'DEFINIDA' } else { 'AUSENTE' }
[IO.File]::AppendAllText((Join-Path $d 'env.log'), "RESTIC_PASSWORD=$senha`n")
$global:LASTEXITCODE = 0
if ($args -contains 'version') {
    if (Test-Path (Join-Path $d 'version')) { Write-Output (Get-Content (Join-Path $d 'version') -Raw).Trim() }
    else { Write-Output 'restic 0.18.1 compiled with go1.24.5 on windows/amd64' }
}
elseif ($args -contains 'cat') {
    $id = (Get-Content (Join-Path $d 'id') -Raw).Trim()
    Write-Output ('{"version":2,"id":"' + $id + '","chunker_polynomial":"00"}')
}
elseif ($args -contains 'backup') {
    $snap = ''
    if (Test-Path (Join-Path $d 'snapshot')) { $snap = (Get-Content (Join-Path $d 'snapshot') -Raw).Trim() }
    if ($snap) {
        Write-Output ('{"message_type":"summary","files_new":3,"snapshot_id":"' + $snap + '"}')
    }
    $global:LASTEXITCODE = [int](Get-Content (Join-Path $d 'code') -Raw)
}
elseif ($args -contains 'snapshots') {
    # snaps.jsonl: uma linha JSON por snapshot (como "restic snapshots --json")
    if (Test-Path (Join-Path $d 'snaps.jsonl')) { Get-Content (Join-Path $d 'snaps.jsonl') }
}
elseif ($args -contains 'ls') {
    # ls.jsonl: nos de "restic ls --json" (type file/dir/symlink/snapshot)
    if (Test-Path (Join-Path $d 'ls.jsonl')) { Get-Content (Join-Path $d 'ls.jsonl') }
}
elseif ($args -contains 'dump') {
    # dump.bin: conteudo do arquivo "restaurado" (bytes, para Invoke-ResticDump)
    # stderr-big: despeja volume grande em stderr (pipe cheio nao pode travar)
    # dump-code:  codigo de saida apos gravar os bytes (falha apos dados)
    # dump-sleep: segundos de espera antes de responder (teste de timeout)
    if (Test-Path (Join-Path $d 'dump-sleep')) {
        Start-Sleep -Seconds ([int](Get-Content (Join-Path $d 'dump-sleep') -Raw))
    }
    # dump-stderr-open: um NETO fica com stderr aberto apos o pai sair --
    # stdout fecha (o neto fecha a copia dele), stderr fica vivo por 30s.
    if (Test-Path (Join-Path $d 'dump-stderr-open')) {
        $psi2 = [Diagnostics.ProcessStartInfo]::new()
        $psi2.FileName = (Get-Process -Id $PID).Path
        $psi2.Arguments = '-NoProfile -Command "[Console]::OpenStandardOutput().Close(); Start-Sleep 30"'
        $psi2.UseShellExecute = $false
        $null = [Diagnostics.Process]::Start($psi2)
    }
    if (Test-Path (Join-Path $d 'stderr-big')) {
        $linha = 'x' * 64
        for ($i = 0; $i -lt 6000; $i++) { [Console]::Error.WriteLine($linha) }
    }
    if (Test-Path (Join-Path $d 'dump.bin')) {
        $b = [IO.File]::ReadAllBytes((Join-Path $d 'dump.bin'))
        [Console]::OpenStandardOutput().Write($b, 0, $b.Length)
        $global:LASTEXITCODE = 0
    } else { $global:LASTEXITCODE = 1 }
    if (Test-Path (Join-Path $d 'dump-code')) {
        $global:LASTEXITCODE = [int](Get-Content (Join-Path $d 'dump-code') -Raw)
    }
}
# Em execucao como processo filho (pwsh -File via RESTIC_STUB_CHILD), o codigo
# de saida real e o do processo--exit e obrigatorio (in-process & le $LASTEXITCODE).
if ($env:RESTIC_STUB_CHILD -eq '1') { exit ([int]$global:LASTEXITCODE) }
