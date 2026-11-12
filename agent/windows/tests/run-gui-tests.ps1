#requires -Version 5.1
# Testes da interface MAC IP Backup — mesma infra da suite do agente.
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
        # NOVAS a cada chamada — mutacoes locais nao vazam para o gravado,
        # como no COM real).
        param([string]$FalharEm = '', [bool]$TemEstranha = $false)
        $b = [pscustomobject]@{ defData = $null; registros = 0; falha = $FalharEm; estado = 1 }
        if ($TemEstranha) {
            $b.defData = @{ descricao='Manutencao da empresa'; userId='OUTRO\usuario'; logon=1
                            enabled=$true; args='-File C:\outra\coisa.ps1'; trigger='' }
        }
        $b | Add-Member ScriptMethod Get {
            if (-not $this.defData) { return $null }
            $d = Set-DefData -Def (New-DefFalsa) -D $this.defData
            $t = [pscustomobject]@{
                Enabled=$d.Settings.Enabled; State=$this.estado
                LastRunTime=''; NextRunTime='amanha'; LastTaskResult=0
                Principal=[pscustomobject]@{UserId=$this.defData.userId}
                Xml=($this.defData | ConvertTo-Json -Compress)
                Definition=$d
            }
            $t | Add-Member ScriptMethod Run { param($x) } | Out-Null
            return $t
        }
        $b | Add-Member ScriptMethod NewDef { param($Tarefa)
            return (New-DefFalsa)   # sempre copia nova — o registrado fica intacto
        }
        $b | Add-Member ScriptMethod Registrar { param($Def,$Conta,$SenhaConta)
            if ($this.falha -eq 'registrar') { throw 'Permissao negada (simulada)' }
            $this.registros++
            $this.defData = Get-DefData -Def $Def
            $this.defData.userId = $Conta
        }
        $b | Add-Member ScriptMethod RegistrarXml { param([string]$Xml,$Conta,$SenhaConta)
            if ($this.falha -eq 'registrarxml') { throw 'Falha rollback (simulada)' }
            $d = $Xml | ConvertFrom-Json
            $this.defData = @{ descricao=[string]$d.descricao; userId=[string]$d.userId; logon=[int]$d.logon
                               enabled=[bool]$d.enabled; args=[string]$d.args; trigger=[string]$d.trigger }
        }
        $b | Add-Member ScriptMethod XmlAtual {
            if ($this.defData) { return ($this.defData | ConvertTo-Json -Compress) } else { return $null }
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
    $script:Backend.falha = 'registrar'
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

    # ---- Executar agora: conta e concorrencia ----
    $script:Backend = New-BackendFalso
    $script:Backend.defData = @{ descricao='MACIP Backup - agente de backup do piloto'; userId='LAB\operador'
                                 logon=1; enabled=$true; args='-DiretorioEstado "e" -Config "c"'; trigger='' }
    $script:Backend.estado = 4
    throws { Start-TarefaAgora } 'execucao em andamento: novo disparo recusado'
    $script:Backend.estado = 1
    Save-GuiMeta -Meta ([pscustomobject]@{estado_atual='e';config_atual='c';conta_tarefa='LAB\operador'})
    { Start-TarefaAgora } | Out-Null
    ok $true 'executar agora: disparo aceito na mesma tarefa/conta'
    $script:Backend.defData.userId = 'OUTRO\usuario'
    throws { Start-TarefaAgora } 'conta divergente do preparo: disparo recusado'

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
    eq (Get-ChildItem -LiteralPath $dest2 -File | Measure-Object).Count 1 'restaurar: [1] literal — um unico arquivo no destino'

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
