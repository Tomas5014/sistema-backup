# Ensaio Windows 0.1.3 → A → B

Estado deste roteiro: preparado para execução pelo operador. Os cliques,
o disparo com sessão encerrada e as restaurações na VM Windows permanecem
pendentes de evidência operacional. O servidor de ensaio deve ser um Linux
descartável e a VM deve conter somente dados fictícios.

Use a central da branch `feature/backup-prepare-installation` com a base
`af0365029a9a179e7675beda7e6ba15300dceda9` e este roteiro. Use o instalador
Windows **0.1.3**, da linha
`7d62a800325146d1357f8832b1df5c4c3b2eff8d`. Antes de instalar, registre
versão e SHA-256 do instalador efetivamente escolhido e confira sua
procedência. O SHA do instalador depende do artefato fornecido pelo operador.
O worktree Windows dessa revisão permanece separado.

## 1. Servidor Linux descartável — preparar a área

Requisitos: Python do ambiente da API, dependências instaladas, Restic
0.18.1, OpenSSH cliente, Docker local, Node e dependências do portal,
`libseccomp.so.2` e `/dev/shm` montado com espaço livre. O volume B desta
prova é tmpfs: guarde as evidências antes de desligar o servidor.

Escolha uma rede isolada do laboratório em que a VM alcance o IPv4 do
servidor. O SFTP será publicado somente nesse IPv4. A conectividade deve
permanecer disponível com a sessão Windows encerrada. Um túnel iniciado
na sessão interativa da VM não atende essa condição. Não altere firewall
ou rede operacional para realizar esta prova.

Em Bash, na raiz do repositório:

```bash
set -euo pipefail
umask 077
git rev-parse HEAD
git -C ../sistema-backup-gui rev-parse HEAD
restic version
docker version
python3 -m venv .venv-api
.venv-api/bin/python -m pip install -e 'api[test]'
docker build -t macip-protection-lab-sshd tests/sftp
docker pull postgres:16-alpine
read -r -p 'IPv4 local do servidor descartável acessível pela VM: ' LAB_IP
LAB=$(mktemp -d /tmp/macip-windows-flow.XXXXXX)
# init exige que a pasta final ainda não exista.
rmdir "$LAB"
export LAB
export PYTHONPATH="$PWD/api/src"
PY="$PWD/.venv-api/bin/python"
LAB_TOOL="$PWD/tools/windows-lab-server.py"
export PY LAB_TOOL
"$PY" "$LAB_TOOL" init "$LAB" --sftp-ip "$LAB_IP"
```

O helper mantém tokens e senhas em `private/`, com permissões privadas.
Somente `evidence/` deve compor o relatório. Não use `set -x`, gravação de
terminal nem captura de tela durante entrada ou entrega de segredos.
O helper aceita apenas contêineres com o marcador completo de sua área.
Uma falha preserva os arquivos para inspeção. Não repita `init` ou
`prepare-a` na mesma área.

Em outro terminal, na mesma raiz, informe o caminho exibido para sua área:

```bash
read -r -p 'Área absoluta criada no terminal inicial: ' LAB
export PYTHONPATH="$PWD/api/src"
.venv-api/bin/python tools/windows-lab-server.py api "$LAB"
```

Deixe a API aberta. No terminal inicial, crie o administrador. Digite uma
senha de teste no prompt privado e entregue-a ao operador fora das evidências:

```bash
"$PY" "$LAB_TOOL" operator "$LAB"
cd portal
npm ci
npm run build
MACIP_API_ORIGIN=http://127.0.0.1:8187 npm run preview -- --host 127.0.0.1 --port 4177 --strictPort
```

Esse terminal passa a servir o portal. Use um terceiro terminal com as
variáveis abaixo, na raiz do repositório, para os próximos comandos:

```bash
set -euo pipefail
read -r -p 'Área absoluta criada no terminal inicial: ' LAB
export LAB
export PYTHONPATH="$PWD/api/src"
PY="$PWD/.venv-api/bin/python"
LAB_TOOL="$PWD/tools/windows-lab-server.py"
```

O navegador do servidor abre
`http://127.0.0.1:4177`. Para acesso por outro computador, use o
encaminhamento SSH autorizado do laboratório para essa porta loopback.

## 2. Painel — cadastrar e observar o bloqueio

1. Entre como `operador-lab`.
2. Abra **Clientes → Novo cliente**.
3. Preencha nome `Cliente Fictício Windows AB`, identificador
   `pc-windows-ab-01`, máquina `VM-WINDOWS-LAB` e horário sugerido.
4. Clique **Preparar instalação → Acompanhar preparação**.
5. Registre cliente e instalação retornados pela API na aba Network do
   navegador, sem copiar cookies ou cabeçalhos de autenticação.
6. Confirme **Aguardando**, **Preparação pendente** e **Baixar perfil**
   desabilitado.

No servidor, informe o UUID de instalação, e não o UUID do cliente:

```bash
read -r -p 'UUID da instalação criada pelo painel: ' INSTALLATION
"$PY" "$LAB_TOOL" prepare-a "$LAB" --installation "$INSTALLATION"
"$PY" "$LAB_TOOL" seed "$LAB"
"$PY" "$LAB_TOOL" prepare-b "$LAB"
"$PY" "$LAB_TOOL" copy-seed "$LAB"
```

O servidor A do ensaio usa a conta derivada da instalação em chroot, o
repositório `/repository` e chave de escrita exclusiva. A chave SSH de B
tem `internal-sftp -R`. B recebe chave Restic adicional de leitura,
senha própria de repositório e volume identificado.

Atualize o painel e registre **Estrutura primária pronta**,
**Estrutura pronta · Prova de restauração pendente**, último ponto
copiado e download bloqueado. O seed contém somente um arquivo fictício,
com manifesto capturado antes do backup.

```bash
"$PY" "$LAB_TOOL" prove-seed "$LAB"
```

A verificação usa B com rede bloqueada no processo de Restic, sem
credenciais de A. Registre `seed-proof.json` e o resultado no painel.
O estado deve ser **Restauração comprovada em laboratório**.
Clique **Baixar perfil** pelo fluxo normal e guarde esse JSON para a VM.
Não entregue o arquivo interno `private/profile.json` ao técnico.
Registre o SHA-256 do JSON baixado e confira seu ID de repositório com
`evidence/primary.json`.

```bash
"$PY" "$LAB_TOOL" export-credentials "$LAB"
```

Entregue `private/delivery/id_windows` por canal privado de teste.
Entregue a senha contida em `private/delivery/restic-password.txt` por
outro canal privado. Não junte esses arquivos ao JSON ou ao relatório.
Use compartilhamento temporário protegido/gerenciador de senhas do
laboratório e remova o compartilhamento depois da cópia. B mantém suas
credenciais no servidor.

## 3. VM Windows — dados e manifesto anteriores ao backup

Confirme que a VM está limpa e que não existe uma tarefa `MACIP-Backup`
de outra instalação. Registre o fuso e o relógio. Instale o aplicativo
0.1.3 aprovado e confira OpenSSH e acesso à conta de execução escolhida.
Use uma conta de teste com senha válida, autorizada a executar em lote.

Copie apenas `tools/windows-lab-data.ps1` para a VM. É um helper do
ensaio, separado do aplicativo. Em PowerShell:

```powershell
& .\windows-lab-data.ps1 -Action Create
Get-FileHash -LiteralPath 'C:\MACIP-Lab-Ficticio\reference.json' -Algorithm SHA256
```

Anote a origem `C:\MACIP-Lab-Ficticio\origem`. A referência fica fora
da origem em `C:\MACIP-Lab-Ficticio\reference.json`. Contém SHA-256 dos
dois arquivos esperados e a exclusão `*.tmp`. A origem também contém
`ignorar.tmp`, que deve ficar ausente dos pontos e das restaurações.
Não altere a origem até concluir o backup e capturar seu ID.

## 4. VM Windows — importar, agendar e encerrar a sessão

1. Abra o aplicativo pela interface.
2. Na aba **Ativar**, importe o JSON baixado do painel.
3. Selecione `id_windows` e informe a senha Restic entregue separadamente.
4. Clique **Validar e continuar**. Registre sucesso e ID esperado nos
   detalhes técnicos. Não capture senha ou conteúdo da chave.
5. Em **Pasta e horário**, escolha a origem fictícia. Abra
   **Exclusões (opcional)** e insira uma linha `*.tmp`.
6. Escolha um horário alguns minutos à frente no relógio da VM.
7. Revise origem, destino, conta e frequência diária. Clique
   **Salvar e agendar**. Informe a senha da conta Windows na janela nativa.
8. Registre o **Próximo backup** informado pelo Agendador. Feche o app e
   encerre a sessão Windows pelo menu **Sair**. Deixe a VM ligada.
9. Aguarde passar o horário, mantendo o servidor A acessível pela rede
   do laboratório. Entre novamente com a mesma conta e abra o app.
10. Em **Situação**, confirme a execução do horário agendado e seu
    resultado **concluído**, com snapshot completo e horário posteriores
    ao logout. Abra **Mostrar detalhes técnicos** e guarde o ID de 64 hex.

Guarde também a evidência do Agendador de que a tarefa pertence à instalação,
executa mesmo sem sessão e teve `LastRunTime` no horário esperado e
`LastTaskResult=0`. Um sucesso anterior ou o clique **Executar backup agora**
não satisfazem esta etapa. Se aparecer **sem resultado deste disparo**,
falha ou incompleto, pare e registre o diagnóstico.

## 5. VM Windows — duas restaurações pela interface

Pause a agenda pelo app depois de capturar o ponto para estabilizar o ensaio.
Na aba **Restaurar backup**, escolha a data/hora e o ID do disparo agendado.

1. Selecione **Restaurar um arquivo**, escolha `documento.txt` na lista
   e uma pasta vazia separada, por exemplo `C:\MACIP-Restore-Arquivo`.
   Confirme a revisão e restaure. Use **Abrir pasta restaurada**.
2. Selecione o mesmo ponto e **Restaurar toda a pasta protegida**.
   Escolha `C:\` como pai e nome novo `MACIP-Restore-Pasta`.
   A revisão deve mostrar a raiz histórica da origem. Restaure e abra a pasta.
3. Registre estado, tempo, ID do ponto, destinos e hashes mostrados.

Confira os destinos efetivos retornados pelo app:

```powershell
& .\windows-lab-data.ps1 -Action CheckFile -Reference 'C:\MACIP-Lab-Ficticio\reference.json' -Target 'C:\MACIP-Restore-Arquivo\documento.txt'
& .\windows-lab-data.ps1 -Action CheckFolder -Reference 'C:\MACIP-Lab-Ficticio\reference.json' -Target 'C:\MACIP-Restore-Pasta'
```

A pasta inteira deve conter exatamente `documento.txt` e
`subpasta\binario.bin`. `ignorar.tmp` deve estar ausente. A interface
restaura o conteúdo da raiz histórica diretamente na pasta escolhida.

## 6. Servidor — copiar exatamente o ponto do Windows

Transfira o arquivo `reference.json` da VM para uma pasta de evidências
do ensaio no servidor, conferindo o SHA-256 já registrado. Ele contém
somente nomes e hashes dos arquivos fictícios. Informe seu caminho e o
snapshot completo copiado dos detalhes do aplicativo:

```bash
read -r -p 'Caminho absoluto do reference.json transferido da VM: ' WINDOWS_REF
read -r -p 'Snapshot Windows completo (64 hex): ' WINDOWS_SNAPSHOT
"$PY" "$LAB_TOOL" windows-point "$LAB" --snapshot "$WINDOWS_SNAPSHOT" --reference "$WINDOWS_REF"
"$PY" "$LAB_TOOL" copy-windows "$LAB"
```

O helper confere o ID exato em A e compara a origem dos metadados do
snapshot com a referência da VM. Converte `C:\pasta` para a raiz
interna `C/pasta` esperada pelo restore integral em B. Guarde
`windows-point.json` e `copy-windows.json`, com IDs completos de A e B.

## 7. Laboratório — indisponibilidade de A/Windows e restauração por B

Antes de parar A, confira os IDs e o sucesso da cópia. Na VM, confirme
a agenda pausada, feche o app e desligue apenas a VM fictícia pelo sistema.
Registre o desligamento no hipervisor. A origem fica inacessível.

No servidor:

```bash
"$PY" "$LAB_TOOL" stop-a "$LAB"
"$PY" "$LAB_TOOL" prove-windows "$LAB"
```

`stop-a` valida o marcador de propriedade e encerra somente o contêiner
SFTP do laboratório. A API, o painel e B continuam disponíveis.
`prove-windows` usa o snapshot B da cópia confirmada, executa
`check --read-data`, restaura com `--verify` e compara todos os arquivos
com o manifesto anterior ao backup. O processo não consegue abrir sockets.
A pasta temporária de restauração é removida ao final.

Guarde `windows-proof.json` com `snapshot_a`, `snapshot_b`,
`manifest_sha256`, `restored_sha256`, contagem e `duration_seconds`.
O tempo medido abrange a verificação de integridade, restauração e comparação.
Registre também o tempo de indisponibilidade observado pelo operador.

## 8. Aceite e encerramento

Preencha cada linha com aprovado, falhou ou pendente e a referência da prova:

| Verificação | Evidência necessária |
| --- | --- |
| Cadastro pelo painel e bloqueio inicial | IDs e captura sem cookies/segredos |
| Prova seed e download normal | IDs A/B, hashes, JSON baixado e seu SHA-256 |
| Importação no aplicativo 0.1.3 | versão/instalador, validação e ID do repositório |
| Agenda sem sessão Windows | horário previsto, logout, LastRunTime, resultado e snapshot |
| Restore de um arquivo pelo app | destino separado e SHA-256 |
| Restore da pasta pelo app | dois hashes, conjunto exato e ausência de ignorar.tmp |
| Cópia do ponto Windows | IDs completos A/B de copy-windows.json |
| Restore por B com A/VM desligados | a-off.json, prova do hipervisor, windows-proof.json e duração |

Leia somente `evidence/` ao montar o relatório. Guarde a referência
Windows e as conferências em conjunto. Tokens, senhas, chaves, cookies,
banco e cofre não compõem evidência pública.

Encerre a API e o preview com Ctrl+C nos terminais correspondentes.
No terminal do servidor:

```bash
"$PY" "$LAB_TOOL" stop "$LAB"
```

Esse comando preserva arquivos e evidências. Após guardá-los, remova somente
a área de ensaio identificada e seu volume B
`/dev/shm/macip-windows-<UUID-da-instalação-sem-hífens>`, com conferência
prévia do caminho e do UUID em `.macip-volume`. Na VM, remova somente a
instalação/tarefa e os arquivos fictícios que pertencem a esta prova,
depois do aceite. A decisão de descarte cabe ao operador.

As provas anteriores de Restic, Read-Perfil e Chrome são complementares.
Até receber as evidências desta VM, o disparo sem sessão e os dois restores
pelo aplicativo permanecem pendentes.

## Verificação dos comandos nesta entrega

Em Linux descartável, o helper do servidor executou `init → api → prepare-a →
seed → prepare-b → copy-seed → prove-seed → export-credentials → stop-a →
prove-seed → stop` com PostgreSQL, OpenSSH e Restic reais. O cadastro no
smoke test foi feito por chamada HTTP administrativa. O cadastro por clique,
a importação e a agenda deste roteiro devem ser comprovados pelo operador.

O teste exerceu caminhos com espaços e expôs uma falha na interpretação
de `UserKnownHostsFile` pelo SSH e das opções CSV de `-o` pelo Restic.
A correção limitada ao executor B é coberta pela cópia real do smoke test.

Com as imagens locais já disponíveis e PowerShell indicado:

```bash
cd api
MACIP_TEST_PROVISIONING_SFTP_IMAGE=macip-protection-lab-sshd \
MACIP_TEST_POSTGRES_IMAGE=postgres:16-alpine \
MACIP_TEST_POWERSHELL=/tmp/pwsh/pwsh \
PYTHONPATH=src ../.venv-api/bin/python -m pytest -o tmp_path_retention_policy=none \
  tests/test_windows_runbook.py -q
```

Resultado desta sessão: **3 aprovados, 0 falhos, 0 ignorados**. O PowerShell
7.4.6 no Linux executou a geração e a conferência dos dados fictícios.
As recusas incluíram área existente, link, referência de outra origem,
item excluído presente e SHA-256 alterado. A sintaxe dos blocos Bash foi
conferida com `bash -n`. Os contêineres e o volume tmpfs criados pelos
testes foram removidos.
