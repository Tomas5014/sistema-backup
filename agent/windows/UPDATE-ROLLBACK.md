# Atualização e reversão do Windows · laboratório

**Recompilação corrigida da dev4:** siga [o procedimento completo](installer/REBUILD-dev4.md), incluindo referência 0.1.3, hashes e bytes Git. Exemplos de build anteriores são históricos.

A candidata atual é **0.1.4-dev4-lab**, com importação assistida por B. Use
[ASSISTED-OPERATIONS.md](ASSISTED-OPERATIONS.md) para a versão/payload atuais.
As referências dev3 a seguir são históricas; o formato persistente/fila continua
v1, sem migração ou troca automática de pausa/configuração.

Candidata: **0.1.4-dev3-lab**. Base preservada:
`97de94ba563feeccb24fabea11d12abada67e247`. O instalador 0.1.3 e o commit
`7d62a800325146d1357f8832b1df5c4c3b2eff8d` continuam referências anteriores.
Sem atualização silenciosa, serviço novo ou senha Windows no painel.
NSIS, payload Restic Windows aprovado e aceite nativo na VM são gates separados.

## Administrador: pré-verificar → atualizar → conferir

1. Obtenha o instalador e o SHA-256 por canal confiável. Confira origem,
   versão e manifesto com os SHAs compatíveis da Central. Não use binário
   de teste como Restic aprovado. Guarde o instalador anterior e as evidências.
2. Feche o aplicativo e aguarde backup, restore e envio terminarem. Execute
   o instalador como administrador; confirme a janela. Cancelar antes da
   aplicação não muda a instalação. `/S` é recusado nesta candidata.
3. O instalador confere hashes, leitor de dados/fila, propriedade das duas
   tarefas, ausência de execução e processos/arquivos em uso. Em dúvida,
   recusa. Configuração e DPAPI permanecem em `%ProgramData%\MacipBackup`;
   somente código em `%ProgramFiles%\MACIP-Backup` é trocado.
4. Abra o aplicativo na **mesma conta executora**, confira pasta, identidade,
   agenda e monitoramento. Agenda pausada deve continuar pausada. Backup e
   envio são preservados independentemente; não use Retomar sem decisão.
   A definição/conta das tarefas não muda; não é necessário recadastrar senha.
   Se futuramente uma mudança exigir registrar a tarefa, continua obrigatório
   o diálogo nativo já usado pelo aplicativo.
5. No ensaio, execute um backup, confira o envio idempotente e restaure um
   arquivo. Nada nesta validação Linux prova DPAPI, COM ou tarefa sem sessão.

A troca usa pasta separada e rename, com journal na área protegida
`%ProgramFiles%\.MACIP-Backup-update`. Antes de permitir novos disparos,
confere novamente propriedade/definição da tarefa e restaura cada Enabled
anterior. Não mata processos para liberar arquivos. Programas antigos são
mantidos para recuperação. Configuração, chaves, credenciais e fila não são
movidas nem regravadas; só seus metadados são conferidos. Consulta de processo,
ACL ou agendador inconclusiva exige recusa, não overwrite.

## Recuperar

- Se o instalador recusou antes da troca: feche o aplicativo, aguarde os
  trabalhos, confira a causa e repita **o mesmo instalador conferido**.
- Se a troca falhou: código anterior é reposto quando possível. Verifique o
  resultado; se a agenda ficou desabilitada por consulta inconclusiva, mantenha
  assim e use o atalho **Reverter atualização**, como administrador.
- O atalho usa o controlador estável na área protegida e a referência anterior
  registrada, não uma URL/branch digitada. Revalida hashes, tarefas e leitor.
  Não remove configurações nem fila. A versão anterior pode não oferecer o
  próprio botão/atalho de atualização; o controlador externo continua disponível.
- Uma reversão não pode usar leitor menor que `format.json`, quando presente,
  nem um código sem resultados sobre uma fila/tarefa de sincronização existente.
  Formato legado conhecido equivale a v1; evolução futura precisa publicar o
  marcador antes de produzir dados incompatíveis. Nunca altere esse marcador
  para forçar downgrade. Correção à frente é preferível ao descarte de fila.
- Caso arquivos ou tarefas tenham sido trocados por outro processo, ou a
  recuperação não consiga repor código/agenda, preserve as duas pastas e o
  journal. Não execute desinstalação forçada nem apague ProgramData. O erro
  informa o caminho protegido para atendimento pela MAC IP.

Não é uma garantia contra perda abrupta de energia em toda combinação de
NTFS, antivírus e permissões. A VM deve provar rename/FILE_SHARE_DELETE, UAC,
consulta de processos de outras sessões, COM e ACL reais antes do piloto.

## Congelar payload e compilar

Use checkout do **commit exato** da branch Windows desta entrega. O gerador
lê Git archive; não empacota arquivos sujos. Forneça caminho e hash do Restic
Windows aprovado, não credenciais. Exemplo em PowerShell 5.1, na raiz Windows:

```powershell
$commit = (git rev-parse HEAD).Trim()
$restic = Read-Host 'Caminho do restic.exe aprovado'
$resticHash = Read-Host 'SHA-256 aprovado do restic.exe'
$output = Join-Path $env:TEMP ('macip-payload-' + [Guid]::NewGuid().ToString('N'))
.\agent\windows\installer\New-ReleaseManifest.ps1 -Commit $commit `
  -Version '0.1.4-dev3-lab' -Restic $restic -ResticHash $resticHash -Output $output
```

O resultado fornece o SHA do MANIFEST.json; não contém hash inventado de
instalador. Copie os arquivos desse diretório novo para `installer\payload`
num checkout de build descartável. NSIS passa a consumir **somente payload**,
conferido contra o manifesto, incluindo scripts; não mistura código do
working tree. Compile com `/DVERSAO=0.1.4-dev3-lab` e
`/DMANIFEST_SHA256=<hash efetivo>`. Registre hash real do exe após o build.

Instalação anterior sem recibo exige um manifesto legado aprovado por canal
confiável. O mesmo gerador com `-Legacy`, commit 97de94b completo e o Restic
aprovado que compôs aquela instalação, gera a referência. Inclua somente o
MANIFEST.json como `payload\LEGACY-MANIFEST.json` e informe seu hash via
`/DLEGACY_SHA256`. Divergência de qualquer módulo recusa adoção; não invente
um manifesto a partir da pasta instalada desconhecida. A referência 0.1.3
pode ser inventariada separadamente; não é leitor da fila central e não pode
substituir uma instalação monitorada.

## Testes

`tests/run-update-tests.ps1`: filesystem/locks reais em diretório temporário;
COM, processos Windows e DPAPI simulados. Cobre troca, rollback, dados/fila,
pausa, tarefa alheia/ativa, arquivos em uso, falha parcial e retomada,
integridade, leitor futuro/incompatível e repetição do mesmo pacote.
Executar também run-gui-tests, run-lab-automation-tests,
run-central-alerts-tests e run-windows-agent-tests.

**Pendentes:** compilação/assinatura/hash do instalador com NSIS e payload
aprovado; DPAPI/COM/WinForms/UAC reais; sessão encerrada; atualização com tarefa
agendada; backup/restauração do ponto Windows somente por B. Nenhuma prova
Linux declara Windows homologado.
