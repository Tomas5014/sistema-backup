#requires -Version 5.1
# Filesystem/modelo reais; nenhum COM, DPAPI, Restic ou tarefa instalada.
$ErrorActionPreference='Stop'
$ok=0
function Eq($Actual,$Expected,$Name){if($Actual -cne $Expected){throw "$Name : esperado=$Expected real=$Actual"};$script:ok++;Write-Host "ok $Name"}
$root=Join-Path ([IO.Path]::GetTempPath()) ('macip-status-'+[Guid]::NewGuid().ToString('N'))
try{
 New-Item -ItemType Directory $root|Out-Null
 . (Join-Path $PSScriptRoot '../gui/MacipBackup-Gui.ps1') -BaseDir $root -AgentePath (Join-Path $PSScriptRoot '../Backup-MacipCliente.ps1')
 $from=[DateTimeOffset]::Parse('2030-11-24T17:00:00Z')
 $cfg=Join-Path $root 'config nova.json';[IO.File]::WriteAllText($cfg,'{"synthetic":true}')
 $boundary=Join-Path $root 'situacao.json'
 [IO.File]::WriteAllText($boundary,(@{schema=1;config_sha256=(Get-HashArquivo $cfg);publicada_utc=$from.ToString('o')}|ConvertTo-Json))
 $ctx=Get-ContextoConfiguracao $cfg
 Eq $ctx.Valido $true 'fronteira publicada lida'
 function Info($Time,[int]$State=3){[pscustomobject]@{Ultima=$Time;Estado=$State;Codigo=0;Instancias=@();Habilitada=$true}}
 function Result($Context,$Start,[string]$Trigger='manual'){
  [pscustomobject]@{config_sha256=$Context.Hash;config_caminho=$Context.Config;execucao_id=[Guid]::NewGuid().ToString();disparo=$Trigger;inicio_utc=$Start.ToString('o');fim_utc=$Start.AddSeconds(2).ToString('o');status='sucesso';codigo_restic=0;snapshot=('a'*64)}
 }
 $old=Info $from.AddDays(-7)
 Eq (Get-TelaExecucao -InfoTarefa $old -Contexto $ctx).Classe 'ainda_nao' 'LastRunTime antigo nao prova disparo da configuracao nova'
 $history=Result $ctx $from.AddDays(-7)
 Eq (Get-TelaExecucao -InfoTarefa $old -Res $history -Contexto $ctx).Classe 'ainda_nao' 'sucesso antigo nao vira resultado atual'
 Update-Confirmado -Estado $root -RepositorioId ('b'*64) -Snapshot $history.snapshot -InicioUtc $history.inicio_utc -FimUtc $history.fim_utc
 Eq (Read-Confirmado -Estado $root -RepositorioId ('b'*64)).snapshot $history.snapshot 'ultimo sucesso preservado separadamente'
 $before=(Get-FileHash (Join-Path $root 'ultimo-confirmado.json')).Hash
 $new=Info $from.AddMinutes(1)
 Eq (Get-TelaExecucao -InfoTarefa $new -Contexto $ctx).Classe 'sem_resultado' 'disparo comprovadamente posterior sem resultado identificado'
 $first=Result $ctx $from.AddMinutes(1) '$(Arg0)'
 Eq (Get-TelaExecucao -InfoTarefa $new -Res $first -Contexto $ctx).Classe 'concluido' 'primeira execucao agendada da configuracao reconhecida'
 $other=[pscustomobject]@{Config=(Join-Path $root 'config anterior.json');Hash=$ctx.Hash;DesdeUtc=$from.AddDays(-1);Valido=$true}
 $late=Result $other $from.AddMinutes(1)
 Eq (Get-TelaExecucao -InfoTarefa $new -Res $late -Contexto $ctx).Classe 'sem_resultado' 'resposta atrasada de outro caminho mesmo com bytes iguais recusada'
 $other.Hash='f'*64;$other.Config=$ctx.Config
 $late=Result $other $from.AddMinutes(1)
 Eq (Get-TelaExecucao -InfoTarefa $new -Res $late -Contexto $ctx).Classe 'sem_resultado' 'outro conteudo da configuracao recusado'
 $refusal=[pscustomobject]@{Classe='recusado';UltimaObservada=$old.Ultima;PedidoUtc=$from;Mensagem='pedido recusado'}
 Eq (Get-TelaExecucao -InfoTarefa $old -Res $history -Resolucao $refusal -Contexto $ctx).Classe 'recusado' 'pedido recusado nao herda sucesso historico'
 $pedido=New-PedidoDisparo -InfoTarefa $old -Res $history -Contexto $ctx
 $pedido.PedidoUtc=$from;$pedido.PrazoFimUtc=$from.AddMinutes(2)
 Eq (Get-TelaExecucao -InfoTarefa $old -Res $history -Pedido $pedido -Contexto $ctx -AgoraUtc $from).Classe 'aguardando' 'aceite sem evidencia nova aguarda'
 Eq (Get-TelaExecucao -InfoTarefa $new -Res $late -Pedido $pedido -Contexto $ctx -AgoraUtc $from.AddMinutes(1)).Classe 'em_execucao' 'resultado alheio nao confirma pedido durante carencia'
 Eq (Get-TelaExecucao -InfoTarefa $new -Res $first -Pedido $pedido -Contexto $ctx -AgoraUtc $from.AddMinutes(1)).Classe 'em_execucao' 'execucao agendada nao confirma pedido manual'
 $manual=Result $ctx $from.AddMinutes(1)
 Eq (Get-TelaExecucao -InfoTarefa $new -Res $manual -Pedido $pedido -Contexto $ctx -AgoraUtc $from.AddMinutes(1)).Classe 'concluido' 'execucao manual nova e vinculada confirma pedido'
 $pedido2=New-PedidoDisparo -InfoTarefa $old -Res $manual -Contexto $ctx
 $pedido2.PedidoUtc=$from;$pedido2.PrazoFimUtc=$from.AddMinutes(2)
 Eq (Get-TelaExecucao -InfoTarefa $new -Res $manual -Pedido $pedido2 -Contexto $ctx -AgoraUtc $from.AddMinutes(1)).Classe 'em_execucao' 'mesma identidade de execucao nao confirma pedido seguinte'
 $sameExecution=Result $ctx $from.AddMinutes(1).AddSeconds(1)
 $sameExecution.execucao_id=$manual.execucao_id
 Eq (Get-TelaExecucao -InfoTarefa $new -Res $sameExecution -Pedido $pedido2 -Contexto $ctx -AgoraUtc $from.AddMinutes(1)).Classe 'em_execucao' 'UUID reutilizado nao confirma mesmo com inicio atualizado'
 $changed=[pscustomobject]@{Config=$ctx.Config;Hash=('c'*64);DesdeUtc=$from.AddMinutes(2);Valido=$true}
 Eq (Get-TelaExecucao -InfoTarefa $old -Res $manual -Pedido $pedido -Contexto $changed -AgoraUtc $from).Origem 'generica' 'pedido de configuracao anterior descartado apos troca'
 Eq (Get-TelaExecucao -InfoTarefa $old -Res $manual -Pedido $pedido -Contexto $changed -AgoraUtc $from).PedidoObsoleto $true 'tela libera pedido obsoleto sem deixa-lo bloqueando o botao'
 $refusal|Add-Member NoteProperty Contexto $ctx
 Eq (Get-TelaExecucao -InfoTarefa $old -Res $manual -Resolucao $refusal -Contexto $changed).Classe 'ainda_nao' 'resolucao manual anterior nao encobre configuracao nova'
 Eq (Get-TelaExecucao -InfoTarefa $old -Res $manual -Resolucao $refusal -Contexto $changed).ResolucaoExpirada $true 'tela descarta resolucao obsoleta da memoria'
 Eq (Get-FileHash (Join-Path $root 'ultimo-confirmado.json')).Hash $before 'classificacao nao modifica historico'
 [IO.File]::WriteAllText($boundary,'{}')
 Eq (Get-TelaExecucao -InfoTarefa $old -Contexto (Get-ContextoConfiguracao $cfg)).Classe 'config_invalida' 'fronteira malformada nao vira execucao'
 Remove-Item -LiteralPath $boundary
 Eq (Get-ContextoConfiguracao $cfg).Valido $true 'configuracao legada sem sidecar continua legivel'
 $legacy=[pscustomobject]@{status='sucesso';codigo_restic=0;snapshot=('d'*64);inicio_utc=$from.AddMinutes(1).ToString('o')}
 Eq (Test-ResultadoContexto $legacy $ctx) $false 'resultado legado sem identidade nao e atribuido a configuracao atual'
 Write-Host "STATUS CONTEXTO: $ok aprovados; 0 falhos; 0 ignorados (modelo/fs, sem Windows nativo)."
}finally{if(Test-Path -LiteralPath $root){Remove-Item -LiteralPath $root -Recurse -Force}}
