# Desinstalar-Tarefa.ps1 -- verificacao ESTRUTURADA da tarefa MACIP-Backup
# chamada pelo desinstalador NSIS antes de remover os executaveis.
# Sem texto livre nem filtro por palavra: usa Schedule.Service (COM) direto.
#
# Codigos de saida (o NSIS decide):
#   0 = tarefa do app, desabilitada e sem instancias ativas/pendentes -> pode remover
#   2 = tarefa AUSENTE confirmada -> pode remover
#   3 = homonima ALHEIA (sem a marca do app ou acao fora da instalacao) -> preservada; abortar
#   4 = consulta inconclusiva / erro / falha ao desabilitar -> preservar executaveis
#   5 = ainda ha instancia ativa/pendente -> preservar executaveis
#
# Windows PowerShell 5.1+ / pwsh 7. Sem segredos, sem parametros da GUI.
[CmdletBinding()]
param(
    [string]$Nome = 'MACIP-Backup',
    [string]$Marca = 'MACIP Backup',
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
    # Status: encontrada | ausente (confirmado) | erro (inconclusivo)
    param($Svc)
    try { $t = $Svc.GetFolder('\').GetTask($Nome) } catch {
        $hr = $null; try { $hr = $_.Exception.HResult } catch { }
        $msg = [string]$_.Exception.Message
        if ($hr -eq -2147024894 -or $msg -match 'cannot find|find the file|encontr|not found') {
            return [pscustomobject]@{ Tarefa = $null; Status = 'ausente'; Erro = $msg }
        }
        return [pscustomobject]@{ Tarefa = $null; Status = 'erro'; Erro = $msg }
    }
    if ($t) { return [pscustomobject]@{ Tarefa = $t; Status = 'encontrada'; Erro = $null } }
    return [pscustomobject]@{ Tarefa = $null; Status = 'ausente'; Erro = $null }
}

function Test-Propriedade {
    # Propriedade exige as DUAS evidencias nos campos esperados:
    #   RegistrationInfo.Description contem a marca  E
    #   a acao aponta para a instalacao MAC IP ($PrefixoAcao).
    # Texto parecido em outro campo NAO comprova propriedade.
    param($Tarefa)
    try {
        $desc = [string]$Tarefa.Definition.RegistrationInfo.Description
        if ($desc -notlike ('*' + $Marca + '*')) { return $false }
        if ($PrefixoAcao) {
            $cmd  = [string]$Tarefa.Definition.Actions.Item(1).Path
            $args = [string]$Tarefa.Definition.Actions.Item(1).Arguments
            $ref  = ($cmd + ' ' + $args) -replace '/', '\'
            $pfx  = $PrefixoAcao -replace '/', '\'
            if ($ref -notlike ('*' + $pfx + '*')) { return $false }
        }
        return $true
    } catch { return $false }
}

function Get-InstanciasAtivas {
    # RegisteredTask.GetInstances(0) -> IRunningTaskCollection: instancias
    # ATIVAS ou PENDENTES de uma vez (0 = todas as associadas a tarefa).
    param($Tarefa)
    try { return @($Tarefa.GetInstances(0)).Count }
    catch { return -1 }   # consulta negada = inconclusivo
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
            "(marca/acao divergentes) - preservada intacta; desinstalacao abortada.")
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
        $n = Get-InstanciasAtivas -Tarefa $t
        if ($n -ne 0) {
            Write-Saida "EXECUCAO: ainda ha instancia ativa/pendente - executaveis preservados."
            return 5
        }
    }
    # 3. confirma o estado final: desabilitada + sem instancias.
    if ($t.Enabled) {
        Write-Saida 'INCONCLUSIVO: Enabled nao ficou false - executaveis preservados.'
        return 4
    }
    Write-Saida 'PROPRIA E PARADA: tarefa desabilitada, sem instancias ativas/pendentes.'
    return 0
}

if ($MyInvocation.InvocationName -ne '.') {
    $codigo = Invoke-DesinstalacaoTarefa
    exit $codigo
}
