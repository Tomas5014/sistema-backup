# Troca de diretórios dev4 — gate nativo antes de reinstalar

Correção baseada em `87158ac965cd339a633783061957fe296487b524`, sem alteração
na Central ou nos formatos de dados/fila. Preserve a VM, seus dados, a referência
0.1.3, instaladores anteriores e todas as pastas da atualização que falhou.
Não execute limpeza manual de `next-*`, `previous-*`, journal ou locks.

## Evidência, hipótese e limite

O operador relatou Windows PowerShell **5.1**, NSIS **3.11**, falha em
`[IO.Directory]::Move`, journal `failed`, aplicativo presente, `next-*` presente
e pasta anterior ausente. A suíte temporária também falhou ao mover `next-*`.
Essa é evidência nativa fornecida pelo operador, não um ensaio executado aqui.

O código anterior retinha handles ReadWrite/FileShare.Delete de arquivos
filhos até o finally, atravessando ambas as renomeações e a tentativa de
reposição. A [documentação Microsoft de rename](https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/ntifs/ns-ntifs-_file_rename_information)
descreve restrição para diretório com arquivos abertos, com exceções de oplock.
Isso sustenta o mecanismo suspeito; não comprova sozinho a causa específica
da VM, nem exclui antivírus, outro processo, ACL ou filesystem diferente.

Em Linux/PowerShell 7.4.6 os quatro casos mínimos renomearam com sucesso,
inclusive o antigo. Resultado registrado como **not-proven-on-Linux**; não é
aprovação NTFS. Não há Windows nativo neste ambiente de desenvolvimento.

## Estratégia corrigida

A sondagem de acesso exclusivo permanece, agora com FileShare.None. Abre os
arquivos, recusa conflito e fecha todos os seus handles **antes** de Directory.Move.
O lock exclusivo entre atualizadores fica fora das pastas e permanece durante
toda a troca e a retomada das tarefas. Propriedade/atividade das tarefas,
processos em execução, manifesto/hashes e compatibilidade de dados continuam
verificados. Tarefas ficam pausadas durante a janela; integridade é conferida
novamente imediatamente antes e depois da publicação. Não mata processos,
modifica ACL, ignora erro ou força substituição de diretório existente.

A sondagem é pontual: outro processo pode abrir arquivo depois dela. Por isso
não se promete impedir qualquer abertura externa/antivírus. A falha real do
rename preserva as pastas, aciona reposição e não é anunciada como sucesso.
Não há garantia contra queda abrupta/energia; isso exige outro ensaio.

Fases fixas distinguem move-active, publish-next, verify-active e recuperação.
Diagnóstico sanitizado contém tipo da exceção interna e HRESULT hexadecimal,
sem mensagem bruta, linha de comando ou conteúdo de arquivos. Falha principal
permanece mesmo se reposição, retomada da agenda ou limpeza falharem; erros
secundários ficam em recovery_errors. Diagnóstico antigo não vira atual.

## Prova segura na VM — antes de outro instalador

Use um clone de build, não a pasta instalada. Estes comandos criam somente
worktree de conferência e fixtures exclusivas em TEMP. As suítes de atualização
usam registro, tarefas e consulta de processos simulados, inclusive no Windows;
**não consultam/alteram HKLM ou tarefas instaladas**. Renames, locks e o processo
filho com arquivo aberto são reais no sistema onde os comandos executarem.

Informe o SHA completo do novo commit fornecido no relatório, já obtido no clone:

```powershell
$ErrorActionPreference = 'Stop'
$repo = (Get-Location).Path
$commit = Read-Host 'SHA completo aprovado da correcao de troca de diretorios'
if ($commit -cnotmatch '^[a-f0-9]{40}$') { throw 'SHA invalido' }
$resolved = & git -C $repo rev-parse --verify "$commit^{commit}"
if ($LASTEXITCODE -ne 0 -or $resolved -cne $commit) { throw 'Commit indisponivel' }
$check = Join-Path $env:TEMP ('macip-swap-check-' + [Guid]::NewGuid().ToString('N'))
& git -C $repo -c core.autocrlf=false -c core.eol=lf worktree add --detach $check $commit
if ($LASTEXITCODE -ne 0) { throw 'Checkout de conferencia falhou' }
$ps51 = Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe'
if ([Environment]::Is64BitOperatingSystem -and -not [Environment]::Is64BitProcess) {
  $ps51 = Join-Path $env:WINDIR 'Sysnative\WindowsPowerShell\v1.0\powershell.exe'
}
& $ps51 -NoProfile -ExecutionPolicy Bypass -File (Join-Path $check 'agent\windows\tests\probe-directory-rename.ps1') |
  Tee-Object -FilePath (Join-Path $check 'rename-native.json')
if ($LASTEXITCODE -ne 0) { throw 'Prova minima recusada; nao instalar' }
& $ps51 -NoProfile -ExecutionPolicy Bypass -File (Join-Path $check 'agent\windows\tests\run-update-tests.ps1')
if ($LASTEXITCODE -ne 0) { throw 'Regressao de atualizacao falhou; nao instalar' }
& $ps51 -NoProfile -ExecutionPolicy Bypass -File (Join-Path $check 'agent\windows\tests\run-installer-contract-tests.ps1') -Commit $commit
if ($LASTEXITCODE -ne 0) { throw 'Contrato/legado falhou; nao instalar' }
```

O primeiro comando de prova exige Windows PowerShell 5.1 e TEMP em NTFS.
Guarde JSON completo e saídas/exit codes. Compare casos sem handles, antigo
com handles em active/next, antigo somente next e sondagem fechada. Ele testa
uma nova tentativa após fechar os handles. Não injeta uma falha NTFS artificial.
`hypothesis` indica se a reprodução antiga ocorreu **nesse ambiente**; caso não
ocorra, não a declare confirmada. Controles sem handles e sondagem fechada devem
passar. Qualquer falha nesses controles ou nas suítes impede outra instalação.

Os testes cobrem instalação inicial, legado com referência aprovada, atualização
com manifesto, reversão, arquivo aberto por outro processo, concorrência entre
atualizadores e falha entre renames. Conferem código ativo, hashes dos dados/fila
fictícios e pausas individuais. Também injetam falhas de reposição/finalização
para provar que elas não escondem a exceção principal. Essas injeções são
simulações, mesmo quando executadas no Windows.

## Próxima tentativa e recuperação

No layout relatado (`failed`, Code presente, previous ausente), **não remova next**.
Depois de passar os gates nativos, compile e execute o novo instalador conforme
[REBUILD-dev4.md](REBUILD-dev4.md), usando esse mesmo SHA aprovado e a referência
legada conhecida. A nova tentativa revalida a instalação ativa; cria outro GUID
para staging; arquiva o journal anterior byte a byte em history/journal-UUID.json
antes de substituí-lo. Pastas next/previous anteriores permanecem para evidência;
não são misturadas com o payload novo nem removidas. Reserve espaço para ambos.

Se Code estiver ausente ou o estado for needs-recovery, Apply recusa em vez de
tratar o caso como instalação nova. Preserve ambos os diretórios e o journal.
Recover exige pasta anterior/manifesto/hashes conferidos e tarefas com as mesmas
definições; só então repõe o código e as pausas originalmente registradas. Se
essas referências não puderem ser conferidas, recusa sem apagar nada. Não use
Recover no caso relatado de previous ausente e código ativo intacto.

O controlador externo na área protegida passa a ser publicado atomicamente
pelo controlador corrigido; a versão anterior é preservada. Não use um atalho
antigo para tentar reparar a VM antes de instalar/conferir essa correção. Um
resgate necessário deve usar o helper do checkout aprovado, com assistência e
verificação dos caminhos reais, não editar journal/recibo manualmente.

Falha depois de gravar recibo novo também repõe o recibo anterior ao repor o
código. Se não for possível confirmar código/agenda, mantém needs-recovery e
não anuncia retomada. Restaurar referência não apaga a fila, dados, chaves ou
backups. Nenhuma etapa autoriza desinstalação forçada ou limpeza ampla.

## Depois dos gates

Recompile com NSIS 3.11 pelo roteiro de build, registre SHA-256 do exe real e
repita a atualização na janela escolhida pelo operador. Confira UAC/ACL/NTFS,
versão, configuração, DPAPI, fila, ambas as pausas e referências das tarefas;
execute backup/monitoramento/restauração previstos no aceite integrado. Só essa
prova posterior decide o aceite nativo. Linux e tarefas simuladas não homologam
a instalação Windows. Preserve os recursos operacionais existentes.
