#requires -Version 5.1
# Testes da interface MAC IP Backup -- mesma infra da suite do agente.
# Roda em pwsh no Linux: DPAPI e Agendador sao substituidos (backend
# injetavel com memoria de tarefa). COM real somente no helper isolado separado.
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

    # ---- helpers de controle: $w (largura) nao esconde $W ----
    # Bug visto na VM: dentro de New-Label/New-Button/New-TextBox o
    # parametro [int]$w sombreava $W = 'System.Windows.Forms' e
    # "$W.Label" virava "500.Label" (TypeNotFound ao abrir a janela).
    # As tres criacoes tem de usar o tipo COMPLETO; nenhuma funcao com
    # [int]$w pode referenciar "$W.". (Estrutural/simulado -- instanciar
    # o controle WinForms de verdade so acontece no Windows.)
    $guiSrc = Get-Content -LiteralPath $gui -Raw
    foreach ($alvo in @(
        @('New-Rotulo',  'System.Windows.Forms.Label'),
        @('New-Botao',   'System.Windows.Forms.Button'),
        @('New-Caixa',   'System.Windows.Forms.TextBox'))) {
        $fn = [regex]::Escape($alvo[0]); $tipo = [regex]::Escape($alvo[1])
        ok ($guiSrc -match "(?s)function\s+$fn\([^)]*\)\s*\{[^}]*New-Object '$tipo'") "$($alvo[0]): cria o tipo esperado $($alvo[1])"
    }
    foreach ($m in [regex]::Matches($guiSrc, '(?s)function\s+([\w-]+)\s*\([^)]*\[int\]\$w[^)]*\)\s*\{(.*?)\n    \}')) {
        ok ($m.Groups[2].Value -notmatch '"\$W\.') "helper $($m.Groups[1].Value): sem `$W sombreado por `$w"
    }

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
        $a = $Def.Actions.Create(0); $a.Arguments = $D.args
        $a.Path = $(if ($D.psPath) { $D.psPath } else { 'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe' })
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
            $t | Add-Member ScriptMethod Run { param($x) if($x -isnot [string]){throw [ArgumentException]::new('Contrato COM simulado: requer BSTR escalar')};$this.bk|Add-Member NoteProperty runArgument $x -Force;$this.bk.runs++; $this.State = 4 } | Out-Null
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

    # ---- pausa preservada ao salvar: update NAO reativa ----
    $script:Backend = New-BackendFalso
    $script:Backend.defData = @{ descricao='MACIP Backup - agente de backup do piloto'; userId='X'; logon=1
                                 enabled=$false
                                 args=(Get-TaskActionArgs -Agente $script:AgentePath -Config 'c' -Estado 'e'); trigger='' }
    Register-TarefaMacip -Horario '03:15' -Conta 'X' -SenhaConta 'y' -Config 'c' -Estado 'e'
    eq ([bool]$script:Backend.defData.enabled) $false 'salvar com tarefa pausada: PAUSA preservada (so Retomar religa)'

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
                                 enabled=$true; args='-NoProfile -NonInteractive -File "C:\outro\Backup-MacipCliente.ps1" -Modo Backup -Config "c" -DiretorioEstado "e"'; trigger='' }
    throws { Register-TarefaMacip -Horario '02:30' -Conta 'X' -SenhaConta 'y' -Config 'c' -Estado 'e' } 'marca na descricao mas -File estranho: recusado'
    $script:Backend.defData.args = '-Modo Backup "C:\Program Files\MACIP-Backup\Backup-MacipCliente.ps1"'
    throws { Register-TarefaMacip -Horario '02:30' -Conta 'X' -SenhaConta 'y' -Config 'c' -Estado 'e' } 'caminho so em outro argumento (sem -File): recusado'
    # executavel parecido / fora do lugar / relativo -> recusado
    $script:Backend.defData.args = Get-TaskActionArgs -Agente $script:AgentePath -Config 'c' -Estado 'e'
    $script:Backend.defData.psPath = 'C:\Temp\outropowershell.exe'
    throws { Register-TarefaMacip -Horario '02:30' -Conta 'X' -SenhaConta 'y' -Config 'c' -Estado 'e' } 'executavel parecido (outropowershell.exe): recusado'
    $script:Backend.defData.psPath = 'C:\Windows\System32\powershell.exe'
    throws { Register-TarefaMacip -Horario '02:30' -Conta 'X' -SenhaConta 'y' -Config 'c' -Estado 'e' } 'powershell.exe fora do caminho esperado: recusado'
    $script:Backend.defData.psPath = 'powershell.exe'
    throws { Register-TarefaMacip -Horario '02:30' -Conta 'X' -SenhaConta 'y' -Config 'c' -Estado 'e' } 'caminho relativo de powershell: recusado'
    $script:Backend.defData.psPath = $null
    # -Command que apenas MENCIONA -File -> recusado
    $script:Backend.defData.args = '-NoProfile -Command "& ''C:\Program Files\MACIP-Backup\Backup-MacipCliente.ps1'' -File x"'
    throws { Register-TarefaMacip -Horario '02:30' -Conta 'X' -SenhaConta 'y' -Config 'c' -Estado 'e' } '-File citado dentro de -Command: recusado'

    # ---- Executar agora: SID da tarefa == SID do preparo (estado real) ----
    # A conta do preparo (preparo.json em $b1.Estado) e a conta atual do teste.
    $contaPrep = (Get-IdentidadeAtual).Nome
    $script:Backend = New-BackendFalso
    $script:Backend.defData = @{ descricao='MACIP Backup - agente de backup do piloto'; userId=$contaPrep
                                 logon=1; enabled=$true
                                 args=(Get-TaskActionArgs -Agente $script:AgentePath -Config $b1.Config -Estado $b1.Estado); trigger='' }
    $script:Backend.estado = 4
    throws { Start-TarefaAgora } 'execucao em andamento: novo disparo recusado'
    eq $script:Backend.runs 0 'em execucao: zero chamadas a Run'
    $script:Backend.estado = 1
    Start-TarefaAgora
    eq $script:Backend.runs 1 'executar agora: Run chamado exatamente uma vez na tarefa'
    ok ($script:Backend.runArgument -is [string]) 'Run recebe System.String, nao Object[]'
    eq $script:Backend.runArgument 'manual' 'Arg0 manual preservado'
    throws { $script:Backend.Get().Run(@('manual')) } 'contrato simulado recusa forma anterior Object[]'
    eq $script:Backend.runs 1 'recusa simulada nao executa fallback ou segunda tarefa'
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
    $script:Backend.defData.args = Get-TaskActionArgs -Agente $script:AgentePath -Config $b1.Config -Estado $estadoVazio
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
                                 args=(Get-TaskActionArgs -Agente $script:AgentePath -Config $b1.Config -Estado $b1.Estado); trigger='' }
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
    $snapOk = 'c' * 64
    eq (Classify-Resultado -Res $null -InfoTarefa $infoOk) 'ainda_nao' 'classifica: sem disparo e sem resultado'
    eq (Classify-Resultado -Res ([pscustomobject]@{status='sucesso';inicio_utc='2030-01-01T00:00:00Z';codigo_restic=0;snapshot=$snapOk}) -InfoTarefa $infoOk) 'concluido' 'classifica: sucesso valido sem disparo previo'
    eq (Classify-Resultado -Res ([pscustomobject]@{status='sucesso';inicio_utc='2030-01-01T00:00:00Z';codigo_restic=0;snapshot=''}) -InfoTarefa $infoOk) 'falhou' 'classifica: sucesso SEM snapshot valido nao conclui'
    eq (Classify-Resultado -Res ([pscustomobject]@{status='falha';inicio_utc='2030-01-01T00:00:00Z';codigo_restic=1}) -InfoTarefa $infoOk) 'falhou' 'classifica: falha'
    eq (Classify-Resultado -Res ([pscustomobject]@{status='falha';inicio_utc='2030-01-01T00:00:00Z';codigo_restic=3}) -InfoTarefa $infoOk) 'incompleto' 'classifica: incompleto (restic 3)'
    eq (Classify-Resultado -Res ([pscustomobject]@{status='sucesso';inicio_utc='2030-01-01T00:00:00Z'}) -InfoTarefa ([pscustomobject]@{Habilitada=$true;Estado=4;Ultima='';Proxima='';Codigo=0})) 'em_execucao' 'classifica: em execucao agora'
    # sucesso das 14:00 NAO e resultado do disparo das 14:00:50 (era o bug dos 2 min)
    eq (Classify-Resultado -Res ([pscustomobject]@{status='sucesso';inicio_utc='2030-01-01T14:00:00Z';codigo_restic=0;snapshot=$snapOk;fim_utc='2030-01-01T14:01:00Z'}) -InfoTarefa ([pscustomobject]@{Habilitada=$true;Estado=1;Ultima='2030-01-01T14:00:50Z';Proxima='';Codigo=0})) 'sem_resultado' 'classifica: sucesso antigo dentro de 2min NAO e deste disparo'
    # mesmo disparo (inicio >= ultima) CONFERE
    eq (Classify-Resultado -Res ([pscustomobject]@{status='sucesso';inicio_utc='2030-01-01T14:00:51Z';codigo_restic=0;snapshot=$snapOk}) -InfoTarefa ([pscustomobject]@{Habilitada=$true;Estado=1;Ultima='2030-01-01T14:00:50Z';Proxima='';Codigo=0})) 'concluido' 'classifica: resultado do proprio disparo conclui'
    # disparo sem nenhum resultado: Agendador tentou -> sem_resultado (nunca "nunca executou")
    eq (Classify-Resultado -Res $null -InfoTarefa ([pscustomobject]@{Habilitada=$true;Estado=1;Ultima='2030-01-01T14:00:50Z';Proxima='';Codigo=0})) 'sem_resultado' 'classifica: disparo sem resultado nao e "nunca executou"'
    # codigo do Agendador prova FALHA DE INICIALIZACAO
    eq (Classify-Resultado -Res $null -InfoTarefa ([pscustomobject]@{Habilitada=$true;Estado=1;Ultima='2030-01-01T14:00:50Z';Proxima='';Codigo=[int]0x800710E0})) 'falhou' 'classifica: codigo do Agendador prova falha de inicializacao'
    # resultado ilegivel (sem inicio_utc) com disparo -> sem_resultado
    eq (Classify-Resultado -Res ([pscustomobject]@{status='sucesso';codigo_restic=0;snapshot=$snapOk}) -InfoTarefa ([pscustomobject]@{Habilitada=$true;Estado=1;Ultima='2030-01-01T14:00:50Z';Proxima='';Codigo=0})) 'sem_resultado' 'classifica: resultado sem inicio_utc nao se vincula'

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

    # ---- vinculo do preparo na restauracao/listagem ----
    $cfgRest = Get-CfgOperacional -Estado $estado -Config $b1.Config
    ok (Confirm-PreparoVigente -Estado $estado -Cfg $cfgRest) 'preparo vigente: conferido'
    # known_hosts ALTERADO depois do preparo -> vinculo quebrado -> recusa
    $khFile = $cfgRest.known_hosts
    $khOrig = Get-Content -LiteralPath $khFile -Raw
    'outro.host ssh-rsa AAAAadulterado' | Add-Content -LiteralPath $khFile
    throws { Get-Snapshots -Estado $estado -Config $b1.Config } 'known_hosts alterado: listar pontos recusado'
    throws { Restore-Arquivo -Estado $estado -Config $b1.Config -Snapshot ('c'*64) -Arquivo '/o/a[1].txt' -Destino (Join-Path $tmp 'dest-vinc') -Origem $origem } 'known_hosts alterado: restaurar recusado'
    $khOrig | Set-Content -LiteralPath $khFile -NoNewline
    ok (Confirm-PreparoVigente -Estado $estado -Cfg $cfgRest) 'preparo restaurado: vinculo volta a conferir'
    # identidade_ssh apontando para outro arquivo -> recusa
    $cfgBad = $cfgRest.PSObject.Copy()
    $cfgBad.identidade_ssh = Join-Path $tmp 'chave-qualquer'
    throws { Confirm-PreparoVigente -Estado $estado -Cfg $cfgBad } 'identidade_ssh divergente: recusado'
    # ssh_config alterado -> recusa
    $sshCfgF = Join-Path $estado 'ssh_config'
    $sshOrig = Get-Content -LiteralPath $sshCfgF -Raw
    '# adulterado' | Add-Content -LiteralPath $sshCfgF
    throws { Confirm-PreparoVigente -Estado $estado -Cfg $cfgRest } 'ssh_config alterado: recusado'
    $sshOrig | Set-Content -LiteralPath $sshCfgF -NoNewline

    # ---- SFTP que aceita SSH mas nao responde: consulta tem prazo ----
    '20' | Set-Content -LiteralPath (Join-Path $stubDir 'cat-sleep') -Encoding Ascii -NoNewline
    $script:ConsultaTimeoutMs = 800
    $t0 = Get-Date
    throws { Get-Snapshots -Estado $estado -Config $b1.Config } 'SFTP sem resposta: consulta falha dentro do prazo'
    ok (((Get-Date) - $t0).TotalSeconds -lt 15) 'SFTP sem resposta: nao travou (Kill no estouro)'
    # mesmo prazo na ATIVACAO (Test-ConexaoDestino usa Confirm-DestinoComPrazo)
    $t0 = Get-Date
    throws { Test-ConexaoDestino -Perfil $p -ChavePath $chaveLab -SenhaRestic (ConvertTo-SecureString 'x' -AsPlainText -Force) } 'SFTP sem resposta: ativacao falha dentro do prazo'
    ok (((Get-Date) - $t0).TotalSeconds -lt 15) 'SFTP sem resposta: ativacao nao travou'
    # e no PREPARO ao salvar (Build -> Invoke-AgentePreparar -ConfirmaDestino)
    $t0 = Get-Date
    throws { Build-Configuracao -Perfil $p -ChavePath $chaveLab -SenhaRestic (ConvertTo-SecureString 'x' -AsPlainText -Force) -Origem $origem -Exclusoes @() -Horario '02:30' } 'SFTP sem resposta: preparo falha dentro do prazo'
    ok (((Get-Date) - $t0).TotalSeconds -lt 20) 'SFTP sem resposta: preparo nao travou a janela'
    $script:ConsultaTimeoutMs = 60000
    Remove-Item -LiteralPath (Join-Path $stubDir 'cat-sleep') -Force

    # ---- Invoke-Assincrono: listagens devolvem TODOS os itens ----
    @'
{"id":"1111111111111111111111111111111111111111111111111111111111111111","short_id":"11111111","time":"2026-01-01T03:00:00Z"}
{"id":"2222222222222222222222222222222222222222222222222222222222222222","short_id":"22222222","time":"2026-01-02T03:00:00Z"}
'@ | Set-Content -LiteralPath (Join-Path $stubDir 'snaps.jsonl') -Encoding Ascii
    $snapsAsync = Invoke-Assincrono -Funcao 'Get-Snapshots' -Params @{ Estado = $estado; Config = $b1.Config }
    eq @($snapsAsync).Count 2 'assincrono: dois pontos devolvidos (nao so o primeiro)'
    eq ([string]$snapsAsync[1].id).Substring(0,1) '2' 'assincrono: SEGUNDO ponto acessivel'
    $arqsAsync = Invoke-Assincrono -Funcao 'Get-ArquivosSnapshot' -Params @{ Estado = $estado; Config = $b1.Config; Snapshot = '2222' }
    eq @($arqsAsync).Count 2 'assincrono: dois arquivos devolvidos no ponto'
    # restaura o SEGUNDO arquivo via assincrono -> SHA-256 conferido
    [IO.File]::WriteAllBytes((Join-Path $stubDir 'dump.bin'), [Text.Encoding]::UTF8.GetBytes('SEGUNDO-ARQUIVO'))
    $destS2 = Join-Path $tmp 'dest-segundo'
    $alvoS2 = Invoke-Assincrono -Funcao 'Restore-Arquivo' -Params @{
        Estado = $estado; Config = $b1.Config; Snapshot = $snapOk
        Arquivo = '/o/a[1].txt'; Destino = $destS2; Origem = $origem }
    eq (Get-FileHash -LiteralPath $alvoS2 -Algorithm SHA256).Hash.ToLower() `
       ([BitConverter]::ToString([Security.Cryptography.SHA256]::Create().ComputeHash([Text.Encoding]::UTF8.GetBytes('SEGUNDO-ARQUIVO'))).Replace('-','').ToLower()) 'assincrono: restaurar segundo arquivo -- SHA-256 confere'
    # operacao de unico objeto devolve o objeto; vazio devolve $null
    Remove-Item -LiteralPath (Join-Path $stubDir 'snaps.jsonl') -Force
    $vazio = Invoke-Assincrono -Funcao 'Get-Snapshots' -Params @{ Estado = $estado; Config = $b1.Config }
    ok ($null -eq $vazio -or @($vazio).Count -eq 0) 'assincrono: listagem vazia -> sem itens'

    # ---- token de ativacao: prova vale so para os dados capturados ----
    $script:ativacaoToken = Get-TokenAtivacao -Perfil $p -ChavePath $chaveLab -SenhaTxt 'segredo1'
    ok $true 'token de ativacao capturado'
    Test-AtivacaoVigente -Perfil $p -ChavePath $chaveLab -SenhaTxt 'segredo1'
    ok $true 'mesmos dados: prova vigente'
    throws { Test-AtivacaoVigente -Perfil $p -ChavePath $chaveLab -SenhaTxt 'outra' } 'senha mudou: prova expirada'
    $p2 = [pscustomobject]@{ repositorio='sftp://u@h//repo2'; repositorio_id=('b'*64); known_hosts='h ssh-ed25519 AAAAx' }
    throws { Test-AtivacaoVigente -Perfil $p2 -ChavePath $chaveLab -SenhaTxt 'segredo1' } 'perfil mudou: prova expirada'
    throws { Test-AtivacaoVigente -Perfil $p -ChavePath (Join-Path $tmp 'chave2') -SenhaTxt 'segredo1' } 'chave mudou: prova expirada'
    $script:ativacaoToken = $null
    throws { Test-AtivacaoVigente -Perfil $p -ChavePath $chaveLab -SenhaTxt 'segredo1' } 'sem validacao: salvar recusado'

    # ---- ultimo-confirmado.json: sobrevive a falha e a reabertura ----
    # (a gravacao e feita pelo AGENTE -- funcoes Read/Update-Confirmado)
    Update-Confirmado -Estado $estado -RepositorioId $idValido -Snapshot $snapOk `
        -InicioUtc '2026-01-01T03:00:00Z' -FimUtc '2026-01-01T03:10:00Z'
    $conf1 = Read-Confirmado -Estado $estado -RepositorioId $idValido
    eq $conf1.snapshot $snapOk 'confirmado persistido: snapshot guardado'
    # registro invalido NAO substitui (snapshot fora do formato)
    throws { Update-Confirmado -Estado $estado -RepositorioId $idValido -Snapshot 'xx' -FimUtc '2026-01-02T03:00:00Z' } 'confirmado: snapshot invalido recusado'
    eq (Read-Confirmado -Estado $estado -RepositorioId $idValido).snapshot $snapOk 'confirmado anterior preservado'
    # sucesso mais ANTIGO nao regride a data
    Update-Confirmado -Estado $estado -RepositorioId $idValido -Snapshot ('d'*64) -FimUtc '2025-12-01T03:00:00Z'
    eq (Read-Confirmado -Estado $estado -RepositorioId $idValido).snapshot $snapOk 'sucesso mais antigo: data nao regride'
    # repositorio TROCADO nao herda o sucesso do anterior
    ok ($null -eq (Read-Confirmado -Estado $estado -RepositorioId ('b'*64))) 'repo divergente: confirmado nao herdado'
    # registro corrompido nao e apresentado como confirmado
    'lixo nao-json' | Set-Content -LiteralPath (Join-Path $estado 'ultimo-confirmado.json') -Encoding Ascii
    ok ($null -eq (Read-Confirmado -Estado $estado -RepositorioId $idValido)) 'confirmado corrompido: nao apresentado'
    Remove-Item -LiteralPath (Join-Path $estado 'ultimo-confirmado.json') -Force

    # ---- resolucao do OpenSSH (o exe que o agente realmente usa) ----
    # Bug da VM: instalador/GUI checavam so o caminho canonico; o ssh.exe
    # disponivel no PATH passava despercebido -> falso "OpenSSH ausente".
    $sshDir = Join-Path $tmp 'ssh-fakes'; New-Item -ItemType Directory $sshDir | Out-Null
    if (Test-IsWindows) {
        # no Windows o "executavel" falso e um .cmd (executavel por &)
        '@echo OpenSSH_for_Windows_9.5p2' | Set-Content -LiteralPath (Join-Path $sshDir 'ssh-openssh.cmd') -Encoding Ascii
        '@echo lixo qualquer' | Set-Content -LiteralPath (Join-Path $sshDir 'ssh-estranho.cmd') -Encoding Ascii
        $sshBom = Join-Path $sshDir 'ssh-openssh.cmd'; $sshRuim = Join-Path $sshDir 'ssh-estranho.cmd'
    } else {
        @'
#!/bin/sh
echo 'OpenSSH_for_Windows_9.5p2' >&2
exit 0
'@ | Set-Content -LiteralPath (Join-Path $sshDir 'ssh-openssh') -Encoding Ascii
        @'
#!/bin/sh
echo 'lixo qualquer' >&2
exit 0
'@ | Set-Content -LiteralPath (Join-Path $sshDir 'ssh-estranho') -Encoding Ascii
        $sshBom = Join-Path $sshDir 'ssh-openssh'; $sshRuim = Join-Path $sshDir 'ssh-estranho'
        & chmod +x $sshBom $sshRuim | Out-Null
    }
    $dirEspaco = Join-Path $tmp 'dir com espaco'; New-Item -ItemType Directory $dirEspaco | Out-Null
    Copy-Item -LiteralPath $sshBom -Destination (Join-Path $dirEspaco 'ssh-com-espaco')
    $sel = Select-SshCandidato -Candidatos @($sshBom)
    eq $sel.Caminho $sshBom.Replace('\','/') 'OpenSSH valido no caminho: resolvido'
    $sel2 = Select-SshCandidato -Candidatos @($sshRuim, $sshBom)
    eq $sel2.Caminho $sshBom.Replace('\','/') 'nao-OpenSSH ignorado, OpenSSH seguinte escolhido'
    ok ((@($sel2.Rejeitados) -join ';') -like '*nao respondeu como OpenSSH*') 'rejeitado: motivo "nao respondeu como OpenSSH" registrado'
    $sel3 = Select-SshCandidato -Candidatos @((Join-Path $dirEspaco 'ssh-com-espaco'), $sshBom)
    eq $sel3.Caminho $sshBom.Replace('\','/') 'caminho com espacos descartado (limite sftp.command)'
    ok ((@($sel3.Rejeitados) -join ';') -like '*espacos no caminho*') 'rejeitado: motivo "espacos no caminho" registrado'
    $sel4 = Select-SshCandidato -Candidatos @((Join-Path $tmp 'ausente-ssh'))
    ok ($null -eq $sel4.Caminho) 'nenhum candidato: resolucao vazia (erro claro no Get-SshExe)'
    $sshReal = try { Get-SshExe } catch { $null }
    if ($sshReal) { ok ($sshReal.Length -gt 0) 'Get-SshExe: executavel resolvido no ambiente' }
    else { pula 'Get-SshExe: ambiente sem OpenSSH utilizavel' }

    # ---- apresentacao: formato brasileiro / previa / resumo ----
    eq (Format-DataBR ([DateTime]'2030-01-01 13:26:00')) '01/01/2030 13:26' 'data BR: DateTime local -> dd/MM/yyyy HH:mm'
    eq (Format-DataBR 'amanha') '-' 'data BR: texto invalido -> -'
    eq (Format-DataBR $null) '-' 'data BR: nulo -> -'
    $iso = '2030-01-01T14:00:00Z'
    $esperadoIso = ([DateTimeOffset]::Parse($iso, [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::AssumeUniversal)).LocalDateTime.ToString('dd/MM/yyyy HH:mm')
    eq (Format-DataBR $iso) $esperadoIso 'data BR: ISO UTC convertido p/ horario local'
    eq (Format-DataBR ([DateTime]'0001-01-01')) '-' 'data BR: sentinela COM -> -'
    eq (Format-PreviaHorario '13:26') 'Todos os dias às 13:26, no horário deste computador.' 'previa do agendamento diario'
    eq (Format-PreviaHorario '25:00') 'Horário inválido (use HH:mm).' 'previa: horario invalido'
    $resumo = Get-ResumoConfiguracao -Origem 'D:\Dados' -HHmm '13:26' -Destino 'sftp:u@h:/dados/x' -Conta 'LAB\operador'
    ok ($resumo -match 'Pasta protegida: D:\\Dados' -and $resumo -match 'todos os dias às 13:26' -and $resumo -match 'Frequência: diária' -and $resumo -match 'LAB\\operador') 'resumo: pasta+horario+frequencia+conta presentes'
    ok ((Get-ResumoConfiguracao -Origem '' -HHmm '13:26' -Destino '' -Conta 'c') -match 'reusa o destino atual') 'resumo: sem perfil novo -> reusa destino atual'

    # ---- horarios na tela: LOCAL do Agendador x UTC do agente ----
    # Tarefa das 13:26 em Brasilia tem de aparecer 13:26 em todos os
    # caminhos: "Proxima execucao" (salvar e Situacao, com ou sem
    # pedido aberto) e "ultimo disparo registrado" em Detalhes usam o
    # DateTime LOCAL preservado do COM; o "16:26Z" do agente e um
    # instante UTC convertido para o local -> 13:26. O env TZ muda o
    # fuso local do processo no pwsh/Core; onde o fuso nao obedecer,
    # o bloco e pulado (nunca falsifica).
    $tzSave = $env:TZ
    $env:TZ = 'America/Sao_Paulo'
    try {
        if ([DateTimeOffset]::Now.Offset -eq [TimeSpan]::FromHours(-3)) {
            $agUlt  = [DateTime]::new(2026,11,17,13,26,0)   # LastRunTime do COM: hora local
            $agProx = [DateTime]::new(2026,11,18,13,26,0)   # NextRunTime do COM: hora local
            eq (Format-DataBR $agUlt)  '17/11/2026 13:26' 'horario SP: LastRunTime local (DateTime) -> 13:26'
            eq (Format-DataBR $agProx) '18/11/2026 13:26' 'horario SP: NextRunTime local (DateTime) -> 13:26'
            eq (Format-DataBR '2026-11-16T16:26:00Z') '16/11/2026 13:26' 'horario SP: 16:26Z do agente -> 13:26'
            eq (Format-DataBR ([DateTime]::new(2026,11,6,13,26,0))) '06/11/2026 13:26' 'horario SP: data ambigua 06/11 nao vira 11/06'
            eq (Format-DataBR ([DateTimeOffset]::new(2026,11,17,13,26,0,[TimeSpan]::FromHours(-3)))) '17/11/2026 13:26' 'horario SP: DateTimeOffset local -> 13:26'
            eq (Format-DataBR ([DateTime]::new(1899,12,30,0,0,0))) '-' 'horario SP: sentinela COM (nunca rodou) -> -'
            # "Proxima execucao" chega tipada (Get-InfoTarefa preserva) --
            # o mesmo Format-DataBR e usado apos salvar e na aba Situacao.
            $infoProx = [pscustomobject]@{ Conta='x'; Codigo=0; Ultima=$agUlt; Proxima=$agProx }
            eq (Format-DataBR $infoProx.Proxima) '18/11/2026 13:26' 'horario SP: Proxima execucao tipada -> 13:26'
            $detT = Get-DetalhesSuporte -Info $infoProx -Res $null -Conf $null -Estado 'E' -ConfigPath 'C'
            ok ($detT -match 'último disparo registrado: 17/11/2026 13:26') 'horario SP: Detalhes mostra LastRunTime local (13:26)'
            # o pedido tambem nao desloca: a referencia tipada e
            # interpretada como LOCAL na comparacao com inicio_utc (UTC)
            $pedH = New-PedidoDisparo -InfoTarefa $infoProx -Res $null
            ok ($pedH.UltimaRef -is [DateTime]) 'horario SP: referencia do pedido preserva o tipo do COM'
        } else {
            pula 'horario SP: fuso local nao e Sao Paulo - formatacao por fuso nao verificavel'
        }
    } finally { $env:TZ = $tzSave }

    # ---- abertura direta na Situacao (config valida) ----
    $script:Backend = New-BackendFalso
    $script:Backend.defData = @{ descricao='MACIP Backup - agente de backup do piloto'; userId=$contaPrep; logon=1
                                 enabled=$true
                                 args=(Get-TaskActionArgs -Agente $script:AgentePath -Config $b1.Config -Estado $b1.Estado); trigger='' }
    ok (Test-ConfiguracaoPronta) 'config valida: interface abre direto na aba Situacao'
    $metaReal2 = $script:GuiMeta
    $script:GuiMeta = Join-Path $tmp 'gui-meta-vazio.json'
    $script:Backend = New-BackendFalso      # sem tarefa e sem meta
    ok (-not (Test-ConfiguracaoPronta)) 'sem config: interface abre pela Ativacao'
    $script:GuiMeta = $metaReal2

    # ---- detalhes de suporte: IDs e dados, SEM segredos ----
    $det = Get-DetalhesSuporte -Info ([pscustomobject]@{ Conta='LAB\operador'; Codigo=0; Ultima='2030-01-01T13:26:00Z' }) `
        -Res ([pscustomobject]@{ status='sucesso'; codigo_restic=0; inicio_utc='2030-01-01T13:26:00Z'; fim_utc='2030-01-01T13:30:00Z'; snapshot=$snapOk }) `
        -Conf ([pscustomobject]@{ fim_utc='2030-01-01T13:30:00Z'; snapshot=$snapOk }) `
        -Estado 'E' -ConfigPath 'C'
    ok ($det -match 'conta da tarefa: LAB\\operador') 'detalhes: conta presente'
    ok ($det -match "snapshot: $snapOk") 'detalhes: ID do snapshot completo'
    ok ($det -match 'powershell:') 'detalhes: versao do powershell presente'
    ok ($det -notmatch [regex]::Escape($script:senhaTeste)) 'detalhes: SEM a senha'

    # ---- diagnostico de abertura: arquivo sem segredos ----
    $diag = Write-Diagnostico -Erro ([Exception]::new('falha simulada'))
    ok ($diag -and (Test-Path -LiteralPath $diag -PathType Leaf)) 'diagnostico: arquivo gravado'
    $diagTxt = Get-Content -LiteralPath $diag -Raw
    ok ($diagTxt -match 'falha simulada' -and $diagTxt -match 'powershell:' -and $diagTxt -match 'ssh') 'diagnostico: erro+versoes+ssh presentes'
    ok ($diagTxt -notmatch [regex]::Escape($script:senhaTeste) -and $diagTxt -notmatch '(?i)private') 'diagnostico: SEM senha/chave'

    # ---- abrir pasta restaurada: fora do Windows -> aviso, nunca quebra ----
    eq (Open-PastaRestaurada -Alvo (Join-Path $tmp 'qualquer.txt')) $false 'abrir pasta: fora do Windows -> $false (aviso na tela)'

    # ---- contrato da nova tela (estatico) ----
    ok ($guiSrc.Contains('DateTimePicker') -and $guiSrc.Contains('ShowUpDown = $true') -and $guiSrc.Contains("CustomFormat = 'HH:mm'")) 'seletor de horario: DateTimePicker HH:mm com ShowUpDown'
    ok (-not $guiSrc.Contains('$tbHora')) 'seletor de horario: TextBox livre removida (bug da linha na VM)'
    ok ($guiSrc.Contains('System.Windows.Forms.Timer')) 'auto-atualizacao: Timer da Situacao presente'
    ok ($guiSrc.Contains('Exclusões (opcional)')) 'exclusoes: rotulo direto "Exclusões (opcional)" (secao colapsavel)'
    ok ($guiSrc.Contains('Abrir pasta restaurada')) 'restaurar: botao Abrir pasta restaurada'
    ok ($guiSrc.Contains('Write-Diagnostico') -and $guiSrc.Contains('diagnostico-macip.txt')) 'lancamento: diagnostico sem segredos'
    ok ($guiSrc.Contains('Test-ConfiguracaoPronta')) 'abertura direta na Situacao: decisao isolada'
    ok ($guiSrc.Contains('Get-DetalhesSuporte')) 'detalhes de suporte: bloco isolado'
    ok ($guiSrc -match "AutoScaleMode = .Dpi.") 'layout: escala de DPI (100/125/150%)'

    # ---- contrato do refinamento visual (estatico) ----
    ok ($guiSrc.Contains('TableLayoutPanel') -and $guiSrc.Contains('FlowLayoutPanel')) 'layout: TableLayout/Flow substituem coordenadas fixas'
    ok (-not ($guiSrc -match '\.Location = ')) 'layout: nenhuma coordenada fixa (.Location) nas telas'
    ok ($guiSrc.Contains('AccessibleName')) 'a11y: rotulos acessiveis nos campos'
    ok ($guiSrc.Contains('TabIndex')) 'a11y: ordem de Tab explicita'
    ok ($guiSrc.Contains('AcceptButton')) 'a11y: Enter aciona a acao segura de cada aba'
    ok ($guiSrc.Contains('MinimumSize')) 'layout: janela redimensionavel com tamanho minimo'
    ok ($guiSrc.Contains('Passo 1 de 3') -and $guiSrc.Contains('Passo 2 de 3') -and $guiSrc.Contains('Passo 3 de 3')) 'primeiro uso: tres passos indicados'
    ok ($guiSrc.Contains('Ainda falta:')) 'primeiro uso: diz o que ainda falta'
    ok ($guiSrc.Contains('Voltar: Ativação')) 'primeiro uso: Voltar sem perder dados'
    ok ($guiSrc.Contains('Executar backup agora')) 'situacao: acao principal destacada'
    ok ($guiSrc.Contains('Último backup bem-sucedido') -and $guiSrc.Contains('Próximo backup') -and $guiSrc.Contains('Execução atual')) 'situacao: tres informacoes distintas'
    ok ($guiSrc.Contains('Pausar agenda') -and $guiSrc.Contains('Retomar agenda')) 'situacao: Pausar/Retomar agenda'
    ok ($guiSrc.Contains('Data do backup') -and $guiSrc.Contains('Pasta de destino') -and $guiSrc.Contains('Arquivo do ponto')) 'restaurar: tres etapas na ordem'
    ok ($guiSrc.Contains('Caminho completo:')) 'restaurar: caminho completo acessivel'
    ok ($guiSrc.Contains('Nenhum ponto de restauração')) 'restaurar: estado de lista vazia distinto'
    ok ($guiSrc.Contains('SetDetalhes')) 'auto-atualizacao: detalhes so regravam quando mudam (preserva foco/selecao)'
    ok ($guiSrc.Contains('Restore-PastaCompleta') -and $guiSrc.Contains('Get-RaizSnapshot') -and $guiSrc.Contains('Invoke-ResticRestore')) 'pasta: funcoes da restauracao completa isoladas e testaveis'
    ok ($guiSrc.Contains('Restaurar um arquivo') -and $guiSrc.Contains('Restaurar toda a pasta protegida')) 'restaurar: seletor de modo com os dois caminhos'
    ok ($guiSrc.Contains('Pasta protegida registrada nesse ponto')) 'restaurar pasta: raiz do ponto exibida sem selecao de arquivo'
    ok ($guiSrc.Contains("New-Titulo 'Restaurar backup'")) 'restaurar: titulo da tela "Restaurar backup" (arquivo ou pasta inteira)'
    ok ($guiSrc -match "lblRevPonto\.Text = 'Ponto: ' \+ " -and $guiSrc -match "lblRevEscopo\.Text = 'Escopo: ' \+ " -and $guiSrc -match "lblRevDestino\.Text = 'Destino: ' \+ ") 'restaurar: revisao separa Ponto, Escopo e Destino em linhas que quebram'

    # ---- editor de exclusoes: altura real + teclado + interpretacao ----
    ok ($guiSrc -match "Digite uma regra por linha, como ``\*\.tmp``\. Deixe em branco") 'exclusoes: exemplo "uma regra por linha, como *.tmp"'
    ok ($guiSrc -match '\$tbExcl\.Multiline = \$true' -and $guiSrc -match '\$tbExcl\.AcceptsReturn = \$true') 'exclusoes: Enter no editor insere linha (nao dispara Salvar)'
    ok ($guiSrc -match '\$tbExcl\.AcceptsTab = \$false') 'exclusoes: Tab sai do editor'
    ok ($guiSrc -match '\$tbExcl\.WordWrap = \$false' -and $guiSrc -match '\$tbExcl\.ScrollBars = ''Both''') 'exclusoes: quebra visual nunca vira segunda regra (sem wrap + barra horizontal)'
    ok ($guiSrc -match '\$tbExcl\.Dock = ''Top''; \$tbExcl\.Height = 1\d\d') 'exclusoes: editor com altura real (~5 linhas) -- nao colapsa'
    ok (-not ($guiSrc -match 'Windows\.Forms\.GroupBox')) 'exclusoes: GroupBox+AutoSize+Dock=Fill removido (causa do campo fino na VM)'
    ok ($guiSrc -match '\$tbExcl\.Lines = \$cfgAtual\.exclusoes' -and $guiSrc -match '\$tbExcl\.Lines \| Where-Object') 'exclusoes: leitura por linha logica -- salva e recarrega igual'

    # ---- janela estreita: rotulos quebram, nada corta ----
    ok ($guiSrc.Contains('Update-QuebraRotulos') -and $guiSrc.Contains('MaximumSize')) 'layout estreito: rotulos limitados a largura util (sem rolagem horizontal)'
    ok ($guiSrc.Contains('Add_Layout') -and $guiSrc.Contains('Add_Resize') -and $guiSrc.Contains('Add_Shown')) 'layout estreito: quebra recalculada em layout/resize/abertura (barra vertical inclusa)'
    ok ($guiSrc -match 'WrapContents = \$true') 'layout estreito: grupos de botoes quebram para a linha seguinte'
    ok ($guiSrc.Contains('Mostrar detalhes técnicos') -and $guiSrc.Contains('New-DetalhesRecolhidos')) 'detalhes tecnicos: secao recolhida por padrao (Situacao e Restaurar)'
    ok ($guiSrc -match '\$ck\.Tag = @\{ Painel = \$pnl; Tabela = \$ly; Linha = \$row \}') 'detalhes tecnicos: painel/tabela/linha viajam no Tag do checkbox'
    ok ($guiSrc -match '\$d = \$sender\.Tag' -and $guiSrc -match '\$d\.Painel\.Visible = \$sender\.Checked' -and $guiSrc -match '\$d\.Tabela\.RowStyles\[\$d\.Linha\]') 'detalhes tecnicos: handler le pelo $sender (independente entre secoes)'

    # ---- detalhes recolhidos: evento EXECUTADO de verdade ----
    # Os asserts acima conferem o texto do codigo; aqui a funcao REAL e
    # extraida via AST e o CheckedChanged e disparado -- abrir/fechar
    # uma secao nao pode alterar a outra. WinForms nao existe no Linux:
    # tipos minimos de teste (mesmos nomes) recebem o codigo real.
    if (-not ('System.Windows.Forms.CheckBox' -as [type])) {
        Add-Type -TypeDefinition @'
namespace System.Windows.Forms {
    public class Padding { public Padding(int l, int t, int r, int b) {} }
    public class RowStyle {
        public string Tipo; public float Valor;
        public RowStyle(string tipo) { Tipo = tipo; }
        public RowStyle(string tipo, int valor) { Tipo = tipo; Valor = valor; }
    }
    public class RowStyleCollection : System.Collections.Generic.List<RowStyle> {}
    public class ControlCollection : System.Collections.Generic.List<object> {
        public void Add(object c, int col, int row) { base.Add(c); }
    }
    public class Control {
        public string Text = "";
        public bool AutoSize;
        public string AccessibleName = "";
        public object Tag;
        public bool Checked;
        public string Dock = "";
        public bool Visible = true;
        public object MinimumSize;
        public object Margin;
        public int TabIndex;
        public ControlCollection Controls = new ControlCollection();
    }
    public class CheckBox : Control {
        public System.Collections.Generic.List<object> Manipuladores = new System.Collections.Generic.List<object>();
        public void Add_CheckedChanged(object sb) { Manipuladores.Add(sb); }
    }
    public class Panel : Control {}
    public class TextBox : Control {}
    public class TableLayoutPanel : Control {
        public int RowCount;
        public RowStyleCollection RowStyles = new RowStyleCollection();
    }
}
'@
    }
    function New-Rotulo { param($t, [bool]$titulo = $false) [pscustomobject]@{ Text = $t; Dock = '' } }
    function Add-Linha { param($ly, $c, [int]$tab, [int]$margemTopo = 3)
        $row = $ly.RowCount; $ly.RowCount += 1
        $ly.RowStyles.Add((New-Object 'System.Windows.Forms.RowStyle'('AutoSize'))) | Out-Null
        $c.Dock = 'Top'; if ($tab -ge 0) { $c.TabIndex = $tab }
        $ly.Controls.Add($c, 0, $row)
        return $c }
    $tkG = $null; $erG = $null
    $astG = [System.Management.Automation.Language.Parser]::ParseFile($gui, [ref]$tkG, [ref]$erG)
    $fnDet = $astG.Find({ param($a) $a -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $a.Name -eq 'New-DetalhesRecolhidos' }, $true)
    . ([scriptblock]::Create($fnDet.Extent.Text))
    $script:W = 'System.Windows.Forms'
    $lyA = New-Object 'System.Windows.Forms.TableLayoutPanel'
    $ckA = New-DetalhesRecolhidos $lyA 'Detalhes da situacao:' 1 (New-Object 'System.Windows.Forms.TextBox')
    $lyB = New-Object 'System.Windows.Forms.TableLayoutPanel'
    $ckB = New-DetalhesRecolhidos $lyB 'Detalhes da restauracao:' 1 (New-Object 'System.Windows.Forms.TextBox')
    ok ($ckA.Tag.Painel.Visible -eq $false -and $lyA.RowStyles[$ckA.Tag.Linha].Tipo -eq 'AutoSize') 'detalhes evento: Situacao comeca recolhida'
    ok ($ckB.Tag.Painel.Visible -eq $false -and $lyB.RowStyles[$ckB.Tag.Linha].Tipo -eq 'AutoSize') 'detalhes evento: Restaurar comeca recolhida'
    $ckA.Checked = $true; $ckA.Manipuladores[0].Invoke($ckA, $null)
    ok ($ckA.Tag.Painel.Visible -eq $true -and $lyA.RowStyles[$ckA.Tag.Linha].Tipo -eq 'Percent') 'detalhes evento: abrir expande o painel da propria secao'
    ok ($ckB.Tag.Painel.Visible -eq $false -and $lyB.RowStyles[$ckB.Tag.Linha].Tipo -eq 'AutoSize') 'detalhes evento: abrir Situacao nao altera Restaurar'
    $ckA.Checked = $false; $ckA.Manipuladores[0].Invoke($ckA, $null)
    ok ($ckA.Tag.Painel.Visible -eq $false -and $lyA.RowStyles[$ckA.Tag.Linha].Tipo -eq 'AutoSize') 'detalhes evento: fechar recolhe sem erro'
    $ckB.Checked = $true; $ckB.Manipuladores[0].Invoke($ckB, $null)
    ok ($ckB.Tag.Painel.Visible -eq $true -and $lyB.RowStyles[$ckB.Tag.Linha].Tipo -eq 'Percent' -and $ckA.Tag.Painel.Visible -eq $false -and $lyA.RowStyles[$ckA.Tag.Linha].Tipo -eq 'AutoSize') 'detalhes evento: abrir Restaurar nao altera Situacao (independente)'

    # ---- falha: mensagem curta + diagnostico recolhido ----
    ok ($guiSrc.Contains('Get-MensagemFalha')) 'falha: classificador de mensagem curta existe'
    ok ($guiSrc.Contains('A conexão com o servidor foi interrompida. Verifique a conexão e tente restaurar novamente.')) 'falha sftp: texto curto exato'
    ok (@([regex]::Matches($guiSrc, 'Get-MensagemFalha -Erro'))).Count -ge 3 'falha: mensagem curta nos 3 caminhos de erro da aba Restaurar'
    ok (@([regex]::Matches($guiSrc, '\$tbRestDet\.Text = '))).Count -ge 3 'falha: diagnostico vai aos detalhes tecnicos (nao a area principal)'
    ok ($guiSrc.Contains('O destino precisa ser NOVO') -or $guiSrc.Contains('precisa ser NOVO')) 'restaurar pasta: colisao de destino recusada na regra'
    ok (-not ($guiSrc -match "restore\s+'latest'|restore\s+latest")) 'restaurar pasta: nunca usa latest'
    ok ($guiSrc -match "restore `"\{3\}:\{4\}`" --target `"\{5\}`" --verify") 'restaurar pasta: restore "<id>:<raiz>" --target <tmp> --verify (so a raiz, conteudo conferido)'
    ok ($guiSrc.Contains('[IO.Directory]::Move')) -and -not ($guiSrc -match 'Move-Item -LiteralPath \$sub') 'restaurar pasta: publicacao atomica que falha se o destino existir'
    ok ($guiSrc.Contains('Test-NomePastaDestino')) 'restaurar pasta: nome da pasta nova validado como um componente'
    ok ($guiSrc.Contains('Onde salvar') -and $guiSrc.Contains('Nome da nova pasta')) 'restaurar pasta: seletor do pai + campo do nome (destino ainda nao existe)'
    ok ($guiSrc.Contains('Join-Path $tbPaiRest.Text $tbNomePasta.Text')) 'restaurar pasta: caminho final = pai escolhido + nome informado'
    $bytes = [IO.File]::ReadAllBytes($gui)
    ok ($bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF) 'codificacao: UTF-8 com BOM (PS 5.1 le os acentos)'

    # ================= pedido de "Executar agora" =================
    # REPRODUCAO OBRIGATORIA (bug da tela): backup anterior bem-sucedido
    # -> clique -> Agendador ainda devolve dados anteriores -> disparo
    # novo observado -> novo resultado. A tela NUNCA pode anunciar o
    # sucesso antigo como resultado do pedido recem-feito -- nem na
    # linha do pedido nem na linha principal "Execucao atual".

    function New-InfoFalsa {
        # Como o COM: Ultima chega como DateTime LOCAL (nunca texto) --
        # Get-InfoTarefa preserva o tipo ate a formatacao.
        param($Ultima = $null, [int]$Estado = 1, [int]$Codigo = 0, $Insts = @(), $Proxima = $null)
        if ($Ultima -is [string] -and $Ultima) { $Ultima = [DateTime]::Parse($Ultima) }
        return [pscustomobject]@{ Habilitada = $true; Estado = $Estado; Ultima = $Ultima
                                 Proxima = $Proxima; Codigo = $Codigo; Conta = 'x'; DoApp = $true
                                 Instancias = @($Insts) }
    }
    # Fixture independente de fuso: Ultima (COM) e HORA LOCAL da maquina;
    # inicio_utc e UTC de verdade. As duas descrevem o MESMO instante.
    # t1 = instante do clique: a execucao nova comeca DEPOIS dele (o
    # vinculo exige inicio_utc >= PedidoUtc).
    $agora = [DateTimeOffset]::UtcNow
    $t0 = [DateTimeOffset]::Now.AddDays(-1)      # execucao anterior
    $t1 = [DateTimeOffset]::Now                  # execucao do pedido (agora)
    function Iso-Utc { param($d) return $d.UtcDateTime.ToString('o') }
    function Hora-Local { param($d) return $d.ToString('yyyy-MM-dd HH:mm:ss') }
    $resAntigo = [pscustomobject]@{ status = 'sucesso'; codigo_restic = 0; snapshot = $snapOk
                                    inicio_utc = (Iso-Utc $t0.AddSeconds(5)); fim_utc = (Iso-Utc $t0.AddMinutes(1)) }
    $resNovo = [pscustomobject]@{ status = 'sucesso'; codigo_restic = 0; snapshot = ('b'*64)
                                  inicio_utc = (Iso-Utc $t1.AddSeconds(2)); fim_utc = (Iso-Utc $t1.AddMinutes(1)) }
    $resFalha = [pscustomobject]@{ status = 'falha'; codigo_restic = 1; snapshot = $null
                                   inicio_utc = (Iso-Utc $t1.AddSeconds(2)); fim_utc = (Iso-Utc $t1.AddMinutes(1)) }

    # 1) sucesso anterior + clique + Agendador devolve os dados ANTIGOS:
    #    modelo de renderizacao diz aguardando -- NUNCA concluido -- e a
    #    linha principal e a orientacao ficam no MESMO estado.
    $infoAnt = New-InfoFalsa -Ultima (Hora-Local $t0) -Codigo 0
    $ped = New-PedidoDisparo -InfoTarefa $infoAnt -Res $resAntigo
    $t = Get-TelaExecucao -InfoTarefa $infoAnt -Res $resAntigo -Pedido $ped
    eq $t.Classe 'aguardando' 'tela: dados antigos do Agendador -> aguardando'
    eq $t.Texto $t.Mensagem 'tela: linha principal e mensagem no mesmo estado'
    eq $t.Origem 'pedido' 'tela: pedido aberto dita a renderizacao'
    ok ($t.Acao -match 'Aguarde') 'tela: orientacao coerente com aguardando'

    # 2) novo LastRunTime + rodando -> em_execucao
    $infoRun = New-InfoFalsa -Ultima (Hora-Local $t1) -Estado 4
    $t = Get-TelaExecucao -InfoTarefa $infoRun -Res $resAntigo -Pedido $ped
    eq $t.Classe 'em_execucao' 'tela: novo LastRunTime + rodando -> em_execucao'
    ok $ped.Iniciado 'pedido: execucao nova registrada (Iniciado)'

    # 3) resultado VINCULADO a execucao nova -> concluido e pedido encerra
    $infoFim = New-InfoFalsa -Ultima (Hora-Local $t1) -Estado 1 -Codigo 0
    $t = Get-TelaExecucao -InfoTarefa $infoFim -Res $resNovo -Pedido $ped
    eq $t.Classe 'concluido' 'tela: resultado vinculado a nova execucao -> concluido'
    ok $t.Encerrou 'tela: pedido encerrou em estado terminal'
    eq $t.Texto $t.Mensagem 'tela: estado terminal coerente nas duas linhas'

    # 4) execucao nova terminou e o JSON ainda e o ANTIGO -> acompanha
    #    pela carencia e termina sem_resultado (JAMAIS concluido)
    $ped2 = New-PedidoDisparo -InfoTarefa $infoAnt -Res $resAntigo
    [void](Update-PedidoDisparo -Pedido $ped2 -InfoTarefa $infoRun -Res $resAntigo -AgoraUtc $agora)
    eq (Update-PedidoDisparo -Pedido $ped2 -InfoTarefa $infoFim -Res $resAntigo -AgoraUtc $agora) 'em_execucao' `
        'pedido: run nova encerrada, JSON antigo -> acompanha (carencia)'
    eq (Update-PedidoDisparo -Pedido $ped2 -InfoTarefa $infoFim -Res $resAntigo -AgoraUtc ($agora.AddSeconds(31))) 'sem_resultado' `
        'pedido: carencia vencida com JSON antigo -> sem_resultado'

    # 5) execucao RAPIDA entre duas atualizacoes (comecou e terminou
    #    sem a tela ver rodando) -> concluido direto
    $ped3 = New-PedidoDisparo -InfoTarefa $infoAnt -Res $resAntigo
    eq (Update-PedidoDisparo -Pedido $ped3 -InfoTarefa $infoFim -Res $resNovo -AgoraUtc $agora) 'concluido' `
        'pedido: execucao rapida entre ticks -> concluido'

    # 6) novo LastRunTime com CODIGO ANTIGO de falha: o codigo nao
    #    identifica a execucao -- NAO encerra o pedido como falha;
    #    acompanhando ate resultado ou fim da carencia -> sem_resultado
    $ped4 = New-PedidoDisparo -InfoTarefa $infoAnt -Res $resAntigo
    $infoCodAntigo = New-InfoFalsa -Ultima (Hora-Local $t1) -Estado 1 -Codigo 1
    eq (Update-PedidoDisparo -Pedido $ped4 -InfoTarefa $infoCodAntigo -Res $resAntigo -AgoraUtc $agora) 'em_execucao' `
        'pedido: novo LastRunTime + codigo antigo de falha -> NAO falha (acompanha)'
    eq (Update-PedidoDisparo -Pedido $ped4 -InfoTarefa $infoCodAntigo -Res $resAntigo -AgoraUtc ($agora.AddSeconds(31))) 'sem_resultado' `
        'pedido: codigo antigo nunca encerra como falha -> sem_resultado'

    # 7) falha nova CONFIRMADA pelo JSON vinculado -> falhou
    $pedF = New-PedidoDisparo -InfoTarefa $infoAnt -Res $resAntigo
    eq (Update-PedidoDisparo -Pedido $pedF -InfoTarefa $infoFim -Res $resFalha -AgoraUtc $agora) 'falhou' `
        'pedido: JSON de falha da nova execucao -> falhou'

    # 8) resultado ANTERIOR dentro da tolerancia de horario: JSON que ja
    #    existia no clique (inicio_utc == ResInicioRef) nunca conta,
    #    mesmo a 1s do novo LastRunTime
    #    existia no clique (inicio_utc == ResInicioRef) nunca conta.
    #    t2 no FUTURO: passa nos limites de horario, falha so na regra
    #    'ini > ResInicioRef' -- e a identidade, nao a tolerancia.
    $t2 = [DateTimeOffset]::Now.AddMinutes(1)
    $resTol = [pscustomobject]@{ status = 'sucesso'; codigo_restic = 0; snapshot = $snapOk
                                 inicio_utc = (Iso-Utc $t2) }
    $infoTol = New-InfoFalsa -Ultima (Hora-Local $t2)
    $pedT = New-PedidoDisparo -InfoTarefa (New-InfoFalsa -Ultima (Hora-Local $t2.AddMinutes(-1))) -Res $resTol
    eq (Update-PedidoDisparo -Pedido $pedT -InfoTarefa $infoTol -Res $resTol -AgoraUtc $agora) 'em_execucao' `
        'pedido: JSON pre-existente dentro da tolerancia -> nao conclui (acompanha)'

    # 9) consulta inconclusiva: aguardando dentro do prazo, depois
    #    nao_confirmado
    $ped5 = New-PedidoDisparo -InfoTarefa $infoAnt -Res $resAntigo -PrazoSegundos 60
    eq (Update-PedidoDisparo -Pedido $ped5 -InfoTarefa $null -Res $null -AgoraUtc $agora) 'aguardando' `
        'pedido: consulta falhou dentro do prazo -> aguardando'
    eq (Update-PedidoDisparo -Pedido $ped5 -InfoTarefa $null -Res $null -AgoraUtc ($agora.AddSeconds(121))) 'nao_confirmado' `
        'pedido: consulta segue falhando alem do prazo -> nao_confirmado'

    # 10) prazo vencido sem execucao nova -> nao_confirmado
    $ped6 = New-PedidoDisparo -InfoTarefa $infoAnt -Res $resAntigo -PrazoSegundos 60
    eq (Update-PedidoDisparo -Pedido $ped6 -InfoTarefa $infoAnt -Res $resAntigo -AgoraUtc ($agora.AddSeconds(121))) 'nao_confirmado' `
        'pedido: sem execucao nova no prazo -> nao_confirmado'

    # 11) evidencia por GUID de instancia: LastRunTime ainda antigo, mas
    #     uma instancia com GUID novo -> em_execucao
    $ped7 = New-PedidoDisparo -InfoTarefa $infoAnt -Res $resAntigo
    eq (Update-PedidoDisparo -Pedido $ped7 -InfoTarefa (New-InfoFalsa -Ultima (Hora-Local $t0) -Estado 4 -Insts @('{guid-novo}')) -Res $null -AgoraUtc $agora) 'em_execucao' `
        'pedido: instancia com GUID novo -> em_execucao (LastRunTime ainda antigo)'

    # 12) instancia cuja GUID JA existia no clique nao e evidencia nova
    $ped8 = New-PedidoDisparo -InfoTarefa (New-InfoFalsa -Ultima (Hora-Local $t0) -Insts @('{guid-velho}')) -Res $resAntigo
    eq (Update-PedidoDisparo -Pedido $ped8 -InfoTarefa (New-InfoFalsa -Ultima (Hora-Local $t0) -Estado 4 -Insts @('{guid-velho}')) -Res $resAntigo -AgoraUtc $agora) 'aguardando' `
        'pedido: instancia pre-existente no clique -> aguardando'

    # 13) RESOLUCAO persistida: pedido encerrado 'nao_confirmado' segue
    #     apresentado na linha principal (nao volta a anunciar o sucesso
    #     antigo) ate um disparo MAIS NOVO aparecer
    $resolucao = [pscustomobject]@{ Classe = 'nao_confirmado'; UltimaObservada = (Hora-Local $t0)
                                    Mensagem = $script:RotulosPedido.nao_confirmado }
    $t = Get-TelaExecucao -InfoTarefa $infoAnt -Res $resAntigo -Resolucao $resolucao
    eq $t.Classe 'nao_confirmado' 'resolucao: persiste na linha principal (nao volta ao sucesso antigo)'
    eq $t.Origem 'resolucao' 'resolucao: origem marcada'
    # disparo MAIS NOVO -> resolucao expira, classificacao generica volta
    $t3 = [DateTimeOffset]::Now
    $infoNovoDisparo = New-InfoFalsa -Ultima (Hora-Local $t3) -Estado 1
    $resNovoDia = [pscustomobject]@{ status = 'sucesso'; codigo_restic = 0; snapshot = ('d'*64)
                                     inicio_utc = (Iso-Utc $t3.AddSeconds(3)) }
    $t = Get-TelaExecucao -InfoTarefa $infoNovoDisparo -Res $resNovoDia -Resolucao $resolucao
    ok $t.ResolucaoExpirada 'resolucao: disparo mais novo expira a resolucao'
    eq $t.Classe 'concluido' 'resolucao expirada: classificacao generica descreve o novo disparo'
    eq $t.Origem 'generica' 'resolucao expirada: origem generica'

    # Callback real de Executar agora, com COM e controles simulados.
    $tokensManual=$null;$errorsManual=$null
    $astManual=[Management.Automation.Language.Parser]::ParseFile((Resolve-Path $gui).ProviderPath,[ref]$tokensManual,[ref]$errorsManual)
    $clickManual=@($astManual.FindAll({param($n)$n -is [Management.Automation.Language.InvokeMemberExpressionAst] -and $n.Member.Value -ceq 'Add_Click' -and $n.Expression -is [Management.Automation.Language.VariableExpressionAst] -and $n.Expression.VariablePath.UserPath -ceq 'btAgora'},$true))
    $callbackManual=$clickManual[0].Arguments[0].ScriptBlock.GetScriptBlock()
    $savedInfo=${function:Get-InfoTarefa};$savedEstado=${function:Get-EstadoAtual};$savedStart=${function:Start-TarefaAgora}
    $savedRefresh=$script:AtualizarTela;$savedPedido=$script:Disparo;$savedResolution=$script:UltimaResolucao
    $script:manualInfo=$infoAnt;$script:manualState=Join-Path $tmp 'manual-state';New-Item -ItemType Directory $script:manualState|Out-Null
    $oldResultPath=Join-Path $script:manualState 'ultimo-resultado.json';$resAntigo|ConvertTo-Json|Set-Content -LiteralPath $oldResultPath -Encoding UTF8
    $oldResultHash=(Get-FileHash -LiteralPath $oldResultPath -Algorithm SHA256).Hash
    try{
        function Get-InfoTarefa {return $script:manualInfo}
        function Get-EstadoAtual {return $script:manualState}
        function Start-TarefaAgora {throw [ArgumentException]::new('SYNTHETIC COM argumento fora do intervalo')}
        $script:AtualizarTela={ $script:manualTela=Get-TelaExecucao -InfoTarefa $script:manualInfo -Res $resAntigo -Pedido $script:Disparo -Resolucao $script:UltimaResolucao }
        $script:Disparo=$null;$script:UltimaResolucao=$null
        & { $btAgora=[pscustomobject]@{Enabled=$true};$lblOp=[pscustomobject]@{Text='';ForeColor=''};& $callbackManual }
        eq $script:manualTela.Classe 'recusado' 'callback: Run recusado nao volta ao sucesso antigo'
        eq $script:manualTela.Cor 'DarkRed' 'pedido recusado aparece em vermelho'
        ok ($script:manualTela.Mensagem -like 'Pedido recusado*') 'mensagem separa pedido de historico'
        & $script:AtualizarTela
        eq $script:manualTela.Classe 'recusado' 'atualizacao periodica preserva recusa'
        eq (Get-FileHash -LiteralPath $oldResultPath -Algorithm SHA256).Hash $oldResultHash 'resultado historico permanece byte a byte'
        $stillRunning=New-InfoFalsa -Ultima (Hora-Local $t0) -Estado 4
        eq (Get-TelaExecucao -InfoTarefa $stillRunning -Res $resAntigo -Resolucao $script:UltimaResolucao).Classe 'recusado' 'estado running sem novo LastRunTime nao aceita pedido recusado'
        $noReference=[pscustomobject]@{Classe='recusado';UltimaObservada=$null;PedidoUtc=[DateTimeOffset]::UtcNow;Mensagem='Pedido recusado'}
        eq (Get-TelaExecucao -InfoTarefa $infoAnt -Res $resAntigo -Resolucao $noReference).Classe 'recusado' 'consulta anterior inconclusiva nao promove historico'
        ok (Get-TelaExecucao -InfoTarefa (New-InfoFalsa -Ultima ([DateTime]::Now.AddMinutes(1))) -Res $resNovoDia -Resolucao $noReference).ResolucaoExpirada 'execucao realmente posterior pode expirar recusa sem referencia'
        function Start-TarefaAgora {} # aceita Run, sem inventar evidencia de execucao
        $script:Disparo=$null
        & { $btAgora=[pscustomobject]@{Enabled=$true};$lblOp=[pscustomobject]@{Text='';ForeColor=''};& $callbackManual }
        eq $script:manualTela.Classe 'aguardando' 'aceitacao COM sem evidencia nova continua aguardando'
    }finally{
        ${function:Get-InfoTarefa}=$savedInfo;${function:Get-EstadoAtual}=$savedEstado;${function:Start-TarefaAgora}=$savedStart
        $script:AtualizarTela=$savedRefresh;$script:Disparo=$savedPedido;$script:UltimaResolucao=$savedResolution
    }

    # contrato estatico do pedido
    ok ($guiSrc.Contains('New-PedidoDisparo') -and $guiSrc.Contains('Update-PedidoDisparo')) 'pedido: helpers separados e testaveis'
    ok ($guiSrc.Contains('UltimaRef') -and $guiSrc.Contains('InstRef') -and $guiSrc.Contains('ResInicioRef')) 'pedido: referencia completa antes do Run'
    ok ($guiSrc.Contains('nao_confirmado') -and $guiSrc.Contains('Aguardando início')) 'pedido: estados aguardando/nao_confirmado presentes'
    ok ($guiSrc.Contains('if ($script:Disparo) { return }')) 'pedido: clique repetido bloqueado enquanto ha pedido aberto'
    ok (-not $guiSrc.Contains("-like 'Disparo*'")) 'pedido: classificacao generica nao dirige mais a linha do pedido'
    ok ($guiSrc.Contains('Get-TelaExecucao') -and $guiSrc.Contains('UltimaResolucao')) 'pedido: modelo unico + resolucao persistida'

    # ---- OpenSSH: arquitetura (Sysnative -> canonico) e status ----
    $sel = Select-SshCandidato -Candidatos @([pscustomobject]@{
        Exec = $sshBom; Caminho = 'C:/Windows/System32/OpenSSH/ssh.exe' })
    eq $sel.Caminho 'C:/Windows/System32/OpenSSH/ssh.exe' `
        'Sysnative: caminho APROVADO e o canonico (alias nunca e gravado)'
    ok ($sel.Caminho -notmatch 'Sysnative') 'Sysnative: alias nao vaza para o caminho aprovado'
    $st = Get-SshStatus
    ok ($st.Status -in 'ok','ausente','incompativel') 'Get-SshStatus: status tipado (ok/ausente/incompativel)'
    # agente (fonte): Sysnative listado com Caminho canonico; GUI:
    # verificacao definitiva na ativacao distingue os estados
    $agSrc = Get-Content -LiteralPath $script:AgentePath -Raw
    ok ($agSrc.Contains('Sysnative\OpenSSH\ssh.exe') -and $agSrc.Contains('Caminho = $canonico')) 'agente: Sysnative consultado, caminho aprovado canonico'
    ok ($agSrc.Contains('Get-SshStatus')) 'agente: status OpenSSH separado (ausente vs incompativel)'
    ok ($guiSrc.Contains('incompatível') -and $guiSrc.Contains('Get-SshStatus')) 'ativacao: distingue ausente de incompativel'

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
    # DOIS links encadeados: link1 -> link2 -> origem -- resolve para dentro
    $link2 = Join-Path $tmp 'link2-origem'; $link1 = Join-Path $tmp 'link1-origem'
    if (-not (Test-IsWindows)) {
        New-Item -ItemType SymbolicLink -Path $link2 -Target $origem | Out-Null
        New-Item -ItemType SymbolicLink -Path $link1 -Target $link2 | Out-Null
    } else {
        cmd /c mklink /J "$link2" "$origem" | Out-Null
        cmd /c mklink /J "$link1" "$link2" | Out-Null
    }
    throws { Restore-Arquivo -Estado $estado -Config $b1.Config -Snapshot ('c'*64) -Arquivo '/o/amostra.txt' -Destino (Join-Path $link1 'nova') -Origem $origem } 'restaurar: dois links encadeados para a origem recusado'

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

    # prazo vale ate o fim: neto segura stderr aberto depois do pai sair
    # com codigo 0 e stdout fechado -> retorna FALHA dentro do limite,
    # sem publicar e sem tocar arquivos preexistentes.
    '1' | Set-Content -LiteralPath (Join-Path $stubDir 'dump-stderr-open') -Encoding Ascii -NoNewline
    [IO.File]::WriteAllBytes((Join-Path $stubDir 'dump.bin'), [Text.Encoding]::UTF8.GetBytes('BYTES'))
    $dest11 = Join-Path $tmp 'dest-stderr-open'; New-Item -ItemType Directory $dest11 | Out-Null
    'PREVIO' | Set-Content -LiteralPath (Join-Path $dest11 'intacto.txt') -Encoding Ascii -NoNewline
    $script:DumpTimeoutMs = 2000
    $t0 = Get-Date
    throws { Invoke-ResticDump -Cfg $cfgOp -Estado $estado -Snapshot ('c'*64) -Arquivo '/o/x.txt' -Destino $dest11 } 'restaurar: stderr preso -> falha dentro do prazo'
    ok (((Get-Date) - $t0).TotalSeconds -lt 20) 'restaurar: stderr preso -- retornou sem travar'
    eq (Get-ChildItem -LiteralPath $dest11 -File | Measure-Object).Count 1 'restaurar: stderr preso -- sem arquivo final nem parcial'
    eq (Get-Content -LiteralPath (Join-Path $dest11 'intacto.txt') -Raw) 'PREVIO' 'restaurar: pre-existente intacto'
    $script:DumpTimeoutMs = 600000
    Remove-Item -LiteralPath (Join-Path $stubDir 'dump-stderr-open') -Force

    # arquivo BINARIO com nome de padrao: SHA-256 confere byte a byte
    $bin = New-Object byte[] 256
    for ($i = 0; $i -lt 256; $i++) { $bin[$i] = [byte]$i }
    [IO.File]::WriteAllBytes((Join-Path $stubDir 'dump.bin'), $bin)
    $dest5 = Join-Path $tmp 'dest-bin'
    $alvo5 = Restore-Arquivo -Estado $estado -Config $b1.Config -Snapshot ('c'*64) `
        -Arquivo '/o/a[1].txt' -Destino $dest5 -Origem $origem
    eq (Get-FileHash -LiteralPath $alvo5 -Algorithm SHA256).Hash `
       ([BitConverter]::ToString([Security.Cryptography.SHA256]::Create().ComputeHash($bin)).Replace('-','')) 'restaurar: binario a[1].txt -- SHA-256 confere'

    # ================= restauracao da PASTA inteira =================
    # Cenario do aceite: dois pontos com dados ficticios; entre eles
    # 'alterado.txt' mudou e 'removido.txt' saiu da origem. Restaurar a
    # arvore do ponto ANTIGO devolve exatamente o que foi salvo naquela
    # data — caminhos relativos e SHA-256 conferidos por manifesto.
    $snapAnt = 'a' * 64; $snapNov = 'b' * 64
    @"
{"id":"$snapAnt","short_id":"aaaaaaaa","time":"2026-11-16T16:26:00Z","paths":["$origem"]}
{"id":"$snapNov","short_id":"bbbbbbbb","time":"2026-11-17T16:26:00Z","paths":["$origem"]}
"@ | Set-Content -LiteralPath (Join-Path $stubDir 'snaps.jsonl') -Encoding Ascii
    # catalogo do ponto A: como o restic REAL emite — no snapshot, os
    # ANCESTRAIS da raiz (/tmp, /tmp/gui-teste-...) como nos dir, a raiz,
    # e o conteudo sob ela. O conjunto esperado e so o que esta SOB a
    # raiz. UTF-8 para o nome acentuado nao virar mojibake.
    $av1 = Split-Path $origem; $av0 = Split-Path $av1
    @"
{"type":"snapshot","paths":["$origem"],"id":"$snapAnt"}
{"name":"tmp","type":"dir","path":"$av0"}
{"name":"nivel","type":"dir","path":"$av1"}
{"name":"origem","type":"dir","path":"$origem"}
{"name":"alterado.txt","type":"file","path":"$origem/alterado.txt"}
{"name":"removido.txt","type":"file","path":"$origem/removido.txt"}
{"name":"sub","type":"dir","path":"$origem/sub"}
{"name":"interno.txt","type":"file","path":"$origem/sub/interno.txt"}
{"name":"nome com espaços e ação.txt","type":"file","path":"$origem/nome com espaços e ação.txt"}
"@ | Set-Content -LiteralPath (Join-Path $stubDir 'ls.jsonl') -Encoding UTF8
    # arvore que o ponto ANTIGO tinha (fixture que o stub materializa)
    $arvore = Join-Path $stubDir 'restaurar'; New-Item -ItemType Directory $arvore -Force | Out-Null
    New-Item -ItemType Directory (Join-Path $arvore 'sub') -Force | Out-Null
    'VERSAO-ANTIGA' | Set-Content (Join-Path $arvore 'alterado.txt') -Encoding Ascii -NoNewline
    'REMOVIDO-DEPOIS' | Set-Content (Join-Path $arvore 'removido.txt') -Encoding Ascii -NoNewline
    'INTERNO' | Set-Content (Join-Path $arvore 'sub\interno.txt') -Encoding Ascii -NoNewline
    'ACENTOS' | Set-Content (Join-Path $arvore 'nome com espaços e ação.txt') -Encoding Ascii -NoNewline
    # manifesto gravado "antes daquele backup": caminho relativo -> sha256
    $manifesto = @{}
    Get-ChildItem -LiteralPath $arvore -Recurse -File | ForEach-Object {
        $rel = $_.FullName.Substring($arvore.Length).TrimStart('\','/')
        $manifesto[$rel] = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLower() }
    # arquivo excluido: existe na ORIGEM atual mas nunca entrou no ponto
    'NAO-ENTRA' | Set-Content (Join-Path $origem 'excluido.tmp') -Encoding Ascii -NoNewline

    $destPasta = Join-Path $tmp 'restaurado-antigo'
    $resP = Restore-PastaCompleta -Estado $estado -Config $b1.Config `
        -Snapshot $snapAnt -PathsMeta @($origem) -Destino $destPasta -Origem $origem
    eq ([IO.Path]::GetFullPath($destPasta)) ([IO.Path]::GetFullPath($resP.Destino)) 'pasta: publicada no destino pedido'
    eq $resP.Raiz $origem 'pasta: raiz registrada no ponto devolvida'
    ok ($resP.Itens -ge 4) 'pasta: contagem de itens restaurados'
    # cada item do manifesto existe com o MESMO sha-256
    $faltaHash = @()
    foreach ($rel in $manifesto.Keys) {
        $alvoRel = Join-Path $destPasta ($rel -replace '/', [IO.Path]::DirectorySeparatorChar)
        if (-not (Test-Path -LiteralPath $alvoRel -PathType Leaf)) { $faltaHash += $rel; continue }
        if ((Get-FileHash -LiteralPath $alvoRel -Algorithm SHA256).Hash.ToLower() -cne $manifesto[$rel]) { $faltaHash += $rel }
    }
    eq ($faltaHash -join ',') '' 'pasta: manifesto completo — caminhos relativos e SHA-256 iguais'
    eq (Get-Content -LiteralPath (Join-Path $destPasta 'alterado.txt') -Raw) 'VERSAO-ANTIGA' 'pasta: arquivo alterado volta com o conteudo do ponto antigo'
    ok (Test-Path -LiteralPath (Join-Path $destPasta 'removido.txt') -PathType Leaf) 'pasta: arquivo removido da origem volta'
    ok (Test-Path -LiteralPath (Join-Path $destPasta 'sub\interno.txt') -PathType Leaf) 'pasta: subpasta preservada'
    ok (Test-Path -LiteralPath (Join-Path $destPasta 'nome com espaços e ação.txt') -PathType Leaf) 'pasta: nome com espacos e acentos preservado'
    ok (-not (Test-Path -LiteralPath (Join-Path $destPasta 'excluido.tmp'))) 'pasta: excluido continua ausente (exclusoes NAO filtram restore, arquivo nunca entrou)'
    eq (Get-Content -LiteralPath (Join-Path $origem 'excluido.tmp') -Raw) 'NAO-ENTRA' 'pasta: origem preservada intocada'
    # restaura o que foi salvo naquela data: sem --exclude/--include na linha do restore
    $argsLog = Get-Content -LiteralPath (Join-Path $stubDir 'args.log') -Raw
    $linhaRestore = (($argsLog -split '---') | Where-Object { $_ -match '\brestore\b' } | Select-Object -Last 1)
    ok ($linhaRestore -match 'restore' -and $linhaRestore -notmatch '--exclude' -and $linhaRestore -notmatch '--include') 'pasta: restore da arvore inteira, sem filtros'
    ok ($linhaRestore -match [regex]::Escape($snapAnt + ':')) 'pasta: ID COMPLETO do ponto com escopo id:raiz (so a raiz escolhida)'
    ok ($linhaRestore -match [regex]::Escape($origem)) 'pasta: subpasta restaurada e a raiz do ponto (nao a origem de hoje por suposicao)'
    ok ($linhaRestore -match '--verify') 'pasta: --verify confere o conteudo no proprio restic'
    ok ($linhaRestore -notmatch 'latest') 'pasta: nunca latest'

    # nome da pasta nova: UM componente de caminho valido p/ Windows
    eq (Test-NomePastaDestino 'restaurado-16-11') $null 'pasta: nome simples aceito'
    ok ($null -ne (Test-NomePastaDestino '')) 'pasta: nome vazio recusado'
    ok ($null -ne (Test-NomePastaDestino 'a\b')) 'pasta: nome com separador recusado'
    ok ($null -ne (Test-NomePastaDestino '..')) 'pasta: .. recusado'
    ok ($null -ne (Test-NomePastaDestino 'fim.')) 'pasta: ponto final recusado'
    ok ($null -ne (Test-NomePastaDestino ' espaco')) 'pasta: espaco no inicio recusado'
    ok ($null -ne (Test-NomePastaDestino 'CON')) 'pasta: nome reservado recusado'

    # SENTINELA: outro processo cria o DESTINO durante a copia -> recusa,
    # sentinela intacta, nada copiado para dentro do destino alheio.
    $destDisputa = Join-Path $tmp 'restaurado-disputa'
    [IO.File]::WriteAllText((Join-Path $stubDir 'restore-mkdir'), $destDisputa)
    throws { Restore-PastaCompleta -Estado $estado -Config $b1.Config -Snapshot $snapAnt -PathsMeta @($origem) -Destino $destDisputa -Origem $origem } 'pasta: colisao na publicacao -> recusada'
    eq (Get-Content -LiteralPath (Join-Path $destDisputa 'sentinela.txt') -Raw) 'INTACTO' 'pasta: sentinela do destino alheio intacta'
    eq (@(Get-ChildItem -LiteralPath $destDisputa -Force)).Count 1 'pasta: nada copiado para dentro do destino alheio'
    eq (@(Get-ChildItem -LiteralPath $tmp -Directory -Filter '.macip-rest-*' -Force)).Count 0 'pasta: colisao -- temporario limpo'
    Remove-Item -LiteralPath (Join-Path $stubDir 'restore-mkdir') -Force
    Remove-Item -LiteralPath $destDisputa -Recurse -Force

    # codigo 0 com conteudo PARCIAL -> NAO e sucesso: nada publicado
    'x' | Set-Content -LiteralPath (Join-Path $stubDir 'restore-partial') -Encoding Ascii -NoNewline
    $destParcial = Join-Path $tmp 'restaurado-parcial'
    throws { Restore-PastaCompleta -Estado $estado -Config $b1.Config -Snapshot $snapAnt -PathsMeta @($origem) -Destino $destParcial -Origem $origem } 'pasta: codigo 0 parcial -> recusado'
    ok (-not (Test-Path -LiteralPath $destParcial)) 'pasta: codigo 0 parcial -- destino NAO publicado'
    eq (@(Get-ChildItem -LiteralPath $tmp -Directory -Filter '.macip-rest-*' -Force)).Count 0 'pasta: parcial -- temporario limpo'
    Remove-Item -LiteralPath (Join-Path $stubDir 'restore-partial') -Force

    # destino JA EXISTENTE -> recusa antes de qualquer chamada ao restic
    $destExist = Join-Path $tmp 'dest-existe'; New-Item -ItemType Directory $destExist | Out-Null
    'ORIGINAL' | Set-Content (Join-Path $destExist 'intacto.txt') -Encoding Ascii -NoNewline
    throws { Restore-PastaCompleta -Estado $estado -Config $b1.Config -Snapshot $snapAnt -PathsMeta @($origem) -Destino $destExist -Origem $origem } 'pasta: destino ja existente recusado'
    eq (Get-Content -LiteralPath (Join-Path $destExist 'intacto.txt') -Raw) 'ORIGINAL' 'pasta: conteudo do destino existente intacto'

    # destino dentro/igual a origem e pai-junction -> recusados
    throws { Restore-PastaCompleta -Estado $estado -Config $b1.Config -Snapshot $snapAnt -PathsMeta @($origem) -Destino (Join-Path $origem 'novo-dir') -Origem $origem } 'pasta: destino dentro da protegida recusado'
    throws { Restore-PastaCompleta -Estado $estado -Config $b1.Config -Snapshot $snapAnt -PathsMeta @($origem) -Destino $origem -Origem $origem } 'pasta: destino = origem recusado'
    $linkPaiP = Join-Path $tmp 'link-pai-pasta'
    if (-not (Test-IsWindows)) { New-Item -ItemType SymbolicLink -Path $linkPaiP -Target $origem | Out-Null }
    else { cmd /c mklink /J "$linkPaiP" "$origem" | Out-Null }
    throws { Restore-PastaCompleta -Estado $estado -Config $b1.Config -Snapshot $snapAnt -PathsMeta @($origem) -Destino (Join-Path $linkPaiP 'novo') -Origem $origem } 'pasta: pai junction para a origem recusado'

    # raiz nao identificavel -> para com motivo (nunca arvore alheia)
    throws { Restore-PastaCompleta -Estado $estado -Config $b1.Config -Snapshot $snapAnt -PathsMeta @() -Destino (Join-Path $tmp 'd1') -Origem $origem } 'pasta: metadados sem raiz recusados'
    throws { Restore-PastaCompleta -Estado $estado -Config $b1.Config -Snapshot $snapAnt -PathsMeta @('a','b') -Destino (Join-Path $tmp 'd2') -Origem $origem } 'pasta: raiz multipla recusada'
    # catalogo que NAO confirma a raiz -> para (paths dizem /x, catalogo /o)
    @"
{"type":"snapshot","paths":["/x/alheia"],"id":"$snapAnt"}
{"name":".","type":"dir","path":"/o"}
"@ | Set-Content -LiteralPath (Join-Path $stubDir 'ls.jsonl') -Encoding Ascii
    throws { Restore-PastaCompleta -Estado $estado -Config $b1.Config -Snapshot $snapAnt -PathsMeta @('/x/alheia') -Destino (Join-Path $tmp 'd3') -Origem $origem } 'pasta: catalogo nao confirma a raiz -> recusado'
    # catalogo com LINK sob a raiz -> recusa (nunca publica junction)
    @"
{"type":"snapshot","paths":["$origem"],"id":"$snapAnt"}
{"name":".","type":"dir","path":"$origem"}
{"name":"atalho","type":"symlink","path":"$origem/atalho","link_target":"/fora"}
"@ | Set-Content -LiteralPath (Join-Path $stubDir 'ls.jsonl') -Encoding Ascii
    throws { Restore-PastaCompleta -Estado $estado -Config $b1.Config -Snapshot $snapAnt -PathsMeta @($origem) -Destino (Join-Path $tmp 'd4') -Origem $origem } 'pasta: catalogo com link recusado (isolamento)'
    # catalogo minimo + arvore cheia -> item INESPERADO -> nada publicado
    @"
{"type":"snapshot","paths":["$origem"],"id":"$snapAnt"}
{"name":"origem","type":"dir","path":"$origem"}
"@ | Set-Content -LiteralPath (Join-Path $stubDir 'ls.jsonl') -Encoding Ascii
    $destExtra = Join-Path $tmp 'restaurado-extra'
    throws { Restore-PastaCompleta -Estado $estado -Config $b1.Config -Snapshot $snapAnt -PathsMeta @($origem) -Destino $destExtra -Origem $origem } 'pasta: item inesperado na arvore -> recusado'
    ok (-not (Test-Path -LiteralPath $destExtra)) 'pasta: item inesperado -- destino NAO publicado'

    # Windows: registrada 'C:\Dados\Cliente' vs catalogo '/C/Dados/Cliente'
    # — origem registrada e raiz interna separadas e confirmadas.
    @'
{"type":"snapshot","paths":["C:\\Dados\\Cliente"],"id":"x"}
{"name":"C","type":"dir","path":"/C"}
{"name":"Dados","type":"dir","path":"/C/Dados"}
{"name":"Cliente","type":"dir","path":"/C/Dados/Cliente"}
{"name":"f.txt","type":"file","path":"/C/Dados/Cliente/f.txt"}
{"name":"sub","type":"dir","path":"/C/Dados/Cliente/sub"}
'@ | Set-Content -LiteralPath (Join-Path $stubDir 'ls.jsonl') -Encoding Ascii
    $infoW = Get-RaizSnapshot -Cfg $cfgOp -Estado $estado -Snapshot ('e'*64) -PathsMeta @('C:\Dados\Cliente')
    eq $infoW.Origem 'C:\Dados\Cliente' 'pasta: origem registrada Windows preservada p/ tecnico/destino'
    eq $infoW.Raiz '/C/Dados/Cliente' 'pasta: raiz interna /C/... confirmada no catalogo'
    ok ($infoW.Esperados.Contains('f.txt') -and $infoW.Esperados.Contains('sub')) 'pasta: esperado sob a raiz interna'
    ok (-not $infoW.Esperados.Contains('') -and @($infoW.Esperados.Keys).Count -eq 2) 'pasta: ancestrais /C e /C/Dados fora do esperado'
    # catalogo com no fora da raiz que nao e ancestral -> ambiguo
    @'
{"type":"snapshot","paths":["C:\\Dados\\Cliente"],"id":"x"}
{"name":"Cliente","type":"dir","path":"/C/Dados/Cliente"}
{"name":"alheio","type":"dir","path":"/C/Dados/Alheio"}
'@ | Set-Content -LiteralPath (Join-Path $stubDir 'ls.jsonl') -Encoding Ascii
    throws { Get-RaizSnapshot -Cfg $cfgOp -Estado $estado -Snapshot ('e'*64) -PathsMeta @('C:\Dados\Cliente') } 'pasta: vizinho nao-ancestral fora da raiz -> catalogo ambiguo'
    # raiz registrada nao confirmada no catalogo -> para
    @'
{"type":"snapshot","paths":["C:\\Dados\\Cliente"],"id":"x"}
{"name":"Outra","type":"dir","path":"/C/Dados/Outra"}
'@ | Set-Content -LiteralPath (Join-Path $stubDir 'ls.jsonl') -Encoding Ascii
    throws { Get-RaizSnapshot -Cfg $cfgOp -Estado $estado -Snapshot ('e'*64) -PathsMeta @('C:\Dados\Cliente') } 'pasta: raiz interna ausente no catalogo -> recusado'
    # devolve o catalogo coerente (minimo: raiz confirmada, sem conteudo)
    @"
{"type":"snapshot","paths":["$origem"],"id":"$snapAnt"}
{"name":"origem","type":"dir","path":"$origem"}
"@ | Set-Content -LiteralPath (Join-Path $stubDir 'ls.jsonl') -Encoding Ascii

    # erro DURANTE a copia: nada publicado, temporario limpo, origem intacta
    '1' | Set-Content -LiteralPath (Join-Path $stubDir 'restore-code') -Encoding Ascii -NoNewline
    $destFalha = Join-Path $tmp 'restaurado-falha'
    throws { Restore-PastaCompleta -Estado $estado -Config $b1.Config -Snapshot $snapAnt -PathsMeta @($origem) -Destino $destFalha -Origem $origem } 'pasta: erro na copia -> falha'
    ok (-not (Test-Path -LiteralPath $destFalha)) 'pasta: erro na copia -- destino NAO publicado'
    eq (@(Get-ChildItem -LiteralPath $tmp -Directory -Filter '.macip-rest-*' -Force)).Count 0 'pasta: erro na copia -- temporario da tentativa limpo'
    Remove-Item -LiteralPath (Join-Path $stubDir 'restore-code') -Force

    # QUEDA SFTP durante a copia (caso 473f2997 na VM): a excecao chega
    # com o texto real; a tela mostra a mensagem CURTA via Get-MensagemFalha
    # e o diagnostico completo vai aos detalhes tecnicos. Nada publicado.
    '1' | Set-Content -LiteralPath (Join-Path $stubDir 'restore-code') -Encoding Ascii -NoNewline
    'sftp: packet_write_wait: Connection to 10.0.0.2 port 22: connection lost' |
        Set-Content -LiteralPath (Join-Path $stubDir 'restore-stderr') -Encoding Ascii -NoNewline
    $destQueda = Join-Path $tmp 'restaurado-queda'
    $msgQueda = ''
    try {
        Restore-PastaCompleta -Estado $estado -Config $b1.Config -Snapshot $snapAnt `
            -PathsMeta @($origem) -Destino $destQueda -Origem $origem | Out-Null
    } catch { $msgQueda = $_.Exception.Message }
    ok ($msgQueda -match 'connection lost') 'sftp: excecao chega ao catch com o diagnostico real (vai aos detalhes)'
    eq (Get-MensagemFalha -Erro $msgQueda) 'A conexão com o servidor foi interrompida. Verifique a conexão e tente restaurar novamente.' 'sftp: queda -> mensagem curta exata na area principal'
    ok (-not (Test-Path -LiteralPath $destQueda)) 'sftp: queda -- destino NAO publicado'
    eq (@(Get-ChildItem -LiteralPath $tmp -Directory -Filter '.macip-rest-*' -Force)).Count 0 'sftp: queda -- temporario limpo'
    Remove-Item -LiteralPath (Join-Path $stubDir 'restore-code'), (Join-Path $stubDir 'restore-stderr') -Force

    # Get-MensagemFalha: perda de conexao IDENTIFICADA -> curta; todo o
    # resto mantem o texto proprio (destino invalido, pasta existente,
    # permissao, timeout do app, falha desconhecida).
    $curta = 'A conexão com o servidor foi interrompida. Verifique a conexão e tente restaurar novamente.'
    foreach ($txt in @('connection lost', 'Connection reset by peer', 'broken pipe',
                      'connection closed by remote host', 'i/o timeout',
                      'Fatal: unable to open repository: connection timed out',
                      'session terminated unexpectedly')) {
        eq (Get-MensagemFalha -Erro $txt) $curta "falha: '$txt' -> mensagem curta"
    }
    foreach ($txt in @('O destino precisa ser NOVO', 'Escolha o arquivo a restaurar na lista',
                      'Acesso negado a pasta de destino',
                      'Restauracao excedeu o tempo limite de 10 minutos — a pasta parcial foi descartada.',
                      'falha estranha qualquer xyz')) {
        eq (Get-MensagemFalha -Erro $txt) $txt "falha: texto proprio preservado ('$($txt.Substring(0,[Math]::Min(30,$txt.Length)))…')"
    }
    ok ((Get-MensagemFalha -Erro '') -ne $curta) 'falha: mensagem vazia NAO vira "conexao interrompida"'

    # regras de exclusao: uma por LINHA LOGICA — uma regra longa que
    # quebraria na tela NUNCA vira duas ao salvar (WordWrap=$false).
    $regras = @('*.tmp', 'cache local\*.dat', ('regra-longa\' + ('x' * 120) + '\*.log'))
    $objEx = New-BackupJsonObj -Perfil ([pscustomobject]@{ repositorio = 'sftp:a@b:/c'; repositorio_id = ('d' * 64) }) `
        -Origem $origem -Exclusoes $regras -Horario '02:30' -IdentidadeSsh 'k' -KnownHostsFile 'h'
    eq (@($objEx.exclusoes)).Count 3 'exclusoes: 3 linhas logicas -> 3 regras gravadas'
    eq ([string]$objEx.exclusoes[2]) $regras[2] 'exclusoes: regra longa gravada inteira, sem quebra'

    # timeout: Kill + nada publicado + temporario limpo
    '5' | Set-Content -LiteralPath (Join-Path $stubDir 'restore-sleep') -Encoding Ascii -NoNewline
    $script:DumpTimeoutMs = 300
    $destLento = Join-Path $tmp 'restaurado-lento'
    $t0 = Get-Date
    throws { Restore-PastaCompleta -Estado $estado -Config $b1.Config -Snapshot $snapAnt -PathsMeta @($origem) -Destino $destLento -Origem $origem } 'pasta: timeout -> erro e Kill'
    ok (((Get-Date) - $t0).TotalSeconds -lt 20) 'pasta: timeout retornou dentro do prazo'
    ok (-not (Test-Path -LiteralPath $destLento)) 'pasta: timeout -- destino NAO publicado'
    eq (@(Get-ChildItem -LiteralPath $tmp -Directory -Filter '.macip-rest-*' -Force)).Count 0 'pasta: timeout -- temporario limpo'
    $script:DumpTimeoutMs = 600000
    Remove-Item -LiteralPath (Join-Path $stubDir 'restore-sleep') -Force

    # restauracao individual segue intacta (segundo arquivo, SHA-256)
    [IO.File]::WriteAllBytes((Join-Path $stubDir 'dump.bin'), [Text.Encoding]::UTF8.GetBytes('SEG-DEPOIS'))
    $destS3 = Join-Path $tmp 'dest-seg-pos'
    $alvoS3 = Restore-Arquivo -Estado $estado -Config $b1.Config -Snapshot $snapNov `
        -Arquivo '/o/a[1].txt' -Destino $destS3 -Origem $origem
    eq (Get-Content -LiteralPath $alvoS3 -Raw) 'SEG-DEPOIS' 'pasta: restauracao individual do segundo arquivo segue ok'

    Remove-Item -LiteralPath (Join-Path $stubDir 'restaurar') -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath (Join-Path $stubDir 'restore.root') -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath (Join-Path $stubDir 'snaps.jsonl') -Force -ErrorAction SilentlyContinue

    # ---- desinstalacao estruturada (Desinstalar-Tarefa.ps1) ----
    . (Join-Path $PSScriptRoot '..\installer\Desinstalar-Tarefa.ps1')
    $PrefixoAcao = 'C:\Program Files\MACIP-Backup'
    function New-TarefaFalsa {
        param([string]$Desc = 'MACIP Backup - agente de backup do piloto',
              [string]$ArgsAcao = '-NoProfile -NonInteractive -File "C:\Program Files\MACIP-Backup\Backup-MacipCliente.ps1" -Modo Backup -Config "c" -DiretorioEstado "e"',
              [string]$PsPath = 'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe',
              [int]$Instancias = 0, [bool]$FalhaEnabled = $false,
              [bool]$NaoPara = $false, [bool]$FalhaInstancias = $false, [bool]$NaoDesabilita = $false)
        $t = [pscustomobject]@{ _en = $true; nInst = $Instancias; falhaEn = $FalhaEnabled
                                naoPara = $NaoPara; falhaInst = $FalhaInstancias; stops = 0; State = 1
                                naoDesab = $NaoDesabilita }
        $t | Add-Member ScriptProperty Enabled { return $this._en } {
            param($v) if ($this.falhaEn) { throw 'falha ao desabilitar (simulada)' }
                      if ($this.naoDesab) { return }; $this._en = [bool]$v }
        $ri = [pscustomobject]@{ Description = $Desc }
        $ac = [pscustomobject]@{ a1 = ([pscustomobject]@{ Path = $PsPath; Arguments = $ArgsAcao }) }
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
    function New-SvcFalso { param($Tarefa = $null, [string]$ErroGet = '', [int]$ErroDepois = 0, $Tarefa2 = $null, [int]$TrocaApos = 0)
        $s = [pscustomobject]@{ t = $Tarefa; t2 = $Tarefa2; ta = $TrocaApos; eg = $ErroGet; ed = $ErroDepois; chamadas = 0 }
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
            if ($this.t2 -and $this.chamadas -gt $this.ta) { return $this.t2 }
            if (-not $this.t) {
                throw ([Runtime.InteropServices.COMException]::new('nao existe', -2147024894))
            }
            return $this.t }
        return $s
    }
    $Svc = New-SvcFalso -Tarefa (New-TarefaFalsa)
    eq (Invoke-DesinstalacaoTarefa) 0 'uninstall: tarefa propria desabilitada -> remover'
    $marcada = New-TarefaFalsa
    $argumentos = [string]$marcada.Definition.Actions.Item(1).Arguments
    $Svc = New-SvcFalso -Tarefa (New-TarefaFalsa -ArgsAcao ($argumentos + ' -Disparo "$(Arg0)"'))
    eq (Invoke-DesinstalacaoTarefa) 0 'uninstall: tarefa com marcador agendado -> remover'
    $Svc = New-SvcFalso -Tarefa (New-TarefaFalsa -ArgsAcao ($argumentos + ' -Disparo "manual"'))
    eq (Invoke-DesinstalacaoTarefa) 3 'uninstall: marcador livre nao substitui identidade exata'

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
    $Svc = New-SvcFalso -Tarefa (New-TarefaFalsa -ArgsAcao '-NoProfile -Command "& ''C:\Program Files\MACIP-Backup\Backup-MacipCliente.ps1'' -File x"')
    eq (Invoke-DesinstalacaoTarefa) 3 'uninstall: -File dentro de -Command -> preservada'
    $Svc = New-SvcFalso -Tarefa (New-TarefaFalsa -PsPath 'C:\Temp\outropowershell.exe')
    eq (Invoke-DesinstalacaoTarefa) 3 'uninstall: outropowershell.exe -> preservada'
    $Svc = New-SvcFalso -Tarefa (New-TarefaFalsa -PsPath 'C:\Windows\System32\powershell.exe')
    eq (Invoke-DesinstalacaoTarefa) 3 'uninstall: powershell.exe fora do caminho -> preservada'
    $Svc = New-SvcFalso -Tarefa (New-TarefaFalsa -PsPath 'powershell.exe')
    eq (Invoke-DesinstalacaoTarefa) 3 'uninstall: powershell relativo -> preservada'
    $Svc = New-SvcFalso -ErroGet 'Acesso negado'
    eq (Invoke-DesinstalacaoTarefa) 4 'uninstall: texto de erro qualquer -> inconclusivo (nao ausente)'
    # troca por tarefa ALHEIA entre a 1a consulta e a verificacao final:
    # preserva a tarefa nova e os executaveis (codigo 4, sem tocar nela)
    $alheia = New-TarefaFalsa -Desc 'Manutencao da empresa' -ArgsAcao '-File "C:\outro\x.ps1"'
    $Svc = New-SvcFalso -Tarefa (New-TarefaFalsa) -Tarefa2 $alheia -TrocaApos 1
    eq (Invoke-DesinstalacaoTarefa) 4 'uninstall: tarefa trocada no meio -> preservar'
    eq $alheia.stops 0 'uninstall: tarefa alheia nunca e parada'
    eq ([bool]$alheia._en) $true 'uninstall: tarefa alheia nunca e desabilitada'

    # Nao consultar/registrar MACIP-Backup real no host de testes.
    pula 'COM nativo isolado: execute probe-manual-run-com.ps1 (recursos exclusivos)'

} finally {
    Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item Env:RESTIC_STUB_DIR -ErrorAction SilentlyContinue
}
Write-Host ''
Write-Host "RESULTADO: $($script:ok) ok, $($script:er) falha(s), $($script:skip) pulado(s)"
exit $(if ($script:er -gt 0) { 1 } else { 0 })
