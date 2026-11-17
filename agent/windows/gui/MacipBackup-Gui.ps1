#requires -Version 5.1
# MacipBackup-Gui.ps1 - interface grafica do agente Windows da MAC IP (piloto).
# WinForms sobre Windows PowerShell 5.1: sem dependencia nova -- a mesma
# tecnologia do agente. ARQUIVO UTF-8 COM BOM: os textos levam acentos e o
# PS 5.1 so interpreta UTF-8 corretamente quando o BOM esta presente.
#
# Fluxo pensado para o tecnico: Ativar -> Pasta e horario -> Situacao -> Restaurar.
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
# A validacao do BaseDir com espacos acontece no lancamento (bloco
# try/catch do rodape) -- mensagem + diagnostico em vez de saida muda.
if (-not $AgentePath) { $AgentePath = Join-Path $PSScriptRoot 'Backup-MacipCliente.ps1' }

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

try {
    . $AgentePath   # reusa Read-AgenteConfig, Write-SshConfig, Invoke-Restic, etc.
} catch {
    if ($MyInvocation.InvocationName -eq '.') { throw }   # testes veem o erro real
    try { Add-Type -AssemblyName System.Windows.Forms } catch { }
    try {
        [void][System.Windows.Forms.MessageBox]::Show(
            ("Nao foi possivel carregar o agente de backup ($AgentePath): $($_.Exception.Message)`r`n`r`n" +
             'Reinstale o aplicativo pelo instalador ou acione a MAC IP.'),
            'MAC IP Backup', 'OK', 'Error')
    } catch { Write-Error "Agente nao carregavel: $($_.Exception.Message)" }
    exit 1
}

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
    # GUIDs das instancias em execucao -- a evidencia de "execucao NOVA"
    # do pedido de Executar agora (o LastRunTime sozinho pode ser o
    # antigo se o Agendador ainda nao atualizou).
    $insts = @()
    try {
        foreach ($i in @($t.GetInstances(0))) {
            if ($i) { $g = [string]$i.InstanceGuid; if ($g) { $insts += $g } }
        }
    } catch { }
    # LastRunTime/NextRunTime chegam do COM como DateTime LOCAL. O TIPO
    # e preservado ate a formatacao: converter para texto e re-interpretar
    # podia assumir UTC (string invariante sem offset + AssumeUniversal)
    # e exibir 10:26 para uma tarefa que roda as 13:26 em Brasilia.
    return [pscustomobject]@{
        Habilitada = [bool]$t.Enabled
        Estado     = [int]$t.State          # 4 = em execucao
        Ultima     = $t.LastRunTime
        Proxima    = $t.NextRunTime
        Codigo     = [int]$t.LastTaskResult
        Conta      = $contaTarefa
        DoApp      = (Test-TarefaDoApp -Tarefa $t)
        Instancias = $insts
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
        # LastRunTime e LOCAL (COM): ConvertTo-DataLocal aceita o DateTime
        # preservado e tambem texto, sempre interpretando como hora local.
        $u = ConvertTo-DataLocal -Valor $InfoTarefa.Ultima
        if ($u -and $u.Year -gt 2000) { $ultima = $u }
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
    concluido          = 'Backup concluído — resultado confirmado'
    falhou             = 'Falhou — a execução terminou com erro'
    incompleto         = 'Incompleto — Restic código 3 (nem todo arquivo lido)'
    em_execucao        = 'Em execução — backup rodando agora'
    sem_resultado      = 'Sem resultado deste disparo — o Agendador tentou, nada foi gravado'
    ainda_nao          = 'Ainda não executou — nenhum disparo até agora'
    sem_tarefa         = 'Sem tarefa — configure e salve primeiro'
    tarefa_estranha    = 'Tarefa estranha — existe MACIP-Backup que não é do aplicativo'
}

$script:AcaoStatus = @{
    concluido          = 'Nenhuma ação pendente.'
    falhou             = 'Ação necessária: veja o resultado na pasta de estado e confira o destino na aba Ativar.'
    incompleto         = 'Ação necessária: confira o que ficou de fora (arquivos em uso/abertos).'
    em_execucao        = 'Aguarde o término do backup em execução.'
    sem_resultado      = 'Ação necessária: verifique destino, conta da tarefa e permissão de execução sem sessão.'
    ainda_nao          = 'Aguarde o próximo disparo ou use “Executar backup agora”.'
    sem_tarefa         = 'Ação necessária: valide na aba Ativar e salve em “Pasta e horário”.'
    tarefa_estranha    = 'Ação necessária: chame a MAC IP — a tarefa estranha não é tocada.'
}

# ---------- pedido de "Executar agora" ----------
# O clique registra o ESTADO DE REFERENCIA (ultimo LastRunTime + GUIDs
# de instancia) ANTES de solicitar. Enquanto o pedido estiver aberto, a
# linha dele so mostra estados do pedido -- um sucesso anterior NUNCA
# e anunciado como resultado do disparo recem-feito.

function New-PedidoDisparo {
    # Referencia capturada ANTES do Run(): LastRunTime atual, GUIDs das
    # instancias em execucao e a IDENTIDADE do resultado ja gravado
    # (inicio_utc). E a linha de corte: "execucao nova" = um LastRunTime
    # mais novo que a referencia OU uma instancia cujo GUID nao existia
    # no clique; "resultado da execucao nova" = um JSON que NAO existia
    # no clique e com inicio vinculado a ela.
    param($InfoTarefa, $Res, [int]$PrazoSegundos = 120)
    $inst = @{}
    if ($InfoTarefa) {
        foreach ($g in @($InfoTarefa.Instancias)) { if ($g) { $inst[[string]$g] = $true } }
    }
    return [pscustomobject]@{
        # LastRunTime preservado no TIPO do COM (DateTime local); os
        # comparadores usam ConvertTo-DataLocal, que aceita o objeto.
        UltimaRef    = $(if ($InfoTarefa) { $InfoTarefa.Ultima } else { $null })
        InstRef      = $inst
        ResInicioRef = $(if ($Res -and $Res.inicio_utc) { ConvertTo-DataObj -Valor $Res.inicio_utc } else { $null })
        PedidoUtc    = [DateTimeOffset]::UtcNow
        PrazoFimUtc  = [DateTimeOffset]::UtcNow.AddSeconds($PrazoSegundos)
        Iniciado     = $false
        NovaUltima   = $null
        FimUtc       = $null
    }
}

function Update-PedidoDisparo {
    # Estado do PEDIDO (nao da execucao generica): aguardando /
    # em_execucao / concluido / falhou / incompleto / sem_resultado /
    # nao_confirmado. Regras duras:
    #  - 'concluido'/'falhou'/'incompleto' SOMENTE com o JSON de
    #    resultado VINCULADO a execucao nova: inicio_utc posterior ao
    #    resultado que ja existia no clique E ao novo LastRunTime
    #    (tolerancia de 1s de granularidade).
    #  - LastTaskResult NAO identifica a execucao: um codigo antigo de
    #    falha nao encerra o pedido como 'falhou'; a falha da tentativa
    #    nova vem do JSON dela (ou da ausencia dele -> 'sem_resultado').
    #  - execucao nova observada mas terminada sem resultado ainda:
    #    segue acompanhando pela carencia (o JSON pode sair depois do
    #    LastRunTime); expirada -> 'sem_resultado'.
    #  - sem evidencia de execucao nova dentro do prazo, ou consulta
    #    que segue falhando -> 'nao_confirmado'.
    param($Pedido, $InfoTarefa, $Res, $AgoraUtc, [int]$CarenciaFimSegundos = 30)
    if ($null -eq $Pedido) { return $null }
    if ($null -eq $AgoraUtc) { $AgoraUtc = [DateTimeOffset]::UtcNow }
    if ($InfoTarefa) {
        # Ultima mais nova que a REFERENCIA do clique atualiza a marca da
        # execucao nova em toda consulta -- se o disparo foi detectado so
        # pelo GUID da instancia, a Ultima chega atrasada e corrige o
        # limite de vinculo. LastRunTime e LOCAL (COM), nao universal.
        $u = ConvertTo-DataLocal -Valor $InfoTarefa.Ultima
        if ($u -and $u.Year -gt 2000) {
            $ref = ConvertTo-DataLocal -Valor $Pedido.UltimaRef
            if (($null -eq $ref -or $u -gt $ref.AddSeconds(1)) -and
                ($null -eq $Pedido.NovaUltima -or $u -gt $Pedido.NovaUltima)) {
                $Pedido.NovaUltima = $u
            }
        }
        if (-not $Pedido.Iniciado) {
            if ($Pedido.NovaUltima) { $Pedido.Iniciado = $true }
            else {
                foreach ($g in @($InfoTarefa.Instancias)) {
                    if ($g -and -not $Pedido.InstRef.ContainsKey([string]$g)) { $Pedido.Iniciado = $true }
                }
            }
        }
    }
    if (-not $Pedido.Iniciado) {
        # Sem evidencia de execucao nova: consulta inconclusiva e
        # demora do Agendador seguem a mesma regra -- aguardando ate o
        # prazo; estourado, nao foi possivel confirmar.
        if ($AgoraUtc -gt $Pedido.PrazoFimUtc) { return 'nao_confirmado' }
        return 'aguardando'
    }
    if ($InfoTarefa -and [int]$InfoTarefa.Estado -eq 4) {
        $Pedido.FimUtc = $null
        return 'em_execucao'
    }
    if ($null -eq $InfoTarefa) {
        if ($AgoraUtc -gt $Pedido.PrazoFimUtc) { return 'nao_confirmado' }
        return 'em_execucao'   # ja iniciada; consulta falhou: segue aguardando o fim
    }
    # a execucao nova comecou (ou ja terminou entre duas atualizacoes):
    # o resultado conta somente se vinculado a ELA -- um JSON que JA
    # EXISTIA no clique nunca vale, mesmo dentro da tolerancia.
    $limite = $(if ($Pedido.NovaUltima) { $Pedido.NovaUltima } else { $Pedido.PedidoUtc })
    $deste = $false
    if ($Res -and $Res.inicio_utc) {
        $ini = ConvertTo-DataObj -Valor $Res.inicio_utc
        if ($ini) {
            $deste = ($ini -ge $limite.AddSeconds(-1)) -and
                     ($ini -ge $Pedido.PedidoUtc.AddSeconds(-1)) -and
                     (($null -eq $Pedido.ResInicioRef) -or ($ini -gt $Pedido.ResInicioRef))
        }
    }
    if ($deste) {
        if ([string]$Res.status -eq 'em_execucao') { return 'em_execucao' }
        if (Test-ResConfirmado -Res $Res) { return 'concluido' }
        if ($null -ne $Res.codigo_restic -and [int]$Res.codigo_restic -eq 3) { return 'incompleto' }
        return 'falhou'
    }
    # nova execucao encerrada, resultado dela ainda nao gravado:
    # continua acompanhando pela carencia (o JSON pode sair depois do
    # LastRunTime); depois dela e que vira 'sem_resultado'.
    if (-not $Pedido.FimUtc) { $Pedido.FimUtc = $AgoraUtc }
    if ($AgoraUtc -le $Pedido.FimUtc.AddSeconds($CarenciaFimSegundos)) { return 'em_execucao' }
    return 'sem_resultado'
}

$script:RotulosPedido = @{
    aguardando     = 'Aguardando início — o Agendador ainda não registrou uma execução nova.'
    em_execucao    = 'Backup em execução agora.'
    concluido      = 'Backup concluído — resultado confirmado desta execução.'
    falhou         = 'A execução terminou com falha — veja a ação indicada e os Detalhes.'
    incompleto     = 'A execução terminou incompleta — veja a ação indicada e os Detalhes.'
    sem_resultado  = 'O Agendador tentou e nada foi gravado — veja a ação indicada e os Detalhes.'
    nao_confirmado = 'Não foi possível confirmar a execução — consulte o Agendador de Tarefas ou chame a MAC IP.'
}

$script:AcaoPedido = @{
    aguardando     = 'Aguarde — a tela acompanha sozinha enquanto o Agendador registra o disparo.'
    em_execucao    = 'Aguarde o término do backup em execução.'
    concluido      = 'Nenhuma ação pendente.'
    falhou         = 'Ação necessária: veja o resultado na pasta de estado e confira o destino na aba Ativar.'
    incompleto     = 'Ação necessária: confira o que ficou de fora (arquivos em uso/abertos).'
    sem_resultado  = 'Ação necessária: verifique destino, conta da tarefa e permissão de execução sem sessão.'
    nao_confirmado = 'Ação necessária: consulte o Agendador de Tarefas ou chame a MAC IP.'
}

function Get-CorEstadoPedido {
    param([string]$Classe)
    switch ($Classe) {
        'concluido'     { return 'DarkGreen' }
        'aguardando'    { return 'DarkBlue' } 'em_execucao' { return 'DarkBlue' }
        'incompleto'    { return 'DarkOrange' } 'sem_resultado' { return 'DarkOrange' }
        default         { return 'DarkRed' }
    }
}

function Get-TelaExecucao {
    # MODELO de renderizacao da aba Situacao -- a MESMA decisao alimenta
    # a linha principal, a cor, a mensagem de orientacao e o botao, para
    # que nunca se contradigam. Um pedido aberto dita tudo; depois de
    # encerrado, sua RESOLUCAO segue sendo a situacao apresentada ate um
    # disparo MAIS NOVO aparecer -- a tela nao volta a anunciar o
    # resultado anterior como se fosse o do pedido recem-encerrado.
    param($InfoTarefa, $Res, $Pedido, $Resolucao, $AgoraUtc)
    if ($null -eq $AgoraUtc) { $AgoraUtc = [DateTimeOffset]::UtcNow }
    if ($Pedido) {
        $st = Update-PedidoDisparo -Pedido $Pedido -InfoTarefa $InfoTarefa -Res $Res -AgoraUtc $AgoraUtc
        return [pscustomobject]@{
            Origem = 'pedido'; Classe = $st; Texto = $script:RotulosPedido[$st]
            Cor = (Get-CorEstadoPedido $st); Acao = $script:AcaoPedido[$st]
            Mensagem = $script:RotulosPedido[$st]
            Encerrou = ($st -in 'concluido','falhou','incompleto','sem_resultado','nao_confirmado')
            ResolucaoExpirada = $false
        }
    }
    if ($Resolucao) {
        # a resolucao vale ate um disparo MAIS NOVO que o observado na
        # resolucao aparecer (rodando agora ou LastRunTime posterior).
        $novo = $false
        if ($InfoTarefa) {
            if ([int]$InfoTarefa.Estado -eq 4) { $novo = $true }
            $u = ConvertTo-DataLocal -Valor $InfoTarefa.Ultima
            if ($u -and $u.Year -gt 2000) {
                $marca = ConvertTo-DataLocal -Valor $Resolucao.UltimaObservada
                if (($null -eq $marca) -or ($u -gt $marca.AddSeconds(1))) { $novo = $true }
            }
        }
        if (-not $novo) {
            return [pscustomobject]@{
                Origem = 'resolucao'; Classe = $Resolucao.Classe
                Texto = $script:RotulosPedido[$Resolucao.Classe]
                Cor = (Get-CorEstadoPedido $Resolucao.Classe); Acao = $script:AcaoPedido[$Resolucao.Classe]
                Mensagem = $Resolucao.Mensagem; Encerrou = $false; ResolucaoExpirada = $false
            }
        }
        # disparo mais novo observado: a resolucao expira e a
        # classificacao generica volta a descrever a execucao atual.
    }
    $cls = Classify-Resultado -Res $Res -InfoTarefa $InfoTarefa
    return [pscustomobject]@{
        Origem = 'generica'; Classe = $cls; Texto = $script:RotulosStatus[$cls]
        Cor = $(switch ($cls) {
            'concluido' { 'DarkGreen' } 'em_execucao' { 'DarkBlue' }
            'incompleto' { 'DarkOrange' } 'ainda_nao' { 'DarkBlue' }
            'sem_resultado' { 'DarkOrange' } default { 'DarkRed' } })
        Acao = $script:AcaoStatus[$cls]; Mensagem = $null
        Encerrou = $false; ResolucaoExpirada = ($null -ne $Resolucao)
    }
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

# ---------- restauracao da pasta protegida inteira ----------

function Test-NomePastaDestino {
    # Nome da pasta nova do destino: UM unico componente de caminho
    # valido para Windows — sem separador, sem '..', sem reservados.
    param([string]$Nome)
    if (-not $Nome -or -not $Nome.Trim()) { return 'Informe o nome da nova pasta.' }
    $n = $Nome.Trim()
    if ($n -ne $Nome) { return 'O nome nao pode comecar nem terminar com espaco.' }
    if ($n.EndsWith('.')) { return 'O nome nao pode terminar com ponto.' }
    if ($n -in @('.', '..')) { return "O nome '$n' nao e uma pasta valida." }
    # conjunto WINDOWS explicito (o alvo e sempre Windows): no Linux,
    # GetInvalidFileNameChars nao inclui '\' nem ':' e deixaria passar
    if ($n.IndexOfAny([char[]]'\/:*?"<>|') -ge 0) {
        return 'O nome tem caractere invalido para pasta do Windows (\ / : * ? " < > |).'
    }
    $base = ($n -split '\.')[0]
    if ($base -match '^(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])$') {
        return "O nome '$base' e reservado pelo Windows."
    }
    return $null
}

function Get-RaizSnapshot {
    # Raiz protegida registrada NO PONTO — metadados (paths do snapshot)
    # confirmados pelo catalogo (restic ls). A origem atual pode ter
    # mudado depois do backup: vale o que o ponto gravou. Sempre o ID
    # COMPLETO escolhido — nunca 'latest' nem arvore alheia.
    #
    # Duas formas coexistem no ponto real:
    #   ORIGEM REGISTRADA — o path gravado nos metadados ('C:\Users\...'
    #     ou '/tmp/...'): identifica o ponto para o tecnico e protege o
    #     destino (comparedo no filesystem).
    #   RAIZ INTERNA — a forma do catalogo ('/C/Users/...' para
    #     'C:\Users\...'; POSIX ja vem assim): e o <id>:<subpasta> do
    #     'restic restore'. Nunca se deduz do primeiro arquivo nem se
    #     passa o caminho Windows bruto: a interna e a registrada
    #     normalizada, e so vale se um no do catalogo a confirma EXATA.
    #
    # O catalogo real inclui os ANCESTRAIS da raiz ('/tmp', '/tmp/x'...)
    # — aceitos por segmento completo e fora do conjunto esperado.
    # Raiz multipla, itens fora da raiz, links, '..' -> para com o motivo.
    param([pscustomobject]$Cfg, [string]$Estado, [string]$Snapshot, [object]$PathsMeta)
    $paths = @($PathsMeta | Where-Object { $_ })
    if ($paths.Count -ne 1) {
        throw ('Nao foi possivel identificar a pasta protegida desse ponto ' +
            '(metadados sem uma raiz unica). Escolha outro ponto ou chame a MAC IP.')
    }
    $origem = [string]$paths[0]
    # registrada -> interna: barras e letra de unidade ('C:\x' -> '/C/x')
    $interna = ([string]$origem).TrimEnd('\','/') -replace '\\','/'
    if ($interna -match '^([A-Za-z]):/') { $interna = '/' + $Matches[1].ToUpper() + $interna.Substring(2) }
    $r = Invoke-ResticConsulta -Cfg $Cfg -Estado $Estado -Comando @('ls', '--json', $Snapshot) -PrazoMs ([int]$script:ConsultaTimeoutMs)
    if ($r.Codigo -ne 0) { throw "Falha ao ler o catalogo do ponto (restic $($r.Codigo)): $(($r.Linhas | Select-Object -First 3) -join ' ')" }
    $confirma = $false
    $esperados = @{}
    foreach ($linha in $r.Linhas) {
        try { $no = $linha | ConvertFrom-Json } catch { continue }
        # no "snapshot" do catalogo repete os paths gravados no ponto
        # (@($null).Count == 1 -- confere o CONTEUDO, nao a propriedade)
        $decl = @($no.paths | Where-Object { $_ })
        if ($decl.Count -gt 0) {
            if ($decl.Count -ne 1 -or
                ([string]$decl[0] -ine $origem -and [string]$decl[0] -ine $interna)) {
                throw ("O catalogo do ponto registra raizes diferentes da escolhida " +
                    "(catalogo: '$($decl -join '|')' / ponto: '$origem') — ambiguo, restauracao recusada.")
            }
            continue
        }
        if (-not $no.path) { continue }
        $np = ([string]$no.path).TrimEnd('\','/')
        if ($np -ieq $interna) {
            # correspondencia EXATA da raiz interna — dir obrigatorio
            if ([string]$no.type -ne 'dir') {
                throw "A raiz registrada nao e pasta no catalogo ('$np') — ambiguo, restauracao recusada."
            }
            $confirma = $true
            continue
        }
        if ($interna.StartsWith($np + '/')) {
            continue   # ancestral legitimo da raiz: aceito, fora do esperado
        }
        if (-not $np.StartsWith($interna + '/')) {
            throw ("O catalogo tem item fora da raiz registrada ('$np') sem outra raiz " +
                'declarada — catalogo ambiguo, restauracao recusada.')
        }
        $tipo = [string]$no.type
        if ($tipo -ne 'file' -and $tipo -ne 'dir') {
            throw ("O ponto contem '$tipo' ($($no.path)) — links e junctions nao sao " +
                'restaurados para nao comprometer o isolamento; restaure por arquivo.')
        }
        # caminho relativo seguro: nada de '..' nem absoluto escondido
        $rel = $np.Substring($interna.Length).TrimStart('\','/')
        $segs = @($rel -split '[\\/]+' | Where-Object { $_ -ne '' })
        if (($segs | Where-Object { $_ -eq '..' }).Count -gt 0 -or
            ($rel -match '^[a-zA-Z]:')) {
            throw "Caminho inseguro no catalogo ('$($no.path)') — restauracao recusada."
        }
        $esperados[($segs -join '/')] = $tipo
    }
    if (-not $confirma) {
        throw 'O catalogo do ponto nao confirma a raiz registrada — recusado (nunca restaurar uma arvore alheia).'
    }
    return [pscustomobject]@{ Origem = $origem; Raiz = $interna; Esperados = $esperados }
}

function Invoke-ResticRestore {
    # 'restic restore "<id>:<raiz>" --target <tmp> --verify' materializa
    # SOMENTE a raiz escolhida do ponto (sintaxe id:subpasta do restic —
    # nunca o ponto inteiro, nunca 'latest', nunca a origem de hoje).
    # Sem --include/--exclude: restaura o que foi efetivamente salvo
    # naquela data (as exclusoes ATUAIS nao se aplicam). --verify faz o
    # proprio restic conferir o conteudo restaurado contra o repositorio.
    # Mesma disciplina do dump: stdout+stderr drenados em paralelo, UM
    # prazo monotonico cobre processo e leitura, no estouro Kill.
    param([pscustomobject]$Cfg, [string]$Estado, [string]$Snapshot, [string]$Raiz, [string]$AlvoTmp)
    $exe = Join-Path $Estado $script:ResticExeNome
    $senha = Read-ResticPassword -Caminho (Join-Path $Estado 'restic.dpapi')
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($senha)
    $p = $null
    try {
        $psi = [Diagnostics.ProcessStartInfo]::new()
        $sftpCmd = 'sftp.command={0} -F {1} -s {2} sftp' -f $Cfg.ssh_exe, $Cfg.ssh_config, $Cfg.sftp_host
        $resticArgs = '--repo "{0}" --cache-dir "{1}" -o "{2}" restore "{3}:{4}" --target "{5}" --verify' -f `
            $Cfg.repositorio, (Join-Path $Estado 'cache'), $sftpCmd, $Snapshot, $Raiz, $AlvoTmp
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
        $prazo = { [int][Math]::Max(0, [int]$script:DumpTimeoutMs - $relogio.ElapsedMilliseconds) }
        $outTask = $p.StandardOutput.ReadToEndAsync()
        $errTask = $p.StandardError.ReadToEndAsync()
        if (-not $p.WaitForExit((& $prazo))) {
            try { $p.Kill() } catch { }
            throw ("Restauracao excedeu o tempo limite de $([int]($script:DumpTimeoutMs / 60000)) " +
                'minutos — a pasta parcial foi descartada.')
        }
        if (-not ($outTask.Wait((& $prazo)) -and $errTask.Wait((& $prazo)))) {
            try { $p.Kill() } catch { }
            throw 'Leitura da saida do restic excedeu o tempo limite — a pasta parcial foi descartada.'
        }
        $err = try { [string]$errTask.Result } catch { '(stderr ilegivel)' }
        if ($p.ExitCode -ne 0) { throw "Falha na restauracao (restic $($p.ExitCode)): $err" }
        return $true
    } finally {
        if ($p) { $p.Dispose() }
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
        if ($senha) { $senha.Dispose() }
    }
}

function Restore-PastaCompleta {
    # Restaura TODA a pasta protegida do ponto escolhido, num destino NOVO:
    # nada existente no caminho final (colisao = recusa), fora da raiz
    # gravada no ponto E fora da origem atual — inclusive por link/junction.
    # Materializa num temporario IRMAO do destino (mesmo volume) e so
    # publica por renomeacao depois de codigo 0 + arvore esperada presente.
    # Em qualquer falha remove APENAS o temporario desta tentativa.
    param([string]$Estado, [string]$Config, [string]$Snapshot, [object]$PathsMeta,
          [string]$Destino, [string]$Origem)
    if (-not $Destino) { throw 'Escolha a pasta de destino da restauracao.' }
    # Resolve todos os componentes — link/junction num pai muda o destino.
    $destResolvido = Resolve-PathSeguro -Caminho $Destino
    if (Test-Path -LiteralPath $destResolvido) {
        throw "O destino precisa ser NOVO — a restauracao nunca sobrescreve: $destResolvido"
    }
    $pai = Split-Path -LiteralPath $destResolvido
    if (-not $pai -or -not (Test-Path -LiteralPath $pai -PathType Container)) {
        throw "A pasta pai do destino nao existe: $pai"
    }
    $cfg = Get-CfgOperacional -Estado $Estado -Config $Config
    # MESMAS verificacoes do backup e do modo arquivo: conta == preparo,
    # vinculos (chave/known_hosts/ssh_config) e ID do repositorio aprovado.
    [void](Confirm-DestinoOperacional -Cfg $cfg -Estado $Estado)
    # raiz gravada NO PONTO (metadados + catalogo): $info.Origem e a
    # forma registrada (tecnico + protecao do destino); $info.Raiz e a
    # raiz INTERNA confirmada (o <id>:<subpasta> do restore).
    $info = Get-RaizSnapshot -Cfg $cfg -Estado $Estado -Snapshot $Snapshot -PathsMeta $PathsMeta
    $raizMeta = $info.Origem
    $raizInt = $info.Raiz
    foreach ($o in @($raizMeta, $cfg.origem, $Origem)) {
        if (-not $o) { continue }
        $oRes = Resolve-PathSeguro -Caminho $o
        if ($destResolvido.TrimEnd('\','/') -ieq $oRes.TrimEnd('\','/')) {
            throw 'A restauracao nao pode ser feita sobre a pasta protegida (nem por link/junction).'
        }
        if ($destResolvido.StartsWith($oRes.TrimEnd('\','/') + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
            throw 'A pasta de destino esta DENTRO da pasta protegida — escolha outra.'
        }
    }
    $tmp = Join-Path $pai ('.macip-rest-' + [Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $tmp | Out-Null
    try {
        [void](Invoke-ResticRestore -Cfg $cfg -Estado $Estado -Snapshot $Snapshot -Raiz $raizInt -AlvoTmp $tmp)
        # 'restore "<id>:<raiz>"' pousa o CONTEUDO da raiz direto na raiz
        # do temporario ('tmp\sub\arq'), nao numa arvore absoluta aninhada.
        # Codigo 0 com pasta parcial NAO e sucesso: o conjunto rel->tipo
        # do catalogo tem de ser reproduzido — falta, item extra, tipo
        # errado ou link restaurado -> nada publicado.
        $vistos = @{}
        foreach ($item in (Get-ChildItem -LiteralPath $tmp -Recurse -Force)) {
            if ($item.LinkType -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
                throw ("Link/junction restaurado ('$($item.FullName)') — publicacao " +
                    'abortada para nao comprometer o isolamento.')
            }
            $relItem = $item.FullName.Substring($tmp.Length).TrimStart('\','/')
            $vistos[($relItem -replace '\\','/')] = $(if ($item.PSIsContainer) { 'dir' } else { 'file' })
        }
        foreach ($relE in $info.Esperados.Keys) {
            if (-not $vistos.Contains($relE) -or $vistos[$relE] -cne $info.Esperados[$relE]) {
                throw "Restauracao incompleta — '$relE' esperado no ponto nao apareceu como $($info.Esperados[$relE]). Nada publicado."
            }
        }
        foreach ($relV in $vistos.Keys) {
            if (-not $info.Esperados.Contains($relV)) {
                throw "Item inesperado na arvore restaurada ('$relV') — nada publicado."
            }
        }
        # Publica com operacao que FALHA se o caminho final existir
        # ([IO.Directory]::Move) — uma checagem previa nao basta: se outro
        # processo criou arquivo ou pasta no destino durante a copia, ele
        # fica INTACTO, o conflito e informado e so o temporario e limpo.
        try { [IO.Directory]::Move($tmp, $destResolvido) }
        catch {
            if (Test-Path -LiteralPath $destResolvido) {
                throw ('Conflito: o destino apareceu durante a restauracao — ' +
                    'preservado intacto, nada foi sobrescrito. Escolha outro nome.')
            }
            throw "Nao foi possivel publicar a pasta restaurada: $($_.Exception.Message)"
        }
        if (-not (Test-Path -LiteralPath $destResolvido -PathType Container)) {
            throw 'Publicacao falhou: destino esperado ausente apos a restauracao.'
        }
        $itens = @(Get-ChildItem -LiteralPath $destResolvido -Recurse -File -Force -ErrorAction SilentlyContinue).Count
        return [pscustomobject]@{ Destino = $destResolvido; Raiz = $raizMeta; Itens = $itens }
    } finally {
        # falha -> limpa APENAS o temporario desta tentativa (no sucesso
        # ele virou o destino; Test-Path torna o Remove inocuo)
        if (Test-Path -LiteralPath $tmp) { Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue }
    }
}

# ---------- pre-requisitos ----------

function Get-PreRequisitos {
    $falta = @()
    $sshResolvido = $null
    # VERIFICACAO DEFINITIVA do OpenSSH: roda aqui, na conta que executa
    # o backup, no MESMO criterio do sftp.command (o instalador so
    # confere existencia -- x86 nem ve o System32 nativo). Distingue
    # ausente de encontrado-mas-incompativel e diz a acao ao tecnico.
    $st = Get-SshStatus
    switch ($st.Status) {
        'ok'           { $sshResolvido = $st.Caminho }
        'incompativel' { $falta += ("Cliente OpenSSH incompatível: $($st.Mensagem). O ssh.exe encontrado " +
            "não serve para o backup — ative o recurso opcional 'Cliente OpenSSH' ou chame a MAC IP.") }
        default        { $falta += ("Cliente OpenSSH ausente: nenhum ssh.exe utilizável encontrado nesta " +
            "conta. Ative o recurso opcional 'Cliente OpenSSH' (Configurações > Aplicativos > Recursos " +
            "opcionais > Adicionar recurso) ou chame a MAC IP.") }
    }
    $restic = Join-Path $script:BaseDir $script:ResticExeNome
    if (-not (Test-Path -LiteralPath $restic -PathType Leaf)) {
        $restic = Join-Path $PSScriptRoot $script:ResticExeNome
    }
    if (-not (Test-Path -LiteralPath $restic -PathType Leaf)) {
        $falta += "$($script:ResticExeNome) ausente — o instalador da MAC IP coloca o binário aprovado na pasta do aplicativo."
    }
    return [pscustomobject]@{ Ok = ($falta.Count -eq 0); Faltas = $falta; ResticExe = $restic; SshExe = $sshResolvido }
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

# ---------- apresentacao (helpers puros, testaveis sem janela) ----------

function ConvertTo-DataObj {
    # Normaliza DateTime/DateTimeOffset/texto em DateTimeOffset; $null se
    # nao reconhecivel. Texto ISO e tratado como universal; texto na
    # cultura local (saida stringificada do COM em pt-BR) tambem casa.
    param($Valor)
    if ($null -eq $Valor) { return $null }
    if ($Valor -is [DateTime]) { return ([DateTimeOffset]$Valor) }
    if ($Valor -is [DateTimeOffset]) { return $Valor }
    # Variavel tipada: sem ela, [ref]$d ($null) nao resolve o overload
    # "out DateTimeOffset" do TryParse.
    [DateTimeOffset]$d = [DateTimeOffset]::MinValue
    if ([DateTimeOffset]::TryParse([string]$Valor, [Globalization.CultureInfo]::InvariantCulture,
            [Globalization.DateTimeStyles]::AssumeUniversal, [ref]$d)) { return $d }
    if ([DateTimeOffset]::TryParse([string]$Valor, [ref]$d)) { return $d }
    return $null
}

function ConvertTo-DataLocal {
    # Horario do Agendador (LastRunTime/NextRunTime do COM) e sempre
    # LOCAL: interpreta a string como hora local desta maquina -- nunca
    # como universal (UTC). Usar AssumeUniversal aqui quebraria o vinculo
    # com inicio_utc (UTC de verdade) em qualquer fuso diferente de +00:00.
    param($Valor)
    if ($null -eq $Valor) { return $null }
    if ($Valor -is [DateTime]) { return ([DateTimeOffset]$Valor) }
    if ($Valor -is [DateTimeOffset]) { return $Valor }
    [DateTimeOffset]$d = [DateTimeOffset]::MinValue
    if ([DateTimeOffset]::TryParse([string]$Valor, [ref]$d)) { return $d }
    return $null
}

function Format-DataBR {
    # Data/hora deste computador em formato brasileiro (dd/MM/yyyy HH:mm)
    # para a tela. Invalido/ausente/ano de sentinelas COM -> '-'.
    param($Valor)
    $d = ConvertTo-DataObj -Valor $Valor
    if ($null -eq $d -or $d.Year -lt 2000) { return '-' }
    return $d.LocalDateTime.ToString('dd/MM/yyyy HH:mm')
}

function Format-PreviaHorario {
    # Previa do agendamento diario mostrada ANTES de salvar.
    param([string]$HHmm)
    if ($HHmm -cnotmatch '^([01][0-9]|2[0-3]):[0-5][0-9]$') { return 'Horário inválido (use HH:mm).' }
    return ("Todos os dias às {0}, no horário deste computador." -f $HHmm)
}

function Get-ResumoConfiguracao {
    # Resumo exibido em "Revisar e salvar" -- o que vai ser gravado.
    param([string]$Origem, [string]$HHmm, [string]$Destino, [string]$Conta)
    $linhas = @(
        ('Pasta protegida: ' + $(if ($Origem) { $Origem } else { '(não escolhida)' }))
        ('Horário: ' + $(if ($HHmm -cmatch '^([01][0-9]|2[0-3]):[0-5][0-9]$') { "todos os dias às $HHmm (horário local)" } else { '(inválido)' }))
        ('Frequência: diária')
        ('Destino: ' + $(if ($Destino) { $Destino } else { '(sem perfil novo — reusa o destino atual)' }))
        ('Conta da tarefa: ' + $(if ($Conta) { $Conta } else { '-' }))
    )
    return ($linhas -join "`r`n")
}

function Get-DetalhesSuporte {
    # Bloco "Detalhes (suporte)" da aba Situacao: IDs e dados tecnicos
    # para repassar a MAC IP -- SEM segredos (nunca chave/senha).
    param($Info, $Res, $Conf, [string]$Estado, [string]$ConfigPath, [string]$Origem = '')
    $linhas = @()
    $linhas += ('conta da tarefa: ' + $(if ($Info -and $Info.Conta) { [string]$Info.Conta } else { '-' }))
    $linhas += ('pasta protegida: ' + $(if ($Origem) { $Origem } else { '-' }))
    $linhas += ('último código do Agendador: ' + $(if ($Info) { ('0x{0:X8}' -f ([int]$Info.Codigo)) } else { '-' }))
    $linhas += ('último disparo registrado: ' + $(if ($Info) { (Format-DataBR $Info.Ultima) } else { '-' }))
    if ($Res) {
        $linhas += ('último resultado gravado: status=' + [string]$Res.status +
            ' restic=' + $(if ($null -ne $Res.codigo_restic) { [string]$Res.codigo_restic } else { '-' }))
        $linhas += ('  início: ' + (Format-DataBR $Res.inicio_utc) + '   fim: ' + (Format-DataBR $Res.fim_utc))
        if ($Res.snapshot) { $linhas += ('  snapshot: ' + [string]$Res.snapshot) }
        if ($Res.confirmado_erro) { $linhas += ('  confirmado_erro: ' + [string]$Res.confirmado_erro) }
        if ($Res.erro) { $linhas += ('  erro: ' + [string]$Res.erro) }
    } else { $linhas += 'último resultado gravado: nenhum arquivo' }
    if ($Conf) {
        $linhas += ('confirmado: ' + (Format-DataBR $Conf.fim_utc) + '   snapshot ' + [string]$Conf.snapshot)
    } else { $linhas += 'confirmado: nenhum ainda' }
    $linhas += ('estado: ' + $(if ($Estado) { $Estado } else { '-' }))
    $linhas += ('config: ' + $(if ($ConfigPath) { $ConfigPath } else { '-' }))
    $linhas += ('powershell: ' + $PSVersionTable.PSVersion.ToString())
    return ($linhas -join "`r`n")
}

function Get-MensagemFalha {
    # Uma mensagem curta por falha: perda de conexao IDENTIFICADA ganha a
    # orientacao propria; todo o resto mantem o texto original -- destino
    # invalido, pasta existente, falta de permissao e falhas desconhecidas
    # NUNCA viram "conexao interrompida". O diagnostico completo continua
    # disponivel nos detalhes tecnicos (recolhidos, sem segredos).
    param([string]$Erro)
    if (-not $Erro) { return 'A operação falhou. Repita; se o erro continuar, acione a MAC IP.' }
    if ($Erro -match '(?i)connection (lost|reset|closed|aborted|timed out)|lost connection|connection closed by|broken pipe|unexpected eof|software caused connection abort|i/o timeout|network is unreachable|no route to host|session.{0,20}(terminated|closed)|connection.{0,20}interromp') {
        return 'A conexão com o servidor foi interrompida. Verifique a conexão e tente restaurar novamente.'
    }
    return $Erro
}

function Test-ConfiguracaoPronta {
    # Abertura direta na aba Situacao: existe uma configuracao que a
    # tarefa usa (arquivo de config) e um estado para ler resultados.
    $c = Get-ConfigAtual; $e = Get-EstadoAtual
    return [bool]($c -and $e -and (Test-Path -LiteralPath $c -PathType Leaf))
}

function Open-PastaRestaurada {
    # Abre o Explorer SELECIONANDO o arquivo restaurado. Nunca derruba a
    # tela: qualquer falha vira $false e o chamador avisa na propria tela.
    param([string]$Alvo)
    try {
        if (-not (Test-IsWindows)) { return $false }
        if (-not $Alvo -or -not (Test-Path -LiteralPath $Alvo)) { return $false }
        # pasta inteira -> abre o proprio destino; arquivo -> /select
        if (Test-Path -LiteralPath $Alvo -PathType Container) {
            [Diagnostics.Process]::Start('explorer.exe', ('"{0}"' -f $Alvo)) | Out-Null
        } else {
            [Diagnostics.Process]::Start('explorer.exe', ('/select,"{0}"' -f $Alvo)) | Out-Null
        }
        return $true
    } catch { return $false }
}

function Write-Diagnostico {
    # Diagnostico SEM SEGREDOS gravado quando a janela nao abre -- o
    # tecnico envia o arquivo a MAC IP. Junta a excecao, versoes, a
    # resolucao do OpenSSH (mesma do agente) e a presenca do restic.
    # NUNCA inclui conteudo de chaves, senhas, backup.json ou perfil.
    param([System.Exception]$Erro)
    $linhas = @(
        'MAC IP Backup - diagnostico de abertura'
        ('data local: ' + (Get-Date).ToString('yyyy-MM-dd HH:mm:ss'))
        ('erro: ' + $(if ($Erro) { [string]$Erro.Message } else { '(sem detalhe)' }))
        ('powershell: ' + $PSVersionTable.PSVersion.ToString())
        ('so: ' + [Environment]::OSVersion.VersionString)
    )
    try { $linhas += ('conta: ' + (Get-IdentidadeAtual).Nome) } catch { }
    $linhas += ('basedir: ' + $script:BaseDir + ' existe=' + (Test-Path -LiteralPath $script:BaseDir))
    try { $linhas += ('ssh resolvido: ' + (Get-SshExe)) } catch { $linhas += ('ssh: ' + $_.Exception.Message) }
    try {
        $pre = Get-PreRequisitos
        $linhas += ('restic: ' + $pre.ResticExe + ' existe=' + (Test-Path -LiteralPath $pre.ResticExe -PathType Leaf))
        foreach ($f in $pre.Faltas) { $linhas += ('falta: ' + $f) }
    } catch { }
    foreach ($p in @($script:BaseDir, $env:TEMP)) {
        try {
            if (-not $p) { continue }
            if (-not (Test-Path -LiteralPath $p)) { New-Item -ItemType Directory -Path $p -Force | Out-Null }
            $f = Join-Path $p 'diagnostico-macip.txt'
            [IO.File]::WriteAllText($f, ($linhas -join "`r`n"))
            return $f
        } catch { }
    }
    return $null
}

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
    $form.Size = New-Object System.Drawing.Size(720, 680)
    $form.MinimumSize = New-Object System.Drawing.Size(660, 560)
    $form.StartPosition = 'CenterScreen'
    # Janela redimensionavel + AutoScroll: textos longos e telas remotas
    # nunca cortam. Escala com a densidade da tela (100/125/150%).
    $form.AutoScaleMode = 'Dpi'
    # Fonte do Windows, superficie neutra, UM acento para a acao
    # principal de cada aba; estado sempre vem com cor E texto.
    try { $form.Font = New-Object System.Drawing.Font('Segoe UI', 9) } catch { }
    $corAcento = [Drawing.Color]::FromArgb(0, 120, 215)      # acento Windows
    $corNeutra = [Drawing.Color]::FromArgb(250, 250, 250)

    $tabs = New-Object "$W.TabControl"
    $tabs.Dock = 'Fill'
    $form.Controls.Add($tabs)

    # Helpers de LAYOUT: cada aba e um TableLayoutPanel em uma coluna --
    # linhas AutoSize empilham os blocos; nenhuma coordenada fixa. Um
    # controle por linha via Add-Linha; rotulos quebram na largura
    # disponivel (MaximumSize atualizado em cada Layout) e campos
    # expandem/encolhem com a janela -- nada de rolagem horizontal.
    $script:Aba = @{}   # titulo -> TabPage (SelectedTab aceita a pagina, nao o painel)
    $script:Rotulos = New-Object 'System.Collections.Generic.List[System.Windows.Forms.Label]'
    function Update-QuebraRotulos {
        # Largura util = cliente do pai imediato (a aba ou o painel
        # interno) menos as margens. Quando a barra vertical aparece, o
        # ClientSize ja encolheu -> a quebra acompanha na mesma passada
        # de layout; sem isso o texto continua cortado atras da barra.
        foreach ($l in $script:Rotulos) {
            try {
                $p = $l.Parent
                if (-not $p) { continue }
                $disp = $p.ClientSize.Width - $p.Padding.Horizontal - 2
                if ($disp -lt 120) { continue }
                if ($l.MaximumSize.Width -ne $disp) {
                    $l.MaximumSize = New-Object System.Drawing.Size($disp, 0)
                }
            } catch { }
        }
    }
    function New-Aba([string]$t) {
        $p = New-Object "$W.TabPage"; $p.Text = $t; $p.BackColor = $corNeutra
        $tabs.TabPages.Add($p)
        $script:Aba[$t] = $p
        $ly = New-Object 'System.Windows.Forms.TableLayoutPanel'
        $ly.Dock = 'Fill'; $ly.AutoScroll = $true; $ly.ColumnCount = 1
        $ly.Padding = New-Object "$W.Padding"(18, 16, 18, 16)
        # Layout dispara em resize, scroll e show: recalcula a quebra dos
        # rotulos SEMPRE pela largura util atual (janela estreita incluida).
        $ly.Add_Layout({ Update-QuebraRotulos })
        $p.Controls.Add($ly)
        return $ly
    }
    function Add-Linha([System.Windows.Forms.TableLayoutPanel]$ly, $c, [int]$tab, [int]$margemTopo = 3) {
        $row = $ly.RowCount
        $ly.RowCount += 1
        $ly.RowStyles.Add((New-Object 'System.Windows.Forms.RowStyle'('AutoSize')))
        $c.Dock = 'Top'
        $c.Margin = New-Object "$W.Padding"(0, $margemTopo, 0, 3)
        if ($tab -ge 0) { $c.TabIndex = $tab }
        $ly.Controls.Add($c, 0, $row)
        return $c
    }
    function New-Rotulo([string]$t, [bool]$titulo = $false) {
        # AutoSize + MaximumSize (definido em Update-QuebraRotulos): texto
        # longo QUEBRA na largura util da aba -- nunca e cortado nem gera
        # rolagem horizontal; redimensionar so muda a quebra.
        $l = New-Object 'System.Windows.Forms.Label'
        $l.Text = $t; $l.AutoSize = $true
        if ($titulo) { $l.Font = New-Object System.Drawing.Font($form.Font, [Drawing.FontStyle]::Bold) }
        $script:Rotulos.Add($l) | Out-Null
        return $l
    }
    function New-Titulo([string]$t) {
        # titulo da TELA: um passo acima do corpo (hierarquia unica)
        $l = New-Rotulo $t
        try { $l.Font = New-Object System.Drawing.Font('Segoe UI', 10.5, [Drawing.FontStyle]::Bold) } catch { }
        return $l
    }
    function New-Secao([string]$t) { return (New-Rotulo $t $true) }   # cabecalho de grupo
    function New-DetalhesRecolhidos($ly, [string]$textoCap, [int]$tabIdx, [System.Windows.Forms.TextBox]$tb) {
        # Secao 'Mostrar detalhes tecnicos': RECOLHIDA por padrao. A linha
        # fica AutoSize (0 com o painel oculto) e vira Percent 100 so
        # quando expandida -- ocupa o restante da aba sem empurrar nada.
        $ck = New-Object 'System.Windows.Forms.CheckBox'
        $ck.Text = 'Mostrar detalhes técnicos'; $ck.AutoSize = $true
        $ck.AccessibleName = 'Mostrar detalhes técnicos (IDs, caminhos e diagnóstico, sem senhas)'
        Add-Linha $ly $ck $tabIdx -margemTopo 10 | Out-Null
        $pnl = New-Object "$W.Panel"
        $pnl.Dock = 'Fill'; $pnl.Visible = $false
        $pnl.MinimumSize = New-Object System.Drawing.Size(0, 140)
        $cap = New-Rotulo $textoCap; $cap.Dock = 'Top'
        $tb.Dock = 'Fill'; $tb.Margin = New-Object "$W.Padding"(0, 3, 0, 3)
        # ultimo Add ancora primeiro: o rotulo toma o topo, o texto preenche o resto
        $pnl.Controls.Add($tb); $pnl.Controls.Add($cap)
        $row = $ly.RowCount; $ly.RowCount += 1
        $ly.RowStyles.Add((New-Object 'System.Windows.Forms.RowStyle'('AutoSize'))) | Out-Null
        $pnl.Margin = New-Object "$W.Padding"(0, 3, 0, 3)
        $ly.Controls.Add($pnl, 0, $row)
        # Estado viajando NO CONTROLE, nao em variavel da funcao: cada
        # checkbox carrega seu proprio painel/tabela/linha no Tag e o
        # handler le pelo $sender -- as duas secoes sao independentes por
        # construcao e nada depende de escopo capturado.
        $ck.Tag = @{ Painel = $pnl; Tabela = $ly; Linha = $row }
        $ck.Add_CheckedChanged({
            $d = $sender.Tag
            $d.Painel.Visible = $sender.Checked
            $d.Tabela.RowStyles[$d.Linha] = New-Object 'System.Windows.Forms.RowStyle'(
                $(if ($sender.Checked) { 'Percent' } else { 'AutoSize' }), $(if ($sender.Checked) { 100 } else { 0 }))
        })
        return $ck
    }
    function New-CampoRotulo([string]$t) { return (New-Rotulo $t) }
    function New-LinhaCampo($campo, $botao) {
        # campo ocupa toda a largura livre; botao a direita no tamanho dele
        $ln = New-Object 'System.Windows.Forms.TableLayoutPanel'
        $ln.AutoSize = $true; $ln.ColumnCount = 2
        $ln.ColumnStyles.Add((New-Object 'System.Windows.Forms.ColumnStyle'('Percent', 100))) | Out-Null
        $ln.ColumnStyles.Add((New-Object 'System.Windows.Forms.ColumnStyle'('AutoSize'))) | Out-Null
        $campo.Dock = 'Fill'
        if ($botao) { $botao.Margin = New-Object "$W.Padding"(8, 0, 0, 0) }
        $ln.Controls.Add($campo, 0, 0)
        if ($botao) { $ln.Controls.Add($botao, 1, 0) }
        return $ln
    }
    function New-Caixa([string]$nomeAcessivel) {
        $tb = New-Object 'System.Windows.Forms.TextBox'
        $tb.AccessibleName = $nomeAcessivel
        return $tb
    }
    function New-Botao([string]$t, [int]$larg = 0, [bool]$primario = $false) {
        $b = New-Object 'System.Windows.Forms.Button'
        $b.Text = $t; $b.AutoSize = $true
        $b.MinimumSize = New-Object System.Drawing.Size($(if ($larg -gt 0) { $larg } else { 110 }), 30)
        $b.Padding = New-Object "$W.Padding"(10, 0, 10, 0)
        if ($primario) {
            # a UNICA acao em destaque da aba: acento solido + texto branco
            $b.FlatStyle = 'Flat'; $b.BackColor = $corAcento; $b.ForeColor = [Drawing.Color]::White
            $b.FlatAppearance.BorderSize = 0
            $b.Font = New-Object System.Drawing.Font($form.Font, [Drawing.FontStyle]::Bold)
        }
        return $b
    }
    function New-LinhaBotoes([object[]]$botoes) {
        $ln = New-Object 'System.Windows.Forms.FlowLayoutPanel'
        $ln.AutoSize = $true; $ln.FlowDirection = 'LeftToRight'; $ln.WrapContents = $true
        foreach ($b in $botoes) { $b.Margin = New-Object "$W.Padding"(0, 0, 8, 0); $ln.Controls.Add($b) }
        return $ln
    }

    $pre = Get-PreRequisitos
    if (-not $pre.Ok) {
        [void][System.Windows.Forms.MessageBox]::Show($form,
            ("Pré-requisitos faltando:`n`n" + ($pre.Faltas -join "`n")),
            'MAC IP Backup', 'OK', 'Warning')
    }

    $conta = (Get-IdentidadeAtual).Nome
    $meta = Read-GuiMeta

    # ----- Tab 1: Ativar (passo 1 de 3 do primeiro uso) -----
    $t1 = New-Aba '1. Ativar'
    Add-Linha $t1 (New-Titulo 'Passo 1 de 3 — Ativação') -1 -margemTopo 0 | Out-Null
    Add-Linha $t1 (New-Rotulo ("Conta Windows em uso: $conta — é ela quem roda o backup, inclusive sem sessão aberta.")) -1 | Out-Null
    Add-Linha $t1 (New-Rotulo 'A MAC IP fornece o perfil (.json), a chave privada e a senha Restic pelos canais combinados. Importe os três aqui — a senha nunca vai para o perfil nem para o arquivo de configuração.') -1 | Out-Null
    Add-Linha $t1 (New-Secao 'Perfil') -1 -margemTopo 12 | Out-Null
    Add-Linha $t1 (New-Rotulo 'Arquivo de perfil MAC IP (.json) recebido:') -1 | Out-Null
    $tbPerfil = New-Caixa 'Caminho do perfil MAC IP importado'
    $tbPerfil.ReadOnly = $true; $tbPerfil.TabStop = $false
    $script:perfilAtual = $null
    $btPerfil = New-Botao 'Importar perfil...' 140
    Add-Linha $t1 (New-LinhaCampo $tbPerfil $btPerfil) 0 | Out-Null
    Add-Linha $t1 (New-Secao 'Chave') -1 -margemTopo 12 | Out-Null
    Add-Linha $t1 (New-Rotulo 'Chave privada SSH fornecida pela MAC IP:') -1 | Out-Null
    $tbChave = New-Caixa 'Caminho da chave privada SSH'
    $tbChave.ReadOnly = $true; $tbChave.TabStop = $false
    $btChave = New-Botao 'Escolher chave...' 140
    Add-Linha $t1 (New-LinhaCampo $tbChave $btChave) 1 | Out-Null
    Add-Linha $t1 (New-Secao 'Senha') -1 -margemTopo 12 | Out-Null
    Add-Linha $t1 (New-Rotulo 'Senha Restic (fica só neste computador, protegida):') -1 | Out-Null
    $tbSenha = New-Caixa 'Senha Restic fornecida pela MAC IP'
    $tbSenha.UseSystemPasswordChar = $true
    Add-Linha $t1 $tbSenha 2 | Out-Null
    Add-Linha $t1 (New-Secao 'Validação') -1 -margemTopo 12 | Out-Null
    $lblFalta = New-Rotulo 'Ainda falta: perfil, chave privada e senha.'
    Add-Linha $t1 $lblFalta -1 | Out-Null
    $btValidar = New-Botao 'Validar e continuar' 0 $true
    Add-Linha $t1 (New-LinhaBotoes @($btValidar)) 3 | Out-Null
    $lblAtv = New-Rotulo ''
    Add-Linha $t1 $lblAtv -1 | Out-Null
    $script:AtualizaFaltaAtv = {
        $f = @()
        if (-not $tbPerfil.Text) { $f += 'perfil' }
        if (-not $tbChave.Text) { $f += 'chave privada' }
        if (-not $tbSenha.Text) { $f += 'senha' }
        $lblFalta.Text = $(if ($f.Count -eq 0) { 'Tudo preenchido — pode validar.' } else { 'Ainda falta: ' + ($f -join ', ') + '.' })
    }
    foreach ($ctl in @($tbPerfil, $tbChave, $tbSenha)) { $ctl.Add_TextChanged({ & $script:AtualizaFaltaAtv }) }
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
                # horario_sugerido do perfil preenche o seletor -- o
                # tecnico ainda pode ajustar antes de salvar.
                if ($script:perfilAtual.horario_sugerido -and $dtpHora) {
                    try {
                        $hm = [DateTime]::ParseExact([string]$script:perfilAtual.horario_sugerido, 'HH:mm', $null)
                        $dtpHora.Value = [DateTime]::Today.AddHours($hm.Hour).AddMinutes($hm.Minute)
                    } catch { }
                }
                if ($script:AtualizaResumo) { & $script:AtualizaResumo }
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
        $lblAtv.ForeColor = 'DarkBlue'; $lblAtv.Text = 'Validando destino…'
        try {
            $id = Invoke-Assincrono -Funcao 'Test-ConexaoDestino' -Form $form -PrazoMs ([int]$script:ConsultaTimeoutMs + 30000) -Params @{
                Perfil = $perfilV; ChavePath = $chaveV
                SenhaRestic = (ConvertTo-SecureString $senhaV -AsPlainText -Force) }
            # so aceita se os dados continuam exatamente os capturados
            if ($tokenV -ne (Get-TokenAtivacao -Perfil $script:perfilAtual -ChavePath $tbChave.Text -SenhaTxt $tbSenha.Text)) {
                throw 'Os dados mudaram durante a validação — valide de novo.'
            }
            $script:ativacaoToken = $tokenV
            $lblAtv.ForeColor = 'DarkGreen'
            $lblAtv.Text = "Destino validado — identidade do servidor e ID do repositório conferem ($($id.Substring(0,12))…)."
            $tabs.SelectedTab = $script:Aba['2. Pasta e horário']     # avanca so com a prova
            $tbOrigem.Focus()                                       # foco ja cai no proximo dado
        } catch {
            $script:ativacaoToken = $null
            $lblAtv.ForeColor = 'DarkRed'; $lblAtv.Text = "Recusado: $($_.Exception.Message)"
        } finally {
            $script:OpRede = $false; $btValidar.Enabled = $true; $btSalvar.Enabled = $true
            foreach ($c in $entradasAtv) { $c.Enabled = $true }
        }
    })

    # ----- Tab 2: Pasta e horario (passos 2 e 3 do primeiro uso) -----
    $t2 = New-Aba '2. Pasta e horário'
    Add-Linha $t2 (New-Titulo 'Passo 2 de 3 — Pasta e horário') -1 -margemTopo 0 | Out-Null
    Add-Linha $t2 (New-Rotulo 'Escolha o que proteger e quando o backup diário roda. Revise antes de salvar — nada é agendado antes.') -1 | Out-Null
    Add-Linha $t2 (New-Secao 'Pasta') -1 -margemTopo 12 | Out-Null
    Add-Linha $t2 (New-Rotulo 'Pasta a proteger (tudo dentro dela entra no backup):') -1 | Out-Null
    $tbOrigem = New-Caixa 'Caminho da pasta a proteger'
    $btOrigem = New-Botao 'Escolher pasta...' 140
    Add-Linha $t2 (New-LinhaCampo $tbOrigem $btOrigem) 0 | Out-Null
    Add-Linha $t2 (New-Secao 'Horário') -1 -margemTopo 12 | Out-Null
    Add-Linha $t2 (New-Rotulo 'Horário diário (hora e minuto, no horário deste computador):') -1 | Out-Null
    # Seletor de hora/minuto nativo -- visivel e operavel por mouse e
    # teclado. O valor sai como HH:mm (mesmo formato do backup.json).
    $dtpHora = New-Object 'System.Windows.Forms.DateTimePicker'
    $dtpHora.Format = 'Custom'; $dtpHora.CustomFormat = 'HH:mm'
    $dtpHora.ShowUpDown = $true
    $dtpHora.Size = New-Object System.Drawing.Size(110, 26)
    $dtpHora.AccessibleName = 'Horário diário do backup, formato HH:mm'
    $dtpHora.TabIndex = 1
    $dtpHora.Value = [DateTime]::Today.AddHours(2).AddMinutes(30)
    $lblPrevHora = New-Rotulo ''
    $linhaHora = New-Object 'System.Windows.Forms.FlowLayoutPanel'
    $linhaHora.AutoSize = $true; $linhaHora.FlowDirection = 'LeftToRight'; $linhaHora.WrapContents = $false
    $linhaHora.Controls.Add($dtpHora)
    $lblPrevHora.Margin = New-Object "$W.Padding"(12, 6, 0, 0)
    $linhaHora.Controls.Add($lblPrevHora)
    $linhaHora.WrapContents = $true    # janela estreita: a previa cai para a linha de baixo
    Add-Linha $t2 $linhaHora 1 | Out-Null
    # Secao Exclusoes (opcional): o texto digitado vive no editor --
    # fechar e reabrir a secao NUNCA perde o que ja foi escrito.
    $ckAvanc = New-Object 'System.Windows.Forms.CheckBox'
    $ckAvanc.Text = 'Exclusões (opcional)'
    $ckAvanc.AutoSize = $true
    $ckAvanc.AccessibleName = 'Mostrar o editor de exclusões (opcional)'
    $ckAvanc.Font = New-Object System.Drawing.Font($form.Font, [Drawing.FontStyle]::Bold)
    Add-Linha $t2 $ckAvanc 2 -margemTopo 12 | Out-Null
    # O editor fica num TableLayoutPanel proprio (Dock=Top, altura fixa de
    # ~5 linhas): NUNCA GroupBox+AutoSize com Dock=Fill, que media so o
    # titulo e colapsava o campo numa linha fina (bug visto na VM).
    $pnlAdv = New-Object 'System.Windows.Forms.TableLayoutPanel'
    $pnlAdv.AutoSize = $true; $pnlAdv.ColumnCount = 1; $pnlAdv.Dock = 'Top'
    $pnlAdv.Visible = $false
    $pnlAdv.Controls.Add((New-Rotulo 'Digite uma regra por linha, como `*.tmp`. Deixe em branco para não excluir nada.'))
    $tbExcl = New-Object "$W.TextBox"
    $tbExcl.Multiline = $true
    $tbExcl.ScrollBars = 'Both'
    $tbExcl.WordWrap = $false        # uma quebra VISUAL nunca vira uma segunda regra
    $tbExcl.AcceptsReturn = $true    # Enter insere nova linha -- NAO dispara "Salvar e agendar"
    $tbExcl.AcceptsTab = $false      # Tab sai do editor (nao insere tabulacao)
    $tbExcl.Dock = 'Top'; $tbExcl.Height = 104
    $tbExcl.AccessibleName = 'Lista de exclusões, uma regra por linha'
    $pnlAdv.Controls.Add($tbExcl)
    Add-Linha $t2 $pnlAdv -1 | Out-Null
    $ckAvanc.Add_CheckedChanged({ $pnlAdv.Visible = $ckAvanc.Checked })
    Add-Linha $t2 (New-Titulo 'Passo 3 de 3 — Revisar e salvar') -1 -margemTopo 14 | Out-Null
    Add-Linha $t2 (New-Rotulo 'Confira antes de gravar — pasta, horário local e frequência:') -1 | Out-Null
    $tbResumo = New-Object "$W.TextBox"
    $tbResumo.Multiline = $true; $tbResumo.ReadOnly = $true
    $tbResumo.ScrollBars = 'Vertical'
    $tbResumo.MinimumSize = New-Object System.Drawing.Size(0, 76)
    $tbResumo.BackColor = [Drawing.Color]::WhiteSmoke
    $tbResumo.AccessibleName = 'Resumo da configuração a salvar'
    $tbResumo.TabStop = $false
    Add-Linha $t2 $tbResumo -1 | Out-Null
    $btVoltar = New-Botao 'Voltar: Ativação' 150
    $btSalvar = New-Botao 'Salvar e agendar' 0 $true
    Add-Linha $t2 (New-LinhaBotoes @($btVoltar, $btSalvar)) 3 | Out-Null
    $btVoltar.Add_Click({ $tabs.SelectedTab = $script:Aba['1. Ativar'] })   # dados preenchidos ficam
    $lblCfg = New-Rotulo ''
    Add-Linha $t2 $lblCfg -1 | Out-Null
    $lblProxSalvo = New-Rotulo ''
    Add-Linha $t2 $lblProxSalvo -1 | Out-Null

    $script:AtualizaResumo = {
        $destino = $null
        if ($script:perfilAtual) { $destino = $script:perfilAtual.repositorio }
        elseif ($cfgPath -and (Test-Path -LiteralPath $cfgPath -PathType Leaf)) {
            try { $destino = [string](Read-AgenteConfig -Caminho $cfgPath).repositorio } catch { }
        }
        $tbResumo.Text = Get-ResumoConfiguracao -Origem $tbOrigem.Text `
            -HHmm ($dtpHora.Value.ToString('HH:mm')) -Destino $destino -Conta $conta
    }
    $tbOrigem.Add_TextChanged({ & $script:AtualizaResumo })
    $dtpHora.Add_ValueChanged({
        $lblPrevHora.Text = Format-PreviaHorario ($dtpHora.Value.ToString('HH:mm'))
        & $script:AtualizaResumo
    })
    $lblPrevHora.Text = Format-PreviaHorario ($dtpHora.Value.ToString('HH:mm'))

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
            if ($cfgAtual.exclusoes -and @($cfgAtual.exclusoes).Count -gt 0) { $ckAvanc.Checked = $true }
            try {
                $hm = [DateTime]::ParseExact([string]$cfgAtual.horario, 'HH:mm', $null)
                $dtpHora.Value = [DateTime]::Today.AddHours($hm.Hour).AddMinutes($hm.Minute)
            } catch { }
        } catch { }
    }
    & $script:AtualizaResumo

    $btSalvar.Add_Click({
        if ($script:OpRede) { return }        # clique repetido / validacao em curso: ignora
        $script:OpRede = $true
        $btSalvar.Enabled = $false; $btValidar.Enabled = $false
        foreach ($c in $entradasAtv) { $c.Enabled = $false }
        $lblCfg.ForeColor = 'DarkBlue'; $lblCfg.Text = 'Salvando…'
        $lblProxSalvo.Text = ''
        $built = $null
        $horario = $dtpHora.Value.ToString('HH:mm')
        try {
            $excl = @($tbExcl.Lines | Where-Object { $_ -and $_.Trim() -ne '' })
            if (-not $tbOrigem.Text) { throw 'Escolha a pasta a proteger.' }
            # perfil novo exige validacao VIGENTE -- conferida ANTES e
            # DEPOIS do build (uma tentativa vencida nao publica nada)
            if ($script:perfilAtual) {
                Test-AtivacaoVigente -Perfil $script:perfilAtual -ChavePath $tbChave.Text -SenhaTxt $tbSenha.Text
            }
            # FASE 1 (consulta o destino no preparo): fora da thread da janela
            $built = Invoke-Assincrono -Funcao 'Build-Configuracao' -Form $form -PrazoMs 180000 -Params @{
                Perfil = $script:perfilAtual; ChavePath = $tbChave.Text
                SenhaRestic = $(if ($tbSenha.Text) { ConvertTo-SecureString $tbSenha.Text -AsPlainText -Force } else { $null })
                Origem = $tbOrigem.Text; Exclusoes = $excl; Horario = $horario }
            if ($script:perfilAtual) {
                Test-AtivacaoVigente -Perfil $script:perfilAtual -ChavePath $tbChave.Text -SenhaTxt $tbSenha.Text
            }
            # credencial do Windows na janela nativa; cancelar => desfaz a fase 1
            $cred = $host.UI.PromptForCredential('MAC IP Backup',
                "Senha do Windows da conta $conta (a tarefa roda mesmo sem sessão aberta)",
                $conta, '')
            if (-not $cred) {
                $aviso = Remove-ConjuntoNovo -Built $built
                throw ('Agendamento cancelado — a configuração anterior continua valendo.' +
                    $(if ($aviso) { " $aviso" } else { '' }))
            }
            # FASE 2: publica (COM + arquivo; operacao local)
            Publish-Configuracao -Built $built -Conta $conta -SenhaConta $cred.GetNetworkCredential().Password
            $cred = $null
            $script:perfilAtual = $null; $script:ativacaoToken = $null
            $script:Disparo = $null    # pedido aberto pertencia a config anterior
            $script:UltimaResolucao = $null
            # a aba Ativar nao exibe dados que ja nao estao mais carregados
            $tbPerfil.Text = ''; $tbChave.Text = ''; $tbSenha.Text = ''; $lblAtv.Text = ''
            $built = $null
            $lblCfg.ForeColor = 'DarkGreen'
            $lblCfg.Text = ("Salvo. Agendamento solicitado para $horario na conta $conta — " +
                'a execução programada só fica comprovada quando o disparo real aparecer na aba Situação.')
            # Proxima execucao REAL informada pelo Agendador (nao a digitada)
            $proxTxt = '-'
            try { $inf2 = Get-InfoTarefa; if ($inf2) { $proxTxt = Format-DataBR $inf2.Proxima } } catch { }
            $lblProxSalvo.ForeColor = 'DarkBlue'
            $lblProxSalvo.Text = "Próximo backup informado pelo Agendador: $proxTxt (horário deste computador)"
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

    # ----- Tab 3: Situacao -----
    # Tres informacoes DISTINTAS: execucao atual (estado + cor), ultimo
    # backup bem-sucedido (evidencia duravel) e proximo backup. Uma
    # mensagem de estado por execucao; IDs e caminhos longos em Detalhes.
    $t3 = New-Aba '3. Situação'
    $script:SetDetalhes = {
        # so reescreve quando o conteudo mudou: nao apaga a selecao de
        # quem esta copiando os Detalhes nem rouba o foco a cada tick.
        param([string]$txt)
        if ($tbDetalhes.Text -cne $txt) { $tbDetalhes.Text = $txt }
    }
    Add-Linha $t3 (New-Titulo 'Situação do backup') -1 -margemTopo 0 | Out-Null
    Add-Linha $t3 (New-Rotulo 'A execução atual, o último backup confirmado e o próximo agendamento — atualizados sozinhos.') -1 | Out-Null
    $lblStatus = New-Rotulo 'Execução atual: -' $true
    Add-Linha $t3 $lblStatus -1 -margemTopo 10 | Out-Null
    $lblUlt = New-Rotulo 'Último backup bem-sucedido: -'
    Add-Linha $t3 $lblUlt -1 | Out-Null
    $lblProx = New-Rotulo 'Próximo backup: -'
    Add-Linha $t3 $lblProx -1 | Out-Null
    $lblAcao = New-Rotulo ''
    Add-Linha $t3 $lblAcao -1 | Out-Null
    $btAgora = New-Botao 'Executar backup agora' 190 $true
    $btPausa = New-Botao 'Pausar agenda' 140
    $btRetomar = New-Botao 'Retomar agenda' 140
    $btAtual = New-Botao 'Atualizar' 110
    Add-Linha $t3 (New-LinhaBotoes @($btAgora, $btPausa, $btRetomar, $btAtual)) 0 -margemTopo 8 | Out-Null
    $lblOp = New-Rotulo ''
    Add-Linha $t3 $lblOp -1 | Out-Null
    $tbDetalhes = New-Object "$W.TextBox"
    $tbDetalhes.Multiline = $true; $tbDetalhes.ReadOnly = $true
    $tbDetalhes.ScrollBars = 'Both'; $tbDetalhes.WordWrap = $false   # caminhos longos: rolagem horizontal
    $tbDetalhes.Font = New-Object System.Drawing.Font('Consolas', 9)
    $tbDetalhes.BackColor = [Drawing.Color]::White
    $tbDetalhes.AccessibleName = 'Detalhes técnicos da situação do backup'
    $tbDetalhes.TabStop = $false
    $ckDet3 = New-DetalhesRecolhidos $t3 'Detalhes (suporte técnico — envie à MAC IP quando pedirem):' 1 $tbDetalhes

    $script:Atualizando = $false
    $script:Disparo = $null            # pedido de "Executar agora" aberto (estado proprio)
    $script:UltimaResolucao = $null    # resolucao do ultimo pedido ate um disparo mais novo
    $script:AtualizarTela = {
        if ($script:Atualizando) { return }
        $script:Atualizando = $true
        try {
            $info = $null; $infoErro = $null
            try { $info = Get-InfoTarefa } catch { $infoErro = $_.Exception.Message }
            $cfgPath = Get-ConfigAtual
            $origem = ''
            if ($cfgPath -and (Test-Path -LiteralPath $cfgPath -PathType Leaf)) {
                try { $origem = [string](Read-AgenteConfig -Caminho $cfgPath).origem } catch { }
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
            $lblUlt.Text = 'Último backup bem-sucedido: ' + $(if ($conf) {
                ('{0} (snapshot {1})' -f (Format-DataBR $conf.fim_utc), ([string]$conf.snapshot).Substring(0,12))
            } else { 'nenhum ainda' })
            # MODELO unico de renderizacao: um pedido de "Executar agora"
            # aberto dita a linha principal, a cor, a orientacao e o
            # botao -- a classificacao generica (que ainda veria o
            # sucesso anterior como "concluido") so vale sem pedido nem
            # resolucao recente. A resolucao do pedido encerrado segue
            # apresentada ate um disparo MAIS NOVO aparecer.
            $tela = Get-TelaExecucao -InfoTarefa $info -Res $res -Pedido $script:Disparo -Resolucao $script:UltimaResolucao
            if ($script:Disparo -and $tela.Encerrou) {
                $script:UltimaResolucao = [pscustomobject]@{
                    Classe = $tela.Classe
                    UltimaObservada = $(if ($script:Disparo.NovaUltima) { $script:Disparo.NovaUltima } else { $script:Disparo.UltimaRef })
                    Mensagem = $tela.Mensagem
                }
                $script:Disparo = $null
            }
            if ($tela.ResolucaoExpirada) { $script:UltimaResolucao = $null }
            if ($tela.Origem -ne 'generica') {
                # MODO PEDIDO/RESOLUCAO: todas as linhas no mesmo estado.
                $lblStatus.ForeColor = $tela.Cor
                $lblStatus.Text = 'Execução atual: ' + $tela.Texto
                $lblAcao.Text = $tela.Acao
                $lblOp.ForeColor = $tela.Cor
                $lblOp.Text = $tela.Mensagem
                if ($info) {
                    $lblProx.Text = 'Próximo backup: ' + $(if ($info.Habilitada) {
                        ((Format-DataBR $info.Proxima) + ' (horário deste computador)') } else { 'pausado — só “Retomar agenda” religa' })
                    & $script:SetDetalhes (Get-DetalhesSuporte -Info $info -Res $res -Conf $conf -Estado $estado -ConfigPath $cfgPath -Origem $origem)
                    # UM dos dois visivel: Pausar OU Retomar conforme o estado
                    $btPausa.Visible = $info.Habilitada; $btRetomar.Visible = -not $info.Habilitada
                    $btAgora.Enabled = ($info.Estado -ne 4) -and (-not $script:Disparo)
                    $btAgora.Text = $(if (Test-ResConfirmado -Res $res) { 'Executar backup agora' } else { 'Executar primeiro backup' })
                } else {
                    $lblProx.Text = 'Próximo backup: (consulta inconclusiva)'
                    & $script:SetDetalhes (Get-DetalhesSuporte -Info $null -Res $res -Conf $conf -Estado $estado -ConfigPath $cfgPath -Origem $origem)
                    $btAgora.Enabled = $false; $btPausa.Visible = $false; $btRetomar.Visible = $false
                }
                return
            }
            if (-not $info) {
                # consulta inconclusiva NAO e "sem tarefa" -- exibe o erro.
                if ($infoErro) {
                    $lblStatus.ForeColor = 'DarkRed'
                    $lblStatus.Text = 'Execução atual: consulta do Agendador inconclusiva — ' + $infoErro
                    $lblProx.Text = 'Próximo backup: (consulta inconclusiva)'
                    $lblAcao.Text = 'Ação necessária: verifique o serviço do Agendador de Tarefas.'
                } else {
                    $lblStatus.ForeColor = 'DarkRed'; $lblStatus.Text = 'Execução atual: ' + $script:RotulosStatus.sem_tarefa
                    $lblProx.Text = 'Próximo backup: -'; $lblAcao.Text = $script:AcaoStatus.sem_tarefa
                }
                $btAgora.Enabled = $false; $btPausa.Visible = $false; $btRetomar.Visible = $false
                & $script:SetDetalhes (Get-DetalhesSuporte -Info $null -Res $res -Conf $conf -Estado $estado -ConfigPath $cfgPath -Origem $origem)
                return
            }
            if (-not $info.DoApp) {
                $lblStatus.ForeColor = 'DarkRed'; $lblStatus.Text = 'Execução atual: ' + $script:RotulosStatus.tarefa_estranha
                $lblAcao.Text = $script:AcaoStatus.tarefa_estranha
                $btAgora.Enabled = $false; $btPausa.Visible = $false; $btRetomar.Visible = $false
                & $script:SetDetalhes (Get-DetalhesSuporte -Info $info -Res $res -Conf $conf -Estado $estado -ConfigPath $cfgPath -Origem $origem)
                return
            }
            $lblStatus.ForeColor = $tela.Cor
            $lblStatus.Text = 'Execução atual: ' + $tela.Texto
            $lblProx.Text = 'Próximo backup: ' + $(if ($info.Habilitada) {
                ((Format-DataBR $info.Proxima) + ' (horário deste computador)') } else { 'pausado — só “Retomar agenda” religa' })
            $lblAcao.Text = $(if (-not $info.Habilitada -and $tela.Classe -ne 'em_execucao') {
                'Ação necessária: a agenda está pausada — só “Retomar agenda” religa o agendamento.'
            } else { $tela.Acao })
            & $script:SetDetalhes (Get-DetalhesSuporte -Info $info -Res $res -Conf $conf -Estado $estado -ConfigPath $cfgPath -Origem $origem)
            $btPausa.Visible = $info.Habilitada; $btRetomar.Visible = -not $info.Habilitada
            # Sem cliques duplicados enquanto um pedido estiver aberto.
            $btAgora.Enabled = ($info.Estado -ne 4) -and (-not $script:Disparo)
            $btAgora.Text = $(if (Test-ResConfirmado -Res $res) { 'Executar backup agora' } else { 'Executar primeiro backup' })
        } catch {
            $lblStatus.ForeColor = 'DarkRed'
            $lblStatus.Text = 'Execução atual: erro ao ler — ' + $_.Exception.Message
        } finally { $script:Atualizando = $false }
    }
    $btAgora.Add_Click({
        if ($script:Disparo) { return }      # ja ha pedido aberto: clique repetido ignorado
        try {
            # Referencia ANTES de solicitar: ultimo LastRunTime, GUIDs
            # de instancia e a identidade do resultado ja gravado. So o
            # que vier DEPOIS conta como execucao deste pedido -- o
            # sucesso anterior nunca e anunciado como resultado do
            # disparo recem-feito.
            $ref = Get-InfoTarefa
            $estadoRef = Get-EstadoAtual
            $resRef = $(if ($estadoRef) { Read-UltimoResultado -Estado $estadoRef } else { $null })
            Start-TarefaAgora
            $script:Disparo = New-PedidoDisparo -InfoTarefa $ref -Res $resRef
            $btAgora.Enabled = $false
            $lblOp.ForeColor = 'DarkBlue'
            $lblOp.Text = $script:RotulosPedido.aguardando
        } catch {
            $script:Disparo = $null
            $lblOp.ForeColor = 'DarkRed'
            $lblOp.Text = $_.Exception.Message
        }
        & $script:AtualizarTela
    })
    $btPausa.Add_Click({ try { Set-TarefaHabilitada $false; $lblOp.ForeColor='DarkBlue'; $lblOp.Text = 'Agenda pausada — o disparo agendado não roda até “Retomar agenda”.' } catch { $lblOp.ForeColor='DarkRed'; $lblOp.Text = $_.Exception.Message }; & $script:AtualizarTela })
    $btRetomar.Add_Click({ try { Set-TarefaHabilitada $true; $lblOp.ForeColor='DarkBlue'; $lblOp.Text = 'Agenda retomada — o próximo backup segue o horário.' } catch { $lblOp.ForeColor='DarkRed'; $lblOp.Text = $_.Exception.Message }; & $script:AtualizarTela })
    $btAtual.Add_Click({ & $script:AtualizarTela })

    # ----- Tab 4: Restaurar (Data do backup -> Arquivo/Pasta -> Destino) -----
    $t4 = New-Aba '4. Restaurar'
    Add-Linha $t4 (New-Titulo 'Restaurar backup') -1 -margemTopo 0 | Out-Null
    Add-Linha $t4 (New-Rotulo 'Restaure um arquivo ou a pasta protegida inteira de um ponto — o original nunca é sobrescrito.') -1 | Out-Null
    Add-Linha $t4 (New-Rotulo '1. Data do backup (ponto de restauração):') -1 -margemTopo 8 | Out-Null
    $cbSnap = New-Object "$W.ComboBox"
    $cbSnap.DropDownStyle = 'DropDownList'
    $cbSnap.AccessibleName = 'Data do backup, ponto de restauração'
    $btSnaps = New-Botao 'Carregar pontos' 140
    Add-Linha $t4 (New-LinhaCampo $cbSnap $btSnaps) 0 | Out-Null
    Add-Linha $t4 (New-Rotulo 'Modo de restauração:') -1 | Out-Null
    $rbArquivo = New-Object "$W.RadioButton"
    $rbArquivo.Text = 'Restaurar um arquivo'; $rbArquivo.AutoSize = $true; $rbArquivo.Checked = $true
    $rbArquivo.AccessibleName = 'Modo: restaurar somente um arquivo'
    $rbPasta = New-Object "$W.RadioButton"
    $rbPasta.Text = 'Restaurar toda a pasta protegida'; $rbPasta.AutoSize = $true
    $rbPasta.AccessibleName = 'Modo: restaurar toda a pasta protegida'
    $lnModo = New-Object 'System.Windows.Forms.FlowLayoutPanel'
    $lnModo.AutoSize = $true; $lnModo.FlowDirection = 'LeftToRight'; $lnModo.WrapContents = $true
    $rbArquivo.Margin = New-Object "$W.Padding"(0, 0, 20, 0)
    $lnModo.Controls.Add($rbArquivo); $lnModo.Controls.Add($rbPasta)
    Add-Linha $t4 $lnModo 1 | Out-Null
    # modo ARQUIVO: etapa 2 = selecao do arquivo na lista (bloco todo some no modo pasta)
    $pnlArq = New-Object 'System.Windows.Forms.TableLayoutPanel'
    $pnlArq.AutoSize = $true; $pnlArq.ColumnCount = 1; $pnlArq.Dock = 'Top'
    $lblEtapa2 = New-Rotulo '2. Arquivo do ponto escolhido (somente pela lista):'
    $pnlArq.Controls.Add($lblEtapa2)
    $cbArq = New-Object "$W.ComboBox"
    $cbArq.DropDownStyle = 'DropDownList'          # selecao fechada: sem texto livre
    $cbArq.AccessibleName = 'Arquivo a restaurar do ponto escolhido'
    $cbArq.Dock = 'Top'
    $pnlArq.Controls.Add($cbArq)
    $lblArqFull = New-Rotulo 'Arquivo: -' $true          # nome em destaque
    $pnlArq.Controls.Add($lblArqFull)
    $lblArqPath = New-Rotulo 'Caminho completo: -'       # caminho sempre acessivel
    $pnlArq.Controls.Add($lblArqPath)
    Add-Linha $t4 $pnlArq 2 | Out-Null
    # modo PASTA: etapa 2 = a raiz registrada NO PONTO (sem selecao de arquivo)
    $pnlRaiz = New-Object 'System.Windows.Forms.TableLayoutPanel'
    $pnlRaiz.AutoSize = $true; $pnlRaiz.ColumnCount = 1; $pnlRaiz.Dock = 'Top'; $pnlRaiz.Visible = $false
    $pnlRaiz.Controls.Add((New-Rotulo '2. Pasta protegida registrada nesse ponto:'))
    $lblRaizPonto = New-Rotulo '-' $true
    $pnlRaiz.Controls.Add($lblRaizPonto)
    Add-Linha $t4 $pnlRaiz 2 | Out-Null
    # destino no modo ARQUIVO: pasta separada e vazia (inalterado)
    $pnlDestArq = New-Object 'System.Windows.Forms.TableLayoutPanel'
    $pnlDestArq.AutoSize = $true; $pnlDestArq.ColumnCount = 1; $pnlDestArq.Dock = 'Top'
    $pnlDestArq.Controls.Add((New-Rotulo '3. Pasta de destino (separada e vazia — o original nunca é sobrescrito):'))
    $tbDest = New-Caixa 'Caminho da pasta de destino da restauração'
    $btDest = New-Botao 'Pasta de destino...' 150
    $pnlDestArq.Controls.Add((New-LinhaCampo $tbDest $btDest))
    Add-Linha $t4 $pnlDestArq 3 | Out-Null
    # destino no modo PASTA: 'Onde salvar' (pasta existente, pelo
    # seletor) + 'Nome da nova pasta' (um componente) — o destino final
    # nao existe ainda e NAO pode ser criado antes; a revisao mostra o
    # caminho final composto.
    $pnlDestPasta = New-Object 'System.Windows.Forms.TableLayoutPanel'
    $pnlDestPasta.AutoSize = $true; $pnlDestPasta.ColumnCount = 1
    $pnlDestPasta.Dock = 'Top'; $pnlDestPasta.Visible = $false
    $pnlDestPasta.Controls.Add((New-Rotulo '3. Onde salvar (pasta existente — escolha pelo seletor):'))
    $tbPaiRest = New-Caixa 'Pasta onde a restauração será salva'
    $btPaiRest = New-Botao 'Onde salvar...' 140
    $pnlDestPasta.Controls.Add((New-LinhaCampo $tbPaiRest $btPaiRest))
    $pnlDestPasta.Controls.Add((New-Rotulo '4. Nome da nova pasta (será criada — não pode existir):'))
    $tbNomePasta = New-Caixa 'Nome da nova pasta de restauração'
    $pnlDestPasta.Controls.Add($tbNomePasta)
    Add-Linha $t4 $pnlDestPasta 4 | Out-Null
    # revisao ao vivo ANTES de iniciar: Ponto, Escopo e Destino em linhas
    # SEPARADAS que quebram na largura util -- caminho longo sempre
    # consultavel por inteiro, nunca cortado atras de rolagem horizontal.
    Add-Linha $t4 (New-Secao 'Revisão') -1 -margemTopo 10 | Out-Null
    $lblRevPonto = New-Rotulo 'Ponto: -'
    Add-Linha $t4 $lblRevPonto -1 | Out-Null
    $lblRevEscopo = New-Rotulo 'Escopo: -'
    Add-Linha $t4 $lblRevEscopo -1 | Out-Null
    $lblRevDestino = New-Rotulo 'Destino: -'
    Add-Linha $t4 $lblRevDestino -1 | Out-Null
    $btRest = New-Botao 'Restaurar arquivo' 160 $true
    $btAbrirDest = New-Botao 'Abrir pasta restaurada' 180
    $btAbrirDest.Visible = $false
    Add-Linha $t4 (New-LinhaBotoes @($btRest, $btAbrirDest)) 4 -margemTopo 8 | Out-Null
    $lblRes = New-Rotulo ''
    Add-Linha $t4 $lblRes -1 | Out-Null
    $tbRestDet = New-Object "$W.TextBox"
    $tbRestDet.Multiline = $true; $tbRestDet.ReadOnly = $true
    $tbRestDet.ScrollBars = 'Both'; $tbRestDet.WordWrap = $false     # snapshot/SHA-256 completos
    $tbRestDet.Font = New-Object System.Drawing.Font('Consolas', 9)
    $tbRestDet.BackColor = [Drawing.Color]::White
    $tbRestDet.AccessibleName = 'Detalhes da restauração: snapshot, SHA-256 e diagnóstico'
    $tbRestDet.TabStop = $false
    $ckDet4 = New-DetalhesRecolhidos $t4 'Detalhes (suporte técnico — ponto, SHA-256 e diagnóstico, sem senhas):' 5 $tbRestDet
    $script:ultimoRestaurado = $null

    $script:AtualizaRevisaoRest = {
        # revisao ao vivo ANTES de iniciar: ponto + escopo + caminho final.
        $ponto = '-'
        if ($cbSnap.SelectedIndex -ge 0 -and $script:snaps -and $cbSnap.SelectedIndex -lt $script:snaps.Count) {
            $ponto = Format-DataBR $script:snaps[$cbSnap.SelectedIndex].time
        }
        $escopo = $(if ($rbPasta.Checked) {
            'pasta protegida inteira' + $(if ($lblRaizPonto.Text -ne '-') { " ($($lblRaizPonto.Text))" } else { '' })
        } else {
            $(if ($cbArq.SelectedItem) { 'arquivo ' + [string]$cbArq.SelectedItem } else { 'arquivo (não escolhido)' })
        })
        $dest = $(if ($rbPasta.Checked) {
            if ($tbPaiRest.Text -and $tbNomePasta.Text) {
                Join-Path $tbPaiRest.Text $tbNomePasta.Text
            } else { '(escolha onde salvar e informe o nome da nova pasta)' }
        } elseif ($tbDest.Text) { $tbDest.Text } else { '(não escolhido)' })
        $lblRevPonto.Text = 'Ponto: ' + $ponto
        $lblRevEscopo.Text = 'Escopo: ' + $escopo
        $lblRevDestino.Text = 'Destino: ' + $dest
    }
    $script:AplicaModo = {
        $pnlArq.Visible = $rbArquivo.Checked
        $pnlRaiz.Visible = $rbPasta.Checked
        $pnlDestArq.Visible = $rbArquivo.Checked
        $pnlDestPasta.Visible = $rbPasta.Checked
        $btRest.Text = $(if ($rbPasta.Checked) { 'Restaurar pasta inteira' } else { 'Restaurar arquivo' })
        # no modo pasta, a lista de arquivos nao e consultada — a raiz vem
        # dos metadados do ponto (o catalogo confirma na hora de restaurar)
        if ($rbPasta.Checked -and $cbSnap.SelectedIndex -ge 0 -and $script:snaps -and $cbSnap.SelectedIndex -lt $script:snaps.Count) {
            $paths = @($script:snaps[$cbSnap.SelectedIndex].paths | Where-Object { $_ })
            $lblRaizPonto.Text = $(if ($paths.Count -eq 1) { [string]$paths[0] } else { '(raiz não identificável nos metadados)' })
        }
        & $script:AtualizaRevisaoRest
    }
    $script:LimpaSelecaoRest = {
        $script:snaps = @(); $cbSnap.Items.Clear(); $cbArq.Items.Clear()
        $lblArqFull.Text = 'Arquivo: -'; $lblArqPath.Text = 'Caminho completo: -'
        $lblRaizPonto.Text = '-'; & $script:AtualizaRevisaoRest
    }
    $rbArquivo.Add_CheckedChanged({ & $script:AplicaModo })
    $btSnaps.Add_Click({
        $btSnaps.Enabled = $false; $lblRes.ForeColor = 'DarkBlue'; $lblRes.Text = 'Carregando pontos de restauração…'
        $btAbrirDest.Visible = $false
        try {
            $estado = Get-EstadoAtual; $cfgPath = Get-ConfigAtual
            if (-not $estado -or -not $cfgPath) { throw 'Sem preparo concluído — valide na aba “Ativar” e salve em “Pasta e horário”.' }
            $snaps = Invoke-Assincrono -Funcao 'Get-Snapshots' -Form $form -PrazoMs ([int]$script:ConsultaTimeoutMs + 30000) -Params @{ Estado = $estado; Config = $cfgPath }
            & $script:LimpaSelecaoRest                       # selecao anterior nunca sobrevive a nova listagem
            $script:snaps = @($snaps)
            # data LOCAL do ponto primeiro; o ID curto vem depois
            foreach ($s in $script:snaps) { [void]$cbSnap.Items.Add(('{0} — {1}' -f (Format-DataBR $s.time), $s.short_id)) }
            if ($cbSnap.Items.Count -gt 0) { $cbSnap.SelectedIndex = $cbSnap.Items.Count - 1 }
            $lblRes.ForeColor = 'DarkGreen'
            $lblRes.Text = $(if ($cbSnap.Items.Count -eq 0) {
                'Nenhum ponto de restauração encontrado neste destino.'
            } else { "$($cbSnap.Items.Count) ponto(s) de restauração disponível(is)." })
        } catch {
            & $script:LimpaSelecaoRest                       # listagem falhou -> descarta a selecao
            $lblRes.ForeColor = 'DarkRed'; $lblRes.Text = Get-MensagemFalha -Erro $_.Exception.Message
            $tbRestDet.Text = ('falha ao carregar pontos — diagnóstico (sem senhas):' + "`r`n" + $_.Exception.Message)
        } finally { $btSnaps.Enabled = $true }
    })
    $cbSnap.Add_SelectedIndexChanged({
        if ($cbSnap.SelectedIndex -lt 0) { return }
        $cbArq.Items.Clear()
        if ($rbPasta.Checked) {
            # modo pasta: sem lista de arquivos — so a raiz dos metadados
            $paths = @($script:snaps[$cbSnap.SelectedIndex].paths | Where-Object { $_ })
            $lblRaizPonto.Text = $(if ($paths.Count -eq 1) { [string]$paths[0] } else { '(raiz não identificável nos metadados)' })
            & $script:AtualizaRevisaoRest; return
        }
        $lblRes.ForeColor = 'DarkBlue'; $lblRes.Text = 'Carregando arquivos do ponto…'
        try {
            $estado = Get-EstadoAtual; $cfgPath = Get-ConfigAtual
            $snap = $script:snaps[$cbSnap.SelectedIndex]
            $arqs = Invoke-Assincrono -Funcao 'Get-ArquivosSnapshot' -Form $form -PrazoMs ([int]$script:ConsultaTimeoutMs + 30000) -Params @{ Estado = $estado; Config = $cfgPath; Snapshot = $snap.id }
            foreach ($a in $arqs) { [void]$cbArq.Items.Add($a) }
            if ($cbArq.Items.Count -gt 0) { $cbArq.SelectedIndex = 0 }
            $lblRes.ForeColor = 'DarkBlue'
            $lblRes.Text = $(if ($cbArq.Items.Count -eq 0) { 'Este ponto não contém arquivos.' } else { '' })
        } catch {
            $cbArq.Items.Clear(); $lblRes.ForeColor = 'DarkRed'
            $lblRes.Text = Get-MensagemFalha -Erro $_.Exception.Message
            $tbRestDet.Text = ('falha ao listar arquivos do ponto — diagnóstico (sem senhas):' + "`r`n" + $_.Exception.Message)
        }
        & $script:AtualizaRevisaoRest
    })
    $cbArq.Add_SelectedIndexChanged({
        # nome do arquivo em destaque; caminho completo logo abaixo
        if ($cbArq.SelectedItem) {
            $p = [string]$cbArq.SelectedItem
            $lblArqFull.Text = 'Arquivo: ' + ([IO.Path]::GetFileName($p.TrimEnd('/','\')))
            $lblArqPath.Text = 'Caminho completo: ' + $p
        } else { $lblArqFull.Text = 'Arquivo: -'; $lblArqPath.Text = 'Caminho completo: -' }
        & $script:AtualizaRevisaoRest
    })
    $tbDest.Add_TextChanged({ & $script:AtualizaRevisaoRest })
    $tbPaiRest.Add_TextChanged({ & $script:AtualizaRevisaoRest })
    $tbNomePasta.Add_TextChanged({ & $script:AtualizaRevisaoRest })
    $btDest.Add_Click({
        $dlg = New-Object "$W.FolderBrowserDialog"
        $dlg.Description = 'Pasta SEPARADA e vazia para receber o arquivo restaurado'
        $dlg.ShowNewFolderButton = $true
        if ($dlg.ShowDialog() -eq 'OK') { $tbDest.Text = $dlg.SelectedPath }
    })
    $btPaiRest.Add_Click({
        # 'Onde salvar': pasta EXISTENTE escolhida pelo seletor nativo —
        # a pasta final e o NOME digitado, criado so na restauracao.
        $dlg = New-Object "$W.FolderBrowserDialog"
        $dlg.Description = 'Pasta EXISTENTE onde a pasta restaurada será criada'
        $dlg.ShowNewFolderButton = $true
        if ($dlg.ShowDialog() -eq 'OK') { $tbPaiRest.Text = $dlg.SelectedPath }
    })
    $btRest.Add_Click({
        $btRest.Enabled = $false; $lblRes.ForeColor = 'DarkBlue'; $lblRes.Text = 'Restaurando…'
        $btAbrirDest.Visible = $false; $script:ultimoRestaurado = $null
        $tbRestDet.Text = 'restauração em andamento…'
        try {
            $estado = Get-EstadoAtual; $cfgPath = Get-ConfigAtual
            $snap = $script:snaps[$cbSnap.SelectedIndex]
            $cfg = Read-AgenteConfig -Caminho $cfgPath
            if ($rbPasta.Checked) {
                # RESTAURACAO COMPLETA: ID completo do ponto, raiz dos
                # metadados confirmada pelo catalogo, destino = pasta pai
                # existente (seletor) + NOME da pasta nova (1 componente).
                $erroNome = Test-NomePastaDestino $tbNomePasta.Text
                if ($erroNome) { throw $erroNome }
                if (-not $tbPaiRest.Text) { throw 'Escolha onde salvar pelo seletor (etapa 3).' }
                $destinoFinal = Join-Path $tbPaiRest.Text $tbNomePasta.Text
                $res2 = Invoke-Assincrono -Funcao 'Restore-PastaCompleta' -Form $form -PrazoMs ([int]$script:DumpTimeoutMs + 60000) -Params @{
                    Estado = $estado; Config = $cfgPath; Snapshot = $snap.id
                    PathsMeta = $snap.paths; Destino = $destinoFinal; Origem = $cfg.origem }
                $script:ultimoRestaurado = $res2.Destino
                $lblRes.ForeColor = 'DarkGreen'
                $lblRes.Text = "Pasta restaurada com sucesso: $($res2.Destino) — o original NÃO foi sobrescrito."
                $tbRestDet.Text = (@(
                    'pasta restaurada: ' + $res2.Destino
                    'raiz registrada no ponto: ' + $res2.Raiz
                    'snapshot: ' + [string]$snap.id
                    ('itens restaurados: ' + $res2.Itens)
                    'escopo: árvore completa do ponto (exclusões atuais NÃO aplicadas)'
                ) -join "`r`n")
                $btAbrirDest.Visible = $true
            } else {
                if (-not $cbArq.SelectedItem) { throw 'Escolha o arquivo a restaurar na lista (etapa 2).' }
                $alvo = Invoke-Assincrono -Funcao 'Restore-Arquivo' -Form $form -PrazoMs ([int]$script:DumpTimeoutMs + 60000) -Params @{
                    Estado = $estado; Config = $cfgPath; Snapshot = $snap.id
                    Arquivo = $cbArq.SelectedItem; Destino = $tbDest.Text; Origem = $cfg.origem }
                $sha = (Get-FileHash -LiteralPath $alvo -Algorithm SHA256).Hash.ToLower()
                $script:ultimoRestaurado = $alvo
                $lblRes.ForeColor = 'DarkGreen'
                $lblRes.Text = "Restaurado com sucesso: $alvo — o original NÃO foi sobrescrito."
                # ID do snapshot + SHA-256 do arquivo vao para Detalhes --
                # a mensagem principal nao e cortada.
                $tbRestDet.Text = (@(
                    'arquivo restaurado: ' + $alvo
                    'snapshot: ' + [string]$snap.id
                    ('sha-256: ' + $sha + '   (mesmo hash do arquivo no backup — comprove se necessário)')
                    'destino: ' + $tbDest.Text
                ) -join "`r`n")
                $btAbrirDest.Visible = $true
            }
        } catch {
            # UMA mensagem curta na area principal: perda de conexao
            # identificada ganha orientacao propria; destino invalido,
            # pasta existente, permissao e falhas desconhecidas mantem o
            # texto original. O diagnostico completo (sem segredos) fica
            # em 'Mostrar detalhes tecnicos' -- nunca despejado aqui.
            $lblRes.ForeColor = 'DarkRed'; $lblRes.Text = Get-MensagemFalha -Erro $_.Exception.Message
            $tbRestDet.Text = ('falha na restauração — diagnóstico (sem senhas):' + "`r`n" + $_.Exception.Message)
        } finally { $btRest.Enabled = $true }
    })
    $btAbrirDest.Add_Click({
        if (-not (Open-PastaRestaurada -Alvo $script:ultimoRestaurado)) {
            $lblRes.ForeColor = 'DarkRed'
            $lblRes.Text = 'Não foi possível abrir a pasta agora — o arquivo continua restaurado no destino.'
        }
    })

    # Atualizacao AUTOMATICA da aba Situacao: a mensagem de solicitacao
    # cede lugar ao estado real (em execucao / concluido / falhou) sem o
    # tecnico clicar em nada. O timer nao roda durante operacao de rede.
    $timer = New-Object 'System.Windows.Forms.Timer'
    $timer.Interval = 15000
    $timer.Add_Tick({
        # com pedido de disparo aberto a tela acompanha mais de perto
        $timer.Interval = $(if ($script:Disparo) { 5000 } else { 15000 })
        if (-not $script:OpRede -and -not $script:Atualizando) { & $script:AtualizarTela }
    })
    $timer.Start()
    $form.Add_FormClosed({ try { $timer.Stop(); $timer.Dispose() } catch { } })

    # Quebra de rotulos na largura util: o Layout de cada aba ja chama
    # Update-QuebraRotulos; Resize cobre a transicao larga->estreita em
    # tempo real e Shown fixa a primeira medida (DPI/escala da sessao).
    $form.Add_Resize({ Update-QuebraRotulos })
    $form.Add_Shown({ Update-QuebraRotulos })

    # Por aba: Enter aciona a acao segura da tela e o foco cai no
    # primeiro dado -- o fluxo inteiro fecha so com teclado.
    $tabs.Add_SelectedIndexChanged({
        $form.AcceptButton = $null
        if ($tabs.SelectedTab -eq $script:Aba['1. Ativar']) { $form.AcceptButton = $btValidar; $btPerfil.Focus() }
        elseif ($tabs.SelectedTab -eq $script:Aba['2. Pasta e horário']) { $form.AcceptButton = $btSalvar; $tbOrigem.Focus() }
        elseif ($tabs.SelectedTab -eq $script:Aba['3. Situação']) { $form.AcceptButton = $btAtual; $btAgora.Focus(); & $script:AtualizarTela }
        elseif ($tabs.SelectedTab -eq $script:Aba['4. Restaurar']) { $form.AcceptButton = $btSnaps; $btSnaps.Focus() }
    })
    & $script:AtualizarTela
    # Com configuracao valida, abre DIRETO na Situacao (sem o tecnico
    # procurar aba); sem config, comeca pela Ativacao.
    if (Test-ConfiguracaoPronta) { $tabs.SelectedTab = $script:Aba['3. Situação'] }
    else { $form.AcceptButton = $btValidar }
    [void]$form.ShowDialog()
}

if ($MyInvocation.InvocationName -eq '.') { return }

if (-not (Test-IsWindows)) {
    Write-Error 'A interface grafica requer Windows (Agendador de Tarefas + WinForms).'
    exit 2
}

try {
    if ($BaseDir -match '\s') {
        throw "BaseDir nao pode conter espacos (limite do sftp.command): '$BaseDir'"
    }
    Show-Gui
} catch {
    # Lancamento falhou (ex.: o atalho do Menu Iniciar): mensagem util +
    # diagnostico SEM SEGREDOS para a MAC IP -- nunca segue em silencio.
    $diag = try { Write-Diagnostico -Erro $_.Exception } catch { $null }
    $msg = "O MAC IP Backup nao conseguiu abrir a janela.`r`n`r`n" + $_.Exception.Message
    if ($diag) {
        $msg += "`r`n`r`nDiagnostico salvo em: $diag`r`nEnvie esse arquivo a MAC IP (nao contem senhas nem chaves)."
    } else {
        $msg += "`r`n`r`nNao foi possivel gravar o diagnostico - anote o erro acima e acione a MAC IP."
    }
    try {
        Add-Type -AssemblyName System.Windows.Forms -ErrorAction SilentlyContinue
        [void][System.Windows.Forms.MessageBox]::Show($msg, 'MAC IP Backup', 'OK', 'Error')
    } catch { Write-Error $msg }
    exit 1
}
