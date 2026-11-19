# Atualização e reversão controladas · laboratório

Este procedimento atende **somente áreas descartáveis** criadas pelo helper
`tools/windows-lab-server.py`, PostgreSQL local exclusivo e Mailpit. Não é um
atualizador da central operacional/Compose, do laboratório congelado ou de
instalações globais. Sem deploy, merge, tags ou atualização silenciosa.

Linhas separadas: Central parte de `fd9a82adc295ec66a652a3d28dc69fe6842be11b`;
Windows parte de `97de94ba563feeccb24fabea11d12abada67e247`. A candidata Windows
nova é **0.1.4-dev3-lab**, preservando 0.1.3. A matriz atual está em
[lab-automation-pins.json](lab-automation-pins.json). O esquema permanece **14**;
esta entrega não acrescenta migração SQL. Contratos de ativação/resultados v1,
controlador de atualização v1 e dados/fila Windows conhecidos v1.

## Administrador — quatro passos normais

Use a conta proprietária da área, acesso Docker local autorizado e a venv Python
já validada. Todos os comandos abaixo são locais ao laboratório escolhido.
Não use `stop`/`down` como parada de atualização: o PostgreSQL efêmero criado
com `--rm` seria removido. O procedimento usa apenas parada dos serviços.

Depois do cadastro inicial dos artefatos aprovados (próxima seção), em Bash:

```bash
set -euo pipefail
umask 077
unset DOCKER_CONTEXT
export DOCKER_HOST=unix:///var/run/docker.sock
CENTRAL="$(pwd -P)"
PY="$CENTRAL/.venv-api/bin/python"
UPDATE="$CENTRAL/tools/lab-update.py"
read -r -p 'Área absoluta do laboratório: ' LAB
read -r -p 'Identificador local do artefato aprovado: ' ARTIFACT
# 1. Pré-verificar: versão, esquema, espaço, processos e trabalhos.
"$PY" "$UPDATE" preflight "$LAB" --artifact "$ARTIFACT"
# 2. Atualizar: confirmação explícita, sem senha em argumentos.
"$PY" "$UPDATE" apply "$LAB" --artifact "$ARTIFACT" --wait 60
# 3. Conferir: só declare concluído após verified/succeeded.
"$PY" "$UPDATE" status "$LAB"
```

O painel mostra versão/estado somente para leitura; não envia comandos,
branches, caminhos de pacote ou URLs. Durante manutenção, o resultado do
Windows deve permanecer na fila local para reenvio. Não confunda o estado do
atualizador com sucesso/falha de um backup.

**4. Recuperar se falhar:** mantenha a área e o backup. Confira a etapa com
`status`. Se o esquema ainda for legível pela versão anterior, execute:

```bash
"$PY" "$UPDATE" recover "$LAB"
"$PY" "$UPDATE" status "$LAB"
```

Não reative workers manualmente sobre estado inconclusivo nem apague locks.
Fila não ociosa, lock ainda ocupado, processo órfão, volume ausente, espaço
insuficiente ou serviço incompleto exigem recusa/espera. Uma manutenção
interrompida de A precisa de inspeção antes de atualizar. Fechar o terminal
do técnico não é motivo para matar backup/restore em andamento.

## Preparação única dos artefatos

O empacotador lê **somente arquivos Git do commit completo escolhido**, valida
os controles de material operacional do empacotador existente e compila o
portal num staging. Reutiliza cache Node somente com lockfile idêntico;
caso contrário, prepare um ambiente descartável com as dependências desse lock.
Não baixa/executa instalador ou comando indicado pelo navegador.

Em checkout central conferido, venv `api[test]`, Node e dependências do portal:

```bash
read -r -p 'SHA completo Central aprovado: ' CENTRAL_COMMIT
read -r -p 'SHA completo Windows compatível: ' WINDOWS_COMMIT
read -r -p 'Caminho absoluto NOVO do arquivo .tar.gz: ' PACKAGE
"$PY" "$CENTRAL/tools/package-lab-release.py" "$CENTRAL_COMMIT" "$PACKAGE" \
  --windows "$WINDOWS_COMMIT"
```

São gerados pacote, `.manifest.json` externo e `.sha256`; há MANIFEST.json
interno e hashes de todos os arquivos. O manifesto registra origem
`Tomas5014/sistema-backup`, commit, contratos, intervalo de leitura PostgreSQL,
Windows compatível e ausência explícita do instalador. A origem depende do
commit revisado e do hash aprovado obtidos por canal confiável; esta entrega
não fornece assinatura/autoupdate autenticado. Não confie apenas no campo de
origem declarado num JSON recebido. Não há garantia byte a byte para imagens
ou dependências. NSIS/Restic Windows aprovado ausentes significam build pendente,
nunca SHA-256 inventado.

Confira os hashes por canal independente. Cadastre somente arquivos locais:

```bash
read -r -p 'SHA-256 do pacote conferido: ' PACKAGE_HASH
"$PY" "$UPDATE" stage "$LAB" --package "$PACKAGE" \
  --sha256 "$PACKAGE_HASH" --commit "$CENTRAL_COMMIT"
```

O retorno é o identificador local do artefato (hash). A operação recusa
origem/contrato divergente, symlinks, caminhos inseguros, hashes/listas de
arquivos diferentes e sobrescrita. A seleção posterior usa só esse identificador,
não uma branch, comando remoto ou URL. Os arquivos de release são imutáveis
para execução normal. Não os edite após aprovação.

Na **primeira adoção**, prepare/cadastre também o artefato exato da versão
anterior. Para a base desta entrega, use o SHA fd9a82a completo indicado acima
e a referência Windows 97de94b completa. A adoção compara o registro da área,
a identidade PID/starttime do processo central e os bytes em uso com o pacote
anterior aprovado. Working tree alterado, frontend compilado de outra origem
ou layout não reconhecido causam recusa; não corrija o registro manualmente.

```bash
read -r -p 'ID do artefato anterior conferido: ' PREVIOUS
read -r -p 'ID do artefato novo com o controlador: ' CONTROLLER
"$PY" "$UPDATE" adopt "$LAB" --artifact "$PREVIOUS" --controller "$CONTROLLER"
```

O controlador é mantido fora do código substituído, em release aprovada e
referenciada pela área. Não apague sua pasta/venv enquanto houver serviços
ou backups que dependam dela. A reversão do código usa esse guardião, mesmo
quando a versão antiga não possuía proteção HTTP própria. O frontend antigo
pode não apresentar a seção nova; `status` e a rota autenticada de versão
continuam disponíveis pelo controlador.

## O que a atualização faz

1. Confere dono/layout privado, HTTPS local registrado, banco/porta/contêiner
   exclusivos por marcador, esquema compatível, serviços, espaço para backup
   e restauração, e ausência de manutenção abandonada.
2. Suspende novas solicitações do operador, permite finalizar trabalhos já
   aceitos e aguarda as filas de provisionamento/recuperação ficarem ociosas.
   Tenta os locks reais com prazo limitado; não força unlock. Uma barreira de
   tabelas também cobre a primeira adoção de código antigo sem guardião HTTP.
3. Congela escritas, encerra somente processos supervisionados dessa área e
   confirma ausência de trabalho. PostgreSQL, A/SFTP, B e Mailpit não são
   derrubados nem recriados. Backups Windows independentes podem continuar em
   A; o envio à central ficará pendente durante a indisponibilidade.
4. Produz `pg_dump -Fc`, restaura em banco temporário exclusivo, compara
   contagem e hash de linhas de todas as tabelas e remove só esse banco de
   verificação. Copia estado/configurações/cofres de laboratório/journals em
   arquivo privado, excluindo logs, PIDs/locks transitórios e fontes de release.
   Não copia o volume de backups B nem o repositório SFTP efetivo A.
5. Aplica migrações da release aprovada em transação e confere a versão real.
   Inicia a API candidata sob barreira, confirma HTTPS/saúde, compara os dados
   anteriores e só então libera workers. Exige processos e heartbeats novos
   antes de declarar verified/succeeded. O PostgreSQL efêmero nunca é parado
   pelo procedimento de atualização.

Backups ficam em `<LAB>/updates/backups/<UUID>`, privados, com dump, estado e
manifesto/hashes. Contêm material de recuperação sensível do laboratório:
não anexar a tickets, portal, Git ou pacote de instalação. O estado registra
etapa e erro sanitizado; não imprime URL de banco, senha, tokens ou chaves.

## Reversão compatível e esquema incompatível

Para voltar a outro artefato aprovado capaz de ler o esquema atual:

```bash
"$PY" "$UPDATE" preflight "$LAB" --artifact "$PREVIOUS"
"$PY" "$UPDATE" rollback "$LAB" --artifact "$PREVIOUS" --wait 60
"$PY" "$UPDATE" status "$LAB"
```

A operação faz novo backup e conserva todas as escritas posteriores. Não
regrava dados antigos nem remove tabelas. Se o esquema tiver avançado além
do leitor anterior, a volta simples é recusada. Retaggear imagem ou editar
`schema_migrations` não torna o banco compatível. Prefira correção à frente.

Restauração explícita, apenas quando aprovada pelo administrador:

```bash
read -r -p 'UUID do backup consistente a recuperar: ' BACKUP
"$PY" "$UPDATE" restore "$LAB" --backup "$BACKUP"
"$PY" "$UPDATE" status "$LAB"
```

Antes da confirmação, o comando informa as tabelas alteradas e que as
escritas posteriores deixarão de aparecer na central ativa. Faz backup
verificado adicional, restaura em **outro banco**, preserva o banco posterior
inteiro e os diretórios atuais em `updates/rescue`. Não descarta dados novos
silenciosamente. O destino ativo só muda após verificar o dump. Se o leitor
do backup não souber reconciliar inventários, usa controlador aprovado
compatível; caso contrário recusa. Recuperação de diretórios parcialmente
interrompida exige atendimento assistido preservando backup/rescue; não use
`recover` de código para ocultar um restore incompleto.

**Dump SQL não recupera snapshots removidos por retenção.** Antes de reabrir
manutenção:

```bash
"$PY" "$UPDATE" reconcile "$LAB"
# Aguarde o trabalho/prova B supervisionados. Consulte status; depois:
"$PY" "$UPDATE" reconcile "$LAB"
"$PY" "$UPDATE" status "$LAB"
```

O primeiro comando consulta identidades/catálogos reais sem inicializar
repositórios, registra pontos históricos ausentes, invalida provas históricas
como autorização de perfil e exige nova prova B. Histórico SQL permanece;
ausência física não vira catálogo inventado nem cópia bem-sucedida. Pontos
históricos ausentes são excluídos da intenção de recópia, com auditoria,
sem apagar o histórico. Sem identidade/volume/seed válidos, continua bloqueado.
A segunda chamada só abre manutenção após novas provas e fila ociosa. Perfil,
retenção e novos pedidos ficam bloqueados durante essa reconciliação.

## Windows

Use a branch Windows própria desta entrega e seu
`agent/windows/UPDATE-ROLLBACK.md`. O instalador interativo confere manifesto,
separa código/ProgramData, preserva DPAPI/chave/configuração/fila e cada pausa,
recusa tarefas alheias/ativas e arquivos em uso. Reversão usa controlador
protegido e leitor compatível. Senha Windows não passa pela central.

## Provas e limites

Testes novos: `api/tests/test_lab_updates.py` (integridade/barreira/refusas),
`api/tests/test_lab_updates_real.py` (processos, PostgreSQL, dump/restore,
migração, A/SFTP/B reais), `portal/tests/lab-version.test.tsx` (fetch simulado)
e `agent/windows/tests/run-update-tests.ps1` (filesystem/locks reais;
COM/DPAPI/processos simulados). Rodadas, versões e resultados no registro.

A prova real exige `MACIP_TEST_LAB_UPDATE_REAL=1`,
`MACIP_TEST_PROVISIONING_SFTP_IMAGE` e `MACIP_TEST_UPDATE_PG_IMAGE` locais,
venv API validada e portal compilado. Da raiz Central, use:

```bash
PYTHONPATH=src:api/src "$PY" -m pytest \
  api/tests/test_lab_updates.py api/tests/test_lab_updates_real.py -q
npm --prefix portal run typecheck
npm --prefix portal test
npm --prefix portal run build
```

As fixtures criam contas só dentro dos contêineres exclusivos e limpam
somente recursos rotulados/registrados por elas. Não configuram SMTP real.
Pacote de candidato durante desenvolvimento é fixture com rótulo sintético;
origem do pacote-base é Git real, e o pacote final deve ser gerado do commit
final, com hashes externos. Ausência de infraestrutura é skip explícito.
Permanecem pendentes: instalador NSIS/payload aprovado, DPAPI/COM/ACL/UAC
nativos, tarefas sem sessão, recuperação do ponto Windows somente por B,
assinatura e aceite integrado. Não é homologação de produção nem resistência
universal a queda de energia/namespace/antivírus.
