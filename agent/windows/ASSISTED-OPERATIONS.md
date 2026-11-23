# Operação e recuperação assistidas · Windows

Candidata **0.1.4-dev4-lab**, em branch separada da Central. Preserve o
instalador 0.1.3. Contratos ativação/resultados v1; ZIP de recuperação v1;
dados/fila Windows v1, sem migração. Aceite nativo da VM ainda pendente.

## Técnico

O administrador solicita a substituição no painel. Códigos/tokens antigos
ficam revogados; o executor A precisa confirmar a autorização SSH removida.
Isso não elimina segredos nem dados copiados do computador anterior. Se
possível, pause sua agenda pelo app; não apague fila ou configuração.

No Windows substituto limpo, cole o **novo código** em Ativar. A chave é
gerada localmente. Retome a mesma tentativa durante a prova B automática,
sem trocar chave/código para contornar o bloqueio. Depois siga Pasta e horário,
com confirmação nativa da conta Windows. Não copie DPAPI, chave privada, token
ou fila do computador anterior. Cancelar/falhar preserva a instalação ativa.

Com A indisponível, o administrador usa **Recuperar usando B** no painel e
fornece o ZIP por canal privado (ou o baixa no navegador autorizado da VM).
Em **Restaurar → Abrir recuperação de B**, escolha o ZIP e a pasta pai.
Confirme: uma subpasta nova é criada, com SHA-256 conferido. A instalação,
fila e pausas individuais não mudam. Nenhuma credencial B vai para o app.
Isso não reconstrói A nem retoma backups. A reabertura depende do administrador.

Pacote alterado, arquivo extra/ausente, colisão, link/junction, nome/caminho
incompatível, destino protegido ou já existente são recusados. Uma escrita
parcial permanece marcada INCOMPLETA e não é reutilizada. Há limite de
1 GiB/1.000 arquivos no transporte de laboratório. `conferencia.json` é um
comprovante de suporte; o técnico não precisa lê-lo/editar JSON.

## Build — administrador

Use Windows PowerShell 5.1 e checkout do SHA exato aprovado da branch.
Restic Windows aprovado e NSIS são pré-requisitos externos, ainda ausentes
nesta entrega. Os comandos não contêm segredos:

```powershell
$ErrorActionPreference='Stop'
$commit=(git rev-parse HEAD).Trim()
$restic=Read-Host 'Caminho do restic.exe aprovado'
$hash=Read-Host 'SHA-256 aprovado do restic.exe'
$output=Join-Path $env:TEMP ('macip-build-'+[Guid]::NewGuid().ToString('N'))
.\agent\windows\installer\New-ReleaseManifest.ps1 -Commit $commit `
 -Version 0.1.4-dev4-lab -Restic $restic -ResticHash $hash -Output $output
$manifestHash=(Get-FileHash -LiteralPath (Join-Path $output 'MANIFEST.json') -Algorithm SHA256).Hash
```

Use checkout descartável de build, com `installer\payload` novo e vazio.
Copie somente o payload produzido, incluindo `Assisted-Recovery.ps1`.
Nesse diretório installer, compile e registre o hash real do instalador:

```powershell
if(-not(Get-Command makensis -ErrorAction SilentlyContinue)){throw 'NSIS ausente'}
& makensis /DVERSAO=0.1.4-dev4-lab "/DMANIFEST_SHA256=$manifestHash" macip-backup.nsi
if($LASTEXITCODE -ne 0){throw 'Build NSIS falhou'}
Get-FileHash -LiteralPath .\MacipBackupSetup-0.1.4-dev4-lab.exe -Algorithm SHA256
```

Siga [UPDATE-ROLLBACK.md](UPDATE-ROLLBACK.md) para manifesto anterior, adoção
e reversão. Sem NSIS/payload aprovado, não há build nem SHA de instalador.

## Provas e freeze

`tests/run-assisted-tests.ps1` usa ZIP, hashes, filesystem e locks reais
em diretórios temporários; não depende de COM/DPAPI. Regressões existentes
continuam obrigatórias. O ZIP de um Restic/SFTP real no Linux é uma prova
complementar; não homologa WinForms, DPAPI, NTFS/antivírus ou tarefa sem sessão.

Roteiro único da matriz Central: `docs/homologacao-candidata-windows-a-b.md`.
Build e aceite integrado na VM ficam separados da implementação. Escopo
funcional congelado para compilar e testar; cofre/hosts, assinatura e demais
decisões de produção permanecem pendentes.
