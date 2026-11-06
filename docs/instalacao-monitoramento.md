# Runbook — instalação e retorno da candidata 0.4.0dev2 (laboratório)

Instalação em laboratório **isolado**, somente após decisão explícita.
Nada aqui autoriza atualizar o piloto (`ea74d5d`), a central operacional
ou qualquer ambiente existente. Use Bash, usuário dedicado sem root,
`umask 077` e caminhos absolutos. Nenhum segredo em argv, histórico ou
saída. Credenciais abaixo são ILUSTRATIVAS de laboratório.

## Isolamento — propostas a confirmar antes de executar

Os valores a seguir são PROPOSTAS — confirme conflitos no host do
laboratório antes de usar; não estão pré-verificados:

| Recurso | Proposta | Por quê |
|---|---|---|
| Diretório do laboratório | `~/macip-lab-monitoramento` | exclusivo, 0700 |
| Projeto Compose | `macip-monlab` | distinto de `macip-central` e de `macip-recuperacao-ea74d5d` |
| Porta HTTPS | `19443` | distante de 8443/8445/18445 já usados |
| Banco/volume | `pgdata` do próprio projeto | volume nomeado pelo projeto |
| Hostname TLS | `localhost` | SAN gerada por `mk-lab-tls.sh` |
| Identidades de teste | prefixo `lab-` | clientes/agentes/tarefas exclusivos |

O compose traz `name: macip-central` e os scripts fixam
`PROJECT="macip-central"` em `deploy/central/scripts/_lib.sh`. No
laboratório, ajuste **a cópia extraída** (nunca o repositório):
`name:` em `compose.yaml` e `PROJECT` em `_lib.sh` para `macip-monlab`
— o nome do projeto define também o prefixo de volumes e rede.

## 1. Conferência do pacote

```bash
cd '/caminho/publicação-do-pacote'
sha256sum -c SHA256SUMS
python3 -m json.tool MANIFEST.json | head -30
tar -tzf macip-backup-linux-0.4.0dev2-*.tar.gz | head -20
```

Confira `commit` (SHA completo esperado), `candidate=0.4.0dev2`,
versões e documentos referenciados. Reprodução byte a byte de imagens
NÃO é prometida — o build acontece na instalação.

```bash
umask 077
mkdir -p ~/macip-lab-monitoramento
tar -xzf macip-backup-linux-0.4.0dev2-*.tar.gz \
  -C ~/macip-lab-monitoramento
LAB=~/macip-lab-monitoramento/macip-backup-linux-0.4.0dev2
cat "$LAB/source/deploy/central/VERSION"   # SHA+data do commit
```

## 2. Requisitos e preflight

```bash
docker info >/dev/null && docker compose version
python3 --version        # >=3.12 para o empacotador; imagens carregam o resto
openssl version
ss -ltn | grep -w 19443 && echo "PORTA OCUPADA — escolha outra"
docker ps --format '{{.Names}}' | grep -w macip-monlab \
  && echo "projeto já existe neste daemon"
```

## 3. Configuração (sem segredos na saída)

```bash
cd "$LAB/source/deploy/central"
# isolamento do projeto (edição da CÓPIA do laboratório):
sed -i 's/^name: macip-central$/name: macip-monlab/' compose.yaml
sed -i 's/^PROJECT="macip-central"$/PROJECT="macip-monlab"/' scripts/_lib.sh
cp .env.example .env
# edite .env: CENTRAL_HTTPS_PORT=19443 e CENTRAL_HOSTNAME=localhost
bash scripts/gen-secrets.sh        # secrets/*.env 0600 — nunca impressos
bash scripts/mk-lab-tls.sh localhost   # tls/lab-ca.pem + cert.pem/key.pem
bash scripts/up.sh                 # valida .env/secrets/TLS, build + sobe
bash scripts/check.sh              # TLS real + /readyz + portal estático
```

`check.sh` valida cadeia **e hostname** com a CA do laboratório — não
use `-k` como aceite. Para conferir configuração sem segredos, prefira
`docker compose -f compose.yaml config --services` a `config` completo
(que expande valores de ambiente).

## 4. Verificação do que está rodando

```bash
docker compose -f compose.yaml ps --format '{{.Name}} {{.Image}}'
docker compose -f compose.yaml exec -T api \
  python -c "print(open('/app/deploy/central/VERSION').read())" \
  2>/dev/null || cat "$LAB/source/deploy/central/VERSION"
docker compose -f compose.yaml exec -T db \
  psql -U macip_central -d macip_central \
  -c 'SELECT version FROM schema_migrations ORDER BY version'
```

O esquema esperado é **1..7** — igual ao piloto; ausência de migração
nova nesta candidata é fato do código, mas confirme na instalação, não
presuma. `/readyz` já prova acesso real da API ao banco (healthcheck
do Compose depende dele).

## 5. Operador e dados de teste exclusivos

```bash
bash scripts/bootstrap-operator.sh 'lab-operador' 'Operador Lab' admin
# senha por getpass — nada em argv
bash scripts/bootstrap-operator.sh 'lab-viewer' 'Viewer Lab' viewer
```

Portal em `https://localhost:19443` confiando em
`$LAB/source/deploy/central/tls/lab-ca.pem` (importar no navegador ou
`curl --cacert`). Cadastre cliente/agente/credencial com prefixo
`lab-`; guarde o token de agente UMA vez. Tarefas de teste criadas no
portal ou via API admin — identifique-as para remoção no encerramento.

## 6. Registro de recursos criados (para encerramento)

- Projeto Compose `macip-monlab`: containers `macip-monlab-{db,api,web}-1`,
  rede `macip-monlab_central`, volume `macip-monlab_pgdata`.
- Imagens `macip-monlab-{api,web}` construídas localmente.
- Diretório `~/macip-lab-monitoramento`, secrets e `tls/` gerados.
- Operadores `lab-*`, identidades `lab-*` no banco do laboratório.

## 7. Encerramento preservando evidências

```bash
cd "$LAB/source/deploy/central"
bash scripts/down.sh        # preserva o volume pgdata
# evidências: saída de check.sh, gerações de distribuição, capturas do
# portal e respostas da API — copie para pasta de evidências ANTES de
# remover diretório e volume com decisão explícita
```

Remover volume (`docker volume rm macip-monlab_pgdata`) e imagens é
decisão separada — faça só depois de arquivar as evidências.

## 8. Retorno — código/imagens vs. banco

**Retorno de código ≠ restauração de banco.** Trate separadamente:

- **Migrações:** esta candidata não adiciona migração — esquema alvo
  1..7, idêntico ao de `ea74d5d`. Confirme com a consulta da seção 4
  antes e depois de instalar. Se o laboratório partir de um banco
  1..7 válido, o código novo e o antigo leem o mesmo esquema.
- **Retorno de código/imagens:** reconstruir as imagens da linha
  anterior a partir do pacote correspondente e `up.sh` na MESMA cópia
  de configuração. Como não houve migração, reverter imagens não
  desfaz dado nenhum — a troca é compatível nos dois sentidos.
- **Escritas posteriores à instalação são preservadas:** tarefas,
  confirmações e execuções criadas no laboratório continuam válidas
  para o código antigo (mesmo esquema). Não restaure dump antigo sobre
  o banco atual como retorno padrão — isso apagaria dados novos sem
  necessidade.
- **Se algo exigir restauração de banco** (corrupção, não rollback):
  `scripts/backup.sh` antes da operação, `pg_restore -l` para validar
  catálogo e ensaio em PG17 separado — restauração real exige decisão
  registrada; `scripts/restore.sh` pede confirmação e retoma serviços.
- **Não execute retorno nesta tarefa** — este roteiro é preparação;
  a execução pertence à janela de homologação do operador.

## 9. Bloqueios que interrompem o roteiro

Pacote ou manifesto divergente, porta/projeto ocupados sem alternativa
acordada, TLS inválido (cadeia ou hostname), `schema_migrations` fora
de 1..7, `/readyz` falhando, ou qualquer recurso fora do isolamento
proposto. Interrompa e registre — não repita ação destrutiva para
obter verde.
