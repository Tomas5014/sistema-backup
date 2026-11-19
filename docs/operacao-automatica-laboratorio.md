# Operação automática do laboratório · candidata do piloto

O laboratório continua limitado a clientes fictícios, Linux nos servidores,
um aplicativo Windows por instalação, origem local, destino SFTP e Mailpit.
Não é adaptador operacional, implantação em produção ou aceite Windows.
Após esta entrega, **as funcionalidades do piloto ficam congeladas** para
compilar o instalador e executar o aceite integrado na VM.

Branches separadas:

- Central: `feature/backup-lab-automation`, base `8c0c51d2899fbe9acf7ff5a516d96b4847fad03d`.
- Windows: `feature/windows-backup-lab-automation`, base `7a5c3acf6b192c4e4cadad445c6ef90c0a02319a`.

Não faça merge das linhas. Preserve o commit Windows
`7d62a800325146d1357f8832b1df5c4c3b2eff8d` e seu instalador 0.1.3.
O arquivo de compatibilidade desta entrega fixa os commits de código
compatíveis; commits posteriores apenas documentais são identificados
separadamente no registro. Não há tag ou release publicada.

## Administrador — preparar uma área e iniciar os serviços

### Requisitos e decisões iniciais

Use um Linux descartável e uma VM Windows limpa. Defina o IPv4 local do
servidor alcançável pela VM, a confiança na CA, a conta Windows executora
e os limites de espaço/tempo. Não use túneis que dependam da sessão do
técnico. Não reutilize contas, volumes, chaves ou banco de outro ensaio.
Não acesse `srvdev1`, centrais/laboratórios anteriores ou timers existentes.

Requisitos do código central: Python >=3.11, dependências de `api`, Node
para build do portal, Docker local, OpenSSH cliente, Restic, libseccomp e
`/dev/shm` montado. A prova Linux usa Restic 0.18.1 e PowerShell 7.4.6
portátil; isso não homologa PowerShell 5.1/DPAPI/COM/WinForms no Windows.
Os scripts candidatos continuam escritos para Windows PowerShell 5.1.
O espaço reservado padrão de B é 64 MiB, além do tamanho necessário à
cópia. Dados sintéticos pequenos não dimensionam volumes de clientes.

Na raiz **central** da candidata, em Bash:

```bash
set -euo pipefail
umask 077
CENTRAL="$(pwd -P)"
python3 -m venv .venv-api
PY="$CENTRAL/.venv-api/bin/python"
"$PY" -m pip install -e 'api[test]'
npm --prefix "$CENTRAL/portal" ci
npm --prefix "$CENTRAL/portal" run build
TOOL="$CENTRAL/tools/windows-lab-server.py"
WINDOWS_COMMIT="$(python3 -c 'import json; print(json.load(open("docs/lab-automation-pins.json"))["windows_code"])')"
export PYTHONPATH="$CENTRAL/api/src"
# Somente o socket Docker local escolhido para o laboratório:
unset DOCKER_CONTEXT
export DOCKER_HOST=unix:///var/run/docker.sock
restic version
docker info >/dev/null
docker build -t macip-auto-sftp-fixture "$CENTRAL/tests/sftp"
```

Garanta que PostgreSQL 16 e Mailpit v1.29.7 estejam disponíveis no cache
de imagens desse host descartável antes do início. Os helpers usam
`--pull=never` para não trocar imagens silenciosamente. Registre os IDs
reais das imagens e os SHAs Git utilizados. Não há promessa de imagens
ou dependências reproduzíveis byte a byte.

Escolha uma área NOVA e um IPv4 local. As leituras abaixo recebem valores
reais do administrador, sem placeholders literais ou segredos no histórico:

```bash
read -r -p 'Área absoluta nova do laboratório: ' LAB
read -r -p 'IPv4 local alcançável pela VM: ' LAB_IP
"$PY" "$TOOL" init "$LAB" --sftp-ip "$LAB_IP" \
  --sftp-image macip-auto-sftp-fixture --postgres-image postgres:16-alpine \
  --windows-commit "$WINDOWS_COMMIT"
openssl req -x509 -newkey rsa:3072 -sha256 -nodes -days 2 \
  -subj '/CN=MACIP Automation Lab' -addext "subjectAltName=IP:${LAB_IP}" \
  -keyout "$LAB/private/tls-key.pem" -out "$LAB/private/tls-cert.pem"
chmod 600 "$LAB/private/tls-key.pem"
"$PY" "$TOOL" configure-activation "$LAB" \
  --public-url "https://${LAB_IP}:8187" \
  --delivery-url "https://${LAB_IP}:9447/v1/activate"
"$PY" "$TOOL" operator "$LAB"
"$PY" "$TOOL" services-start "$LAB" \
  --certificate "$LAB/private/tls-cert.pem" \
  --key "$LAB/private/tls-key.pem" --ca "$LAB/private/tls-cert.pem"
"$PY" "$TOOL" services-status "$LAB"
```

O helper registra o SHA central e o SHA Windows escolhido em `evidence/base.json`.
O bootstrap `operator` solicita a senha pelo mecanismo existente; não a
passe em argumentos nem a fotografe. Confirme que 8187 e 9447 estão livres
antes do início. A CA pública `tls-cert.pem` deve ser instalada/verificada
na VM e no navegador pelo administrador. A chave privada permanece no
servidor. Certificado vencido ou não confiável bloqueia a ativação;
não desative a validação TLS no aplicativo.

`services-start` retorna os endereços da central e do capturador Mailpit.
A API serve o build do portal pela mesma origem HTTPS. O supervisor fica
em sessão POSIX separada, sem stdin/terminal, com logs privados em
`private/services`. Mantém API/portal, endpoint A, worker A, worker B e
avaliador de alertas. Não instala serviço global, conta no host ou timer
Linux. Pode fechar o terminal após conferir o status e o painel.

O supervisor repõe workers encerrados e usa PID **e starttime** para
controle. Após encerrar seus filhos originais, libera somente a reserva
correspondente, permitindo retomada imediata. Se a API estiver fora ou a
reserva tiver sido obtida antes da persistência local do identificador,
a expiração normal da reserva continua sendo o mecanismo de recuperação:
5 minutos para A e 30 minutos para B. Resultados de reservas anteriores
são recusados. Filhos Restic mantêm a exclusão local até terminar.

Controle somente desta área:

```bash
"$PY" "$TOOL" services-status "$LAB"
"$PY" "$TOOL" services-restart "$LAB" --role protection
"$PY" "$TOOL" services-stop "$LAB"
"$PY" "$TOOL" services-start "$LAB"
```

`services-stop` encerra os processos supervisionados; preserva banco,
repositórios, Mailpit, cofres e evidências para retomada. Após queda do
supervisor, `services-start` recusa duplicar workers ainda vivos: execute
`services-stop` para encerrar exclusivamente os processos registrados e
então inicie novamente. Não há inicialização automática após reboot do
host. O laboratório e a supervisão precisam ser iniciados explicitamente.
Isto não é substituto de supervisão operacional de produção.

Para **encerrar a infraestrutura descartável** após guardar evidências:

```bash
"$PY" "$TOOL" stop "$LAB"
```

O comando valida os marcadores completos antes de parar contêineres.
Preserva os diretórios da área e o volume B; não implementa limpeza de
snapshots, retenção ou prune. O PostgreSQL criado com `--rm` é efêmero:
encerrá-lo encerra aquele laboratório, não é procedimento de atualização.
Não use `docker system prune` ou remoções por prefixos. Remoção posterior
de dados descartáveis requer conferir a área, o marcador do volume e os
IDs efetivamente criados, sem tocar laboratórios anteriores.

### Como o encadeamento funciona

A migração expansiva **13** estende `provisioning_operations`, sem criar
outra fila de provisionamento. A pega somente `prepare_primary` e
`retry_primary`; B pega `protect_installation` e `copy_points`. Continua
existindo no máximo uma operação ativa por instalação. O contrato de
ativação e de resultados permanece versão 1, com extensões aditivas.

B gera sua chave privada localmente e registra somente a pública pela
API autorizada. A cria uma conta restrita em **contêiner exclusivo**, nunca
no host, e um repositório por instalação. A referência fictícia é escrita
com exclusividade e hash **antes** do backup. O ensaio confirma o ponto em
A; B copia, executa `check --read-data`, restaura sem rede via seccomp e
compara SHA-256. Só então libera perfil/código. Referência inválida,
volume ausente, identidade divergente ou segredo original ausente bloqueia
em vez de fabricar sucesso ou reinicializar um repositório publicado.

Inicialização nova usa estágio privado e publicação atômica. Um estágio
não publicado, sem config e sem snapshots, pode ser reconstruído após
interrupção, sob lock e com intenção registrada. Config concluída no
estágio é reaproveitada. Repositório final não vazio, material inesperado,
link, ponto existente ou segredo ausente exige inspeção; não é apagado
para fazer a preparação passar. `unlock` usa somente o comportamento
padrão do Restic, após exclusão local e conferência de identidade.

Ao registrar a chave Windows, a prova anterior é invalidada e outra
operação B é enfileirada na mesma transação. A senha não é entregue
antes da nova prova vigente. Nenhum comando `refresh-b`, `copy-activation`
ou `prove-activation` é necessário nesse modo.

Cada resultado recebido com snapshot gera intenção durável de cópia.
O worker reconcilia todos os pontos pendentes do vínculo atual, sem
escolher apenas o último. Cópia repetida preserva a correspondência A/B,
sem duplicar pontos. Falha de uma instalação termina sua tentativa e
permite processar outra; há nova tentativa com espera de 15–300 segundos.
A última falha registrada não apaga o último sucesso ou o histórico.
Não existe retenção/exclusão automática nesta entrega.

O Mailpit permanece em loopback e pode ser consultado no próprio servidor
descartável pelo administrador. O técnico reconhece a ocorrência no painel;
não precisa abrir túnel por instalação.

Cada worker envia atualização independente a cada 5 segundos. Mais de
60 segundos sem atualização aparece no painel como **Sem atualização
recente**, sem concluir que o backup falhou. O avaliador continua cobrando
janelas na central mesmo com o PC desligado. Mailpit recebe exclusivamente
pelo loopback, para o domínio reservado `example.invalid`.

O cofre é o adaptador de arquivos privados do laboratório, não o cofre
definitivo. Os arquivos de cada papel recebem apenas suas dependências;
API/A não recebem a senha de escrita de B. Como a prova roda sob um mesmo
usuário Linux, não se alega isolamento hostil entre processos desse usuário.
Hosts, cofre, identidades e supervisão de produção permanecem pendentes.

## Técnico — usar somente as interfaces por instalação

1. Entre no painel HTTPS com conta autorizada. Clique **Novo cliente**,
   preencha cliente fictício, instalação, máquina e horário sugerido e
   clique **Preparar instalação**. Acompanhe A, B, a última tentativa,
   falhas registradas e atualização dos executores. Não abra terminal em A/B.
2. Perfil e código começam bloqueados. Aguarde o ensaio comprovado de B.
   Se A falhar, use **Tentar novamente**. B retoma automaticamente; estado
   sem atualização exige que o administrador confira o serviço, não novo
   cadastro. Não altere IDs ou credenciais para contornar bloqueios.
3. Clique **Gerar código de ativação** e entregue pelo canal privado do
   laboratório. Não registre o código em capturas ou logs.
4. No aplicativo Windows candidato, aba **Ativar**, cole o código. A chave
   nasce no Windows; apenas a pública sai. O aplicativo aguarda a nova
   verificação automática de B por até 90 segundos (limite configurado no
   código: 120). Se houver demora/rede indisponível, retome com o **mesmo
   código**; UUID, chave e identidade da tentativa permanecem iguais.
   A configuração ativa não é substituída em erro/cancelamento.
5. Após conferir SFTP e ID do repositório, avance para **Pasta e horário**.
   Escolha pasta fictícia e exclusão conhecida (`*.tmp`), revise e salve.
   A janela nativa confirma a conta Windows. A mesma conta registra backup
   e sincronização; sua senha não passa pelo painel.
6. O aplicativo cria/atualiza **MACIP-Backup** e **MACIP-Backup-Sync**. A
   segunda roda a cada 5 minutos, sem GUI e sem novo backup, com logon por
   senha na conta DPAPI. Limita uma tentativa a 45 segundos/20 mensagens,
   cada requisição a até 10 segundos e a tarefa inteira a 2 minutos.
   Trava exclusiva impede dois envios simultâneos. A agenda de backup pode
   ficar pausada sem suspender o envio dos resultados e da própria pausa.
7. Aba **Situação** distingue **Monitoramento sincronizado**, com o último
   envio confirmado, e **Envio pendente**. Fila vazia não inventa novo
   contato. Central indisponível preserva UUID/conteúdo; reenvio não muda
   sucesso/falha do backup. O caminho **Importar perfil** continua válido,
   mostrando **Monitoramento não configurado** se não houver vínculo.
8. No painel, confira **Último backup**, **Última cópia protegida** e
   **Próximo prazo**. B copia todos os pontos confirmados em A. Confira
   alertas, mensagens no capturador e reconhecimento pelo operador. Não
   trate a prova seed como confirmação da cópia do ponto Windows novo.
9. Pela interface Windows, restaure arquivo e pasta em destinos novos.
   A recuperação do ponto Windows somente por B, com A e origem
   inacessíveis, permanece passo obrigatório do aceite integrado da VM.

Uma tarefa de envio alheia, execução ainda ativa ou consulta inconclusiva
bloqueia a alteração. Publicação tenta restaurar a definição anterior em
falha; arquivos ainda referenciados são preservados. O desinstalador
confere e para separadamente as duas tarefas antes de remover executáveis.
Não remove fila, DPAPI, configurações, repositórios ou recuperações.

## Gerar e aceitar o instalador candidato

A candidata proposta é **0.1.4-dev2-lab**. Use o commit Windows fixado no
arquivo de compatibilidade. NSIS e `restic.exe` Windows **aprovado pela
MAC IP** são requisitos de build. O Restic Linux usado nos testes não é
payload elegível. Não substitua nem sobrescreva o instalador 0.1.3.

No Windows de build, após checkout do commit e obtenção do payload por
canal aprovado:

```powershell
$ErrorActionPreference='Stop'
git rev-parse HEAD
$TrackedChanges=git status --porcelain --untracked-files=no
if($LASTEXITCODE -ne 0 -or $TrackedChanges){throw 'Checkout versionado alterado'}
Set-Location agent\windows\installer
$ApprovedSha256=Read-Host 'SHA-256 aprovado do restic.exe Windows'
if($ApprovedSha256 -cnotmatch '^[0-9a-fA-F]{64}$'){throw 'Hash aprovado invalido'}
$Actual=(Get-FileHash -LiteralPath .\payload\restic.exe -Algorithm SHA256).Hash
if($Actual -ine $ApprovedSha256){throw 'Payload diverge do aprovado'}
if(-not(Get-Command makensis -ErrorAction SilentlyContinue)){throw 'NSIS ausente'}
makensis /DVERSAO=0.1.4-dev2-lab macip-backup.nsi
if($LASTEXITCODE -ne 0){throw 'Build NSIS falhou'}
Get-FileHash -LiteralPath .\MacipBackupSetup-0.1.4-dev2-lab.exe -Algorithm SHA256
```

Registre SHA Git, versão/ID do Restic aprovado, comando, hash do instalador
e política de assinatura aplicada. Sem esses insumos, o build é
**pendente**, não instalador homologado. Depois do build, congele também
os artefatos antes da VM; correções bloqueadoras exigem nova candidata.

## Evidências e gates

A prova descartável usa supervisor/processos POSIX, PostgreSQL, HTTPS,
Chrome, OpenSSH, Restic e Mailpit reais quando configurados. Exercita
cadastro visual, perfil bloqueado antes de B, código pelo painel, nova
prova automática após a chave, reinício de B, dois clientes, vários pontos,
atraso de cópia, reconhecimento no painel e reenvio após indisponibilidade.
O consumidor PowerShell roda no Linux; DPAPI é simulado. Disparo da tarefa
periódica, COM e WinForms são simulados/testados por definições e chamadas
públicas, não executados no Agendador Windows real.

Execute as regressões descritas no registro. A integração opta explicitamente
por `MACIP_TEST_AUTOMATION_BROWSER=1`, `MACIP_TEST_POWERSHELL`,
`MACIP_TEST_WINDOWS_WORKTREE` e `MACIP_TEST_PROVISIONING_SFTP_IMAGE`.
Sem infraestrutura há skip identificado; não conte como aprovação.
Guarde somente IDs, horários, hashes, estados e evidências sem credenciais.

Aceite VM pendente: instalação do pacote, confiança TLS, DPAPI real,
cliques, senha nativa cancelada, registro/remoção seletivos COM, backup
com sessão encerrada, sincronização periódica sem novo backup, fila e
retomada após reinício, exclusão e restauração Windows e recuperação do
ponto exato somente por B. Sem essa evidência, não liberar dados reais.
