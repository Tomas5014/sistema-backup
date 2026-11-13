#requires -Version 5.1
# Testes da interface MAC IP Backup -- mesma infra da suite do agente.
# Roda em pwsh no Linux: DPAPI e Agendador sao substituidos (backend
# injetavel com memoria de tarefa); no Windows roda a prova COM real.
# Cobre: perfil, salvamento em 2 fases com rollback, tarefa (logon type,
# propriedade, idempotencia, conta, execucao unica), classificacao e
# restauracao exata (ls --json + dump + link/junction).
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

    # ---- backend de tarefa FALSO com memoria (substitui o COM) ----
    function New-ColFalsa {
        # Colecao no estilo COM: Clear/Create/Remove(1-based)/Item(1-based)/Count.
        param([scriptblock]$Fabrica)
        $c = [pscustomobject]@{ itens = (New-Object 'System.Collections.ArrayList'); fabrica = $Fabrica }
        $c | Add-Member ScriptProperty Count { return $this.itens.Count }
        $c | Add-Member ScriptMethod Clear { $this.itens.Clear() }
        $c | Add-Member ScriptMethod Create { param($t) $o = & $this.fabrica $t; [void]$this.itens.Add($o); return $o }
        $c | Add-Member ScriptMethod Remove { param($i) $this.itens.RemoveAt($i - 1) }
        $c | Add-Member ScriptMethod Item { param($i) return $this.itens[$i - 1] }
        return $c
    }
    function New-DefFalsa {
        return [pscustomobject]@{
            RegistrationInfo = [pscustomobject]@{ Description = '' }
            Settings = [pscustomobject]@{ Enabled=$null; MultipleInstances=$null; DisallowStartIfOnBatteries=$null; StopIfGoingOnBatteries=$null; ExecutionTimeLimit=$null }
            Principal = [pscustomobject]@{ UserId=''; LogonType=0; RunLevel=0 }
            Triggers = (New-ColFalsa -Fabrica { param($t) [pscustomobject]@{ StartBoundary=''; DaysInterval=0; Enabled=$false } })
            Actions  = (New-ColFalsa -Fabrica { param($t) [pscustomobject]@{ Path=''; Arguments='' } })
        }
    }
    function Set-DefData {
        # Aplica um snapshot {descricao,userId,logon,args,enabled} numa def falsa.
        param($Def, $D)
        $Def.RegistrationInfo.Description = $D.descricao
        $Def.Principal.UserId = $D.userId; $Def.Principal.LogonType = $D.logon
        $Def.Settings.Enabled = $D.enabled
        $a = $Def.Actions.Create(0); $a.Arguments = $D.args; $a.Path = 'powershell.exe'
        $tr = $Def.Triggers.Create(0); $tr.StartBoundary = $D.trigger
        return $Def
    }
    function Get-DefData {
        # Extrai o snapshot de uma def (usado pelo "registro" do backend falso).
        param($Def)
        return @{
            descricao = [string]$Def.RegistrationInfo.Description
            userId = [string]$Def.Principal.UserId; logon = [int]$Def.Principal.LogonType
            enabled = [bool]$Def.Settings.Enabled
            args = $(if ($Def.Actions.Count -gt 0) { [string]$Def.Actions.Item(1).Arguments } else { '' })
            trigger = $(if ($Def.Triggers.Count -gt 0) { [string]$Def.Triggers.Item(1).StartBoundary } else { '' })
        }
    }
    function New-BackendFalso {
        # defData = a tarefa "gravada" (definicoes devolvidas por Get() sao
        # NOVAS a cada chamada -- mutacoes locais nao vazam para o gravado,
        # como no COM real).
        param([string[]]$FalharEm = @(), [bool]$TemEstranha = $false)
        $b = [pscustomobject]@{ defData = $null; registros = 0; falha = $FalharEm; estado = 1; runs = 0; inst = 0 }
        if ($TemEstranha) {
            $b.defData = @{ descricao='Manutencao da empresa'; userId='OUTRO\usuario'; logon=1
                            enabled=$true; args='-File C:\outra\coisa.ps1'; trigger='' }
        }
        $b | Add-Member ScriptMethod Conectar {
            if ($this.falha -contains 'conectar') { throw 'Servico do Agendador indisponivel (simulado)' }
        }
        $b | Add-Member ScriptMethod Get {
            if ($this.falha -contains 'gettask') {
                throw ([Runtime.InteropServices.COMException]::new('Acesso negado (simulado)', -2147024891))
            }
            # texto "not found" com HRESULT ERRADO -> inconclusivo, NAO ausente
            if ($this.falha -contains 'gettask-txt') {
                throw ([Runtime.InteropServices.COMException]::new('odd not found text', -2147023085))
            }
            # "ausente" mentiroso: devolve nao-encontrada MESMO com tarefa
            # gravada -- o CREATE abaixo e quem barra a colisao real.
            if ($this.falha -contains 'ausente-falsa') {
                throw ([Runtime.InteropServices.COMException]::new('nao existe', -2147024894))
            }
            # falha temporizada: consulta quebra SOMENTE apos um registro ter
            # acontecido (cenario "acesso negado logo apos registrar").
            if (($this.falha -contains 'gettask-pos-reg') -and ($this.registros -gt 0)) {
                throw ([Runtime.InteropServices.COMException]::new('Acesso negado (simulado)', -2147024891))
            }
            if (-not $this.defData) {
                # como o COM real: tarefa inexistente THROW 0x80070002 -- nao $null
                throw ([Runtime.InteropServices.COMException]::new('nao existe', -2147024894))
            }
            $d = Set-DefData -Def (New-DefFalsa) -D $this.defData
            # formato REAL do COM: Principal fica em Definition.Principal --
            # NAO existe Principal de primeiro nivel na tarefa.
            $t = [pscustomobject]@{
                Enabled=$d.Settings.Enabled; State=$this.estado
                LastRunTime=''; NextRunTime='amanha'; LastTaskResult=0
                Xml=($this.defData | ConvertTo-Json -Compress)
                Definition=$d
                bk=$this
            }
            $t | Add-Member ScriptMethod Run { param($x) $this.bk.runs++; $this.State = 4 } | Out-Null
            $t | Add-Member ScriptMethod GetInstances { param($n)
                # ," preserva a colecao vazia -- return puro viraria $null
                return ,(New-Object 'object[]' $this.bk.inst) } | Out-Null
            return $t
        }
        $b | Add-Member ScriptMethod NewDef { param($Tarefa)
            return (New-DefFalsa)   # sempre copia nova -- o registrado fica intacto
        }
        $b | Add-Member ScriptMethod Registrar { param($Def,$Conta,$SenhaConta,[int]$Flag)
            if ($this.falha -contains 'registrar') { throw 'Permissao negada (simulada)' }
            # homonima aparece ENTRE a consulta e o cadastro: CREATE (2) deve
            # falhar sem sobrescreve-la (como o COM real faria).
            if ($Flag -eq 2 -and $this.defData) { throw 'Ja existe uma tarefa com esse nome' }
            if ($Flag -eq 4 -and -not $this.defData) { throw 'A tarefa nao existe' }
            # 're-registrar': outra definicao toma o lugar logo apos o nosso
            # registro (simula re-registro concorrente).
            $this.registros++
            if ($this.falha -contains 're-registrar') {
                $this.defData = @{ descricao='Manutencao da empresa'; userId='OUTRO\usuario'; logon=1
                                  enabled=$true; args='-File C:\outra\coisa.ps1'; trigger='' }
                return
            }
            $this.defData = Get-DefData -Def $Def
            $this.defData.userId = $Conta
        }
        $b | Add-Member ScriptMethod RegistrarXml { param([string]$Xml,$Conta,$SenhaConta)
            if ($this.falha -contains 'registrarxml') { throw 'Falha rollback (simulada)' }
            $d = $Xml | ConvertFrom-Json
            $this.defData = @{ descricao=[string]$d.descricao; userId=[string]$d.userId; logon=[int]$d.logon
                               enabled=[bool]$d.enabled; args=[string]$d.args; trigger=[string]$d.trigger }
        }
        $b | Add-Member ScriptMethod XmlAtual {
            try { if ($this.defData) { return ($this.defData | ConvertTo-Json -Compress) } else { return $null } }
            catch { return $null }
        }
        return $b
    }

    # ---- Read-Perfil ----
    $kh = 'backup.macip.example ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFicticia'
    $idValido = 'a' * 64
    function Write-Perfil($obj) {
        $f = Join-Path $tmp ('perfil-' + [Guid]::NewGuid().ToString('N') + '.json')
        ($obj | ConvertTo-Json -Depth 4) | Set-Content -LiteralPath $f -Encoding UTF8
        return $f
    }
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

    # ---- fixtures comuns ----
    $origem = Join-Path $tmp 'origem'; New-Item -ItemType Directory $origem | Out-Null
    'x' | Set-Content (Join-Path $origem 'amostra.txt') -Encoding Ascii
    $chaveLab = Join-Path $tmp 'chave-lab'; 'k' | Set-Content $chaveLab -Encoding Ascii
    $script:senhaTeste = 'senha-lab-x'
    $stubDir = Join-Path $tmp 'stub'
    New-Item -ItemType Directory $stubDir -Force | Out-Null
    ($idValido) | Set-Content (Join-Path $stubDir 'id') -Encoding Ascii -NoNewline
    '0' | Set-Content (Join-Path $stubDir 'code') -Encoding Ascii -NoNewline
    ('c' * 64) | Set-Content (Join-Path $stubDir 'snapshot') -Encoding Ascii -NoNewline
    $env:RESTIC_STUB_DIR = $stubDir
    New-Item -ItemType Directory -Path $script:BaseDir -Force | Out-Null
    Copy-Item -LiteralPath $Stub -Destination (Join-Path $script:BaseDir 'restic-stub.ps1')

    # ---- FASE 1+2: ativacao + salvamento felizes ----
    $script:Backend = New-BackendFalso
    $b1 = Build-Configuracao -Perfil $p -ChavePath $chaveLab `
        -SenhaRestic (ConvertTo-SecureString $script:senhaTeste -AsPlainText -Force) `
        -Origem $origem -Exclusoes @('*.tmp') -Horario '02:30'
    ok (Test-Path -LiteralPath $b1.Config -PathType Leaf) 'fase1: backup.json do conjunto novo criado'
    ok (Test-Path -LiteralPath $b1.Estado -PathType Container) 'fase1: estado novo preparado'
    ok ((Get-ChildItem -LiteralPath $script:BaseDir -Directory -Filter 'estado-*').Count -ge 1) 'fase1: estado-N indexado'
    Publish-Configuracao -Built $b1 -Conta 'LAB\operador' -SenhaConta 'win-senha'
    eq $script:Backend.registros 1 'fase2: tarefa registrada uma vez'
    eq (Get-EstadoAtual) $b1.Estado 'meta: estado atual publicado'
    eq (Get-ConfigAtual) $b1.Config 'meta: config atual publicada'
    $defPublicada = $script:Backend.Get().Definition
    eq $defPublicada.Principal.LogonType 1 'TAREFA: Principal.LogonType = 1 (PASSWORD, nao S4U)'
    eq $defPublicada.Principal.UserId 'LAB\operador' 'TAREFA: conta da GUI registrada'
    ok ($defPublicada.RegistrationInfo.Description -like '*MACIP Backup*') 'TAREFA: marcada como do aplicativo'

    # salvar de novo (mesmo destino): atualiza a MESMA tarefa, nao duplica
    $b2 = Build-Configuracao -Perfil $null -ChavePath '' -SenhaRestic $null `
        -Origem $origem -Exclusoes @('*.tmp','cache*') -Horario '03:00'
    Publish-Configuracao -Built $b2 -Conta 'LAB\operador' -SenhaConta 'win-senha'
    eq $script:Backend.registros 2 'fase2(2): segunda gravacao = segunda registracao da MESMA tarefa'
    ok ($null -ne $script:Backend.Get() -and $script:Backend.Get().Definition -is [pscustomobject]) 'fase2(2): ainda uma unica tarefa'
    eq $b2.Estado $b1.Estado 'mesmo destino: reusa o estado (DPAPI) anterior'

    # ---- falha no registro: conjunto novo removido, anterior intacto ----
    $configAntes = Get-ConfigAtual; $estadoAntes = Get-EstadoAtual
    $defAntiga = $script:Backend.Get().Definition
    $script:Backend = New-BackendFalso
    $script:Backend.Registrar($defAntiga, 'LAB\operador', 'w')   # restaura a "anterior"
    $script:Backend.falha = @('registrar')
    throws { Publish-Configuracao -Built (Build-Configuracao -Perfil $null -ChavePath '' -SenhaRestic $null -Origem $origem -Exclusoes @() -Horario '04:00') -Conta 'LAB\operador' -SenhaConta 'w' } 'falha COM no registro propaga erro'
    eq (Get-ConfigAtual) $configAntes 'registro falhou: config anterior preservada'
    eq (Get-EstadoAtual) $estadoAntes 'registro falhou: estado anterior preservado'
    $n = [regex]::Match($b2.ConfigDir, 'config-(\d+)$').Groups[1].Value
    ok (-not (Test-Path -LiteralPath (Join-Path $script:BaseDir ("config-" + ([int]$n + 1))))) 'registro falhou: conjunto novo removido'

    # ---- cancelamento: nada muda ----
    $script:Backend = New-BackendFalso
    $script:Backend.Registrar($defAntiga, 'LAB\operador', 'w')   # restaura a def da 2a gravacao
    $configAntes = Get-ConfigAtual
    $built = Build-Configuracao -Perfil $null -ChavePath '' -SenhaRestic $null -Origem $origem -Exclusoes @() -Horario '05:00'
    Remove-ConjuntoNovo -Built $built      # simula cancelamento da credencial
    eq (Get-ConfigAtual) $configAntes 'cancelamento: config anterior preservada'
    ok (-not (Test-Path -LiteralPath $built.ConfigDir)) 'cancelamento: conjunto novo removido'

    # ---- falha no preparo (perfil novo, ID do repositorio divergente) ----
    ('b' * 64) | Set-Content (Join-Path $stubDir 'id') -Encoding Ascii -NoNewline
    throws { Build-Configuracao -Perfil $p -ChavePath $chaveLab -SenhaRestic (ConvertTo-SecureString 'x' -AsPlainText -Force) -Origem $origem -Exclusoes @() -Horario '02:30' } 'preparo falho (destino divergente): excecao propagada'
    eq (Get-ConfigAtual) $configAntes 'preparo falho: config anterior preservada'
    ($idValido) | Set-Content (Join-Path $stubDir 'id') -Encoding Ascii -NoNewline

    # ---- propriedade da tarefa: estranha recusada ----
    $script:Backend = New-BackendFalso -TemEstranha $true
    throws { Register-TarefaMacip -Horario '02:30' -Conta 'X' -SenhaConta 'y' -Config 'c' -Estado 'e' } 'tarefa homonima estranha: registro recusado'
    throws { Set-TarefaHabilitada $false } 'tarefa estranha: pausa recusada'
    throws { Start-TarefaAgora } 'tarefa estranha: executar agora recusado'

    # ---- consulta INCONCLUSIVA: GUI nao cria nem altera sem confirmacao ----
    $script:Backend = New-BackendFalso -FalharEm @('gettask')
    throws { Register-TarefaMacip -Horario '02:30' -Conta 'X' -SenhaConta 'y' -Config 'c' -Estado 'e' } 'consulta negada: registro recusado (inconclusivo)'
    eq $script:Backend.registros 0 'consulta negada: nenhum registro tentado'
    throws { Set-TarefaHabilitada $false } 'consulta negada: pausa recusada'
    throws { Start-TarefaAgora } 'consulta negada: executar agora recusado'

    # texto "not found" com HRESULT errado -> INCONCLUSIVO (nao ausente)
    $script:Backend = New-BackendFalso -FalharEm @('gettask-txt')
    throws { Register-TarefaMacip -Horario '02:30' -Conta 'X' -SenhaConta 'y' -Config 'c' -Estado 'e' } 'texto "not found" sem codigo: inconclusivo, recusado'
    eq $script:Backend.registros 0 'texto "not found": nenhum registro tentado'
    # falha de SERVICO (conexao ao COM) -> inconclusivo
    $script:Backend = New-BackendFalso -FalharEm @('conectar')
    throws { Register-TarefaMacip -Horario '02:30' -Conta 'X' -SenhaConta 'y' -Config 'c' -Estado 'e' } 'falha de conexao COM: registro recusado'
    eq $script:Backend.registros 0 'falha de conexao: nenhum registro tentado'

    # homonima criada ENTRE consulta e cadastro: CREATE falha sem tocar nela
    $script:Backend = New-BackendFalso -TemEstranha $true -FalharEm @('ausente-falsa')
    throws { Register-TarefaMacip -Horario '02:30' -Conta 'X' -SenhaConta 'y' -Config 'c' -Estado 'e' } 'homonima entre consulta e cadastro: CREATE falha'
    eq $script:Backend.defData.descricao 'Manutencao da empresa' 'homonima entre consulta e cadastro: estranha intacta'

    # propriedade pela ACAO: marca certa na descricao + -File em outro lugar
    $script:Backend = New-BackendFalso
    $script:Backend.defData = @{ descricao='MACIP Backup - agente de backup do piloto'; userId='X'; logon=1
                                 enabled=$true; args='-File "C:\outro\Backup-MacipCliente.ps1" -Modo Backup'; trigger='' }
    throws { Register-TarefaMacip -Horario '02:30' -Conta 'X' -SenhaConta 'y' -Config 'c' -Estado 'e' } 'marca na descricao mas -File estranho: recusado'
    $script:Backend.defData.args = '-Modo Backup "C:\Program Files\MACIP-Backup\Backup-MacipCliente.ps1"'
    throws { Register-TarefaMacip -Horario '02:30' -Conta 'X' -SenhaConta 'y' -Config 'c' -Estado 'e' } 'caminho so em outro argumento (sem -File): recusado'

    # ---- Executar agora: SID da tarefa == SID do preparo (estado real) ----
    # A conta do preparo (preparo.json em $b1.Estado) e a conta atual do teste.
    $contaPrep = (Get-IdentidadeAtual).Nome
    $script:Backend = New-BackendFalso
    $script:Backend.defData = @{ descricao='MACIP Backup - agente de backup do piloto'; userId=$contaPrep
                                 logon=1; enabled=$true
                                 args=('-File "{0}" -Modo Backup -DiretorioEstado "{1}" -Config "{2}"' -f $script:AgentePath, $b1.Estado, $b1.Config); trigger='' }
    $script:Backend.estado = 4
    throws { Start-TarefaAgora } 'execucao em andamento: novo disparo recusado'
    eq $script:Backend.runs 0 'em execucao: zero chamadas a Run'
    $script:Backend.estado = 1
    Start-TarefaAgora
    eq $script:Backend.runs 1 'executar agora: Run chamado exatamente uma vez na tarefa'
    $script:Backend.estado = 1
    $script:Backend.defData.userId = 'OUTRO\usuario'
    throws { Start-TarefaAgora } 'conta divergente do preparo (SID): disparo recusado'
    eq $script:Backend.runs 1 'conta divergente: zero chamadas novas a Run'
    $script:Backend.defData.userId = ''
    throws { Start-TarefaAgora } 'sem UserId na tarefa: disparo recusado'
    eq $script:Backend.runs 1 'sem UserId: zero chamadas novas a Run'
    # mesmo nome de usuario em outro dominio NAO equivale (SID diverge)
    $script:Backend.defData.userId = ('DOMINIOFALSO\' + $contaPrep)
    throws { Start-TarefaAgora } 'mesmo nome em dominio diferente: disparo recusado (SID)'
    eq $script:Backend.runs 1 'dominio diferente: zero chamadas novas a Run'
    # estado sem preparo.json -> sem identidade verificavel -> recusa
    $estadoVazio = Join-Path $tmp 'estado-sem-preparo'; New-Item -ItemType Directory $estadoVazio | Out-Null
    $script:Backend.defData.userId = $contaPrep
    $script:Backend.defData.args = '-File "{0}" -Modo Backup -DiretorioEstado "{1}" -Config "{2}"' -f $script:AgentePath, $estadoVazio, $b1.Config
    throws { Start-TarefaAgora } 'estado sem preparo.json: disparo recusado'
    eq $script:Backend.runs 1 'sem preparo verificavel: zero chamadas novas a Run'

    # ---- falha na PUBLICACAO do meta (Save-GuiMeta) apos registro COM ----
    # seam de falha injetavel
    $script:SaveMetaReal = ${function:Save-GuiMeta}
    function script:Save-GuiMeta { param($Meta) if ($script:FalhaSaveMeta) { throw 'falha meta (simulada)' }; & $script:SaveMetaReal -Meta $Meta }
    $script:FalhaSaveMeta = $true

    # caso A: primeiro cadastro (sem tarefa anterior) -> conjunto novo
    # PRESERVADO porque a tarefa nova ja o referencia; nada fica apagado.
    $script:Backend = New-BackendFalso   # sem tarefa gravada
    $builtA = Build-Configuracao -Perfil $null -ChavePath '' -SenhaRestic $null -Origem $origem -Exclusoes @() -Horario '06:00'
    throws { Publish-Configuracao -Built $builtA -Conta $contaPrep -SenhaConta 'w' } 'pub falhou (1o cadastro): excecao propagada'
    ok (Test-Path -LiteralPath $builtA.Config -PathType Leaf) 'pub falhou (1o cadastro): arquivos novos preservados'
    $arg = $script:Backend.Get().Definition.Actions.Item(1).Arguments
    ok ($arg -like ('*' + $builtA.Config + '*')) 'pub falhou (1o cadastro): tarefa aponta p/ conjunto novo (funcional)'
    $script:FalhaSaveMeta = $false

    # caso B: atualizacao com reposicao da tarefa anterior -> conjunto novo limpo
    $defAntiga2 = $script:Backend.Get().Definition   # def do caso A virou "anterior"
    $script:Backend = New-BackendFalso
    $script:Backend.Registrar($defAntiga2, $contaPrep, 'w')
    $configAntes = Get-ConfigAtual
    $script:FalhaSaveMeta = $true
    $builtB = Build-Configuracao -Perfil $null -ChavePath '' -SenhaRestic $null -Origem $origem -Exclusoes @() -Horario '07:00'
    throws { Publish-Configuracao -Built $builtB -Conta $contaPrep -SenhaConta 'w' } 'pub falhou (reposicao ok): excecao propagada'
    eq (Get-ConfigAtual) $configAntes 'pub falhou (reposicao ok): tarefa volta a config anterior'
    ok (Test-Path -LiteralPath $configAntes -PathType Leaf) 'pub falhou (reposicao ok): config anterior existe'
    ok (-not (Test-Path -LiteralPath $builtB.ConfigDir)) 'pub falhou (reposicao ok): conjunto novo removido'
    $script:FalhaSaveMeta = $false

    # caso C: atualizacao com FALHA na reposicao -> conjunto novo preservado,
    # tarefa segue apontando para ele (nada apagado sob ela).
    $defAntiga3 = $script:Backend.Get().Definition
    $script:Backend = New-BackendFalso
    $script:Backend.Registrar($defAntiga3, $contaPrep, 'w')
    $script:FalhaSaveMeta = $true
    $builtC = Build-Configuracao -Perfil $null -ChavePath '' -SenhaRestic $null -Origem $origem -Exclusoes @() -Horario '08:00'
    $script:Backend.falha = @('registrarxml')
    throws { Publish-Configuracao -Built $builtC -Conta $contaPrep -SenhaConta 'w' } 'pub falhou (reposicao falhou): excecao propagada'
    ok (Test-Path -LiteralPath $builtC.Config -PathType Leaf) 'pub falhou (reposicao falhou): arquivos novos preservados'
    $argC = $script:Backend.Get().Definition.Actions.Item(1).Arguments
    ok ($argC -like ('*' + $builtC.Config + '*')) 'pub falhou (reposicao falhou): tarefa aponta p/ conjunto novo'
    # a def anterior NAO foi reposta (falha injetada) -- mas seus arquivos
    # continuam existindo: nada foi apagado sob nenhuma das duas defs.
    $cfgAnt = Get-ConfigAtual
    ok ($cfgAnt -eq $builtC.Config) 'pub falhou (reposicao falhou): sem config anterior na tarefa'
    $script:FalhaSaveMeta = $false

    # caso D: consulta negada logo APOS o registro (reposicao ok, limpeza
    # nao consegue confirmar) -> conjunto novo PRESERVADO e aviso; tarefa
    # reposta continua apontando para a config anterior.
    $defAntiga4 = $script:Backend.Get().Definition
    $cfgAntes4 = $defAntiga4.Actions.Item(1).Arguments
    $script:Backend = New-BackendFalso
    $script:Backend.Registrar($defAntiga4, $contaPrep, 'w')
    $script:FalhaSaveMeta = $true
    $builtD = Build-Configuracao -Perfil $null -ChavePath '' -SenhaRestic $null -Origem $origem -Exclusoes @() -Horario '08:30'
    $script:Backend.falha = @('gettask-pos-reg') # consulta quebra SO depois do registro
    throws { Publish-Configuracao -Built $builtD -Conta $contaPrep -SenhaConta 'w' } 'pub falhou (consulta negada pos-registro): excecao propagada'
    ok (Test-Path -LiteralPath $builtD.Config -PathType Leaf) 'consulta negada pos-registro: conjunto novo preservado'
    $script:Backend.falha = @()                 # consulta volta: confere estado real
    eq (Get-ConfigAtual) $builtC.Config 'consulta negada pos-registro: tarefa reposta p/ config anterior'
    $script:FalhaSaveMeta = $false

    # caso E: reposicao E consulta ambas inconclusivas -> nada e removido
    $script:Backend = New-BackendFalso
    $script:Backend.Registrar($defAntiga4, $contaPrep, 'w')
    $script:FalhaSaveMeta = $true
    $builtE = Build-Configuracao -Perfil $null -ChavePath '' -SenhaRestic $null -Origem $origem -Exclusoes @() -Horario '09:30'
    $script:Backend.falha = @('registrarxml', 'gettask-pos-reg')
    throws { Publish-Configuracao -Built $builtE -Conta $contaPrep -SenhaConta 'w' } 'pub falhou (reposicao+consulta inconclusivas): excecao propagada'
    ok (Test-Path -LiteralPath $builtE.Config -PathType Leaf) 'reposicao inconclusiva: arquivos preservados'
    $script:Backend.falha = @()
    $script:FalhaSaveMeta = $false

    # caso F: entre o registro e a reposicao, a def foi TROCADA por outra
    # (re-registro concorrente) -> NAO repoe; arquivos preservados.
    $script:Backend = New-BackendFalso
    $script:Backend.Registrar($defAntiga4, $contaPrep, 'w')
    $script:FalhaSaveMeta = $true
    $builtF = Build-Configuracao -Perfil $null -ChavePath '' -SenhaRestic $null -Origem $origem -Exclusoes @() -Horario '10:00'
    $script:Backend.falha = @('re-registrar')
    throws { Publish-Configuracao -Built $builtF -Conta $contaPrep -SenhaConta 'w' } 'pub falhou (def trocada): excecao propagada'
    ok (Test-Path -LiteralPath $builtF.Config -PathType Leaf) 'def trocada: arquivos preservados'
    eq $script:Backend.defData.descricao 'Manutencao da empresa' 'def trocada: estranha nao foi reposta por cima'
    $script:Backend.falha = @()
    $script:FalhaSaveMeta = $false

    # instancia ATIVA/PENDENTE bloqueia a limpeza do conjunto
    $script:Backend = New-BackendFalso
    $script:Backend.defData = @{ descricao='MACIP Backup - agente de backup do piloto'; userId=$contaPrep; logon=1
                                 enabled=$true
                                 args=('-File "{0}" -Modo Backup -DiretorioEstado "{1}" -Config "{2}"' -f $script:AgentePath, $b1.Estado, $b1.Config); trigger='' }
    $script:Backend.inst = 1
    $builtG = Build-Configuracao -Perfil $null -ChavePath '' -SenhaRestic $null -Origem $origem -Exclusoes @() -Horario '10:30'
    $avisoG = Remove-ConjuntoNovo -Built $builtG
    ok ($null -ne $avisoG) 'instancia ativa/pendente: conjunto preservado com aviso'
    ok (Test-Path -LiteralPath $builtG.Config -PathType Leaf) 'instancia ativa/pendente: arquivos intactos'
    $script:Backend.inst = 0
    Remove-ConjuntoNovo -Built $builtG | Out-Null
    ok (-not (Test-Path -LiteralPath $builtG.ConfigDir)) 'sem instancia: conjunto removido'

    # ---- conta do preparo diverge da sessao: reuso de estado recusado ----
    $prepFile = Join-Path $b1.Estado 'preparo.json'
    $prepJson = Get-Content -LiteralPath $prepFile -Raw
    ($prepJson | ConvertFrom-Json | ForEach-Object { $_.sid = 'S-1-OUTRA-CONTA'; $_ } | ConvertTo-Json) | Set-Content -LiteralPath $prepFile -Encoding UTF8
    throws { Build-Configuracao -Perfil $null -ChavePath '' -SenhaRestic $null -Origem $origem -Exclusoes @() -Horario '09:00' } 'reuso de estado com SID divergente: recusado'
    $prepJson | Set-Content -LiteralPath $prepFile -Encoding UTF8
    ok $true 'reuso de estado: SID da sessao == SID do preparo (fluxo normal ja passou acima)'

    # ---- classificacao ----
    $infoOk = [pscustomobject]@{ Habilitada=$true; Estado=1; Ultima=''; Proxima='a'; Codigo=0 }
    eq (Classify-Resultado -Res $null -InfoTarefa $infoOk) 'nunca_executou' 'classifica: sem resultado'
    eq (Classify-Resultado -Res ([pscustomobject]@{status='sucesso';inicio_utc='2030-01-01T00:00:00Z';codigo_restic=0}) -InfoTarefa $infoOk) 'sucesso' 'classifica: sucesso'
    eq (Classify-Resultado -Res ([pscustomobject]@{status='falha';inicio_utc='2030-01-01T00:00:00Z';codigo_restic=1}) -InfoTarefa $infoOk) 'falha' 'classifica: falha'
    eq (Classify-Resultado -Res ([pscustomobject]@{status='falha';inicio_utc='2030-01-01T00:00:00Z';codigo_restic=3}) -InfoTarefa $infoOk) 'incompleto' 'classifica: incompleto (restic 3)'
    eq (Classify-Resultado -Res ([pscustomobject]@{status='sucesso';inicio_utc='2030-01-01T00:00:00Z'}) -InfoTarefa ([pscustomobject]@{Habilitada=$true;Estado=4;Ultima='';Proxima='';Codigo=0})) 'em_execucao' 'classifica: em execucao agora'
    eq (Classify-Resultado -Res ([pscustomobject]@{status='sucesso';inicio_utc='2026-01-01T00:00:00Z'}) -InfoTarefa ([pscustomobject]@{Habilitada=$true;Estado=1;Ultima='2030-06-01T03:00:00Z';Proxima='';Codigo=0})) 'sem_resultado_novo' 'classifica: resultado antigo nao conta como novo'

    # ---- restauracao: ls --json filtra so arquivos regulares ----
    @'
{"name":".","type":"dir","path":"/o"}
{"name":"amostra.txt","type":"file","path":"/o/amostra.txt","size":43}
{"name":"link","type":"symlink","path":"/o/link"}
{"name":"a[1].txt","type":"file","path":"/o/a[1].txt","size":5}
'@ | Set-Content (Join-Path $stubDir 'ls.jsonl') -Encoding Ascii
    $estado = $b1.Estado
    $arqs = Get-ArquivosSnapshot -Estado $estado -Config $b1.Config -Snapshot ('c'*64)
    eq $arqs.Count 2 'ls --json: somente arquivos regulares (dir e symlink fora)'
    ok ($arqs -contains '/o/a[1].txt') 'ls --json: caminho exato com [] preservado'

    # Restore-Arquivo: destino = origem / dentro da origem / junction
    throws { Restore-Arquivo -Estado $estado -Config $b1.Config -Snapshot ('c'*64) -Arquivo '/o/amostra.txt' -Destino $origem -Origem $origem } 'restaurar: destino = origem recusado'
    throws { Restore-Arquivo -Estado $estado -Config $b1.Config -Snapshot ('c'*64) -Arquivo '/o/amostra.txt' -Destino (Join-Path $origem 'sub') -Origem $origem } 'restaurar: destino dentro da origem recusado'
    $junDir = Join-Path $tmp 'link-para-origem'
    if (-not (Test-IsWindows)) { New-Item -ItemType SymbolicLink -Path $junDir -Target $origem | Out-Null }
    else { cmd /c mklink /J "$junDir" "$origem" | Out-Null }
    throws { Restore-Arquivo -Estado $estado -Config $b1.Config -Snapshot ('c'*64) -Arquivo '/o/amostra.txt' -Destino $junDir -Origem $origem } 'restaurar: destino equivalente por link/junction recusado'
    $cheio = Join-Path $tmp 'cheio'; New-Item -ItemType Directory $cheio | Out-Null; 'x' | Set-Content (Join-Path $cheio 'f')
    throws { Restore-Arquivo -Estado $estado -Config $b1.Config -Snapshot ('c'*64) -Arquivo '/o/amostra.txt' -Destino $cheio -Origem $origem } 'restaurar: destino nao vazio recusado'
    # pasta final INEXISTENTE sob um PAI link para a origem -> resolve p/ dentro da origem -> recusa
    $linkPai = Join-Path $tmp 'link-pai-origem'
    if (-not (Test-IsWindows)) { New-Item -ItemType SymbolicLink -Path $linkPai -Target $origem | Out-Null }
    else { cmd /c mklink /J "$linkPai" "$origem" | Out-Null }
    throws { Restore-Arquivo -Estado $estado -Config $b1.Config -Snapshot ('c'*64) -Arquivo '/o/amostra.txt' -Destino (Join-Path $linkPai 'subdir-nova') -Origem $origem } 'restaurar: pai link + pasta nova = dentro da origem (recusado)'

    # Restore real via stub dump: arquivo exato, bytes e SHA-256 conferidos.
    $conteudo = 'CONTEUDO-LAB-' + ('A' * 8)
    [IO.File]::WriteAllBytes((Join-Path $stubDir 'dump.bin'), [Text.Encoding]::UTF8.GetBytes($conteudo))
    $dest = Join-Path $tmp 'dest-restore'
    $alvo = Restore-Arquivo -Estado $estado -Config $b1.Config -Snapshot ('c'*64) `
        -Arquivo '/o/a[1].txt' -Destino $dest -Origem $origem
    eq (Split-Path -Leaf $alvo) 'a[1].txt' 'restaurar: nome exato do arquivo escolhido'
    eq (Get-Content -LiteralPath $alvo -Raw) $conteudo 'restaurar: conteudo conferido'
    eq (Get-FileHash -LiteralPath $alvo -Algorithm SHA256).Hash.ToLower() `
       ([BitConverter]::ToString([Security.Cryptography.SHA256]::Create().ComputeHash([Text.Encoding]::UTF8.GetBytes($conteudo))).Replace('-','').ToLower()) 'restaurar: SHA-256 bate com a fonte'

    # nome com caracteres de padrao nao arrasta outros arquivos
    [IO.File]::WriteAllBytes((Join-Path $stubDir 'dump.bin'), [Text.Encoding]::UTF8.GetBytes('SO-A1'))
    $dest2 = Join-Path $tmp 'dest2'
    $alvo2 = Restore-Arquivo -Estado $estado -Config $b1.Config -Snapshot ('c'*64) `
        -Arquivo '/o/a[1].txt' -Destino $dest2 -Origem $origem
    eq (Get-ChildItem -LiteralPath $dest2 -File | Measure-Object).Count 1 'restaurar: [1] literal -- um unico arquivo no destino'

    # colisao: arquivo existente NUNCA e sobrescrito (criacao exclusiva)
    $dest3 = Join-Path $tmp 'dest-colisao'; New-Item -ItemType Directory $dest3 | Out-Null
    'CONTEUDO-ORIGINAL-PROTEGIDO' | Set-Content -LiteralPath (Join-Path $dest3 'a[1].txt') -Encoding Ascii -NoNewline
    throws { Restore-Arquivo -Estado $estado -Config $b1.Config -Snapshot ('c'*64) `
        -Arquivo '/o/a[1].txt' -Destino $dest3 -Origem $origem } 'restaurar: arquivo existente recusado (sem sobrescrever)'
    eq (Get-Content -LiteralPath (Join-Path $dest3 'a[1].txt') -Raw) 'CONTEUDO-ORIGINAL-PROTEGIDO' 'restaurar: conteudo existente preservado'

    # arquivo ausente no repositorio (dump falha) -> so o parcial da tentativa some
    Remove-Item -LiteralPath (Join-Path $stubDir 'dump.bin') -Force
    $dest4 = Join-Path $tmp 'dest-ausente'
    throws { Restore-Arquivo -Estado $estado -Config $b1.Config -Snapshot ('c'*64) `
        -Arquivo '/o/inexistente.txt' -Destino $dest4 -Origem $origem } 'restaurar: arquivo ausente no repositorio falha'
    eq (Get-ChildItem -LiteralPath $dest4 -Force | Measure-Object).Count 0 'restaurar: parcial removido, destino volta vazio'

    # processo falha APOS gravar bytes (codigo != 0) -> parcial removido
    [IO.File]::WriteAllBytes((Join-Path $stubDir 'dump.bin'), [Text.Encoding]::UTF8.GetBytes('PARCIAL'))
    '1' | Set-Content -LiteralPath (Join-Path $stubDir 'dump-code') -Encoding Ascii -NoNewline
    $dest6 = Join-Path $tmp 'dest-falha-bytes'
    throws { Restore-Arquivo -Estado $estado -Config $b1.Config -Snapshot ('c'*64) `
        -Arquivo '/o/parcial.txt' -Destino $dest6 -Origem $origem } 'restaurar: falha apos gravar bytes -> erro'
    eq (Get-ChildItem -LiteralPath $dest6 -Force | Measure-Object).Count 0 'restaurar: falha apos bytes -- parcial removido'
    Remove-Item -LiteralPath (Join-Path $stubDir 'dump-code') -Force

    # stderr com volume suficiente para encher o pipe -> nao trava
    'big' | Set-Content -LiteralPath (Join-Path $stubDir 'stderr-big') -Encoding Ascii -NoNewline
    [IO.File]::WriteAllBytes((Join-Path $stubDir 'dump.bin'), [Text.Encoding]::UTF8.GetBytes('OK-PIPES'))
    $dest7 = Join-Path $tmp 'dest-stderr'
    $alvo7 = Restore-Arquivo -Estado $estado -Config $b1.Config -Snapshot ('c'*64) `
        -Arquivo '/o/ok.txt' -Destino $dest7 -Origem $origem
    eq (Get-Content -LiteralPath $alvo7 -Raw) 'OK-PIPES' 'restaurar: stderr grande drenado em paralelo'
    Remove-Item -LiteralPath (Join-Path $stubDir 'stderr-big') -Force

    # colisao no nome FINAL durante a publicacao -> arquivo existente intacto
    $cfgOp = Get-CfgOperacional -Estado $estado -Config $b1.Config
    $dest8 = Join-Path $tmp 'dest-colisao-pub'; New-Item -ItemType Directory $dest8 | Out-Null
    'ORIGINAL' | Set-Content -LiteralPath (Join-Path $dest8 'a[1].txt') -Encoding Ascii -NoNewline
    [IO.File]::WriteAllBytes((Join-Path $stubDir 'dump.bin'), [Text.Encoding]::UTF8.GetBytes('NOVO'))
    throws { Invoke-ResticDump -Cfg $cfgOp -Estado $estado -Snapshot ('c'*64) -Arquivo '/o/a[1].txt' -Destino $dest8 } 'restaurar: colisao no destino -> recusado (sem sobrescrever)'
    eq (Get-Content -LiteralPath (Join-Path $dest8 'a[1].txt') -Raw) 'ORIGINAL' 'restaurar: arquivo pre-existente intacto'
    eq (Get-ChildItem -LiteralPath $dest8 -Force -File | Measure-Object).Count 1 'restaurar: parcial da tentativa removido na colisao'

    # timeout: processo que nao termina -> Kill + parcial removido
    '5' | Set-Content -LiteralPath (Join-Path $stubDir 'dump-sleep') -Encoding Ascii -NoNewline
    $script:DumpTimeoutMs = 300
    $dest9 = Join-Path $tmp 'dest-timeout'; New-Item -ItemType Directory $dest9 | Out-Null
    throws { Invoke-ResticDump -Cfg $cfgOp -Estado $estado -Snapshot ('c'*64) -Arquivo '/o/lento.txt' -Destino $dest9 } 'restaurar: timeout -> erro e Kill'
    eq (Get-ChildItem -LiteralPath $dest9 -Force | Measure-Object).Count 0 'restaurar: timeout -- parcial removido'
    $script:DumpTimeoutMs = 600000
    Remove-Item -LiteralPath (Join-Path $stubDir 'dump-sleep') -Force

    # falha de ESCRITA com restic retornando 0 -> FALHA, nada publicado
    $script:DumpStreamFail = $true
    $dest10 = Join-Path $tmp 'dest-writefail'; New-Item -ItemType Directory $dest10 | Out-Null
    throws { Invoke-ResticDump -Cfg $cfgOp -Estado $estado -Snapshot ('c'*64) -Arquivo '/o/a[1].txt' -Destino $dest10 } 'restaurar: falha de escrita -> erro mesmo com restic 0'
    eq (Get-ChildItem -LiteralPath $dest10 -Force | Measure-Object).Count 0 'restaurar: falha de escrita -- nada publicado nem parcial'
    $script:DumpStreamFail = $false

    # arquivo BINARIO com nome de padrao: SHA-256 confere byte a byte
    $bin = New-Object byte[] 256
    for ($i = 0; $i -lt 256; $i++) { $bin[$i] = [byte]$i }
    [IO.File]::WriteAllBytes((Join-Path $stubDir 'dump.bin'), $bin)
    $dest5 = Join-Path $tmp 'dest-bin'
    $alvo5 = Restore-Arquivo -Estado $estado -Config $b1.Config -Snapshot ('c'*64) `
        -Arquivo '/o/a[1].txt' -Destino $dest5 -Origem $origem
    eq (Get-FileHash -LiteralPath $alvo5 -Algorithm SHA256).Hash `
       ([BitConverter]::ToString([Security.Cryptography.SHA256]::Create().ComputeHash($bin)).Replace('-','')) 'restaurar: binario a[1].txt -- SHA-256 confere'

    # ---- desinstalacao estruturada (Desinstalar-Tarefa.ps1) ----
    . (Join-Path $PSScriptRoot '..\installer\Desinstalar-Tarefa.ps1')
    $PrefixoAcao = 'C:\Program Files\MACIP-Backup'
    function New-TarefaFalsa {
        param([string]$Desc = 'MACIP Backup - agente de backup do piloto',
              [string]$ArgsAcao = '-NoProfile -File "C:\Program Files\MACIP-Backup\Backup-MacipCliente.ps1" -Modo Backup',
              [int]$Instancias = 0, [bool]$FalhaEnabled = $false,
              [bool]$NaoPara = $false, [bool]$FalhaInstancias = $false, [bool]$NaoDesabilita = $false)
        $t = [pscustomobject]@{ _en = $true; nInst = $Instancias; falhaEn = $FalhaEnabled
                                naoPara = $NaoPara; falhaInst = $FalhaInstancias; stops = 0; State = 1
                                naoDesab = $NaoDesabilita }
        $t | Add-Member ScriptProperty Enabled { return $this._en } {
            param($v) if ($this.falhaEn) { throw 'falha ao desabilitar (simulada)' }
                      if ($this.naoDesab) { return }; $this._en = [bool]$v }
        $ri = [pscustomobject]@{ Description = $Desc }
        $ac = [pscustomobject]@{ a1 = ([pscustomobject]@{ Path = 'powershell.exe'; Arguments = $ArgsAcao }) }
        $ac | Add-Member ScriptProperty Count { return 1 }
        $ac | Add-Member ScriptMethod Item { param($i) return $this.a1 }
        $df = [pscustomobject]@{ RegistrationInfo = $ri; Actions = $ac }
        $t | Add-Member NoteProperty Definition $df
        $t | Add-Member ScriptMethod GetInstances { param($n)
            if ($this.falhaInst) { throw 'acesso negado a instancias (simulado)' }
            return ,(New-Object 'object[]' $this.nInst) }
        $t | Add-Member ScriptMethod Stop { param($x) $this.stops++; if (-not $this.naoPara) { $this.nInst = 0 } }
        return $t
    }
    function New-SvcFalso { param($Tarefa = $null, [string]$ErroGet = '', [int]$ErroDepois = 0)
        $s = [pscustomobject]@{ t = $Tarefa; eg = $ErroGet; ed = $ErroDepois; chamadas = 0 }
        $s | Add-Member ScriptMethod GetFolder { param($p) return $this }
        $s | Add-Member ScriptMethod GetTask { param($n)
            $this.chamadas++
            if ($this.eg) {
                # 'naoexiste' -> codigo de tarefa inexistente; outro texto -> acesso
                if ($this.eg -eq 'naoexiste') {
                    throw ([Runtime.InteropServices.COMException]::new('nao existe', -2147024894))
                }
                throw ([Runtime.InteropServices.COMException]::new($this.eg, -2147024891))
            }
            if ($this.ed -gt 0 -and $this.chamadas -gt $this.ed) {
                throw ([Runtime.InteropServices.COMException]::new('re-consulta negada', -2147024891))
            }
            if (-not $this.t) {
                throw ([Runtime.InteropServices.COMException]::new('nao existe', -2147024894))
            }
            return $this.t }
        return $s
    }
    $Svc = New-SvcFalso -Tarefa (New-TarefaFalsa)
    eq (Invoke-DesinstalacaoTarefa) 0 'uninstall: tarefa propria desabilitada -> remover'
    $tFalsa = New-TarefaFalsa -Instancias 1
    $Svc = New-SvcFalso -Tarefa $tFalsa
    eq (Invoke-DesinstalacaoTarefa) 0 'uninstall: execucao em andamento -> encerrada, remover'
    eq $tFalsa.stops 1 'uninstall: Stop() chamado para encerrar a instancia'
    $Svc = New-SvcFalso
    eq (Invoke-DesinstalacaoTarefa) 2 'uninstall: ausente confirmada -> remover'
    $Svc = New-SvcFalso -ErroGet 'Acesso negado'
    eq (Invoke-DesinstalacaoTarefa) 4 'uninstall: consulta negada -> preservar'
    $Svc = New-SvcFalso -Tarefa (New-TarefaFalsa -Desc 'Manutencao da empresa')
    eq (Invoke-DesinstalacaoTarefa) 3 'uninstall: homonima sem a marca -> preservada'
    $Svc = New-SvcFalso -Tarefa (New-TarefaFalsa -ArgsAcao '-File "C:\outro\coisa.ps1"')
    eq (Invoke-DesinstalacaoTarefa) 3 'uninstall: marca ok mas acao fora da instalacao -> preservada'
    $Svc = New-SvcFalso -Tarefa (New-TarefaFalsa -Instancias 1 -NaoPara $true)
    eq (Invoke-DesinstalacaoTarefa) 5 'uninstall: instancia que nao encerra -> preservar'
    $Svc = New-SvcFalso -Tarefa (New-TarefaFalsa -FalhaEnabled $true)
    eq (Invoke-DesinstalacaoTarefa) 4 'uninstall: falha ao desabilitar -> preservar'
    $Svc = New-SvcFalso -Tarefa (New-TarefaFalsa -FalhaInstancias $true)
    eq (Invoke-DesinstalacaoTarefa) 4 'uninstall: consulta de instancias negada -> preservar'
    $Svc = New-SvcFalso -Tarefa (New-TarefaFalsa -ArgsAcao '-File "C:\Program Files\MACIP-Backup2\Backup-MacipCliente.ps1"')
    eq (Invoke-DesinstalacaoTarefa) 3 'uninstall: prefixo parecido (MACIP-Backup2) -> preservada'
    $Svc = New-SvcFalso -Tarefa (New-TarefaFalsa) -ErroDepois 1
    eq (Invoke-DesinstalacaoTarefa) 4 'uninstall: re-consulta falha -> preservar'
    $Svc = New-SvcFalso -Tarefa (New-TarefaFalsa -NaoDesabilita $true)
    eq (Invoke-DesinstalacaoTarefa) 4 'uninstall: Enabled nao ficou false -> preservar'
    $Svc = New-SvcFalso -Tarefa (New-TarefaFalsa -ArgsAcao '-Modo Backup "C:\Program Files\MACIP-Backup\Backup-MacipCliente.ps1"')
    eq (Invoke-DesinstalacaoTarefa) 3 'uninstall: caminho sem -File -> preservada'
    $Svc = New-SvcFalso -ErroGet 'Acesso negado'
    eq (Invoke-DesinstalacaoTarefa) 4 'uninstall: texto de erro qualquer -> inconclusivo (nao ausente)'

    # ---- agendamento COM real (so no Windows) ----
    if (Test-IsWindows) {
        $script:Backend = $null
        Register-TarefaMacip -Horario '02:30' -Conta 'LAB\operador' -SenhaConta 'x' -Config 'c' -Estado 'e'
        Register-TarefaMacip -Horario '03:30' -Conta 'LAB\operador' -SenhaConta 'x' -Config 'c' -Estado 'e'
        $t = (Get-Backend).Get()
        ok ($t.Definition.Principal.LogonType -eq 1) 'COM: LogonType = TASK_LOGON_PASSWORD (1)'
        ok ($t.Definition.RegistrationInfo.Description -like '*MACIP Backup*') 'COM: marca do aplicativo'
    } else {
        pula 'registro COM real (somente Windows)'
    }
} finally {
    Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item Env:RESTIC_STUB_DIR -ErrorAction SilentlyContinue
}
Write-Host ''
Write-Host "RESULTADO: $($script:ok) ok, $($script:er) falha(s), $($script:skip) pulado(s)"
exit $(if ($script:er -gt 0) { 1 } else { 0 })
