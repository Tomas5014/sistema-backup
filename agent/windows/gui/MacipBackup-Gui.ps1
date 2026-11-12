#requires -Version 5.1
# MacipBackup-Gui.ps1 - interface grafica do agente Windows da MAC IP (piloto).
# WinForms sobre Windows PowerShell 5.1: sem dependencia nova — a mesma
# tecnologia do agente. Todas as strings em ASCII: o arquivo roda em PS 5.1
# sem BOM como o agente.
#
# Fluxo pensado para o tecnico: Ativar -> Configurar -> Operar -> Restaurar.
# Nenhuma etapa pede terminal, edicao de JSON ou o Agendador de Tarefas.
# O Agendador continua por tras: a tarefa MACIP-Backup roda com o app fechado,
# sem sessao aberta e apos reinicio.
#
# Reaproveitamento: o agente Backup-MacipCliente.ps1 e dot-sourced — validacao
# de backup.json, ssh_config restrito, Confirm-Destino, Invoke-Restic e
# Invoke-AgentePreparar sao os mesmos ja homologados em laboratorio.
#
# Modelo de salvamento em 2 fases (rollback seguro):
#   Build-Configuracao  - monta um conjunto NOVO e imutavel (config-N/ com
#                         identidade + backup.json; estado-N quando o perfil
#                         muda) sem tocar nada usado pela tarefa ativa.
#   Publish-Configuracao - registra a tarefa e so depois publica gui.json.
#                         Qualquer falha desfaz o conjunto novo e restaura
#                         a definicao anterior da tarefa; o conjunto anterior
#                         (config + identidade + estado DPAPI + tarefa)
#                         continua junto e funcional.
[CmdletBinding()]
param(
    # Pasta base de dados do app (config + estado). Padrao: ProgramData\MacipBackup.
    # Precisa ser um caminho SEM espacos (limite do sftp.command do Restic).
    [string]$BaseDir,
    # Caminho do agente (instalador coloca ao lado; override so para teste)
    [string]$AgentePath
)

$ErrorActionPreference = 'Stop'

if (-not $BaseDir) { $BaseDir = Join-Path $env:ProgramData 'MacipBackup' }
if ($BaseDir -match '\s') {
    throw "BaseDir nao pode conter espacos (limite do sftp.command): '$BaseDir'"
}
if (-not $AgentePath) { $AgentePath = Join-Path $PSScriptRoot 'Backup-MacipCliente.ps1' }
if (-not (Test-Path -LiteralPath $AgentePath -PathType Leaf)) {
    throw "Agente nao encontrado ao lado da interface: $AgentePath"
}

$script:BaseDir    = [IO.Path]::GetFullPath($BaseDir)
$script:GuiMeta    = Join-Path $script:BaseDir 'gui.json'
$script:TaskName   = 'MACIP-Backup'
$script:AgentePath = $AgentePath
$script:LogonPassword = 1   # TASK_LOGON_PASSWORD = 1 (o 2 e S4U — sem DPAPI/rede)
$script:TaskMarker    = 'MACIP Backup'
$script:Backend       = $null   # injetavel nos testes; padrao COM no Windows

. $AgentePath   # reusa Read-AgenteConfig, Write-SshConfig, Invoke-Restic, etc.

# ---------- dados do app ----------

function Read-GuiMeta {
    if (Test-Path -LiteralPath $script:GuiMeta -PathType Leaf) {
        try { return (Get-Content -LiteralPath $script:GuiMeta -Raw | ConvertFrom-Json) }
        catch { }
    }
    return [pscustomobject]@{ estado_atual = $null; config_atual = $null; conta_tarefa = $null }
}

function Save-GuiMeta {
    param([pscustomobject]$Meta)
    if (-not (Test-Path -LiteralPath $script:BaseDir)) { New-Item -ItemType Directory -Path $script:BaseDir | Out-Null }
    $tmp = "$($script:GuiMeta).tmp"
    $Meta | ConvertTo-Json | Set-Content -LiteralPath $tmp -Encoding UTF8
    Move-Item -LiteralPath $tmp -Destination $script:GuiMeta -Force
}

# ---------- perfil (dados, sem segredos) ----------

$script:PerfilCampos = @('nome', 'repositorio', 'repositorio_id', 'known_hosts', 'horario_sugerido')
$script:PadroesSegredo = '(?i)(senha|password|private|secret|-----BEGIN|identidade)'

function Read-Perfil {
    param([string]$Caminho)
    if (-not (Test-Path -LiteralPath $Caminho -PathType Leaf)) {
        throw "Perfil nao encontrado: $Caminho"
    }
    $abs = (Resolve-Path -LiteralPath $Caminho).ProviderPath
    try { $p = Get-Content -LiteralPath $abs -Raw -Encoding UTF8 | ConvertFrom-Json }
    catch { throw "Perfil nao e um JSON valido: $($_.Exception.Message)" }
    if ($null -eq $p -or $p -isnot [pscustomobject]) { throw 'Perfil deve ser um objeto JSON.' }
    foreach ($c in 'repositorio', 'repositorio_id', 'known_hosts') {
        if (-not $p.PSObject.Properties[$c] -or -not $p.$c) {
            throw "Perfil incompleto: campo obrigatorio ausente ou vazio: $c"
        }
    }
    foreach ($c in @($p.PSObject.Properties.Name)) {
        if ($script:PerfilCampos -cnotcontains $c) {
            throw "Perfil com campo desconhecido '$c' - perfis sao gerados pela MAC IP e nao devem ser editados."
        }
        if ([string]$p.$c -match $script:PadroesSegredo) {
            throw "Perfil contem dado que parece segredo no campo '$c'. Senha e chave privada nunca viajam no perfil."
        }
    }
    $repo = [string]$p.repositorio
    $m1 = [regex]::Match($repo, '^sftp:([^@\s/]+)@([^:/\s]+):(/{1,2}\S+)$')
    $m2 = [regex]::Match($repo, '^sftp://([^@\s/]+)@([^:/\s]+)(?::(\d{1,5}))?//(\S+)$')
    if (-not $m1.Success -and -not ($m2.Success -and $m2.Groups[4].Success)) {
        throw "Perfil com repositorio fora das formas canonicas: '$repo'"
    }
    if ([string]$p.repositorio_id -cnotmatch '^[0-9a-f]{64}$') {
        throw 'Perfil com repositorio_id invalido (esperado ID Restic de 64 hex).'
    }
    $kh = @($p.known_hosts) -join "`n"
    if ($kh -notmatch '^\S+\s+(ssh-|ecdsa-)') {
        throw 'Perfil com known_hosts invalido (esperado "<host> <tipo> <chave>").'
    }
    $r = [ordered]@{
        caminho = $abs
        nome = [string]$p.nome
        repositorio = $repo
        repositorio_id = [string]$p.repositorio_id
        known_hosts = $kh
        horario_sugerido = [string]$p.horario_sugerido
    }
    if ($r.horario_sugerido -and $r.horario_sugerido -cnotmatch '^([01][0-9]|2[0-3]):[0-5][0-9]$') {
        throw "horario_sugerido invalido no perfil: '$($r.horario_sugerido)'"
    }
    return [pscustomobject]$r
}

function New-BackupJsonObj {
    param([pscustomobject]$Perfil, [string]$Origem, [object[]]$Exclusoes,
          [string]$Horario, [string]$IdentidadeSsh, [string]$KnownHostsFile)
    return [ordered]@{
        origem         = $Origem
        exclusoes      = @($Exclusoes)
        repositorio    = $Perfil.repositorio
        horario        = $Horario
        repositorio_id = $Perfil.repositorio_id
        identidade_ssh = $IdentidadeSsh
        known_hosts    = $KnownHostsFile
    }
}

# ---------- tarefa: backend injetavel (COM no Windows) ----------

function Get-Backend {
    if (-not $script:Backend) {
        if (-not (Test-IsWindows)) { throw 'Agendador de Tarefas disponivel apenas no Windows.' }
        $script:Backend = New-BackendCom
    }
    return $script:Backend
}

function New-BackendCom {
    $b = [pscustomobject]@{ svc = $null }
    $b | Add-Member ScriptMethod Conectar {
        if (-not $this.svc) {
            $this.svc = New-Object -ComObject Schedule.Service
            $this.svc.Connect()
        }
    }
    $b | Add-Member ScriptMethod Get {
        $this.Conectar()
        try { return $this.svc.GetFolder('\').GetTask($script:TaskName) } catch { return $null }
    }
    $b | Add-Member ScriptMethod NewDef {
        param($TarefaExistente)
        $this.Conectar()
        if ($TarefaExistente) { return $TarefaExistente.Definition }
        return $this.svc.NewTask(0)
    }
    $b | Add-Member ScriptMethod Registrar {
        param($Def, $Conta, $SenhaConta)
        $this.Conectar()
        # 6 = TASK_CREATE_OR_UPDATE | TASK_LOGON_PASSWORD (1): conta com
        # senha — token completo (rede + DPAPI) mesmo sem sessao aberta.
        $this.svc.GetFolder('\').RegisterTaskDefinition(
            $script:TaskName, $Def, 6, $Conta, $SenhaConta, $script:LogonPassword, $null) | Out-Null
    }
    $b | Add-Member ScriptMethod RegistrarXml {
        param([string]$Xml, $Conta, $SenhaConta)
        $this.Conectar()
        # Rollback: repoe a definicao anterior exatamente como estava (XML
        # serializado ANTES de qualquer alteracao — um objeto Definition
        # reusado poderia ja vir modificado).
        $this.svc.GetFolder('\').RegisterTask(
            $script:TaskName, $Xml, 6, $Conta, $SenhaConta, $script:LogonPassword, $null) | Out-Null
    }
    $b | Add-Member ScriptMethod XmlAtual {
        $t = $this.Get(); if ($t) { return [string]$t.Xml } else { return $null }
    }
    return $b
}

function Test-TarefaDoApp {
    # Uma tarefa MACIP-Backup so e "nossa" quando traz a marca do aplicativo
    # na descricao. Tarefa homonima estranha -> recusar qualquer alteracao.
    param($Tarefa)
    if (-not $Tarefa) { return $false }
    try {
        $desc = [string]$Tarefa.Definition.RegistrationInfo.Description
        return ($desc -like ('*' + $script:TaskMarker + '*'))
    } catch { return $false }
}

function Get-TaskActionArgs {
    param([string]$Agente, [string]$Config, [string]$Estado)
    return '-NoProfile -NonInteractive -File "{0}" -Modo Backup -Config "{1}" -DiretorioEstado "{2}"' -f $Agente, $Config, $Estado
}

function Write-TaskDef {
    # Preenche a definicao: marca do app, acao do agente e disparo diario.
    param($Def, [string]$Horario, [string]$Config, [string]$Estado, [string]$Conta)
    if ($Horario -cnotmatch '^([01][0-9]|2[0-3]):[0-5][0-9]$') { throw "Horario invalido: $Horario" }
    $Def.RegistrationInfo.Description = "$($script:TaskMarker) - agente de backup do piloto"
    $Def.Settings.Enabled = $true
    $Def.Settings.MultipleInstances = 2            # IGNORE_NEW: nunca duas execucoes
    $Def.Settings.DisallowStartIfOnBatteries = $false
    $Def.Settings.StopIfGoingOnBatteries = $false
    $Def.Settings.ExecutionTimeLimit = 'PT0S'      # sem limite de duracao
    $Def.Triggers.Clear()
    $trg = $Def.Triggers.Create(2)                 # DAILY
    $hoje = Get-Date
    $trg.StartBoundary = (Get-Date -Year $hoje.Year -Month $hoje.Month -Day $hoje.Day `
        -Hour ([int]$Horario.Substring(0,2)) -Minute ([int]$Horario.Substring(3,2)) -Second 0).ToString('s')
    $trg.DaysInterval = 1
    $trg.Enabled = $true
    $ps = if (Test-IsWindows) { Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe' }
          else { 'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe' }
    while ($Def.Actions.Count -gt 0) { $Def.Actions.Remove(1) }   # IActionCollection nao tem Clear()
    $act = $Def.Actions.Create(0)
    $act.Path = $ps
    $act.Arguments = Get-TaskActionArgs -Agente $script:AgentePath -Config $Config -Estado $Estado
    $Def.Principal.UserId = $Conta
    $Def.Principal.LogonType = $script:LogonPassword   # TASK_LOGON_PASSWORD = 1
    $Def.Principal.RunLevel = 0                        # LIMITADO: sem elevacao
}

function Register-TarefaMacip {
    # Unico ponto que cria/altera a tarefa. Idempotente (CreateOrUpdate);
    # recusa tarefa homonima que nao seja do app; devolve a def anterior
    # para rollback.
    param([string]$Horario, [string]$Conta, [string]$SenhaConta, [string]$Config, [string]$Estado)
    $b = Get-Backend
    $t = $b.Get()
    if ($t -and -not (Test-TarefaDoApp -Tarefa $t)) {
        throw ("Ja existe uma tarefa '$($script:TaskName)' que NAO pertence ao MAC IP Backup. " +
            'Renomeie ou remova a tarefa estranha - o aplicativo nao a altera.')
    }
    $xmlAnterior = $b.XmlAtual()                 # copia serializada — imune a mutacao da def
    $def = $b.NewDef($t)
    Write-TaskDef -Def $def -Horario $Horario -Config $Config -Estado $Estado -Conta $Conta
    $b.Registrar($def, $Conta, $SenhaConta)      # erro de permissao/credencial sobe para a tela
    return $xmlAnterior
}

function Get-TaskArg {
    param([string]$Nome)
    $t = (Get-Backend).Get()
    if (-not $t) { return $null }
    try {
        $args = [string]$t.Definition.Actions.Item(1).Arguments
        if ($args -match ('-' + $Nome + '\s+"([^"]+)"')) { return $Matches[1] }
        if ($args -match ('-' + $Nome + '\s+(\S+)')) { return $Matches[1] }
    } catch { }
    return $null
}

function Get-EstadoAtual {
    # Fonte primaria: o que a TAREFA usa — se o meta ficou para tras numa
    # falha de publicacao, a tela acompanha a realidade do Agendador.
    $e = Get-TaskArg -Nome 'DiretorioEstado'
    if ($e) { return $e }
    $meta = Read-GuiMeta
    return $meta.estado_atual
}

function Get-ConfigAtual {
    $c = Get-TaskArg -Nome 'Config'
    if ($c) { return $c }
    $meta = Read-GuiMeta
    if ($meta.config_atual) { return $meta.config_atual }
    return $null
}

function Set-TarefaHabilitada {
    param([bool]$On)
    $t = (Get-Backend).Get()
    if (-not $t) { throw "Tarefa $($script:TaskName) nao existe - salve a configuracao primeiro." }
    if (-not (Test-TarefaDoApp -Tarefa $t)) { throw "Tarefa $($script:TaskName) nao pertence ao MAC IP Backup - recusado." }
    $t.Enabled = $On
}

function Start-TarefaAgora {
    # Executar agora = a MESMA tarefa agendada, na MESMA conta registrada.
    $t = (Get-Backend).Get()
    if (-not $t) { throw "Tarefa $($script:TaskName) nao existe - salve a configuracao primeiro." }
    if (-not (Test-TarefaDoApp -Tarefa $t)) { throw "Tarefa $($script:TaskName) nao pertence ao MAC IP Backup - recusado." }
    if ($t.State -eq 4) { throw 'Ja ha uma execucao em andamento.' }   # TASK_STATE_RUNNING
    $meta = Read-GuiMeta
    if ($meta.conta_tarefa -and $t.Principal.UserId) {
        $dom = [string]$t.Principal.UserId
        if ($dom -and ($dom -split '[\\/]')[-1] -cne ($meta.conta_tarefa -split '[\\/]')[-1]) {
            throw "Conta da tarefa ($dom) difere da conta do preparo ($($meta.conta_tarefa)) - recusado."
        }
    }
    $t.Run($null) | Out-Null
}

function Get-InfoTarefa {
    $t = (Get-Backend).Get()
    if (-not $t) { return $null }
    return [pscustomobject]@{
        Habilitada = [bool]$t.Enabled
        Estado     = [int]$t.State          # 4 = em execucao
        Ultima     = [string]$t.LastRunTime
        Proxima    = [string]$t.NextRunTime
        Codigo     = [int]$t.LastTaskResult
        Conta      = [string]$t.Principal.UserId
        DoApp      = (Test-TarefaDoApp -Tarefa $t)
    }
}

# ---------- ultimo resultado / classificacao na tela ----------

function Read-UltimoResultado {
    param([string]$Estado)
    $f = Join-Path $Estado 'ultimo-resultado.json'
    if (-not (Test-Path -LiteralPath $f -PathType Leaf)) { return $null }
    try { return (Get-Content -LiteralPath $f -Raw | ConvertFrom-Json) } catch { return $null }
}

function Classify-Resultado {
    # Nunca trata resultado antigo como prova da execucao atual: so e "novo"
    # quando inicio_utc pertence ao disparo informado (LastRunTime da tarefa).
    param($Res, $InfoTarefa)
    if ($InfoTarefa -and $InfoTarefa.Estado -eq 4) { return 'em_execucao' }
    if (-not $Res) { return 'nunca_executou' }
    $novo = $true
    if ($InfoTarefa -and $InfoTarefa.Ultima) {
        try {
            $ultima = [DateTimeOffset]::Parse($InfoTarefa.Ultima)
            $inicio = [DateTimeOffset]::Parse([string]$Res.inicio_utc)
            if ($inicio -lt $ultima.AddMinutes(-2)) { $novo = $false }  # resultado anterior ao ultimo disparo
        } catch { }
    }
    if (-not $novo) { return 'sem_resultado_novo' }
    if ([string]$Res.status -eq 'sucesso') { return 'sucesso' }
    if ($null -ne $Res.codigo_restic -and [int]$Res.codigo_restic -eq 3) { return 'incompleto' }
    return 'falha'
}

$script:RotulosStatus = @{
    sucesso            = 'SUCESSO - backup concluido neste disparo'
    falha              = 'FALHA - execucao terminou com erro'
    incompleto         = 'INCOMPLETO - Restic codigo 3 (nem todo arquivo lido)'
    em_execucao        = 'EM EXECUCAO - backup rodando agora'
    sem_resultado_novo = 'SEM RESULTADO NOVO - o ultimo resultado e de um disparo anterior'
    nunca_executou     = 'NUNCA EXECUTOU - ainda nao ha resultado registrado'
    sem_tarefa         = 'SEM TAREFA - configure e salve primeiro'
    tarefa_estranha    = 'TAREFA ESTRANHA - existe MACIP-Backup que nao e do aplicativo'
}

# ---------- restauracao ----------

function Get-CfgOperacional {
    param([string]$Estado, [string]$Config)
    $cfg = Read-AgenteConfig -Caminho $Config
    $cfg | Add-Member -NotePropertyName ssh_exe -NotePropertyValue (Get-SshExe) -Force
    $cfg | Add-Member -NotePropertyName ssh_config -NotePropertyValue ((Join-Path $Estado 'ssh_config').Replace('\', '/')) -Force
    return $cfg
}

function Confirm-DestinoOperacional {
    # Antes de listar ou restaurar: confere que o destino responde e que o
    # ID do repositorio e o aprovado no preparo.
    param([pscustomobject]$Cfg, [string]$Estado)
    return (Confirm-Destino -Cfg $Cfg -Estado $Estado)
}

function Get-Snapshots {
    param([string]$Estado, [string]$Config)
    $cfg = Get-CfgOperacional -Estado $Estado -Config $Config
    [void](Confirm-DestinoOperacional -Cfg $cfg -Estado $Estado)
    $r = Invoke-Restic -Cfg $cfg -Estado $Estado -Comando @('snapshots', '--json')
    if ($r.Codigo -ne 0) { throw "Falha ao listar pontos de restauracao (restic $($r.Codigo))." }
    $snaps = @($r.Linhas | ForEach-Object { try { $_ | ConvertFrom-Json } catch { } } |
        Where-Object { $_.id } | ForEach-Object { $_ })
    return $snaps
}

function Get-ArquivosSnapshot {
    # restic ls --json: um objeto por no. Retorna SOMENTE arquivos regulares
    # (type == 'file'), com o caminho exato do snapshot — nada de texto livre.
    param([string]$Estado, [string]$Config, [string]$Snapshot)
    $cfg = Get-CfgOperacional -Estado $Estado -Config $Config
    [void](Confirm-DestinoOperacional -Cfg $cfg -Estado $Estado)
    $r = Invoke-Restic -Cfg $cfg -Estado $Estado -Comando @('ls', '--json', $Snapshot)
    if ($r.Codigo -ne 0) { throw "Falha ao listar arquivos do ponto (restic $($r.Codigo))." }
    $arquivos = @()
    foreach ($linha in $r.Linhas) {
        try { $no = $linha | ConvertFrom-Json } catch { continue }
        if ($no.type -ceq 'file' -and $no.path) { $arquivos += [string]$no.path }
    }
    return $arquivos
}

function Resolve-LinkPath {
    # Resolve links simbolicos/junctions: um destino "equivalente" por link
    # conta como a propria origem.
    param([string]$Caminho)
    $atual = $Caminho
    for ($i = 0; $i -lt 8; $i++) {
        if (-not (Test-Path -LiteralPath $atual)) { break }
        try { $item = Get-Item -LiteralPath $atual -Force } catch { break }
        if (-not $item.LinkType -or -not $item.Target) { break }
        $alvo = $item.Target
        if ($alvo -is [array]) { $alvo = $alvo[0] }
        if (-not [IO.Path]::IsPathRooted($alvo)) {
            $alvo = Join-Path (Split-Path -LiteralPath $atual -Parent) $alvo
        }
        $atual = [IO.Path]::GetFullPath($alvo)
    }
    return [IO.Path]::GetFullPath($atual)
}

function Get-NomeArquivoRestic {
    # Folha do caminho dentro do snapshot ('/D/pasta/arq.txt' -> 'arq.txt').
    param([string]$PathRestic)
    return ($PathRestic -split '[\\/]')[-1]
}

function Invoke-ResticDump {
    # 'restic dump <snap> <caminho>' escreve o conteudo do ARQUIVO EXATO no
    # stdout — sem glob: um '*' no nome nao arrasta outros arquivos.
    # stdout vai direto para o destino via stream (nao passa por string do
    # PS — preserva binario).
    param([pscustomobject]$Cfg, [string]$Estado, [string]$Snapshot, [string]$Arquivo, [string]$Destino)
    $exe = Join-Path $Estado $script:ResticExeNome
    $senha = Read-ResticPassword -Caminho (Join-Path $Estado 'restic.dpapi')
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($senha)
    $alvo = Join-Path $Destino (Get-NomeArquivoRestic -PathRestic $Arquivo)
    try {
        $psi = [Diagnostics.ProcessStartInfo]::new()
        $sftpCmd = 'sftp.command={0} -F {1} -s {2} sftp' -f $Cfg.ssh_exe, $Cfg.ssh_config, $Cfg.sftp_host
        $resticArgs = '--repo "{0}" --cache-dir "{1}" -o "{2}" dump {3} "{4}"' -f `
            $Cfg.repositorio, (Join-Path $Estado 'cache'), $sftpCmd, $Snapshot, $Arquivo
        if (Test-IsWindows) {
            $psi.FileName = $exe
            $psi.Arguments = $resticArgs
        } else {
            # Em Windows o restic.exe e nativo. Fora do Windows (so em teste) o
            # "exe" e um script ps1 — roda pelo interpretador que hospeda a GUI.
            $psi.FileName = (Get-Process -Id $PID).Path
            $psi.Arguments = '-NoProfile -File "{0}" {1}' -f $exe, $resticArgs
        }
        $psi.UseShellExecute = $false
        $psi.RedirectStandardOutput = $true
        $psi.RedirectStandardError = $true
        $psi.EnvironmentVariables['RESTIC_PASSWORD'] = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
        $psi.EnvironmentVariables.Remove('RESTIC_PASSWORD_FILE') | Out-Null
        $psi.EnvironmentVariables.Remove('RESTIC_PASSWORD_COMMAND') | Out-Null
        $p = [Diagnostics.Process]::Start($psi)
        $fs = [IO.File]::Create($alvo)
        try { $p.StandardOutput.BaseStream.CopyTo($fs) } finally { $fs.Close() }
        $p.WaitForExit()
        $err = $p.StandardError.ReadToEnd()
        $codigo = $p.ExitCode
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
        if ($senha) { $senha.Dispose() }
    }
    if ($codigo -ne 0) {
        Remove-Item -LiteralPath $alvo -Force -ErrorAction SilentlyContinue
        throw "Falha na restauracao (restic $codigo): $err"
    }
    return $alvo
}

function Restore-Arquivo {
    # Piloto: restaura SOMENTE o arquivo escolhido, em pasta nova ou vazia,
    # fora da arvore protegida - inclusive quando a equivalencia vem de
    # link/junction. Sucesso exige o arquivo esperado no destino.
    param([string]$Estado, [string]$Config, [string]$Snapshot, [string]$Arquivo,
          [string]$Destino, [string]$Origem)
    if (-not $Destino) { throw 'Escolha a pasta de destino da restauracao.' }
    if (-not $Arquivo) { throw 'Escolha o arquivo a restaurar na lista.' }
    $destAbs = [IO.Path]::GetFullPath($Destino)
    $origAbs = if ($Origem) { [IO.Path]::GetFullPath($Origem) } else { $null }
    $destResolvido = Resolve-LinkPath -Caminho $destAbs
    $origResolvido = if ($origAbs) { Resolve-LinkPath -Caminho $origAbs } else { $null }
    if ($origResolvido -and $destResolvido.TrimEnd('\','/') -ieq $origResolvido.TrimEnd('\','/')) {
        throw 'A restauracao do piloto nao pode ser feita sobre a pasta protegida (nem por link/junction).'
    }
    if ($origResolvido -and $destResolvido.StartsWith($origResolvido.TrimEnd('\','/') + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'A pasta de destino esta DENTRO da pasta protegida - escolha outra.'
    }
    if (Test-Path -LiteralPath $destAbs) {
        if ((Get-ChildItem -LiteralPath $destAbs -Force | Measure-Object).Count -gt 0) {
            throw "Destino precisa estar vazio (restauracao nunca sobrescreve): $destAbs"
        }
    } else { New-Item -ItemType Directory -Path $destAbs -Force | Out-Null }

    $cfg = Get-CfgOperacional -Estado $Estado -Config $Config
    [void](Confirm-DestinoOperacional -Cfg $cfg -Estado $Estado)
    $alvo = Invoke-ResticDump -Cfg $cfg -Estado $Estado -Snapshot $Snapshot -Arquivo $Arquivo -Destino $destAbs
    if (-not (Test-Path -LiteralPath $alvo -PathType Leaf)) {
        throw "Restauracao falhou: arquivo esperado ausente no destino ($alvo)."
    }
    return $alvo
}

# ---------- pre-requisitos ----------

function Get-PreRequisitos {
    $falta = @()
    if (Test-IsWindows) {
        $ssh = Join-Path $env:WINDIR 'System32\OpenSSH\ssh.exe'
        if (-not (Test-Path -LiteralPath $ssh -PathType Leaf)) {
            $falta += "Cliente OpenSSH ausente ($ssh). Ative em Configuracoes > Recursos opcionais > Cliente OpenSSH."
        }
    }
    $restic = Join-Path $script:BaseDir $script:ResticExeNome
    if (-not (Test-Path -LiteralPath $restic -PathType Leaf)) {
        $restic = Join-Path $PSScriptRoot $script:ResticExeNome
    }
    if (-not (Test-Path -LiteralPath $restic -PathType Leaf)) {
        $falta += "$($script:ResticExeNome) ausente - o instalador da MAC IP coloca o binario aprovado na pasta do aplicativo."
    }
    return [pscustomobject]@{ Ok = ($falta.Count -eq 0); Faltas = $falta; ResticExe = $restic }
}

# ---------- ativacao / salvamento em 2 fases ----------

function Test-ConexaoDestino {
    # Prova a identidade do servidor e o ID do repositorio SEM gravar estado
    # do app: pasta temporaria descartavel, com ACL restrita a conta atual.
    param([pscustomobject]$Perfil, [string]$ChavePath, [securestring]$SenhaRestic)
    if (-not (Test-Path -LiteralPath $ChavePath -PathType Leaf)) { throw "Chave privada nao encontrada: $ChavePath" }
    $tmp = Join-Path ([IO.Path]::GetTempPath()) ('macip-teste-' + [Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $tmp | Out-Null
    try {
        Protect-PastaEstado -Estado $tmp     # ACL: so a conta atual le a chave copiada
        $kh = Join-Path $tmp 'known_hosts'
        [IO.File]::WriteAllText($kh, $Perfil.known_hosts + "`n")
        $chave = Join-Path $tmp 'id'
        Copy-Item -LiteralPath $ChavePath -Destination $chave
        if (-not (Test-IsWindows)) { & chmod 600 $chave 2>$null | Out-Null }
        $m1 = [regex]::Match($Perfil.repositorio, '^sftp:([^@\s/]+)@([^:/\s]+):')
        $m2 = [regex]::Match($Perfil.repositorio, '^sftp://([^@\s/]+)@([^:/\s]+)(?::(\d{1,5}))?//')
        if ($m1.Success) { $usr = $m1.Groups[1].Value; $hst = $m1.Groups[2].Value; $prt = '22' }
        else { $usr = $m2.Groups[1].Value; $hst = $m2.Groups[2].Value
               $prt = $(if ($m2.Groups[3].Success) { $m2.Groups[3].Value } else { '22' }) }
        $cfg = [pscustomobject]@{
            repositorio = $Perfil.repositorio; repositorio_id = $Perfil.repositorio_id
            sftp_user = $usr; sftp_host = $hst; sftp_porta = $prt
            identidade_ssh = $chave; known_hosts = $kh
            ssh_exe = (Get-SshExe); ssh_config = $null
        }
        $cfg.ssh_config = Write-SshConfig -Cfg $cfg -Estado $tmp
        foreach ($src in @((Join-Path $script:BaseDir $script:ResticExeNome), (Join-Path $PSScriptRoot $script:ResticExeNome))) {
            if (Test-Path -LiteralPath $src -PathType Leaf) {
                Copy-Item -LiteralPath $src -Destination (Join-Path $tmp $script:ResticExeNome) -Force
                break
            }
        }
        if (-not (Test-IsWindows)) { & chmod +x (Join-Path $tmp $script:ResticExeNome) 2>$null | Out-Null }
        Protect-ResticPassword -Senha $SenhaRestic -Destino (Join-Path $tmp 'restic.dpapi')
        $id = Confirm-Destino -Cfg $cfg -Estado $tmp
        return $id
    } finally {
        Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
    }
}

function Get-ProximoIndice {
    $n = 0
    if (Test-Path -LiteralPath $script:BaseDir) {
        Get-ChildItem -LiteralPath $script:BaseDir -Directory -Filter 'config-*' |
            ForEach-Object { if ($_.Name -match 'config-(\d+)$' -and [int]$Matches[1] -gt $n) { $n = [int]$Matches[1] } }
        Get-ChildItem -LiteralPath $script:BaseDir -Directory -Filter 'estado-*' |
            ForEach-Object { if ($_.Name -match 'estado-(\d+)$' -and [int]$Matches[1] -gt $n) { $n = [int]$Matches[1] } }
    }
    return ($n + 1)
}

function Build-Configuracao {
    # FASE 1: cria o conjunto NOVO (config-N/ com identidade + backup.json;
    # estado-N apenas quando o perfil muda). Nada do conjunto ativo e tocado.
    # Retorna o descritor; em erro, o que esta fase criou e removido.
    param([pscustomobject]$Perfil, [string]$ChavePath, [securestring]$SenhaRestic,
          [string]$Origem, [object[]]$Exclusoes, [string]$Horario)
    if ($Horario -cnotmatch '^([01][0-9]|2[0-3]):[0-5][0-9]$') { throw "Horario invalido (use HH:mm): $Horario" }
    $perfilNovo = ($null -ne $Perfil)
    $n = Get-ProximoIndice
    $cfgDir = Join-Path $script:BaseDir "config-$n"
    $estadoNovo = $null
    try {
        New-Item -ItemType Directory -Path $cfgDir -Force | Out-Null
        Protect-PastaEstado -Estado $cfgDir
        if ($Perfil) {
            # nova ativacao/troca de perfil: identidade propria neste conjunto
            if (-not $ChavePath -or -not (Test-Path -LiteralPath $ChavePath -PathType Leaf)) {
                throw 'Escolha o arquivo da chave privada fornecida pela MAC IP.'
            }
            $chave = Join-Path $cfgDir 'id_cliente'
            Copy-Item -LiteralPath $ChavePath -Destination $chave -Force
            if (-not (Test-IsWindows)) { & chmod 600 $chave 2>$null | Out-Null }
            $kh = Join-Path $cfgDir 'known_hosts'
            [IO.File]::WriteAllText($kh, $Perfil.known_hosts + "`n")
            $repo = $Perfil.repositorio; $rid = $Perfil.repositorio_id
        } else {
            # mesmo destino: reusa identidade e repositorio da config ativa
            $configAtual = Get-ConfigAtual
            if (-not $configAtual) { throw 'Sem ativacao anterior - conclua a aba Ativar primeiro.' }
            $cfgAnt = Read-AgenteConfig -Caminho $configAtual
            $chave = $cfgAnt.identidade_ssh
            $kh = $cfgAnt.known_hosts
            $repo = $cfgAnt.repositorio; $rid = $cfgAnt.repositorio_id
            $Perfil = [pscustomobject]@{ repositorio = $repo; repositorio_id = $rid }
        }
        $jsonPath = Join-Path $cfgDir 'backup.json'
        $tmp = "$jsonPath.tmp"
        (New-BackupJsonObj -Perfil $Perfil -Origem $Origem -Exclusoes $Exclusoes `
            -Horario $Horario -IdentidadeSsh $chave -KnownHostsFile $kh |
            ConvertTo-Json -Depth 4) | Set-Content -LiteralPath $tmp -Encoding UTF8
        [void](Read-AgenteConfig -Caminho $tmp)         # valida ANTES de nomear
        Move-Item -LiteralPath $tmp -Destination $jsonPath -Force

        if ($perfilNovo) {
            # perfil novo: preparo completo num estado NOVO (o anterior fica intacto)
            if (-not $SenhaRestic) { throw 'Informe a senha Restic na aba Ativar.' }
            $estadoNovo = Join-Path $script:BaseDir "estado-$n"
            Invoke-AgentePreparar -Config $jsonPath -DiretorioEstado $estadoNovo `
                -ResticExe (Get-PreRequisitos).ResticExe -SenhaRestic $SenhaRestic | Out-Null
        }
        $estado = if ($estadoNovo) { $estadoNovo } else { Get-EstadoAtual }
        if (-not $estado) { throw 'Sem estado preparado - conclua a ativacao primeiro.' }
        return [pscustomobject]@{
            Indice = $n; ConfigDir = $cfgDir; Config = $jsonPath
            Estado = $estado; EstadoNovo = ($null -ne $estadoNovo); Horario = $Horario
        }
    } catch {
        # rollback da fase 1: o conjunto anterior nao foi tocado
        Remove-Item -LiteralPath $cfgDir -Recurse -Force -ErrorAction SilentlyContinue
        if ($estadoNovo) { Remove-Item -LiteralPath $estadoNovo -Recurse -Force -ErrorAction SilentlyContinue }
        throw
    }
}

function Remove-ConjuntoNovo {
    # Remove o que a fase 1 criou (cancelamento ou falha posterior).
    param($Built)
    if (-not $Built) { return }
    Remove-Item -LiteralPath $Built.ConfigDir -Recurse -Force -ErrorAction SilentlyContinue
    if ($Built.EstadoNovo) { Remove-Item -LiteralPath $Built.Estado -Recurse -Force -ErrorAction SilentlyContinue }
}

function Publish-Configuracao {
    # FASE 2: registra a tarefa apontando para o conjunto NOVO e so depois
    # publica gui.json. Falha de registro -> Remove-ConjuntoNovo; falha apos
    # o registro (ex.: gui.json) -> restaura a definicao anterior da tarefa
    # para manter o conjunto anterior coerente (config+estado+tarefa).
    param($Built, [string]$Conta, [string]$SenhaConta)
    $xmlAnterior = $null
    try {
        $xmlAnterior = Register-TarefaMacip -Horario $Built.Horario -Conta $Conta `
            -SenhaConta $SenhaConta -Config $Built.Config -Estado $Built.Estado
    } catch {
        Remove-ConjuntoNovo -Built $Built
        throw
    }
    try {
        $meta = Read-GuiMeta
        $meta.estado_atual = $Built.Estado
        $meta.config_atual = $Built.Config
        $meta.conta_tarefa = $Conta
        Save-GuiMeta -Meta $meta
    } catch {
        # tarefa ja aponta para o novo conjunto: repoe a definicao anterior
        # (XML capturado antes da alteracao) para o conjunto anterior
        # continuar coerente — config+estado+tarefa andam juntos.
        if ($xmlAnterior) {
            try { (Get-Backend).RegistrarXml($xmlAnterior, $Conta, $SenhaConta) } catch { }
        }
        Remove-ConjuntoNovo -Built $Built
        throw
    }
}

# =================== INTERFACE ===================

function Show-Gui {
    Add-Type -AssemblyName System.Windows.Forms
    Add-Type -AssemblyName System.Drawing
    $W = 'System.Windows.Forms'

    $form = New-Object "$W.Form"
    $form.Text = 'MAC IP Backup'
    $form.Size = New-Object System.Drawing.Size(560, 470)
    $form.StartPosition = 'CenterScreen'
    $form.MaximizeBox = $false
    $form.FormBorderStyle = 'FixedSingle'

    $tabs = New-Object "$W.TabControl"
    $tabs.Dock = 'Fill'
    $form.Controls.Add($tabs)

    function New-Tab([string]$t) {
        $p = New-Object "$W.TabPage"; $p.Text = $t; $tabs.TabPages.Add($p); return $p
    }
    function New-Label([System.Windows.Forms.Control]$p, [string]$t, [int]$x, [int]$y, [int]$w = 500) {
        $l = New-Object "$W.Label"; $l.Text = $t; $l.Location = New-Object System.Drawing.Point($x, $y)
        $l.Size = New-Object System.Drawing.Size($w, 20); $p.Controls.Add($l); return $l
    }
    function New-Button([System.Windows.Forms.Control]$p, [string]$t, [int]$x, [int]$y, [int]$w = 130) {
        $b = New-Object "$W.Button"; $b.Text = $t; $b.Location = New-Object System.Drawing.Point($x, $y)
        $b.Size = New-Object System.Drawing.Size($w, 28); $p.Controls.Add($b); return $b
    }
    function New-TextBox([System.Windows.Forms.Control]$p, [string]$t, [int]$x, [int]$y, [int]$w = 380) {
        $tb = New-Object "$W.TextBox"; $tb.Text = $t; $tb.Location = New-Object System.Drawing.Point($x, $y)
        $tb.Size = New-Object System.Drawing.Size($w, 24); $p.Controls.Add($tb); return $tb
    }

    $pre = Get-PreRequisitos
    if (-not $pre.Ok) {
        [void][System.Windows.Forms.MessageBox]::Show($form,
            ("Pre-requisitos faltando:`n`n" + ($pre.Faltas -join "`n")),
            'MAC IP Backup', 'OK', 'Warning')
    }

    $conta = (Get-IdentidadeAtual).Nome
    $meta = Read-GuiMeta

    # ----- Tab 1: Ativacao -----
    $t1 = New-Tab '1. Ativar'
    New-Label $t1 "Conta Windows em uso: $conta" 10 12 | Out-Null
    New-Label $t1 'A conta acima sera a conta da tarefa de backup (o preparo e a senha ficam vinculados a ela).' 10 34 | Out-Null
    $tbPerfil = New-TextBox $t1 '' 10 66 380
    $tbPerfil.ReadOnly = $true
    $script:perfilAtual = $null
    $btPerfil = New-Button $t1 'Importar perfil...' 400 64
    $tbChave = New-TextBox $t1 '' 10 106 380
    $tbChave.ReadOnly = $true
    $btChave = New-Button $t1 'Chave privada...' 400 104
    New-Label $t1 'Senha Restic (fornecida pela MAC IP - nunca vai para o perfil nem para o JSON):' 10 142 | Out-Null
    $tbSenha = New-TextBox $t1 '' 10 164 240
    $tbSenha.UseSystemPasswordChar = $true
    $lblAtv = New-Label $t1 '' 10 230 520
    $btTestar = New-Button $t1 'Testar conexao' 10 196
    $btAtivar = New-Button $t1 'Ativar' 150 196
    $btAtivar.Enabled = $false

    $btPerfil.Add_Click({
        $dlg = New-Object "$W.OpenFileDialog"
        $dlg.Filter = 'Perfil MAC IP (*.json)|*.json'
        if ($dlg.ShowDialog() -eq 'OK') {
            try {
                $script:perfilAtual = Read-Perfil -Caminho $dlg.FileName
                $tbPerfil.Text = $dlg.FileName
                $lblAtv.ForeColor = 'DarkGreen'
                $lblAtv.Text = "Perfil OK: $($script:perfilAtual.repositorio)"
            } catch {
                $script:perfilAtual = $null; $tbPerfil.Text = ''
                $lblAtv.ForeColor = 'DarkRed'; $lblAtv.Text = $_.Exception.Message
            }
        }
    })
    $btChave.Add_Click({
        $dlg = New-Object "$W.OpenFileDialog"
        $dlg.Title = 'Chave privada SSH fornecida pela MAC IP'
        if ($dlg.ShowDialog() -eq 'OK') { $tbChave.Text = $dlg.FileName }
    })
    $btTestar.Add_Click({
        $btTestar.Enabled = $false; $lblAtv.ForeColor = 'DarkBlue'; $lblAtv.Text = 'Testando...'
        $form.Refresh()
        try {
            if (-not $script:perfilAtual) { throw 'Importe o perfil primeiro.' }
            if (-not $tbChave.Text) { throw 'Escolha o arquivo da chave privada.' }
            if (-not $tbSenha.Text) { throw 'Informe a senha Restic.' }
            $ss = ConvertTo-SecureString $tbSenha.Text -AsPlainText -Force
            $id = Test-ConexaoDestino -Perfil $script:perfilAtual -ChavePath $tbChave.Text -SenhaRestic $ss
            $lblAtv.ForeColor = 'DarkGreen'
            $lblAtv.Text = "Conexao OK - identidade do servidor e ID do repositorio conferem ($($id.Substring(0,12))...)."
            $btAtivar.Enabled = $true
        } catch {
            $lblAtv.ForeColor = 'DarkRed'; $lblAtv.Text = "Recusado: $($_.Exception.Message)"
            $btAtivar.Enabled = $false
        } finally { $btTestar.Enabled = $true }
    })
    $btAtivar.Add_Click({
        $tabs.SelectedTab = $t2
        [void][System.Windows.Forms.MessageBox]::Show($form,
            'Ativacao validada. Agora escolha a pasta, as exclusoes e o horario na aba Configurar.',
            'MAC IP Backup', 'OK', 'Information')
    })

    # ----- Tab 2: Configurar -----
    $t2 = New-Tab '2. Configurar'
    New-Label $t2 'Pasta protegida:' 10 14 | Out-Null
    $tbOrigem = New-TextBox $t2 '' 10 36 380
    $btOrigem = New-Button $t2 'Escolher pasta...' 400 34
    New-Label $t2 'Exclusoes (opcional, uma por linha - ex.: *.tmp):' 10 70 | Out-Null
    $tbExcl = New-Object "$W.TextBox"
    $tbExcl.Multiline = $true; $tbExcl.ScrollBars = 'Vertical'
    $tbExcl.Location = New-Object System.Drawing.Point(10, 92)
    $tbExcl.Size = New-Object System.Drawing.Size(520, 80)
    $t2.Controls.Add($tbExcl)
    New-Label $t2 'Horario diario (HH:mm):' 10 184 | Out-Null
    $tbHora = New-TextBox $t2 '02:30' 150 182 80
    $lblCfg = New-Label $t2 '' 10 250 520
    $btSalvar = New-Button $t2 'Salvar e agendar' 10 216 160

    $btOrigem.Add_Click({
        $dlg = New-Object "$W.FolderBrowserDialog"
        $dlg.Description = 'Escolha a pasta local a proteger'
        if ($dlg.ShowDialog() -eq 'OK') { $tbOrigem.Text = $dlg.SelectedPath }
    })

    # Preenche com a config ativa (a que a TAREFA usa), se houver.
    $cfgPath = Get-ConfigAtual
    if ($cfgPath -and (Test-Path -LiteralPath $cfgPath -PathType Leaf)) {
        try {
            $cfgAtual = Read-AgenteConfig -Caminho $cfgPath
            $tbOrigem.Text = $cfgAtual.origem
            $tbExcl.Lines = $cfgAtual.exclusoes
            $tbHora.Text = $cfgAtual.horario
        } catch { }
    }

    $btSalvar.Add_Click({
        $btSalvar.Enabled = $false; $lblCfg.ForeColor = 'DarkBlue'; $lblCfg.Text = 'Salvando...'
        $form.Refresh()
        $built = $null
        try {
            $excl = @($tbExcl.Lines | Where-Object { $_ -and $_.Trim() -ne '' })
            if (-not $tbOrigem.Text) { throw 'Escolha a pasta a proteger.' }
            if ($tbHora.Text -cnotmatch '^([01][0-9]|2[0-3]):[0-5][0-9]$') { throw 'Horario invalido (use HH:mm).' }
            # FASE 1: conjunto novo, sem tocar no ativo
            $senhaSs = if ($tbSenha.Text) { ConvertTo-SecureString $tbSenha.Text -AsPlainText -Force } else { $null }
            $built = Build-Configuracao -Perfil $script:perfilAtual -ChavePath $tbChave.Text `
                -SenhaRestic $senhaSs -Origem $tbOrigem.Text -Exclusoes $excl -Horario $tbHora.Text
            # credencial do Windows na janela nativa; cancelar => desfaz a fase 1
            $cred = $host.UI.PromptForCredential('MAC IP Backup',
                "Senha do Windows da conta $conta (a tarefa roda mesmo sem sessao aberta)",
                $conta, '')
            if (-not $cred) {
                Remove-ConjuntoNovo -Built $built
                throw 'Agendamento cancelado - a configuracao anterior continua valendo.'
            }
            # FASE 2: publica
            Publish-Configuracao -Built $built -Conta $conta -SenhaConta $cred.GetNetworkCredential().Password
            $cred = $null
            $script:perfilAtual = $null
            $built = $null
            $lblCfg.ForeColor = 'DarkGreen'
            $lblCfg.Text = "Salvo. Tarefa '$($script:TaskName)' agendada para $($tbHora.Text) na conta $conta."
        } catch {
            if ($built) { Remove-ConjuntoNovo -Built $built }
            $lblCfg.ForeColor = 'DarkRed'; $lblCfg.Text = "Erro: $($_.Exception.Message)"
        } finally { $btSalvar.Enabled = $true }
    })

    # ----- Tab 3: Operar -----
    $t3 = New-Tab '3. Operar'
    $lblPasta = New-Label $t3 'Pasta protegida: -' 10 14
    $lblStatus = New-Label $t3 'Situacao: -' 10 40
    $lblStatus.Font = New-Object System.Drawing.Font($lblStatus.Font, 'Bold')
    $lblProx = New-Label $t3 'Proxima execucao: -' 10 66
    $lblUlt = New-Label $t3 'Ultimo resultado: -' 10 92
    $btAgora = New-Button $t3 'Executar agora' 10 130 130
    $btPausa = New-Button $t3 'Pausar' 150 130 90
    $btRetomar = New-Button $t3 'Retomar' 250 130 90
    $btAtual = New-Button $t3 'Atualizar' 400 130 90
    $lblOp = New-Label $t3 '' 10 170 520

    $script:AtualizarTela = {
        try {
            $info = Get-InfoTarefa
            $cfgPath = Get-ConfigAtual
            if ($cfgPath -and (Test-Path -LiteralPath $cfgPath -PathType Leaf)) {
                try { $lblPasta.Text = 'Pasta protegida: ' + (Read-AgenteConfig -Caminho $cfgPath).origem } catch { }
            }
            if (-not $info) { $lblStatus.Text = 'Situacao: ' + $script:RotulosStatus.sem_tarefa; $lblProx.Text = 'Proxima execucao: -'; $lblUlt.Text = 'Ultimo resultado: -'; return }
            if (-not $info.DoApp) {
                $lblStatus.ForeColor = 'DarkRed'; $lblStatus.Text = 'Situacao: ' + $script:RotulosStatus.tarefa_estranha
                return
            }
            $estado = Get-EstadoAtual
            $res = if ($estado) { Read-UltimoResultado -Estado $estado } else { $null }
            $cls = Classify-Resultado -Res $res -InfoTarefa $info
            $cor = switch ($cls) {
                'sucesso' { 'DarkGreen' } 'em_execucao' { 'DarkBlue' } 'incompleto' { 'DarkOrange' }
                default { 'DarkRed' }
            }
            $lblStatus.ForeColor = $cor
            $lblStatus.Text = 'Situacao: ' + $script:RotulosStatus[$cls]
            $lblProx.Text = 'Proxima execucao: ' + $(if ($info.Habilitada) { $info.Proxima } else { 'PAUSADO' })
            $lblUlt.Text = 'Ultimo resultado: ' + $(if ($res) {
                "$($res.status) em $($res.fim_utc) (restic $($res.codigo_restic), snapshot $($res.snapshot))"
            } else { '-' })
            $btPausa.Enabled = $info.Habilitada
            $btRetomar.Enabled = -not $info.Habilitada
            $btAgora.Enabled = ($info.Estado -ne 4)
        } catch { $lblStatus.Text = 'Situacao: erro ao ler - ' + $_.Exception.Message }
    }
    $btAgora.Add_Click({ try { Start-TarefaAgora; $lblOp.ForeColor='DarkGreen'; $lblOp.Text = 'Disparo manual enviado - a mesma tarefa agendada executa agora.' } catch { $lblOp.ForeColor='DarkRed'; $lblOp.Text = $_.Exception.Message }; & $script:AtualizarTela })
    $btPausa.Add_Click({ try { Set-TarefaHabilitada $false; $lblOp.Text = 'Tarefa pausada - o disparo agendado nao roda ate retomar.' } catch { $lblOp.Text = $_.Exception.Message }; & $script:AtualizarTela })
    $btRetomar.Add_Click({ try { Set-TarefaHabilitada $true; $lblOp.Text = 'Tarefa retomada.' } catch { $lblOp.Text = $_.Exception.Message }; & $script:AtualizarTela })
    $btAtual.Add_Click({ & $script:AtualizarTela })

    # ----- Tab 4: Restaurar -----
    $t4 = New-Tab '4. Restaurar'
    New-Label $t4 'Ponto de restauracao:' 10 14 | Out-Null
    $cbSnap = New-Object "$W.ComboBox"
    $cbSnap.DropDownStyle = 'DropDownList'; $cbSnap.Location = New-Object System.Drawing.Point(10, 36)
    $cbSnap.Size = New-Object System.Drawing.Size(520, 24); $t4.Controls.Add($cbSnap)
    New-Label $t4 'Arquivo no ponto escolhido (somente lista - sem texto livre):' 10 70 | Out-Null
    $cbArq = New-Object "$W.ComboBox"
    $cbArq.DropDownStyle = 'DropDownList'          # selecao fechada: sem texto livre
    $cbArq.Location = New-Object System.Drawing.Point(10, 92)
    $cbArq.Size = New-Object System.Drawing.Size(520, 24); $t4.Controls.Add($cbArq)
    $tbDest = New-TextBox $t4 '' 10 136 380
    $btDest = New-Button $t4 'Pasta de destino...' 400 134
    $lblRes = New-Label $t4 '' 10 210 520
    $btSnaps = New-Button $t4 'Carregar pontos' 10 170 130
    $btRest = New-Button $t4 'Restaurar arquivo' 150 170 150

    $btSnaps.Add_Click({
        $btSnaps.Enabled = $false; $lblRes.ForeColor = 'DarkBlue'; $lblRes.Text = 'Listando pontos...'; $form.Refresh()
        try {
            $estado = Get-EstadoAtual; $cfgPath = Get-ConfigAtual
            if (-not $estado -or -not $cfgPath) { throw 'Sem preparo concluido - ative e configure primeiro.' }
            $snaps = Get-Snapshots -Estado $estado -Config $cfgPath
            $script:snaps = @($snaps)
            $cbSnap.Items.Clear()
            foreach ($s in $script:snaps) { [void]$cbSnap.Items.Add(('{0} - {1}' -f $s.short_id, $s.time)) }
            if ($cbSnap.Items.Count -gt 0) { $cbSnap.SelectedIndex = $cbSnap.Items.Count - 1 }
            $lblRes.ForeColor = 'DarkGreen'; $lblRes.Text = "$($cbSnap.Items.Count) ponto(s) disponivel(is)."
        } catch { $lblRes.ForeColor = 'DarkRed'; $lblRes.Text = $_.Exception.Message }
        finally { $btSnaps.Enabled = $true }
    })
    $cbSnap.Add_SelectedIndexChanged({
        try {
            $cbArq.Items.Clear()
            $estado = Get-EstadoAtual; $cfgPath = Get-ConfigAtual
            $snap = $script:snaps[$cbSnap.SelectedIndex]
            foreach ($a in (Get-ArquivosSnapshot -Estado $estado -Config $cfgPath -Snapshot $snap.id)) {
                [void]$cbArq.Items.Add($a)
            }
            if ($cbArq.Items.Count -gt 0) { $cbArq.SelectedIndex = 0 }
        } catch { $lblRes.ForeColor = 'DarkRed'; $lblRes.Text = $_.Exception.Message }
    })
    $btDest.Add_Click({
        $dlg = New-Object "$W.FolderBrowserDialog"
        $dlg.Description = 'Pasta SEPARADA e vazia para receber o arquivo restaurado'
        if ($dlg.ShowDialog() -eq 'OK') { $tbDest.Text = $dlg.SelectedPath }
    })
    $btRest.Add_Click({
        $btRest.Enabled = $false; $lblRes.ForeColor = 'DarkBlue'; $lblRes.Text = 'Restaurando...'; $form.Refresh()
        try {
            $estado = Get-EstadoAtual; $cfgPath = Get-ConfigAtual
            $snap = $script:snaps[$cbSnap.SelectedIndex]
            $cfg = Read-AgenteConfig -Caminho $cfgPath
            $alvo = Restore-Arquivo -Estado $estado -Config $cfgPath -Snapshot $snap.id `
                -Arquivo $cbArq.SelectedItem -Destino $tbDest.Text -Origem $cfg.origem
            $sha = (Get-FileHash -LiteralPath $alvo -Algorithm SHA256).Hash.ToLower()
            $lblRes.ForeColor = 'DarkGreen'
            $lblRes.Text = "Restaurado: $alvo (SHA-256 $sha) - o original NAO foi sobrescrito."
        } catch { $lblRes.ForeColor = 'DarkRed'; $lblRes.Text = $_.Exception.Message }
        finally { $btRest.Enabled = $true }
    })

    $tabs.Add_SelectedIndexChanged({ if ($tabs.SelectedTab -eq $t3) { & $script:AtualizarTela } })
    & $script:AtualizarTela
    [void]$form.ShowDialog()
}

if ($MyInvocation.InvocationName -eq '.') { return }

if (-not (Test-IsWindows)) {
    Write-Error 'A interface grafica requer Windows (Agendador de Tarefas + WinForms).'
    exit 2
}
Show-Gui
