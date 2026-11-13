# Desinstalar-Tarefa.ps1 -- verificacao ESTRUTURADA da tarefa MACIP-Backup
# chamada pelo desinstalador NSIS antes de remover os executaveis.
# Sem texto livre, schtasks, XML ou findstr: usa Schedule.Service (COM).
#
# Codigos de saida (o NSIS decide):
#   0 = tarefa do app, desabilitada e sem instancias ativas/pendentes -> pode remover
#   2 = tarefa AUSENTE confirmada -> pode remover
#   3 = homonima ALHEIA (sem a descricao/acao esperadas) -> preservada; abortar
#   4 = consulta inconclusiva / erro / falha ao desabilitar -> preservar executaveis
#   5 = ainda ha instancia ativa/pendente -> preservar executaveis
#
# Windows PowerShell 5.1+ / pwsh 7. Sem segredos, sem parametros da GUI.
[CmdletBinding()]
param(
    [string]$Nome = 'MACIP-Backup',
    [string]$DescricaoEsperada = 'MACIP Backup - agente de backup do piloto',
    [string]$PrefixoAcao = '',          # $INSTDIR -- a acao precisa apontar p/ a instalacao
    $Svc = $null,                       # injetavel em teste; $null cria o COM real
    [switch]$SomenteVerificar           # sem -SomenteVerificar age (desabilita/encerra)
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Off

function Get-SchedService {
    if ($Svc) { return $Svc }
    $s = New-Object -ComObject Schedule.Service
    $s.Connect()
    return $s
}

function Get-TarefaInfo {
    # Status: encontrada | ausente (CONFIRMADA: o proprio GetTask devolveu
    # o codigo de tarefa inexistente, HRESULT 0x80070002) | erro.
    # Falha de pasta/servico e falha da consulta sao separadas; texto de
    # excecao e retorno vazio NUNCA provam ausencia.
    param($Svc)
    try { $pasta = $Svc.GetFolder('\') } catch {
        return [pscustomobject]@{ Tarefa = $null; Status = 'erro'
            Erro = "pasta do Agendador indisponivel: $($_.Exception.Message)" }
    }
    try { $t = $pasta.GetTask($Nome) } catch {
        # excecao empacotada pelo PowerShell -> procura o HRESULT na cadeia
        $hrOk = $false; $e = $_.Exception
        while ($null -ne $e) {
            try { if ($e.HResult -eq -2147024894) { $hrOk = $true; break } } catch { }
            $e = $e.InnerException
        }
        if ($hrOk) { return [pscustomobject]@{ Tarefa = $null; Status = 'ausente'; Erro = [string]$_.Exception.Message } }
        return [pscustomobject]@{ Tarefa = $null; Status = 'erro'; Erro = [string]$_.Exception.Message }
    }
    if ($t) { return [pscustomobject]@{ Tarefa = $t; Status = 'encontrada'; Erro = $null } }
    return [pscustomobject]@{ Tarefa = $null; Status = 'erro'; Erro = 'consulta devolveu tarefa vazia' }
}

function ConvertTo-CaminhoNormal {
    # Normaliza separador e resolve o caminho absoluto NO WINDOWS; fora
    # dele (so teste) devolve a forma textual -- a comparacao segue exata
    # dos dois lados, sem transformar prefixo parecido em igualdade.
    param([string]$P)
    $p2 = $P.Trim() -replace '/', '\'
    if ($env:OS -ne 'Windows_NT') { return $p2 }
    try { return [IO.Path]::GetFullPath($p2) } catch { return $p2 }
}

function Test-Propriedade {
    # Propriedade EXIGE os campos esperados (mesma regra da GUI):
    #   1. descricao EXATA publicada pelo app;
    #   2. UMA unica acao (acoes ambiguas = estranha);
    #   3. executavel = powershell.exe;
    #   4. -File apontando para o caminho ABSOLUTO NORMALIZADO de
    #      Backup-MacipCliente.ps1 DESTA instalacao ($PrefixoAcao).
    # Prefixo parecido e caminho em outro argumento nao comprovam nada.
    param($Tarefa)
    try {
        $def = $Tarefa.Definition
        if ([string]$def.RegistrationInfo.Description -cne $DescricaoEsperada) { return $false }
        if ([int]$def.Actions.Count -ne 1) { return $false }
        $acao = $def.Actions.Item(1)
        if ([string]$acao.Path -notlike '*powershell.exe') { return $false }
        $args = [string]$acao.Arguments
        if ($args -match '-File\s+"([^"]+)"') { $alvo = $Matches[1] }
        elseif ($args -match '-File\s+(\S+)') { $alvo = $Matches[1] }
        else { return $false }
        $esperado = ConvertTo-CaminhoNormal -P ($PrefixoAcao.TrimEnd('\', '/') + '\Backup-MacipCliente.ps1')
        $real = ConvertTo-CaminhoNormal -P $alvo
        return ($real -ieq $esperado)
    } catch { return $false }
}

function Get-InstanciasAtivas {
    # Contagem da colecao COM IRunningTaskCollection: ATIVAS + PENDENTES.
    # Falta de visibilidade/falha de consulta = inconclusivo (-1).
    param($Tarefa)
    try {
        $col = $Tarefa.GetInstances(0)
        if ($null -eq $col) { return -1 }
        return [int]$col.Count
    } catch { return -1 }
}

function Write-Saida {
    # stdout do console (capturado pelo nsExec) SEM poluir o retorno da funcao.
    param([string]$Msg)
    [Console]::Out.WriteLine($Msg)
}

function Invoke-DesinstalacaoTarefa {
    $svc = Get-SchedService
    $info = Get-TarefaInfo -Svc $svc
    if ($info.Status -eq 'ausente') {
        Write-Saida "AUSENTE: nenhuma tarefa '$Nome' no Agendador."
        return 2
    }
    if ($info.Status -eq 'erro') {
        Write-Saida "INCONCLUSIVO: consulta da tarefa falhou ($($info.Erro)) - executaveis preservados."
        return 4
    }
    $t = $info.Tarefa
    if (-not (Test-Propriedade -Tarefa $t)) {
        Write-Saida ("HOMONIMA ALHEIA: '$Nome' existe mas nao pertence ao MAC IP Backup " +
            "(descricao/acao divergentes) - preservada intacta; desinstalacao abortada.")
        return 3
    }
    if ($SomenteVerificar) {
        Write-Saida "PROPRIA: tarefa do app encontrada (somente verificacao)."
        return 0
    }
    # 1. desabilita PRIMEIRO: impede novos disparos (agendados ou manuais).
    try { $t.Enabled = $false } catch {
        Write-Saida "ERRO: nao foi possivel desabilitar a tarefa ($($_.Exception.Message)) - executaveis preservados."
        return 4
    }
    # 2. encerra instancias se necessario.
    $n = Get-InstanciasAtivas -Tarefa $t
    if ($n -lt 0) {
        Write-Saida 'INCONCLUSIVO: nao foi possivel listar instancias - executaveis preservados.'
        return 4
    }
    if ($n -gt 0) {
        try { $t.Stop('') } catch { }
        Start-Sleep -Seconds 2
    }
    # 3. RE-CONSULTA: so libera quando confirmar desabilitada + sem
    #    instancia ativa ou pendente (fresca, nao o objeto antigo).
    $info2 = Get-TarefaInfo -Svc $svc
    if ($info2.Status -ne 'encontrada') {
        Write-Saida "INCONCLUSIVO: re-consulta falhou ($($info2.Erro)) - executaveis preservados."
        return 4
    }
    $t2 = $info2.Tarefa
    if ($t2.Enabled) {
        Write-Saida 'INCONCLUSIVO: a tarefa nao ficou desabilitada - executaveis preservados.'
        return 4
    }
    $n2 = Get-InstanciasAtivas -Tarefa $t2
    if ($n2 -lt 0) {
        Write-Saida 'INCONCLUSIVO: instancias ilegiveis na verificacao final - executaveis preservados.'
        return 4
    }
    if ($n2 -gt 0) {
        Write-Saida "EXECUCAO: ainda ha instancia ativa/pendente - executaveis preservados."
        return 5
    }
    Write-Saida 'PROPRIA E PARADA: tarefa desabilitada, sem instancias ativas/pendentes.'
    return 0
}

if ($MyInvocation.InvocationName -ne '.') {
    $codigo = Invoke-DesinstalacaoTarefa
    exit $codigo
}
