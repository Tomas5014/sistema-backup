# Capacidade e retenção manual no laboratório

Esta entrega é exclusiva de clientes fictícios no fixture automatizado.
**Retenção automática de produção não homologada.** Não implanta política
operacional, cofre definitivo, hosts de produção nem destinatários reais.
O aceite integrado na VM Windows continua pendente. O Windows de referência
permanece em `97de94ba563feeccb24fabea11d12abada67e247`, sem alterações.

## O que foi reaproveitado

A base central é `7fb7bd823f8cd8b1b4c14cd16f87fa1696e0958c`.
O agente Linux já tinha retenção automática por destino: após backup,
`orchestrator` chama `ResticAdapter.forget`, que combina `--keep-last` com
`--prune`. Esse caminho não foi modificado nem usado nesta ação do painel.
O Windows candidato não chama forget/prune.

A nova operação usa `provisioning_operations`, autenticação A/B separada,
reserva com expiração, exclusão por instalação, os workers e supervisor
existentes. A migração **14**, expansiva, mantém o histórico anterior,
amplia os tipos da fila e acrescenta configurações/capacidade e auditoria.
Não é uma segunda fila nem execução de Restic numa requisição HTTP.

O repositório efetivo A está em `/jail/repository` no contêiner SFTP
exclusivo da instalação. A árvore inicial de preparação do host não é
utilizada para medir ou excluir os pontos criados pelo Windows.

## Administrador: habilitar numa área nova

Siga [Operação automática](operacao-automatica-laboratorio.md), usando
esta branch central e o Windows fixado acima. No comando `init`, acrescente
`--storage-lab`. Exemplo com as variáveis já definidas naquele roteiro:

```bash
"$PY" "$TOOL" init "$LAB" --sftp-ip "$LAB_IP" \
  --sftp-image macip-auto-sftp-fixture --postgres-image postgres:16-alpine \
  --windows-commit 97de94ba563feeccb24fabea11d12abada67e247 --storage-lab
```

`LAB` deve ser uma área absoluta **nova**. Não edite configurações de uma
área antiga/congelada para habilitar o recurso. A opção registra a escolha
na configuração privada; no início dos serviços ela habilita a API
`MACIP_API_LAB_STORAGE=1` e os mesmos workers A/B passam a medir/processar
as operações. Sem a opção, as rotas retornam 404 e nada é excluído.
Não exporte credenciais no navegador nem as copie para os comandos.
O acesso Docker deste fixture equivale a administração do host descartável;
não use esse adaptador em produção.

Instalar dependências/build do portal, TLS, operador e `services-start`
seguem o roteiro base. Nenhum comando por instalação é necessário para
capacidade ou retenção. `services-stop` para exclusivamente os processos
da área; `stop` encerra sua infraestrutura conforme as regras do roteiro.
Não remova locks, filas, snapshots ou evidências para contornar uma recusa.

## Operador: fluxo pela interface

1. Cadastre e prepare a instalação pelo painel. Aguarde A e B verificados.
2. Em Clientes, abra o cliente e a seção **Capacidade e retenção ·
   Laboratório** da instalação. A e B são apresentados separadamente.
3. Confira a última medição válida, o espaço livre/usado/total do volume
   compartilhado e o uso alocado individual do repositório (`du`).
   Não some os volumes de instalações: várias linhas podem medir o mesmo
   filesystem Docker ou a mesma montagem de B. Não são quotas por cliente.
4. Abra **Editar política do laboratório**. Defina quantos pontos manter
   em A e em B, independentemente. Vazio significa manter todos. Defina,
   opcionalmente, o aviso de espaço livre **em bytes**. Não há escolha de
   caminho, comandos ou credenciais nessa tela.
5. Fixe IDs completos de pontos de recuperação que devam permanecer,
   um por linha, no servidor correspondente. Antes de uma recuperação
   demorada, fixe o ponto escolhido. A central não conhece restaurações
   históricas feitas apenas pela interface Windows. Prova, ensaio e último
   ponto são protegidos automaticamente, mesmo sem fixação manual.
6. **Simular retenção A** ou **Simular retenção B** consulta o catálogo
   real. A tabela mostra data, ID completo, candidatos e motivo para manter.
   A operação registra solicitante, horário e identidade do repositório.
   Não remove pontos, dados nem locks. Pode haver mais pontos mantidos que
   o número desejado: o número é o mínimo de recentes, não uma autorização
   para violar as proteções abaixo.
7. Confira instalação, lado e IDs. Marque a confirmação explícita e use
   **Executar retenção A/B**. O worker recalcula a decisão. Política,
   identidade ou catálogo divergentes exigem nova simulação. A não exclui
   nada em B, nem B em A.
8. Confira o resultado e os IDs efetivamente removidos no histórico.
   Faça restauração de um ponto mantido em A e outra, separada, em B;
   compare SHA-256 com referência anterior ao backup.

Viewer somente consulta. Escritas exigem administrador e CSRF para sessão
por cookie. Não há exclusão automática: salvar uma política, iniciar ou
reiniciar um worker nunca autoriza uma exclusão sem solicitação explícita.

## Medições e avisos

Os workers tentam medir a cada 30 segundos. Falha de volume, conexão ou
identidade preserva a última medição boa e registra a última tentativa.
Após 120 segundos, erro recente ou executor sem heartbeat recente, a tela
mostra **Sem medição recente** e rotula a medição anterior como histórica.
O aviso de espaço baixo só é calculado para medições atuais, com livre
menor ou igual ao limite. É aviso no painel, sem novo canal de e-mail.
Falha de medição não altera o resultado do backup nem remove dados.

B verifica montagem, dispositivo e marcador de volume antes de medir.
A reserva de espaço necessária à cópia continua valendo; medir não exige
que a reserva esteja disponível, pois essa é justamente a situação que
precisa de aviso. Medida individual não significa espaço recuperável:
packs podem continuar referenciados por outros pontos do repositório.

## Proteções e concorrência

- Ausência de política em cada lado significa nenhuma exclusão naquele lado.
  Quantidade: inteiro positivo até 10000; `true`, zero, negativos,
  fracionários e strings são recusados. Avisos: inteiro positivo em bytes
  ou vazio. IDs fixados: SHA completo, até 100 por lado.
- A só exclui um candidato se houver correspondência confirmada no vínculo
  B **e** existência comprovada no inventário recente de B. Inventários privados
  do par são coletados sob o mesmo lock; têm validade de 90 segundos.
  Fonte, volume, repositório e vínculo devem corresponder ao estado atual.
  Na dúvida, a execução é recusada ou o ponto fica protegido.
- B mantém cópias de pontos ainda publicados em A. Para reduzir pontos em B,
  primeiro remova em A os candidatos copiados e aprovados, aguarde a medição
  e faça outra simulação de B. O ponto de prova, último copiado, ensaio A,
  IDs fixados e pelo menos o mais recente nunca são candidatos.
- A reserva da fila permite só uma operação ativa por instalação. O lock
  de B também é usado pela manutenção de A; a identidade de conta usa o
  lock da ativação. O lock de preparação A também é respeitado.
- Na exclusão em A, o worker suspende temporariamente **somente o listener
  SSH do contêiner exclusivo**, confere ausência de sessões e não aceita
  novas sessões até concluir o recálculo e a ação. Sessões SFTP existentes
  causam recusa; não são mortas para fazer a retenção passar. Tentativas
  novas de backup nesse pequeno intervalo podem aguardar/falhar e devem
  ser reagendadas. Faça a operação fora da janela prevista do cliente.
- `restic forget` usa seu lock exclusivo nativo, sem `--no-lock`, `unlock`,
  `--prune` ou remoção forçada. Backup/restore que mantenha lock no
  repositório causa recusa. Consulte a implementação do
  [Restic 0.18.1](https://github.com/restic/restic/blob/v0.18.1/cmd/restic/cmd_forget.go).
- A execução aplica IDs completos do plano, após recálculo igual ao aprovado.
  Snapshot novo, removido, política ou vínculo mudado bloqueiam a lista antiga.
  Uma simulação pode autorizar solicitação por até 10 minutos; o executor
  ainda confere os inventários antes de qualquer exclusão.
- Antes dos efeitos, há journal privado persistido por operação. Retomada
  após expirar a reserva aceita somente remoções dos candidatos já aprovados;
  não aceita perda dos mantidos nem novos pontos. Falha parcial registra os
  IDs observados como removidos, preserva o restante e exige inspeção/nova
  simulação. Não repete uma operação terminal ao perder sua resposta HTTP.
- Limite conservador deste incremento: 1000 pontos por inventário. Catálogo
  maior recusa manutenção, sem truncar a lista e sem excluir nada. Timeout
  dos comandos A: 100 s mais até 3 s para término dentro do contêiner;
  B: 600 s pelo executor existente. Nenhuma ação inicializa repositório.
- Prune não é oferecido. Remover snapshots remove referências de recuperação;
  não há promessa de espaço liberado. Recuperação de espaço e validação de
  retenção automática operacional exigem entrega/aceite próprios.

Se um executor cair abruptamente durante a manutenção, preserve a área e
inspecione filhos, journal e o listener daquele contêiner antes da retomada.
Não retome um listener por nome ou mate processos de outro laboratório.
O ensaio desta entrega não homologa SIGKILL/queda de energia, namespaces
arbitrários, storage operacional ou as cargas dos clientes.

## Reproduzir as provas

Use banco PostgreSQL com nome contendo `test`, sem dados reais. As fixtures
limpam tabelas desse banco e só seus próprios contêineres/volumes. Obtenha
`MACIP_API_TEST_DATABASE_URL` da preparação descartável, não de `.env` real.
A imagem SFTP deve ser construída de `tests/sftp`. Use o socket Docker local
explícito do laboratório. Execute da raiz central:

```bash
npm --prefix portal ci
npm --prefix portal run typecheck
npm --prefix portal test
npm --prefix portal run build
cd api
PYTHONPATH=src:../src ../.venv-api/bin/python -m pytest \
  tests/test_storage.py tests/test_storage_api.py -q
PYTHONPATH=src:../src ../.venv-api/bin/python -m pytest \
  tests/test_storage_real.py -q
```

O primeiro grupo separa política/locks, PostgreSQL real e Restic **local**
real com falha parcial injetada. O segundo usa o helper público de supervisão,
HTTPS, A/SFTP, B, Mailpit e Restic reais: duas instalações, pontos pendentes,
simulação, exclusão, restore e SHA-256, indisponibilidade A e volume B ausente.
Defina `MACIP_TEST_PROVISIONING_SFTP_IMAGE` para a imagem descartável. Sem
infraestrutura, há skip explícito. `MACIP_TEST_STORAGE_BROWSER=1` acrescenta
Chrome real; não substitui a VM Windows. Fetch dos testes React é simulado.
Nenhum desses testes autoriza usar dados reais ou mudar o Windows congelado.

A reserva de manutenção expira em 30 minutos. Perda abrupta do executor A
pode deixar um comando Docker ainda em término e um marcador durável no
estado B. Esse marcador bloqueia cópia/prova/manutenção do par até inspeção;
não é removido automaticamente para fazer a fila andar. Processos Z/X são
encerrados; processos SSH S/R/T/t continuam contando como vivos. Uma sessão
viva ou parada nunca é ignorada para permitir uma exclusão.

Retorno de versão: código anterior à migração 14 recusa esquema 14. Não
apague tabelas/versões para forçar downgrade. Preserve journal/auditoria e
use atualização corretiva ou restaure um backup consistente exclusivo do
laboratório, considerando quaisquer escritas posteriores. Isso não é um
procedimento autorizado de retorno em central operacional.


O arquivo `docs/lab-automation-pins.json` continua fixando a entrega anterior.
Esta extensão deve usar o HEAD da branch `feature/backup-retention-capacity`
e exige migração 14; não muda os contratos Windows de ativação/resultados v1.
Não regenerar nem substituir o instalador congelado para testar esta central.

### Preparação descartável para a bateria de testes

Em Bash, na raiz desta branch central, com Docker local e a imagem
PostgreSQL 16 já disponível. A imagem pode ser indicada por ID local; não
use um endereço de banco existente. Este bloco não cria serviço global:

```bash
set -euo pipefail
umask 077
unset DOCKER_CONTEXT
export DOCKER_HOST=unix:///var/run/docker.sock
read -r -p 'Imagem PostgreSQL 16 local para o banco descartável: ' PG_IMAGE
RUN_ID="$(python3 -c 'import uuid; print(uuid.uuid4().hex)')"
PG_NAME="macip-retention-test-$RUN_ID"
SFTP_IMAGE="macip-retention-sftp-$RUN_ID"
PG_ID=''
SFTP_CREATED=0
cleanup_retention_test() {
  original=$?
  trap - EXIT
  target="${PG_ID:-$PG_NAME}"
  owner="$(docker inspect -f '{{index .Config.Labels "macip.retention.test"}}' "$target" 2>/dev/null || true)"
  if [ "$owner" = "$RUN_ID" ]; then docker stop "$target" >/dev/null || true; fi
  # Remove só a tag exclusiva; não força remoção de imagens compartilhadas.
  if [ "$SFTP_CREATED" = 1 ]; then docker image rm "$SFTP_IMAGE" >/dev/null 2>&1 || true; fi
  exit "$original"
}
trap cleanup_retention_test EXIT
docker image inspect "$PG_IMAGE" >/dev/null
if docker image inspect "$SFTP_IMAGE" >/dev/null 2>&1; then
  echo "Tag proposta já existe; recusando sobrescrita" >&2
  exit 1
fi
SFTP_CREATED=1
docker build -t "$SFTP_IMAGE" tests/sftp
PG_ID="$(docker run --pull=never --rm -d --name "$PG_NAME" \
  --label "macip.retention.test=$RUN_ID" \
  -e POSTGRES_HOST_AUTH_METHOD=trust -e POSTGRES_DB=macip_retention_test \
  -p 127.0.0.1::5432 "$PG_IMAGE")"
for attempt in $(seq 1 60); do
  if docker exec "$PG_ID" pg_isready -U postgres >/dev/null 2>&1; then break; fi
  sleep 1
done
docker exec "$PG_ID" pg_isready -U postgres >/dev/null
PG_PORT="$(docker port "$PG_ID" 5432/tcp)"
PG_PORT="${PG_PORT##*:}"
export MACIP_API_TEST_DATABASE_URL="postgresql+psycopg://postgres@127.0.0.1:$PG_PORT/macip_retention_test"
export MACIP_TEST_PROVISIONING_SFTP_IMAGE="$SFTP_IMAGE"
# Opcional: requer Chrome instalado; não é uma prova Windows.
export MACIP_TEST_STORAGE_BROWSER=1
CENTRAL="$(pwd -P)"
cd "$CENTRAL/api"
PYTHONPATH=src:../src "$CENTRAL/.venv-api/bin/python" -m pytest \
  tests/test_storage.py tests/test_storage_api.py tests/test_storage_real.py -q
```

Os dados de acesso desse PostgreSQL trust são fictícios, transitórios e
restritos à porta loopback. Nunca replique essa autenticação num host
operacional. Não execute baterias concorrentes contra o mesmo banco,
pois a fixture esvazia suas tabelas entre testes. Sem Chrome, omita a flag
e registre a prova visual como não executada; os testes React continuam
sendo simulações. O bloco pressupõe a venv com `api[test]`, Restic, OpenSSH,
libseccomp e portal já compilado, conforme o roteiro base.
