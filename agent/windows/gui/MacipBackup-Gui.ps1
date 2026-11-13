#requires -Version 5.1
# MacipBackup-Gui.ps1 - interface grafica do agente Windows da MAC IP (piloto).
# WinForms sobre Windows PowerShell 5.1: sem dependencia nova -- a mesma
# tecnologia do agente. Todas as strings em ASCII: o arquivo roda em PS 5.1
# sem BOM como o agente.
#
# Fluxo pensado para o tecnico: Ativar -> Configurar -> Operar -> Restaurar.
# Nenhuma etapa pede terminal, edicao de JSON ou o Agendador de Tarefas.
# O Agendador continua por tras: a tarefa MACIP-Backup roda com o app fechado,
# sem sessao aberta e apos reinicio.
#
# Reaproveitamento: o agente Backup-MacipCliente.ps1 e dot-sourced -- validacao
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
$script:LogonPassword = 1   # TASK_LOGON_PASSWORD = 1 (o 2 e S4U -- sem DPAPI/rede)
$script:TaskMarker    = 'MACIP Backup'
$script:TaskDescricao = "$($script:TaskMarker) - agente de backup do piloto"
$script:TaskCreate    = 2   # TASK_CREATE   -- FALHA se a tarefa ja existir
$script:TaskUpdate    = 4   # TASK_UPDATE   -- FALHA se a tarefa nao existir
$script:PsExeEsperado = 'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe'
$script:DumpTimeoutMs = 600000   # restauracao: 10 min -- alem disso Kill + parcial fora
$script:ConsultaTimeoutMs = 60000 # consultas (cat/snapshots/ls): 60 s
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
        # NAO engole erro aqui: Get-TarefaInfo decide se a falha e ausencia
        # confirmada ou consulta inconclusiva (acesso negado etc).
        return $this.svc.GetFolder('\').GetTask($script:TaskName)
    }
    $b | Add-Member ScriptMethod NewDef {
        param($TarefaExistente)
        $this.Conectar()
        if ($TarefaExistente) { return $TarefaExistente.Definition }
        return $this.svc.NewTask(0)
    }
    $b | Add-Member ScriptMethod Registrar {
        # $Flag: TASK_CREATE (2) = so se nao existir (falha se uma homonima
        # aparecer entre a consulta e aqui -- NUNCA sobrescreve a estranha);
        # TASK_UPDATE (4) = so se existir. Sem CREATE_OR_UPDATE cego.
        param($Def, $Conta, $SenhaConta, [int]$Flag)
        $this.Conectar()
        $this.svc.GetFolder('\').RegisterTaskDefinition(
            $script:TaskName, $Def, $Flag, $Conta, $SenhaConta, $script:LogonPassword, $null) | Out-Null
    }
    $b | Add-Member ScriptMethod RegistrarXml {
        # Rollback com TASK_UPDATE: repoe a definicao anterior exatamente
        # como estava (XML serializado ANTES de qualquer alteracao); falha
        # se a tarefa sumiu entre a tentativa e a reposicao.
        param([string]$Xml, $Conta, $SenhaConta)
        $this.Conectar()
        $this.svc.GetFolder('\').RegisterTask(
            $script:TaskName, $Xml, $script:TaskUpdate, $Conta, $SenhaConta, $script:LogonPassword, $null) | Out-Null
    }
    $b | Add-Member ScriptMethod XmlAtual {
        try { $t = $this.Get() } catch { return $null }   # sem tarefa -> sem XML
        if ($t) { return [string]$t.Xml } else { return $null }
    }
    return $b
}

function Get-TarefaInfo {
    # Consulta estruturada a tarefa do app. Status:
    #   encontrada  -> objeto em .Tarefa
    #   ausente     -> ausencia CONFIRMADA (o proprio GetTask devolveu o
    #                  codigo de "tarefa inexistente", HRESULT 0x80070002)
    #   erro        -> inconclusivo (falha de conexao, pasta, acesso...)
    # NADA de texto de excecao nem retorno vazio como prova de ausencia:
    # so o codigo especifico do GetTask classifica 'ausente'.
    $b = Get-Backend
    try { $b.Conectar() } catch {
        return [pscustomobject]@{ Tarefa = $null; Status = 'erro'
            Erro = "servico/pasta do Agendador indisponivel: $($_.Exception.Message)" }
    }
    try {
        $t = $b.Get()
    } catch {
        # O PowerShell empacota a excecao COM (MethodInvocation) -- procura
        # o HRESULT na cadeia interna inteira.
        $hrOk = $false; $e = $_.Exception
        while ($null -ne $e) {
            try { if ($e.HResult -eq -2147024894) { $hrOk = $true; break } } catch { }
            $e = $e.InnerException
        }
        $msg = [string]$_.Exception.Message
        if ($hrOk) {
            return [pscustomobject]@{ Tarefa = $null; Status = 'ausente'; Erro = $msg }
        }
        return [pscustomobject]@{ Tarefa = $null; Status = 'erro'; Erro = $msg }
    }
    if ($t) { return [pscustomobject]@{ Tarefa = $t; Status = 'encontrada'; Erro = $null } }
    # retorno inesperado/vazio NAO prova ausencia -- trata como inconclusivo
    return [pscustomobject]@{ Tarefa = $null; Status = 'erro'; Erro = 'consulta devolveu tarefa vazia' }
}

function ConvertTo-CaminhoNormal {
    # Normaliza separador e resolve o caminho absoluto NO WINDOWS; fora
    # dele (so teste) devolve a forma textual -- comparacao exata dos
    # dois lados.
    param([string]$P)
    $p2 = $P.Trim() -replace '/', '\'
    if ($env:OS -ne 'Windows_NT') { return $p2 }
    try { return [IO.Path]::GetFullPath($p2) } catch { return $p2 }
}

function Get-PadraoAcaoApp {
    # Estrutura EXATA dos argumentos que Get-TaskActionArgs gera, ancorada
    # e com o -File capturado: ele e o PRIMEIRO parametro efetivo; nada
    # antes nem depois. -File citado dentro de -Command ou tokens extras
    # nao casam. A igualdade do caminho e conferida depois, normalizada.
    return '^-NoProfile -NonInteractive -File "([^"]+)" -Modo Backup -Config "[^"]+" -DiretorioEstado "[^"]+"$'
}

function Test-TarefaDoApp {
    # Propriedade EXIGE os campos esperados -- nunca so trecho de texto:
    #   1. descricao EXATA publicada pelo app;
    #   2. UMA unica acao de execucao (sem acoes ambiguas);
    #   3. executavel = caminho ABSOLUTO do PowerShell esperado (nao um
    #      sufixo: outropowershell.exe e powershell.exe fora do lugar nao
    #      valem; caminho relativo tambem nao);
    #   4. argumentos casam o formato gerado pelo app, com -File no
    #      Backup-MacipCliente.ps1 DESTA instalacao.
    param($Tarefa)
    if (-not $Tarefa) { return $false }
    try {
        $def = $Tarefa.Definition
        if ([string]$def.RegistrationInfo.Description -cne $script:TaskDescricao) { return $false }
        if ([int]$def.Actions.Count -ne 1) { return $false }
        $acao = $def.Actions.Item(1)
        if ((ConvertTo-CaminhoNormal -P ([string]$acao.Path)) -ine (ConvertTo-CaminhoNormal -P $script:PsExeEsperado)) { return $false }
        $args = [string]$acao.Arguments
        if ($args -notmatch (Get-PadraoAcaoApp)) { return $false }
        return ((ConvertTo-CaminhoNormal -P $Matches[1]) -ieq (ConvertTo-CaminhoNormal -P $script:AgentePath))
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
    $Def.RegistrationInfo.Description = $script:TaskDescricao
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
          else { $script:PsExeEsperado }
    while ($Def.Actions.Count -gt 0) { $Def.Actions.Remove(1) }   # IActionCollection nao tem Clear()
    $act = $Def.Actions.Create(0)
    $act.Path = $ps
    $act.Arguments = Get-TaskActionArgs -Agente $script:AgentePath -Config $Config -Estado $Estado
    $Def.Principal.UserId = $Conta
    $Def.Principal.LogonType = $script:LogonPassword   # TASK_LOGON_PASSWORD = 1
    $Def.Principal.RunLevel = 0                        # LIMITADO: sem elevacao
}

function Register-TarefaMacip {
    # Unico ponto que cria/altera a tarefa. Idempotente (CreateOrUpdate).
    # Novo cadastro EXIGE ausencia confirmada; atualizacao EXIGE a marca do
    # app; consulta inconclusiva e homonima estranha sao recusadas.
    # Devolve a def anterior (XML) para rollback.
    param([string]$Horario, [string]$Conta, [string]$SenhaConta, [string]$Config, [string]$Estado)
    $b = Get-Backend
    $info = Get-TarefaInfo
    if ($info.Status -eq 'erro') {
        throw "Consulta da tarefa inconclusiva ($($info.Erro)) - o aplicativo nao altera nada sem confirmacao."
    }
    $t = $info.Tarefa
    if ($info.Status -eq 'encontrada' -and -not (Test-TarefaDoApp -Tarefa $t)) {
        throw ("Ja existe uma tarefa '$($script:TaskName)' que NAO pertence ao MAC IP Backup. " +
            'Renomeie ou remova a tarefa estranha - o aplicativo nao a altera.')
    }
    $xmlAnterior = $b.XmlAtual()                 # copia serializada -- imune a mutacao da def
    $def = $b.NewDef($t)
    Write-TaskDef -Def $def -Horario $Horario -Config $Config -Estado $Estado -Conta $Conta
    # A PAUSA e do operador: atualizar uma tarefa pausada NAO a reativa --
    # so o botao Retomar reabilita. No primeiro cadastro, nasce habilitada.
    if ($info.Status -eq 'encontrada') { $def.Settings.Enabled = [bool]$t.Enabled }
    # CREATE no 1o cadastro: se uma homonima aparecer entre a consulta e o
    # registro, a criacao FALHA sem tocar nela. UPDATE so na tarefa nossa.
    $flag = $(if ($info.Status -eq 'ausente') { $script:TaskCreate } else { $script:TaskUpdate })
    try {
        $b.Registrar($def, $Conta, $SenhaConta, $flag)
    } catch {
        throw ($_.Exception.Message + ' Se faltar permissao para executar com a sessao encerrada ' +
            '(direito "Fazer logon como trabalho em lotes"), acione o administrador do Windows - ' +
            'o cadastro por si so nao comprova que o backup agendado vai rodar.')
    }
    return $xmlAnterior
}

function Get-TaskArg {
    param([string]$Nome)
    $info = Get-TarefaInfo
    if ($info.Status -ne 'encontrada') { return $null }
    $t = $info.Tarefa
    try {
        $args = [string]$t.Definition.Actions.Item(1).Arguments
        if ($args -match ('-' + $Nome + '\s+"([^"]+)"')) { return $Matches[1] }
        if ($args -match ('-' + $Nome + '\s+(\S+)')) { return $Matches[1] }
    } catch { }
    return $null
}

function Get-EstadoAtual {
    # Fonte primaria: o que a TAREFA usa -- se o meta ficou para tras numa
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
    $info = Get-TarefaInfo
    if ($info.Status -eq 'erro') { throw "Consulta da tarefa inconclusiva ($($info.Erro)) - recusado." }
    if ($info.Status -ne 'encontrada') { throw "Tarefa $($script:TaskName) nao existe - salve a configuracao primeiro." }
    $t = $info.Tarefa
    if (-not (Test-TarefaDoApp -Tarefa $t)) { throw "Tarefa $($script:TaskName) nao pertence ao MAC IP Backup - recusado." }
    $t.Enabled = $On
}

function Resolve-ContaSid {
    # Resolve "DOMINIO\conta" para o SID real. Duas contas de mesmo nome em
    # dominios diferentes NAO sao equivalentes -- so o SID decide.
    param([string]$Conta)
    if (-not $Conta) { return $null }
    if (Test-IsWindows) {
        try { return ([Security.Principal.NTAccount]$Conta).Translate([Security.Principal.SecurityIdentifier]).Value }
        catch { return $null }
    }
    return ('posix:' + $Conta)   # fora do Windows (testes): nome completo e a identidade
}

function Read-PreparoSid {
    # SID da conta que fez o preparo (gravado pelo agente em preparo.json).
    param([string]$Estado)
    $f = Join-Path $Estado 'preparo.json'
    if (-not (Test-Path -LiteralPath $f -PathType Leaf)) { return $null }
    try { return [string](Get-Content -LiteralPath $f -Raw | ConvertFrom-Json).sid }
    catch { return $null }
}

function Start-TarefaAgora {
    # Executar agora = a MESMA tarefa agendada, na MESMA conta registrada.
    # A conta da tarefa (Definition.Principal.UserId) e a conta do preparo
    # (preparo.json no estado que a TAREFA referencia) sao comparadas por SID.
    $info = Get-TarefaInfo
    if ($info.Status -eq 'erro') { throw "Consulta da tarefa inconclusiva ($($info.Erro)) - recusado." }
    if ($info.Status -ne 'encontrada') { throw "Tarefa $($script:TaskName) nao existe - salve a configuracao primeiro." }
    $t = $info.Tarefa
    if (-not (Test-TarefaDoApp -Tarefa $t)) { throw "Tarefa $($script:TaskName) nao pertence ao MAC IP Backup - recusado." }
    if ([int]$t.State -eq 4) { throw 'Ja ha uma execucao em andamento.' }   # TASK_STATE_RUNNING
    $estado = Get-TaskArg -Nome 'DiretorioEstado'
    if (-not $estado) { throw 'Tarefa sem estado verificavel nos argumentos - recusado.' }
    $sidPreparo = Read-PreparoSid -Estado $estado
    $contaTarefa = [string]$t.Definition.Principal.UserId
    $sidTarefa = Resolve-ContaSid -Conta $contaTarefa
    if (-not $sidPreparo -or -not $sidTarefa) {
        throw 'Sem identidade verificavel (SID ausente no preparo ou na tarefa) - recusado.'
    }
    if ($sidTarefa -cne $sidPreparo) {
        throw "Conta da tarefa ($contaTarefa) difere da conta do preparo (SID divergente) - recusado."
    }
    $t.Run($null) | Out-Null
}

function Get-InfoTarefa {
    $info = Get-TarefaInfo
    if ($info.Status -eq 'erro') { throw "Consulta da tarefa inconclusiva: $($info.Erro)" }
    if ($info.Status -ne 'encontrada') { return $null }
    $t = $info.Tarefa
    $contaTarefa = ''
    try { $contaTarefa = [string]$t.Definition.Principal.UserId } catch { }
    return [pscustomobject]@{
        Habilitada = [bool]$t.Enabled
        Estado     = [int]$t.State          # 4 = em execucao
        Ultima     = [string]$t.LastRunTime
        Proxima    = [string]$t.NextRunTime
        Codigo     = [int]$t.LastTaskResult
        Conta      = $contaTarefa
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

function Test-ResConfirmado {
    # Um resultado CONFIRMADO: status sucesso + restic 0 + snapshot valido.
    param($Res)
    return ($Res -and [string]$Res.status -eq 'sucesso' -and
            $null -ne $Res.codigo_restic -and [int]$Res.codigo_restic -eq 0 -and
            [string]$Res.snapshot -match '^[0-9a-f]{64}$')
}

function Classify-Resultado {
    # "Concluido" EXIGE resultado valido VINCULADO ao ultimo disparo
    # (inicio_utc >= LastRunTime da tarefa -- tolerancia so de 1s para
    # granularidade, nunca minutos). Um sucesso das 14:00 NAO e resultado
    # do disparo das 14:00:50. Sem vinculo -> sem_resultado; sem tentativa
    # do Agendador E sem resultado -> ainda_nao (resultado ausente/ilegivel
    # sozinho nunca prova "nunca rodou"). O codigo do Agendador so prova
    # FALHA DE INICIALIZACAO quando nao ha resultado do disparo.
    param($Res, $InfoTarefa)
    if ($InfoTarefa -and $InfoTarefa.Estado -eq 4) { return 'em_execucao' }
    $ultima = $null
    if ($InfoTarefa -and $InfoTarefa.Ultima) {
        try { $u = [DateTimeOffset]::Parse($InfoTarefa.Ultima); if ($u.Year -gt 2000) { $ultima = $u } } catch { }
    }
    $agendadorTentou = ($null -ne $ultima)
    # o resultado pertence a ESTE disparo?
    $deste = $false
    if ($Res -and $Res.inicio_utc) {
        try {
            $inicio = [DateTimeOffset]::Parse([string]$Res.inicio_utc)
            if ($ultima) { $deste = ($inicio -ge $ultima.AddSeconds(-1)) }
            else { $deste = $true }   # sem disparo registrado: o resultado e a unica evidencia
        } catch { }
    }
    if ($deste) {
        if ([string]$Res.status -eq 'em_execucao') { return 'em_execucao' }
        if (Test-ResConfirmado -Res $Res) { return 'concluido' }
        if ($null -ne $Res.codigo_restic -and [int]$Res.codigo_restic -eq 3) { return 'incompleto' }
        return 'falhou'
    }
    if ($agendadorTentou) {
        # Agendador tentou e nao ha resultado do disparo: o codigo dele so
        # vale como prova de falha de INICIALIZACAO (nao de execucao).
        if ($InfoTarefa.Codigo -ne 0) { return 'falhou' }
        return 'sem_resultado'
    }
    if ($Res) { return 'falhou' }      # resultado ilegivel/incompleto, sem disparo: nao prova nada
    return 'ainda_nao'
}

$script:RotulosStatus = @{
    concluido          = 'BACKUP CONCLUIDO - resultado confirmado'
    falhou             = 'FALHOU - a execucao terminou com erro'
    incompleto         = 'INCOMPLETO - Restic codigo 3 (nem todo arquivo lido)'
    em_execucao        = 'EM EXECUCAO - backup rodando agora'
    sem_resultado      = 'SEM RESULTADO DESTE DISPARO - o Agendador tentou, nada foi gravado'
    ainda_nao          = 'AINDA NAO EXECUTOU - nenhum disparo ate agora'
    sem_tarefa         = 'SEM TAREFA - configure e salve primeiro'
    tarefa_estranha    = 'TAREFA ESTRANHA - existe MACIP-Backup que nao e do aplicativo'
}

$script:AcaoStatus = @{
    concluido          = 'Nenhuma acao pendente.'
    falhou             = 'Acao necessaria: veja o resultado na pasta de estado e confira o destino na aba 1.'
    incompleto         = 'Acao necessaria: confira o que ficou de fora (arquivos em uso/abertos).'
    em_execucao        = 'Aguarde o termino do backup em execucao.'
    sem_resultado      = 'Acao necessaria: verifique destino, conta da tarefa e permissao de execucao sem sessao.'
    ainda_nao          = 'Aguarde o proximo disparo ou use "Executar agora".'
    sem_tarefa         = 'Acao necessaria: valide na aba 1 e salve na aba 2.'
    tarefa_estranha    = 'Acao necessaria: chame a MAC IP - a tarefa estranha nao e tocada.'
}

# ---------- restauracao ----------

function Get-CfgOperacional {
    param([string]$Estado, [string]$Config)
    $cfg = Read-AgenteConfig -Caminho $Config
    $cfg | Add-Member -NotePropertyName ssh_exe -NotePropertyValue (Get-SshExe) -Force
    $cfg | Add-Member -NotePropertyName ssh_config -NotePropertyValue ((Join-Path $Estado 'ssh_config').Replace('\', '/')) -Force
    return $cfg
}

function Confirm-PreparoVigente {
    # Mesma verificacao que o backup faz do preparo, reutilizada para
    # listar pontos, listar arquivos e restaurar. Confere:
    #   conta (SID da sessao == SID do preparo);
    #   repositorio + repositorio_id (config == preparo);
    #   VINCULO EFETIVO: identidade_ssh, known_hosts e ssh_config sao os
    #   mesmos arquivos do preparo, no mesmo caminho e com o mesmo SHA-256.
    # Divergencia -> erro com a correcao pela interface (refazer ativacao).
    param([string]$Estado, [pscustomobject]$Cfg)
    $prepFile = Join-Path $Estado 'preparo.json'
    if (-not (Test-Path -LiteralPath $prepFile -PathType Leaf)) {
        throw "Estado sem preparo verificavel (preparo.json ausente) - refaca a ativacao na aba 1."
    }
    $prep = try { Get-Content -LiteralPath $prepFile -Raw | ConvertFrom-Json }
            catch { throw "preparo.json ilegivel - refaca a ativacao na aba 1." }
    $sidAtual = (Get-IdentidadeAtual).Sid
    if ($sidAtual -and [string]$prep.sid -cne $sidAtual) {
        throw 'A conta desta sessao difere da conta do preparo - abra a interface na conta do backup.'
    }
    if ([string]$prep.repositorio -cne [string]$Cfg.repositorio -or
        [string]$prep.repositorio_id -cne [string]$Cfg.repositorio_id) {
        throw 'Repositorio divergente do preparo - a configuracao mudou; salve de novo na aba 2.'
    }
    $vinculos = @(
        @{ Nome = 'identidade_ssh'; Arquivo = [string]$Cfg.identidade_ssh; Esperado = [string]$prep.identidade_ssh; Hash = [string]$prep.identidade_sha256 },
        @{ Nome = 'known_hosts';   Arquivo = [string]$Cfg.known_hosts;   Esperado = [string]$prep.known_hosts;   Hash = [string]$prep.known_hosts_sha256 },
        @{ Nome = 'ssh_config';    Arquivo = (Join-Path $Estado 'ssh_config'); Esperado = $null;                   Hash = [string]$prep.ssh_config_sha256 }
    )
    foreach ($v in $vinculos) {
        $arq = $v.Arquivo
        if ($v.Esperado -and ((ConvertTo-CaminhoNormal -P $arq) -cne (ConvertTo-CaminhoNormal -P $v.Esperado))) {
            throw "$($v.Nome) aponta para outro arquivo que nao o do preparo - refaca a ativacao na aba 1."
        }
        if (-not $arq -or -not (Test-Path -LiteralPath $arq -PathType Leaf)) {
            throw "$($v.Nome) ausente ($arq) - refaca a ativacao na aba 1."
        }
        if ($v.Hash -and ((Get-HashArquivo $arq) -cne $v.Hash)) {
            throw "$($v.Nome) foi alterado desde o preparo - refaca a ativacao na aba 1."
        }
    }
    return $true
}

function Invoke-ResticConsulta {
    # Como Invoke-Restic, mas com prazo (relogio monotonico): consultas de
    # leitura (cat config, snapshots, ls) NUNCA podem travar a janela.
    # stdout e stderr drenados em paralelo; no estouro, mata o processo e
    # devolve codigo de falha com a mensagem -- o chamador decide.
    param([pscustomobject]$Cfg, [string]$Estado, [string[]]$Comando, [int]$PrazoMs = 60000)
    $exe = Join-Path $Estado $script:ResticExeNome
    $senha = Read-ResticPassword -Caminho (Join-Path $Estado 'restic.dpapi')
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($senha)
    $p = $null
    try {
        $psi = [Diagnostics.ProcessStartInfo]::new()
        $sftpCmd = 'sftp.command={0} -F {1} -s {2} sftp' -f $Cfg.ssh_exe, $Cfg.ssh_config, $Cfg.sftp_host
        $resticArgs = '--repo "{0}" --cache-dir "{1}" -o "{2}" {3}' -f `
            $Cfg.repositorio, (Join-Path $Estado 'cache'), $sftpCmd, ($Comando -join ' ')
        if (Test-IsWindows) {
            $psi.FileName = $exe; $psi.Arguments = $resticArgs
        } else {
            $psi.FileName = (Get-Process -Id $PID).Path
            $psi.Arguments = '-NoProfile -File "{0}" {1}' -f $exe, $resticArgs
        }
        $psi.UseShellExecute = $false
        $psi.RedirectStandardOutput = $true
        $psi.RedirectStandardError = $true
        $psi.EnvironmentVariables['RESTIC_PASSWORD'] = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
        $psi.EnvironmentVariables.Remove('RESTIC_PASSWORD_FILE') | Out-Null
        $psi.EnvironmentVariables.Remove('RESTIC_PASSWORD_COMMAND') | Out-Null
        if (-not (Test-IsWindows)) { $psi.EnvironmentVariables['RESTIC_STUB_CHILD'] = '1' }
        try { $p = [Diagnostics.Process]::Start($psi) }
        catch { throw "Nao foi possivel iniciar o restic ($exe): $($_.Exception.Message)" }
        $relogio = [Diagnostics.Stopwatch]::StartNew()
        $prazo = { [int][Math]::Max(0, $PrazoMs - $relogio.ElapsedMilliseconds) }
        $outTask = $p.StandardOutput.ReadToEndAsync()
        $errTask = $p.StandardError.ReadToEndAsync()
        if (-not $p.WaitForExit((& $prazo))) {
            try { $p.Kill() } catch { }
            return [pscustomobject]@{ Codigo = -1; Linhas = @("tempo limite de $([int]($PrazoMs/1000))s excedido - verifique destino e rede") }
        }
        if (-not ($outTask.Wait((& $prazo)) -and $errTask.Wait((& $prazo)))) {
            return [pscustomobject]@{ Codigo = -1; Linhas = @('leitura da saida do restic excedeu o tempo limite') }
        }
        $linhas = @()
        $linhas += [string]$outTask.Result -split "`r?`n" | Where-Object { $_ -ne '' }
        $linhas += [string]$errTask.Result -split "`r?`n" | Where-Object { $_ -ne '' }
        return [pscustomobject]@{ Codigo = $p.ExitCode; Linhas = $linhas }
    } finally {
        if ($p) { $p.Dispose() }
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
        if ($senha) { $senha.Dispose() }
    }
}

function Confirm-DestinoComPrazo {
    # 'cat config' com prazo (Invoke-ResticConsulta): usado na ativacao,
    # no preparo ao salvar e antes de listar/restaurar. Um servidor que
    # aceita SSH mas nao responde SFTP nao trava a janela nem o preparo.
    param([pscustomobject]$Cfg, [string]$Estado)
    $resp = Invoke-ResticConsulta -Cfg $Cfg -Estado $Estado -Comando @('cat', 'config') -PrazoMs ([int]$script:ConsultaTimeoutMs)
    if ($resp.Codigo -ne 0) {
        throw ('Repositorio inacessivel ou identidade recusada (restic codigo {0}: {1}). ' +
            'Verifique se o servidor responde SFTP e se a chave/perfil sao os aprovados.') -f `
            $resp.Codigo, (($resp.Linhas | Select-Object -First 3) -join ' ')
    }
    $linha = $resp.Linhas | Where-Object { $_ -match '"id"' } | Select-Object -First 1
    $id = $null
    if ($linha) { try { $id = ($linha | ConvertFrom-Json).id } catch { } }
    if (-not $id -and $linha -match '"id"\s*:\s*"([0-9a-f]{64})"') { $id = $Matches[1] }
    if (-not $id) { throw 'Repositorio respondeu sem ID verificavel - recusado.' }
    if ($id -cne $Cfg.repositorio_id) {
        throw 'Destino divergente do aprovado pela MAC IP (repositorio_id nao confere) - recusado.'
    }
    return $id
}

function Confirm-DestinoOperacional {
    # Antes de listar ou restaurar: MESMA verificacao do backup -- preparo
    # vigente (conta, repo, vinculos) + destino responde + ID aprovado.
    param([pscustomobject]$Cfg, [string]$Estado)
    [void](Confirm-PreparoVigente -Estado $Estado -Cfg $Cfg)
    return (Confirm-DestinoComPrazo -Cfg $Cfg -Estado $Estado)
}

function Get-Snapshots {
    param([string]$Estado, [string]$Config)
    $cfg = Get-CfgOperacional -Estado $Estado -Config $Config
    [void](Confirm-DestinoOperacional -Cfg $cfg -Estado $Estado)
    $r = Invoke-ResticConsulta -Cfg $cfg -Estado $Estado -Comando @('snapshots', '--json') -PrazoMs ([int]$script:ConsultaTimeoutMs)
    if ($r.Codigo -ne 0) { throw "Falha ao listar pontos de restauracao (restic $($r.Codigo)): $(($r.Linhas | Select-Object -First 3) -join ' ')" }
    $snaps = @($r.Linhas | ForEach-Object { try { $_ | ConvertFrom-Json } catch { } } |
        Where-Object { $_.id } | ForEach-Object { $_ })
    return $snaps
}

function Get-ArquivosSnapshot {
    # restic ls --json: um objeto por no. Retorna SOMENTE arquivos regulares
    # (type == 'file'), com o caminho exato do snapshot -- nada de texto livre.
    param([string]$Estado, [string]$Config, [string]$Snapshot)
    $cfg = Get-CfgOperacional -Estado $Estado -Config $Config
    [void](Confirm-DestinoOperacional -Cfg $cfg -Estado $Estado)
    $r = Invoke-ResticConsulta -Cfg $cfg -Estado $Estado -Comando @('ls', '--json', $Snapshot) -PrazoMs ([int]$script:ConsultaTimeoutMs)
    if ($r.Codigo -ne 0) { throw "Falha ao listar arquivos do ponto (restic $($r.Codigo)): $(($r.Linhas | Select-Object -First 3) -join ' ')" }
    $arquivos = @()
    foreach ($linha in $r.Linhas) {
        try { $no = $linha | ConvertFrom-Json } catch { continue }
        if ($no.type -ceq 'file' -and $no.path) { $arquivos += [string]$no.path }
    }
    return $arquivos
}

function Resolve-LinkPath {
    # Resolve links simbolicos/junctions encadeados. Se a cadeia nao fecha em
    # 8 saltos, recusa -- melhor recusar do que comparar um caminho ambiguo.
    param([string]$Caminho)
    $atual = $Caminho
    for ($i = 0; $i -lt 8; $i++) {
        if (-not (Test-Path -LiteralPath $atual)) { return ([IO.Path]::GetFullPath($atual)) }
        try { $item = Get-Item -LiteralPath $atual -Force }
        catch { throw "Componente nao verificavel (acesso negado ou erro de leitura): $atual" }
        if (-not $item.LinkType -or -not $item.Target) { return ([IO.Path]::GetFullPath($atual)) }
        $alvo = $item.Target
        if ($alvo -is [array]) { $alvo = $alvo[0] }
        if (-not [IO.Path]::IsPathRooted($alvo)) {
            $alvo = Join-Path (Split-Path -LiteralPath $atual -Parent) $alvo
        }
        $atual = [IO.Path]::GetFullPath($alvo)
    }
    throw "Cadeia de links/junctions longa demais para resolver com seguranca: $Caminho"
}

function Resolve-PathSeguro {
    # Resolve o caminho efetivo olhando TODOS os componentes -- inclusive
    # quando a pasta final ainda nao existe (o link pode estar num pai).
    param([string]$Caminho)
    $full = [IO.Path]::GetFullPath($Caminho)
    $raiz = [IO.Path]::GetPathRoot($full)
    $rel = $full.Substring($raiz.Length)
    $atual = $raiz
    foreach ($seg in ($rel -split '[\\/]+')) {
        if (-not $seg) { continue }
        $atual = Join-Path $atual $seg
        if (Test-Path -LiteralPath $atual) {
            try { $atual = Resolve-LinkPath -Caminho $atual }
            catch { throw "Caminho nao resolvido com seguranca ($atual): $($_.Exception.Message)" }
        }
    }
    return $atual
}

function Get-NomeArquivoRestic {
    # Folha do caminho dentro do snapshot ('/D/pasta/arq.txt' -> 'arq.txt').
    param([string]$PathRestic)
    return ($PathRestic -split '[\\/]')[-1]
}

function Invoke-ResticDump {
    # 'restic dump <snap> <caminho>' escreve o conteudo do ARQUIVO EXATO no
    # stdout -- sem glob: um '*' no nome nao arrasta outros arquivos.
    # stdout vai direto para um arquivo TEMPORARIO exclusivo; stderr e lido
    # em paralelo (pipe cheio nao trava o filho). So depois de codigo 0 o
    # arquivo e publicado no nome final -- mova falha se o destino existir
    # (File.Move nao sobrescreve), nunca toca um arquivo pre-existente.
    # Em qualquer falha remove APENAS o parcial desta tentativa.
    param([pscustomobject]$Cfg, [string]$Estado, [string]$Snapshot, [string]$Arquivo, [string]$Destino)
    $exe = Join-Path $Estado $script:ResticExeNome
    $senha = Read-ResticPassword -Caminho (Join-Path $Estado 'restic.dpapi')
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($senha)
    $nome = Get-NomeArquivoRestic -PathRestic $Arquivo
    $alvo = Join-Path $Destino $nome
    $tmp = Join-Path $Destino ('.~' + $nome + '.part')
    $fs = $null; $p = $null
    try {
        # Temporario exclusivo ANTES de disparar o restic.
        try {
            $fs = [IO.File]::Open($tmp, 'CreateNew', 'Write')
        } catch [IO.IOException] {
            $tmp = Join-Path $Destino ('.~' + $nome + '-' + [Guid]::NewGuid().ToString('N') + '.part')
            $fs = [IO.File]::Open($tmp, 'CreateNew', 'Write')
        }
        $psi = [Diagnostics.ProcessStartInfo]::new()
        $sftpCmd = 'sftp.command={0} -F {1} -s {2} sftp' -f $Cfg.ssh_exe, $Cfg.ssh_config, $Cfg.sftp_host
        $resticArgs = '--repo "{0}" --cache-dir "{1}" -o "{2}" dump {3} "{4}"' -f `
            $Cfg.repositorio, (Join-Path $Estado 'cache'), $sftpCmd, $Snapshot, $Arquivo
        if (Test-IsWindows) {
            $psi.FileName = $exe
            $psi.Arguments = $resticArgs
        } else {
            # Em Windows o restic.exe e nativo. Fora do Windows (so em teste) o
            # "exe" e um script ps1 -- roda pelo interpretador que hospeda a GUI.
            $psi.FileName = (Get-Process -Id $PID).Path
            $psi.Arguments = '-NoProfile -File "{0}" {1}' -f $exe, $resticArgs
        }
        $psi.UseShellExecute = $false
        $psi.RedirectStandardOutput = $true
        $psi.RedirectStandardError = $true
        $psi.EnvironmentVariables['RESTIC_PASSWORD'] = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
        $psi.EnvironmentVariables.Remove('RESTIC_PASSWORD_FILE') | Out-Null
        $psi.EnvironmentVariables.Remove('RESTIC_PASSWORD_COMMAND') | Out-Null
        if (-not (Test-IsWindows)) { $psi.EnvironmentVariables['RESTIC_STUB_CHILD'] = '1' }
        try { $p = [Diagnostics.Process]::Start($psi) }
        catch { throw "Nao foi possivel iniciar o restic ($exe): $($_.Exception.Message)" }
        # stdout e stderr drenados EM PARALELO -- um pipe cheio nao trava o
        # filho. UM unico prazo (relogio monotonico) cobre o processo E as
        # duas leituras; cada espera e conferida e so se le o resultado de
        # operacao concluida -- nunca um .Result que possa bloquear de novo.
        $relogio = [Diagnostics.Stopwatch]::StartNew()
        $prazo = { [int][Math]::Max(0, [int]$script:DumpTimeoutMs - $relogio.ElapsedMilliseconds) }
        # seam de teste: falha de escrita apos a abertura do temporario
        if ($script:DumpStreamFail) { try { $fs.Close() } catch { } }
        $outTask = $p.StandardOutput.BaseStream.CopyToAsync($fs)
        $errTask = $p.StandardError.ReadToEndAsync()
        if (-not $p.WaitForExit((& $prazo))) {
            try { $p.Kill() } catch { }
            try { $fs.Close() } catch { }; $fs = $null
            throw ("Restauracao excedeu o tempo limite de $([int]($script:DumpTimeoutMs / 60000)) " +
                'minutos - arquivo parcial descartado.')
        }
        if (-not $outTask.Wait((& $prazo))) {
            try { $p.Kill() } catch { }
            throw 'Leitura do stdout do restic excedeu o tempo limite - parcial descartado.'
        }
        # erro de escrita no temporario (mesmo apos gravar bytes) = FALHA
        try { $outTask.GetAwaiter().GetResult() | Out-Null }
        catch { throw "Falha ao gravar o arquivo restaurado: $($_.Exception.Message)" }
        if (-not $errTask.Wait((& $prazo))) {
            # processo ja retornou mas o pipe de erro segue aberto (ex.: um
            # neto vivo). NAO esperar de novo: falha, sem ler .Result.
            throw 'Leitura do stderr do restic excedeu o tempo limite - parcial descartado.'
        }
        # so fecha depois de confirmar a copia; fechamento falho = FALHA
        try { $fs.Close() } catch { throw "Falha ao fechar o arquivo restaurado: $($_.Exception.Message)" }
        $fs = $null
        $err = try { [string]$errTask.Result } catch { '(stderr ilegivel)' }
        if ($p.ExitCode -ne 0) { throw "Falha na restauracao (restic $($p.ExitCode)): $err" }
        # Publica no nome final SOMENTE apos sucesso; File.Move falha se o
        # destino existir -- um arquivo que apareceu durante o dump (e qualquer
        # pre-existente) fica intacto.
        try { [IO.File]::Move($tmp, $alvo) }
        catch [IO.IOException] {
            throw "Ja existe arquivo em '$alvo' - a restauracao nunca sobrescreve."
        }
        if (-not (Test-Path -LiteralPath $alvo -PathType Leaf)) {
            throw 'Restauracao falhou: arquivo esperado ausente no destino.'
        }
        return $alvo
    } finally {
        if ($fs) { try { $fs.Close() } catch { } }
        if ($p) { $p.Dispose() }
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
        if ($senha) { $senha.Dispose() }
        # falha -> remove APENAS o parcial desta tentativa (no sucesso $tmp
        # ja nao existe; Test-Path torna o Remove inocuo).
        if (Test-Path -LiteralPath $tmp) { Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue }
    }
}

function Restore-Arquivo {
    # Piloto: restaura SOMENTE o arquivo escolhido, em pasta nova ou vazia,
    # fora da arvore protegida - inclusive quando a equivalencia vem de
    # link/junction. Sucesso exige o arquivo esperado no destino.
    param([string]$Estado, [string]$Config, [string]$Snapshot, [string]$Arquivo,
          [string]$Destino, [string]$Origem)
    if (-not $Destino) { throw 'Escolha a pasta de destino da restauracao.' }
    if (-not $Arquivo) { throw 'Escolha o arquivo a restaurar na lista.' }
    # Resolve o caminho efetivo olhando TODOS os componentes -- um link ou
    # junction num pai (mesmo com a pasta final inexistente) muda o destino.
    $destResolvido = Resolve-PathSeguro -Caminho $Destino
    $origResolvido = if ($Origem) { Resolve-PathSeguro -Caminho $Origem } else { $null }
    if ($origResolvido -and $destResolvido.TrimEnd('\','/') -ieq $origResolvido.TrimEnd('\','/')) {
        throw 'A restauracao do piloto nao pode ser feita sobre a pasta protegida (nem por link/junction).'
    }
    if ($origResolvido -and $destResolvido.StartsWith($origResolvido.TrimEnd('\','/') + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'A pasta de destino esta DENTRO da pasta protegida - escolha outra.'
    }
    $destAbs = $destResolvido
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
        $id = Confirm-DestinoComPrazo -Cfg $cfg -Estado $tmp
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
            # a conferencia do destino usa o prazo da GUI (SSH sem SFTP
            # nao pode segurar o preparo nem a janela)
            Invoke-AgentePreparar -Config $jsonPath -DiretorioEstado $estadoNovo `
                -ResticExe (Get-PreRequisitos).ResticExe -SenhaRestic $SenhaRestic `
                -ConfirmaDestino ${function:Confirm-DestinoComPrazo} | Out-Null
        }
        $estado = if ($estadoNovo) { $estadoNovo } else { Get-EstadoAtual }
        if (-not $estado) { throw 'Sem estado preparado - conclua a ativacao primeiro.' }
        if (-not $estadoNovo) {
            # Estado reaproveitado: a conta que vai registrar a tarefa PRECISA
            # ser a do preparo -- o SID esta em preparo.json e e inegociavel
            # (a senha DPAPI so abre na conta original).
            $sidPrep = Read-PreparoSid -Estado $estado
            if (-not $sidPrep) { throw "Estado sem preparo verificavel (preparo.json ausente em $estado) - reative pelo perfil." }
            if ($sidPrep -cne (Get-IdentidadeAtual).Sid) {
                throw 'A conta desta sessao difere da conta do preparo (SID divergente) - abra a interface na conta do backup.'
            }
        }
        return [pscustomobject]@{
            Indice = $n; ConfigDir = $cfgDir; Config = $jsonPath
            Estado = $estado; EstadoNovo = ($null -ne $estadoNovo); Horario = $Horario
            Publicado = $false
        }
    } catch {
        # rollback da fase 1: o conjunto anterior nao foi tocado
        Remove-Item -LiteralPath $cfgDir -Recurse -Force -ErrorAction SilentlyContinue
        if ($estadoNovo) { Remove-Item -LiteralPath $estadoNovo -Recurse -Force -ErrorAction SilentlyContinue }
        throw
    }
}

function Test-ConjuntoEmUso {
    # $true = o Agendador referencia este conjunto (config/estado nos
    # argumentos) ou NAO foi possivel confirmar -- inclusive execucao ativa.
    param($Built)
    try {
        $info = Get-TarefaInfo
        if ($info.Status -eq 'erro') { return $true }    # consulta inconclusiva = preserva
        if ($info.Status -ne 'encontrada') { return $false }
        $t = $info.Tarefa
        if ([int]$t.State -eq 4) { return $true }        # em execucao pode depender dele
        # instancia ATIVA ou PENDENTE pode estar usando este conjunto
        $inst = $t.GetInstances(0)
        if ($null -eq $inst -or [int]$inst.Count -gt 0) { return $true }
        $c = Get-TaskArg -Nome 'Config'
        $e = Get-TaskArg -Nome 'DiretorioEstado'
        if ((-not $c) -or (-not $e)) { return $true }  # argumentos ilegiveis = nao confirmado
        if ($c -ieq $Built.Config) { return $true }
        # o estado so e removivel quando ESTA tentativa o criou (perfil novo);
        # estado reaproveitado ja era da tarefa anterior, nao do conjunto novo.
        if ($Built.EstadoNovo -and $Built.Estado -and ($e -ieq $Built.Estado)) { return $true }
        return $false
    } catch { return $true }                            # falha na consulta = nao confirmado
}

function Remove-ConjuntoNovo {
    # UNICO ponto de limpeza do conjunto novo. So remove quando o Agendador
    # confirma que NADA depende dele; na duvida preserva e devolve aviso.
    param($Built)
    if (-not $Built) { return $null }
    if (Test-ConjuntoEmUso -Built $Built) {
        return ("Conjunto novo PRESERVADO: a tarefa ainda o referencia, esta em execucao " +
            "ou nao foi possivel confirmar (mantido: $($Built.ConfigDir)).")
    }
    Remove-Item -LiteralPath $Built.ConfigDir -Recurse -Force -ErrorAction SilentlyContinue
    if ($Built.EstadoNovo) { Remove-Item -LiteralPath $Built.Estado -Recurse -Force -ErrorAction SilentlyContinue }
    return $null
}

function Publish-Configuracao {
    # FASE 2 -- DONA da limpeza a partir daqui ($Built.Publicado avisa o
    # tratador da interface para NAO repetir a remocao):
    #   1. registra a tarefa apontando para o conjunto NOVO;
    #   2. publica gui.json.
    # Falha no registro -> conjunto novo descartavel (o Agendador confirma).
    # Falha na publicacao -> tenta repor a definicao anterior:
    #   reposta  -> conjunto novo livre -> limpa;
    #   nao reposta (ou 1o cadastro sem anterior) -> a tarefa CONTINUA
    #   apontando para o conjunto novo: arquivos preservados e funcionais.
    param($Built, [string]$Conta, [string]$SenhaConta)
    $Built.Publicado = $true
    $xmlAnterior = $null
    try {
        $xmlAnterior = Register-TarefaMacip -Horario $Built.Horario -Conta $Conta `
            -SenhaConta $SenhaConta -Config $Built.Config -Estado $Built.Estado
    } catch {
        $aviso = Remove-ConjuntoNovo -Built $Built
        throw ($_.Exception.Message + $(if ($aviso) { " $aviso" } else { '' }))
    }
    try {
        $meta = Read-GuiMeta
        $meta.estado_atual = $Built.Estado
        $meta.config_atual = $Built.Config
        $meta.conta_tarefa = $Conta
        Save-GuiMeta -Meta $meta
    } catch {
        # Antes de repor a definicao anterior, confirma que a tarefa ATUAL
        # e a do app e aponta para o conjunto publicado POR ESTA tentativa
        # (outra definicao = alguem mexeu no meio -> inconclusivo, preserva).
        $reposto = $false
        if ($xmlAnterior) {
            try {
                $info2 = Get-TarefaInfo
                if ($info2.Status -ne 'encontrada' -or -not (Test-TarefaDoApp -Tarefa $info2.Tarefa)) {
                    throw 'tarefa nao confirmada como a publicada pela tentativa'
                }
                $c2 = Get-TaskArg -Nome 'Config'; $e2 = Get-TaskArg -Nome 'DiretorioEstado'
                if ($c2 -ine $Built.Config -or $e2 -ine $Built.Estado) {
                    throw 'a tarefa atual nao e a def publicada por esta tentativa'
                }
                (Get-Backend).RegistrarXml($xmlAnterior, $Conta, $SenhaConta)
                $reposto = $true
            } catch { }
        }
        if ($reposto) {
            # tarefa anterior restaurada -> conjunto novo livre
            $aviso = Remove-ConjuntoNovo -Built $Built
            throw ("Falha ao publicar os metadados; a tarefa anterior foi restaurada " +
                "e a configuracao anterior continua valendo. $($_.Exception.Message)" +
                $(if ($aviso) { " $aviso" } else { '' }))
        }
        # sem reposicao (ou impossivel confirmar a def atual): os arquivos
        # ficam PRESERVADOS -- nada e apagado sem confirmacao do Agendador.
        throw ("Falha ao publicar os metadados ($($_.Exception.Message)). " +
            "Os arquivos novos foram PRESERVADOS - tente salvar novamente para concluir a publicacao.")
    }
}

# O que prova a ativacao: perfil + chave + senha usados na validacao.
# Senha so entra no HASH -- nunca em argumento, mensagem ou log.
function Get-TokenAtivacao {
    param($Perfil, [string]$ChavePath, [string]$SenhaTxt)
    $sha = [Security.Cryptography.SHA256]::Create()
    $hashChave = ''
    if ($ChavePath -and (Test-Path -LiteralPath $ChavePath -PathType Leaf)) {
        $hashChave = (Get-FileHash -LiteralPath $ChavePath -Algorithm SHA256).Hash
    }
    $bytes = [Text.Encoding]::UTF8.GetBytes(($Perfil | ConvertTo-Json -Compress) + '|' + $ChavePath + '|' + $hashChave + '|' + $SenhaTxt)
    return ([BitConverter]::ToString($sha.ComputeHash($bytes)))
}

function Test-AtivacaoVigente {
    # A prova so vale para os dados capturados na validacao: perfil, chave
    # ou senha diferentes (ou prova ausente) exigem "Validar e continuar".
    param($Perfil, [string]$ChavePath, [string]$SenhaTxt)
    $tok = Get-TokenAtivacao -Perfil $Perfil -ChavePath $ChavePath -SenhaTxt $SenhaTxt
    if (-not $script:ativacaoToken -or $tok -cne $script:ativacaoToken) {
        throw 'Perfil, chave ou senha alterados apos a validacao - clique "Validar e continuar" de novo na aba 1.'
    }
}

# ---------- ultimo backup confirmado (evidencia propria) ----------
# ultimo-confirmado.json e gravado pelo AGENTE a cada sucesso real
# (Read-Confirmado/Update-Confirmado vem do Backup-MacipCliente.ps1
# dot-sourced). A interface so LE e, para resultados de agentes
# antigos que nao gravavam o confirmado, faz o backfill na leitura --
# nao depende de a janela ter sido aberta antes.

# ---------- execucao fora da thread da janela ----------

function Invoke-Assincrono {
    # Roda UMA funcao do app num runspace separado (a funcao e resolvida
    # dot-sourcing este arquivo la dentro) e bombeia a fila de mensagens
    # da janela enquanto espera -- a janela NUNCA trava numa operacao de
    # rede. Prazo monotonico: no estouro, para o runspace e reporta. O
    # resultado volta como objeto; erros do trabalho viram excecao aqui.
    param([string]$Funcao, [hashtable]$Params, [int]$PrazoMs = 300000, $Form)
    $guiPath = $PSCommandPath
    # O runspace dot-source o arquivo de novo: os override injetaveis dos
    # testes (stub do restic, ssh, prazos) precisam viajar junto -- senao o
    # trabalhador volta aos binarios/prazos de producao e falha diferente.
    $scriptVars = @{
        ResticExeNome     = $script:ResticExeNome
        SshExe            = $script:SshExe
        ConsultaTimeoutMs = $script:ConsultaTimeoutMs
        DumpTimeoutMs     = $script:DumpTimeoutMs
    }
    $ps = [powershell]::Create()
    $async = $null
    try {
        [void]$ps.AddScript('param($GuiPath,$Base,$Agente,$Fn,$P,$Vars) . $GuiPath -BaseDir $Base -AgentePath $Agente; foreach ($k in $Vars.Keys) { Set-Variable -Scope Script -Name $k -Value $Vars[$k] }; & $Fn @P')
        [void]$ps.AddParameter('GuiPath', $guiPath)
        [void]$ps.AddParameter('Base', $script:BaseDir)
        [void]$ps.AddParameter('Agente', $script:AgentePath)
        [void]$ps.AddParameter('Fn', $Funcao)
        [void]$ps.AddParameter('P', $Params)
        [void]$ps.AddParameter('Vars', $scriptVars)
        $async = $ps.BeginInvoke()
        $relogio = [Diagnostics.Stopwatch]::StartNew()
        while (-not $async.IsCompleted) {
            if ($relogio.ElapsedMilliseconds -gt $PrazoMs) {
                try { $ps.Stop() } catch { }
                throw ("Operacao excedeu o tempo limite de $([int]($PrazoMs/1000)) segundos. " +
                    'Se o servidor nao responde, verifique rede, SFTP e a chave aprovada.')
            }
            if ($Form) { [System.Windows.Forms.Application]::DoEvents() } else { Start-Sleep -Milliseconds 50 }
        }
        $out = $null; $endErr = $null
        try { $out = $ps.EndInvoke($async) }
        catch {
            $endErr = $_.Exception
            while ($endErr.InnerException) { $endErr = $endErr.InnerException }
            $endErr = $endErr.Message
        }
        if ($ps.Streams.Error.Count -gt 0) { throw ([string]$ps.Streams.Error[0].Exception.Message) }
        if ($endErr) { throw $endErr }
        if ($ps.HadErrors) { throw 'A operacao falhou (detalhes indisponiveis no runspace).' }
        # Listagens devolvem TODOS os itens (0 -> $null, 1 -> o objeto,
        # N -> todos) -- antes so $out[0] voltava e a aba Restaurar perdia
        # pontos/arquivos.
        if ($null -eq $out -or $out.Count -eq 0) { return $null }
        if ($out.Count -eq 1) { return $out[0] }
        return @($out)
    } finally { $ps.Dispose() }
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
    $btValidar = New-Button $t1 'Validar e continuar' 10 196 170
    $script:ativacaoToken = $null     # prova de que ESTA combinacao perfil+chave+senha validou
    $script:OpRede = $false           # trava: uma operacao de rede por vez
    $entradasAtv = @($tbPerfil, $btPerfil, $tbChave, $btChave, $tbSenha)

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
    # "Validar e continuar": captura perfil+chave+senha NO INSTANTE do
    # clique e vincula a prova a eles. Durante a tentativa as entradas
    # ficam travadas (OpRede impede clique repetido e a salvar concorrente);
    # ao fim, o token so e aceito se os dados nao mudaram -- uma tentativa
    # antiga nunca libera uma configuracao nova.
    $btValidar.Add_Click({
        if ($script:OpRede) { return }
        if (-not $script:perfilAtual) { $lblAtv.ForeColor='DarkRed'; $lblAtv.Text='Importe o perfil primeiro.'; return }
        if (-not $tbChave.Text) { $lblAtv.ForeColor='DarkRed'; $lblAtv.Text='Escolha o arquivo da chave privada.'; return }
        if (-not $tbSenha.Text) { $lblAtv.ForeColor='DarkRed'; $lblAtv.Text='Informe a senha Restic.'; return }
        $script:OpRede = $true
        $btValidar.Enabled = $false; $btSalvar.Enabled = $false
        foreach ($c in $entradasAtv) { $c.Enabled = $false }
        # captura do que vai ser validado -- NAO o que estiver na tela depois
        $perfilV = $script:perfilAtual; $chaveV = [string]$tbChave.Text; $senhaV = [string]$tbSenha.Text
        $tokenV = Get-TokenAtivacao -Perfil $perfilV -ChavePath $chaveV -SenhaTxt $senhaV
        $lblAtv.ForeColor = 'DarkBlue'; $lblAtv.Text = 'Validando destino...'
        try {
            $id = Invoke-Assincrono -Funcao 'Test-ConexaoDestino' -Form $form -PrazoMs ([int]$script:ConsultaTimeoutMs + 30000) -Params @{
                Perfil = $perfilV; ChavePath = $chaveV
                SenhaRestic = (ConvertTo-SecureString $senhaV -AsPlainText -Force) }
            # so aceita se os dados continuam exatamente os capturados
            if ($tokenV -ne (Get-TokenAtivacao -Perfil $script:perfilAtual -ChavePath $tbChave.Text -SenhaTxt $tbSenha.Text)) {
                throw 'Os dados mudaram durante a validacao - valide de novo.'
            }
            $script:ativacaoToken = $tokenV
            $lblAtv.ForeColor = 'DarkGreen'
            $lblAtv.Text = "Destino validado - identidade do servidor e ID do repositorio conferem ($($id.Substring(0,12))...)."
            $tabs.SelectedTab = $t2     # avanca so com a prova
        } catch {
            $script:ativacaoToken = $null
            $lblAtv.ForeColor = 'DarkRed'; $lblAtv.Text = "Recusado: $($_.Exception.Message)"
        } finally {
            $script:OpRede = $false; $btValidar.Enabled = $true; $btSalvar.Enabled = $true
            foreach ($c in $entradasAtv) { $c.Enabled = $true }
        }
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
        if ($script:OpRede) { return }        # clique repetido / validacao em curso: ignora
        $script:OpRede = $true
        $btSalvar.Enabled = $false; $btValidar.Enabled = $false
        foreach ($c in $entradasAtv) { $c.Enabled = $false }
        $lblCfg.ForeColor = 'DarkBlue'; $lblCfg.Text = 'Salvando...'
        $built = $null
        try {
            $excl = @($tbExcl.Lines | Where-Object { $_ -and $_.Trim() -ne '' })
            if (-not $tbOrigem.Text) { throw 'Escolha a pasta a proteger.' }
            if ($tbHora.Text -cnotmatch '^([01][0-9]|2[0-3]):[0-5][0-9]$') { throw 'Horario invalido (use HH:mm).' }
            # perfil novo exige validacao VIGENTE -- conferida ANTES e
            # DEPOIS do build (uma tentativa vencida nao publica nada)
            if ($script:perfilAtual) {
                Test-AtivacaoVigente -Perfil $script:perfilAtual -ChavePath $tbChave.Text -SenhaTxt $tbSenha.Text
            }
            # FASE 1 (consulta o destino no preparo): fora da thread da janela
            $built = Invoke-Assincrono -Funcao 'Build-Configuracao' -Form $form -PrazoMs 180000 -Params @{
                Perfil = $script:perfilAtual; ChavePath = $tbChave.Text
                SenhaRestic = $(if ($tbSenha.Text) { ConvertTo-SecureString $tbSenha.Text -AsPlainText -Force } else { $null })
                Origem = $tbOrigem.Text; Exclusoes = $excl; Horario = $tbHora.Text }
            if ($script:perfilAtual) {
                Test-AtivacaoVigente -Perfil $script:perfilAtual -ChavePath $tbChave.Text -SenhaTxt $tbSenha.Text
            }
            # credencial do Windows na janela nativa; cancelar => desfaz a fase 1
            $cred = $host.UI.PromptForCredential('MAC IP Backup',
                "Senha do Windows da conta $conta (a tarefa roda mesmo sem sessao aberta)",
                $conta, '')
            if (-not $cred) {
                $aviso = Remove-ConjuntoNovo -Built $built
                throw ('Agendamento cancelado - a configuracao anterior continua valendo.' +
                    $(if ($aviso) { " $aviso" } else { '' }))
            }
            # FASE 2: publica (COM + arquivo; operacao local)
            Publish-Configuracao -Built $built -Conta $conta -SenhaConta $cred.GetNetworkCredential().Password
            $cred = $null
            $script:perfilAtual = $null; $script:ativacaoToken = $null
            $built = $null
            $lblCfg.ForeColor = 'DarkGreen'
            $lblCfg.Text = ("Salvo. Agendamento SOLICITADO para $($tbHora.Text) na conta $conta - " +
                'a execucao programada so fica comprovada quando o disparo real aparecer na aba Operar.')
            & $script:AtualizarTela
        } catch {
            # Limpeza so aqui se a fase 2 ainda nao assumiu (Publicado):
            # depois de Publish-Configuracao ela e a unica responsavel.
            if ($built -and -not $built.Publicado) { Remove-ConjuntoNovo -Built $built | Out-Null }
            $lblCfg.ForeColor = 'DarkRed'; $lblCfg.Text = "Erro: $($_.Exception.Message)"
        } finally {
            $script:OpRede = $false; $btSalvar.Enabled = $true; $btValidar.Enabled = $true
            foreach ($c in $entradasAtv) { $c.Enabled = $true }
        }
    })

    # ----- Tab 3: Operar -----
    $t3 = New-Tab '3. Operar'
    $lblPasta = New-Label $t3 'Pasta protegida: -' 10 14
    $lblStatus = New-Label $t3 'Situacao: -' 10 40
    $lblStatus.Font = New-Object System.Drawing.Font($lblStatus.Font, 'Bold')
    $lblProx = New-Label $t3 'Proxima execucao: -' 10 66
    $lblUlt = New-Label $t3 'Ultimo backup confirmado: -' 10 92
    $lblAcao = New-Label $t3 '' 10 118 520
    $btAgora = New-Button $t3 'Executar agora' 10 150 160
    $btPausa = New-Button $t3 'Pausar' 180 150 90
    $btRetomar = New-Button $t3 'Retomar' 280 150 90
    $btAtual = New-Button $t3 'Atualizar' 400 150 90
    $lblOp = New-Label $t3 '' 10 190 520

    $script:AtualizarTela = {
        try {
            $info = Get-InfoTarefa
            $cfgPath = Get-ConfigAtual
            if ($cfgPath -and (Test-Path -LiteralPath $cfgPath -PathType Leaf)) {
                try { $lblPasta.Text = 'Pasta protegida: ' + (Read-AgenteConfig -Caminho $cfgPath).origem } catch { }
            }
            $estado = Get-EstadoAtual
            $res = if ($estado) { Read-UltimoResultado -Estado $estado } else { $null }
            # Ultimo backup CONFIRMADO: evidencia propria persistida em
            # ultimo-confirmado.json PELO AGENTE a cada sucesso real -- a
            # tela so le; uma falha posterior ou ausencia de novo
            # resultado nao apaga; repositorio trocado nao herda.
            $repoId = ''
            if ($cfgPath -and (Test-Path -LiteralPath $cfgPath -PathType Leaf)) {
                try { $repoId = [string](Read-AgenteConfig -Caminho $cfgPath).repositorio_id } catch { }
            }
            if ($estado -and (Test-ResConfirmado -Res $res)) {
                # backfill: agentes antigos gravavam so ultimo-resultado.
                try { Update-Confirmado -Estado $estado -RepositorioId $repoId -Snapshot ([string]$res.snapshot) `
                        -InicioUtc ([string]$res.inicio_utc) -FimUtc ([string]$res.fim_utc) } catch { }
            }
            $conf = if ($estado) { Read-Confirmado -Estado $estado -RepositorioId $repoId } else { $null }
            $lblUlt.Text = 'Ultimo backup confirmado: ' + $(if ($conf) {
                ('{0} (snapshot {1})' -f $conf.fim_utc, ([string]$conf.snapshot).Substring(0,12))
            } else { 'nenhum ainda' })
            if (-not $info) {
                $lblStatus.ForeColor = 'DarkRed'; $lblStatus.Text = 'Situacao: ' + $script:RotulosStatus.sem_tarefa
                $lblProx.Text = 'Proxima execucao: -'; $lblAcao.Text = $script:AcaoStatus.sem_tarefa
                $btAgora.Enabled = $false; $btPausa.Enabled = $false; $btRetomar.Enabled = $false; return
            }
            if (-not $info.DoApp) {
                $lblStatus.ForeColor = 'DarkRed'; $lblStatus.Text = 'Situacao: ' + $script:RotulosStatus.tarefa_estranha
                $lblAcao.Text = $script:AcaoStatus.tarefa_estranha
                $btAgora.Enabled = $false; $btPausa.Enabled = $false; $btRetomar.Enabled = $false; return
            }
            $cls = Classify-Resultado -Res $res -InfoTarefa $info
            $cor = switch ($cls) {
                'concluido' { 'DarkGreen' } 'em_execucao' { 'DarkBlue' }
                'incompleto' { 'DarkOrange' } 'ainda_nao' { 'DarkBlue' }
                'sem_resultado' { 'DarkOrange' } default { 'DarkRed' }
            }
            $lblStatus.ForeColor = $cor
            $lblStatus.Text = 'Situacao: ' + $script:RotulosStatus[$cls]
            $lblProx.Text = 'Proxima execucao: ' + $(if ($info.Habilitada) { $info.Proxima } else { 'PAUSADO - so Retomar religa' })
            $lblAcao.Text = $(if (-not $info.Habilitada -and $cls -ne 'em_execucao') {
                'Acao necessaria: a tarefa esta PAUSADA - so "Retomar" religa o agendamento.'
            } else { $script:AcaoStatus[$cls] })
            $btPausa.Enabled = $info.Habilitada
            $btRetomar.Enabled = -not $info.Habilitada
            $btAgora.Enabled = ($info.Estado -ne 4)
            $btAgora.Text = $(if (Test-ResConfirmado -Res $res) { 'Executar agora' } else { 'Executar primeiro backup' })
        } catch { $lblStatus.Text = 'Situacao: erro ao ler - ' + $_.Exception.Message }
    }
    $btAgora.Add_Click({ try { Start-TarefaAgora; $lblOp.ForeColor='DarkBlue'; $lblOp.Text = 'Disparo SOLICITADO - a conclusao aparece aqui quando houver resultado deste disparo.' } catch { $lblOp.ForeColor='DarkRed'; $lblOp.Text = $_.Exception.Message }; & $script:AtualizarTela })
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

    $script:LimpaSelecaoRest = {
        $script:snaps = @(); $cbSnap.Items.Clear(); $cbArq.Items.Clear()
    }
    $btSnaps.Add_Click({
        $btSnaps.Enabled = $false; $lblRes.ForeColor = 'DarkBlue'; $lblRes.Text = 'Listando pontos...'
        try {
            $estado = Get-EstadoAtual; $cfgPath = Get-ConfigAtual
            if (-not $estado -or -not $cfgPath) { throw 'Sem preparo concluido - valide na aba 1 e salve na aba 2.' }
            $snaps = Invoke-Assincrono -Funcao 'Get-Snapshots' -Form $form -PrazoMs ([int]$script:ConsultaTimeoutMs + 30000) -Params @{ Estado = $estado; Config = $cfgPath }
            & $script:LimpaSelecaoRest                       # selecao anterior nunca sobrevive a nova listagem
            $script:snaps = @($snaps)
            foreach ($s in $script:snaps) { [void]$cbSnap.Items.Add(('{0} - {1}' -f $s.short_id, $s.time)) }
            if ($cbSnap.Items.Count -gt 0) { $cbSnap.SelectedIndex = $cbSnap.Items.Count - 1 }
            $lblRes.ForeColor = 'DarkGreen'; $lblRes.Text = "$($cbSnap.Items.Count) ponto(s) disponivel(is)."
        } catch {
            & $script:LimpaSelecaoRest                       # listagem falhou -> descarta a selecao
            $lblRes.ForeColor = 'DarkRed'; $lblRes.Text = $_.Exception.Message
        } finally { $btSnaps.Enabled = $true }
    })
    $cbSnap.Add_SelectedIndexChanged({
        if ($cbSnap.SelectedIndex -lt 0) { return }
        $cbArq.Items.Clear()
        try {
            $estado = Get-EstadoAtual; $cfgPath = Get-ConfigAtual
            $snap = $script:snaps[$cbSnap.SelectedIndex]
            $arqs = Invoke-Assincrono -Funcao 'Get-ArquivosSnapshot' -Form $form -PrazoMs ([int]$script:ConsultaTimeoutMs + 30000) -Params @{ Estado = $estado; Config = $cfgPath; Snapshot = $snap.id }
            foreach ($a in $arqs) { [void]$cbArq.Items.Add($a) }
            if ($cbArq.Items.Count -gt 0) { $cbArq.SelectedIndex = 0 }
        } catch { $cbArq.Items.Clear(); $lblRes.ForeColor = 'DarkRed'; $lblRes.Text = $_.Exception.Message }
    })
    $btDest.Add_Click({
        $dlg = New-Object "$W.FolderBrowserDialog"
        $dlg.Description = 'Pasta SEPARADA e vazia para receber o arquivo restaurado'
        if ($dlg.ShowDialog() -eq 'OK') { $tbDest.Text = $dlg.SelectedPath }
    })
    $btRest.Add_Click({
        $btRest.Enabled = $false; $lblRes.ForeColor = 'DarkBlue'; $lblRes.Text = 'Restaurando...'
        try {
            $estado = Get-EstadoAtual; $cfgPath = Get-ConfigAtual
            $snap = $script:snaps[$cbSnap.SelectedIndex]
            $cfg = Read-AgenteConfig -Caminho $cfgPath
            if (-not $cbArq.SelectedItem) { throw 'Escolha o arquivo a restaurar na lista.' }
            $alvo = Invoke-Assincrono -Funcao 'Restore-Arquivo' -Form $form -PrazoMs ([int]$script:DumpTimeoutMs + 60000) -Params @{
                Estado = $estado; Config = $cfgPath; Snapshot = $snap.id
                Arquivo = $cbArq.SelectedItem; Destino = $tbDest.Text; Origem = $cfg.origem }
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
