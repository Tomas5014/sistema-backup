# Runbook — instalação e retorno da candidata 0.4.0dev2 (laboratório)

Instalação em laboratório **isolado**, somente após decisão explícita.
Nada aqui autoriza atualizar o piloto (`ea74d5d`), a central operacional
ou qualquer ambiente existente. Use Bash, usuário dedicado sem root,
`umask 077` e caminhos absolutos. Nenhum segredo em argv, histórico ou
saída — os helpers abaixo transportam credenciais por stdin/arquivos
0600. Credenciais mencionadas são ILUSTRATIVAS de laboratório.

## Isolamento — propostas a confirmar pelo preflight

Valores PROPOSTOS — não estão pré-verificados; `preflight-lab.sh`
confirma ou INTERROMPE o procedimento:

| Recurso | Proposta | Por quê |
|---|---|---|
| Diretório do laboratório | `~/macip-lab-monitoramento` | exclusivo, 0700 |
| Projeto Compose | `macip-monlab` | distinto de `macip-central` e de `macip-recuperacao-ea74d5d` |
| Porta HTTPS | `19443` | distante de 8443/8445/18445 já usados |
| Banco/volume | `pgdata` do próprio projeto | volume nomeado pelo projeto |
| Hostname TLS | `localhost` | SAN gerada por `mk-lab-tls.sh` |
| Identidades de teste | prefixo `lab-` | clientes/agentes/tarefas exclusivos |

O compose traz `name: macip-central` e os scripts fixam
`PROJECT="macip-central"` em `deploy/central/scripts/_lib.sh`. O
laboratório ajusta **a cópia extraída** (nunca o repositório) e o
preflight CONFERE que as substituições aconteceram de fato.

## 1. Conferência do pacote

```bash
cd '/caminho/publicação-do-pacote'
sha256sum -c SHA256SUMS
python3 -m json.tool MANIFEST.json | head -30
tar -tzf macip-backup-linux-0.4.0dev2-*.tar.gz | head -20
```

Confira `commit` (SHA completo esperado), `candidate=0.4.0dev2`,
versões e documentos referenciados. Reprodução byte a byte de imagens
NÃO é prometida — o build acontece na instalação e as imagens carregam
o SHA da fonte como label OCI (ver §4).

## 2. Preflight BLOQUEANTE — antes de extrair

```bash
cd /caminho/onde/o/pacote/será/extraído   # scripts vêm do repositório
# ou apontando para deploy/central/scripts do checkout
umask 077
export LAB_DIR=~/macip-lab-monitoramento \
       LAB_PROJECT=macip-monlab LAB_PORT=19443 LAB_BIND=127.0.0.1
bash /caminho/repo/deploy/central/scripts/preflight-lab.sh
```

`preflight-lab.sh` RECUSA (saída ≠ 0) e interrompe o roteiro quando:

- `docker info`/`docker ps`/`docker volume ls` não respondem — estado
  não verificável nunca conta como "livre";
- existe contêiner (mesmo parado) ou volume do projeto `LAB_PROJECT`;
- `LAB_DIR` já existe — conteúdo preservado, nada é removido;
- a porta `LAB_BIND:LAB_PORT` está ocupada (bind real, não aviso);
- `COMPOSE_PROJECT_NAME`, `COMPOSE_FILE` ou `DOCKER_CONTEXT` estão
  exportados — override herdado mudaria o projeto silenciosamente.

Só depois de `preflight ok`, extraia em diretório criado exclusivo:

```bash
mkdir "$LAB_DIR"    # sem -p: falha se já existir
tar -xzf macip-backup-linux-0.4.0dev2-*.tar.gz -C "$LAB_DIR"
LAB="$LAB_DIR/macip-backup-linux-0.4.0dev2"
```

## 3. Configuração e subida (sem segredos na saída)

```bash
cd "$LAB/source/deploy/central"
# isolamento do projeto — substituições conferidas pelo preflight depois:
sed -i 's/^name: macip-central$/name: '"$LAB_PROJECT"'/' compose.yaml
sed -i 's/^PROJECT="macip-central"$/PROJECT="'"$LAB_PROJECT"'"/' scripts/_lib.sh
cp .env.example .env
# edite .env: CENTRAL_HTTPS_PORT=19443 (idêntico a LAB_PORT) e
# CENTRAL_HOSTNAME=localhost
bash scripts/gen-secrets.sh          # secrets/*.env 0600 — nunca impressos
bash scripts/mk-lab-tls.sh localhost # tls/lab-ca.pem + cert.pem/key.pem
```

`preflight-lab.sh --post-config` CONFERE a cópia antes de subir —
porta configurada igual à aprovada, bind efetivo igual a `LAB_BIND`,
`name:` e `PROJECT=` iguais a `LAB_PROJECT`, `.env` SEM overrides
Compose, projeto/volumes ainda inexistentes e a porta ainda livre.

Execute a sequência encadeada — a recusa do preflight precisa
INTERROMPER o fluxo; comandos soltos continuariam após a recusa:

```bash
LAB_DIR="$LAB_DIR" LAB_PROJECT="$LAB_PROJECT" LAB_PORT="$LAB_PORT" \
  bash scripts/preflight-lab.sh --post-config "$PWD" \
  && bash scripts/up.sh \
  && bash scripts/check.sh
# up.sh: exige VERSION com SHA; recusa COMPOSE_PROJECT_NAME/COMPOSE_FILE/
# DOCKER_CONTEXT no .env ou herdados divergentes; compõe sempre com
# -p "$PROJECT" — o destino aprovado não depende do ambiente
# check.sh: TLS real (cadeia+hostname) + /readyz + estático
```

`check.sh` valida cadeia **e hostname** — não use `-k` como aceite.

## 4. Verificação do que está rodando (cadeia de identidade)

```bash
bash scripts/verify-identity.sh   # MANIFEST padrão: ../../../MANIFEST.json
```

`verify-identity.sh` confere, por serviço (`api`, `web`, `db`):
contêiner encontrado no projeto `LAB_PROJECT`, serviço compose
correto, e — para `api`/`web` — label `org.opencontainers.image.revision`
da imagem em execução **igual ao `commit` do MANIFEST.json**; para
`db`, imagem `postgres:17*`. Identidade ausente, divergente ou não
consultável **falha** — nenhum arquivo local substitui a identidade
em execução. O script só lê labels/imagem — nunca imprime inspect
completo (que pode conter segredos).

O esquema esperado é **1..7** — igual ao piloto; ausência de migração
nova nesta candidata é fato do código, mas confirme na instalação:

```bash
docker compose --project-directory "$PWD" -f compose.yaml exec -T db \
  psql -U macip_central -d macip_central \
  -c 'SELECT version FROM schema_migrations ORDER BY version'
```

`/readyz` (em `check.sh`) prova acesso real da API ao banco.

## 5. Operadores e credenciais — provisionamento sem exposição

```bash
bash scripts/bootstrap-operator.sh 'lab-operador' 'Operador Lab' admin
bash scripts/bootstrap-operator.sh 'lab-viewer'  'Viewer Lab'  viewer
# senhas por getpass — nada em argv
```

Credenciais de API em arquivos 0600 (permissão conferida a cada uso):

- **admin** → `secrets/api.env` já traz `MACIP_API_ADMIN_TOKEN`
  (gerado por `gen-secrets.sh` — nunca exibido).
- **agente** → `secrets/agent-token.env`, criado a partir da resposta
  da criação de credencial — gravado em arquivo, jamais impresso:
- **operadores (portal)** → `secrets/lab-operator.env` com
  `MACIP_HOMOLOG_LOGIN=`/`MACIP_HOMOLOG_PASSWORD=` — usado pelo C15
  dentro de subshell (ver homologacao-monitoramento.md).

```bash
bash scripts/lab-curl.sh admin POST \
  /api/v1/agents/<agent_id>/credentials '{"label":"lab"}' \
  | python3 -c 'import json,sys;print("MACIP_AGENT_TOKEN="+json.load(sys.stdin)["token"])' \
  > secrets/agent-token.env    # umask 077 já garante 600; NUNCA ecoar
```bash
bash scripts/lab-curl.sh admin POST \
  /api/v1/agents/<agent_id>/credentials '{"label":"lab"}' \
  | python3 -c 'import json,sys;print("MACIP_AGENT_TOKEN="+json.load(sys.stdin)["token"])' \
  > secrets/agent-token.env    # umask 077 já garante 600; NUNCA ecoar
umask 077
printf 'MACIP_HOMOLOG_LOGIN=lab-operador\n' > secrets/lab-operator.env
read -rs -p 'senha do lab-operador: ' PW \
  && printf 'MACIP_HOMOLOG_PASSWORD=%s\n' "$PW" >> secrets/lab-operator.env \
  && unset PW                 # digitada em prompt — fora de argv/histórico
```

Chamadas de API nos roteiros usam o helper — token por **stdin**
(`curl -H @-`), TLS sempre verificado, redirects desativados, HTTP ≠2xx
retorna erro com diagnóstico sem segredos:

```bash
bash scripts/lab-curl.sh admin GET  /api/v1/tasks
bash scripts/lab-curl.sh agent POST /api/v1/agent/executions '<json>'
```

Portal em `https://localhost:19443` confiando em `tls/lab-ca.pem`.
Cadastre cliente/agente/tarefas com prefixo `lab-`.

## 6. Registro de recursos criados (para encerramento)

- Projeto Compose `macip-monlab`: containers `macip-monlab-{db,api,web}-1`,
  rede `macip-monlab_central`, volume `macip-monlab_pgdata`.
- Imagens `macip-monlab-{api,web}` (label `image.revision` = SHA).
- Diretório `~/macip-lab-monitoramento`, `secrets/`, `tls/` gerados.
- Operadores `lab-*`, identidades `lab-*` no banco do laboratório.

## 7. Encerramento preservando evidências

```bash
cd "$LAB/source/deploy/central"
bash scripts/down.sh        # preserva o volume pgdata
# evidências: check.sh, verify-identity.sh, gerações, capturas do
# portal e respostas da API — copie ANTES de remover diretório/volume
# com decisão explícita
```

## 8. Retorno — código/imagens vs. banco

**Retorno de código ≠ restauração de banco.** Trate separadamente:

- **Migrações:** esta candidata não adiciona migração — esquema alvo
  1..7, idêntico ao de `ea74d5d`. Confirme com a consulta da seção 4
  antes e depois de instalar; não presuma.
- **Retorno de código/imagens:** reconstruir imagens da linha anterior
  a partir do pacote correspondente e `up.sh` na MESMA configuração.
  Sem migração, reverter imagens não desfaz dados — compatível nos
  dois sentidos; `verify-identity.sh` confirma qual SHA voltou a rodar.
- **Escritas posteriores à instalação são preservadas:** tarefas,
  confirmações e execuções continuam válidas (mesmo esquema). Não
  restaure dump antigo sobre o banco atual como retorno padrão.
- **Se algo exigir restauração de banco** (corrupção, não rollback):
  `scripts/backup.sh` antes, `pg_restore -l` para validar catálogo,
  ensaio em PG17 separado; `scripts/restore.sh` pede confirmação.
- **Não execute retorno nesta tarefa** — pertence à janela do operador.

## 9. Bloqueios que interrompem o roteiro

Pacote ou manifesto divergente, preflight recusado (porta, projeto,
volume, diretório, daemon, override herdado), post-config divergente,
VERSION sem SHA, TLS inválido, identidade de imagem divergente ou
não verificável, `schema_migrations` fora de 1..7, `/readyz` falhando,
ou qualquer recurso fora do isolamento proposto. Interrompa e
registre — não repita ação destrutiva para obter verde.
