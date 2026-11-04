# Runbook da candidata Linux 0.3.0rc1

Execução futura somente após decisão explícita sobre máquina e instalação.
Não executar este roteiro na central principal durante a consolidação.
Use Bash, usuário dedicado sem root, `umask 077` e caminhos absolutos.
Os exemplos entre aspas aceitam espaços. Defina os caminhos reais antes de
executar; nenhum segredo deve entrar em argv, histórico ou saída de logs.

## Conferência inicial e instalação nova

1. Verifique `SHA256SUMS`, manifesto e wheel conforme
   [candidata](candidata-linux.md). Extraia em diretório novo privado.
2. Confira requisitos: `python3 --version`, `restic version`,
   `ssh -V`, `systemctl --user is-system-running`, `systemd-analyze --version`.
   Na central confira `docker info` e `docker compose version` com o usuário
   executor. Docker precisa de daemon funcional; versão de CLI não basta.
3. Defina diretórios exclusivos. Não reutilize o projeto Compose
   `macip-central` em paralelo com outra instalação nesse daemon: os scripts
   existentes usam esse nome. Use host/daemon descartável separado para prova.

```bash
umask 077
export MACIP_HOME='/caminho privado/programa'
export MACIP_BIN='/caminho privado/bin'
export MACIP_CONFIG='/caminho privado/config'
export MACIP_STATE='/caminho privado/estado'
export MACIP_CACHE='/caminho privado/cache'
PACKAGE='/caminho/pacote extraído'
COMMIT_EXATO='SHA completo do MANIFEST.json aprovado'
bash "$PACKAGE/source/deploy/install.sh" \
  "$PACKAGE/wheels/macip_backup_poc-0.2.1-py3-none-any.whl" --commit "$COMMIT_EXATO"
"$MACIP_BIN/macip-backup" --version
```

Instalador cria venv por versão+hash, launcher próprio e `current` atômico;
configuração 0700 e agent.env novo 0600. Não inicia tarefas. Não confundir
versão 0.2.1 do componente com distribuição 0.3.0rc1; conferir install.json.

Na central nova: use fontes extraídas, sem misturar configuração antiga.

```bash
cd "$PACKAGE/source/deploy/central"
cp .env.example .env
bash scripts/gen-secrets.sh
# Edite .env e provisione TLS validado. Nunca regenere secrets em atualização.
bash scripts/up.sh
bash scripts/check.sh
bash scripts/bootstrap-operator.sh 'operador' 'Operador do piloto' admin
```

Provisione cadeia/certificado e chave 0600 conforme
[TLS da central](homologacao-central.md). Valide cadeia **e hostname** com
`curl --cacert '/caminho/CA.pem' 'https://hostname:porta/readyz'` e o script
check. Não use `-k` como aceite. O agente usa `SSL_CERT_FILE` quando a CA é
privada; configure também no contexto dos serviços e teste nesse contexto.
Bootstrap solicita senha por getpass. Guarde token administrativo no cofre.

No portal cadastre cliente, agente e credencial; guarde o token de agente
uma única vez no cofre. Crie central.json com URL HTTPS, UUID e **nome** da
variável do token. Provisione token e senha Restic pelo editor em agent.env
0600 ou keyring, sem valores em comandos. Keyring pode não sobreviver reboot.
Para layout customizado, confirme `EnvironmentFile` das units geradas.

### Credenciais na CLI manual

O launcher não lê agent.env. Depois de exportar os diretórios acima, defina:

```bash
manual() {
  "$MACIP_HOME/current/bin/python" -m macip_backup.manual \
    --env-file "$MACIP_CONFIG/agent.env" -- "$@"
}
manual --version
```

A função carrega credenciais e `SSL_CERT_FILE` no processo CLI e seus filhos,
sem depender de segredos exportados. Arquivo: dono atual, modo 0600 ou 0400.
Edite por editor/cofre; não use valores em argv, histórico, substituição de
comando ou `source`.

Formato é um subconjunto literal de EnvironmentFile, não Bash/dotenv: UTF-8,
LF, `NOME=valor` em uma linha; nome ASCII, sem `export` ou espaços em torno
de `=`. Comentários começam com `#` na coluna 1; linhas vazias são aceitas.
Valores não vazios preservam espaços internos, `$`, `#`, `;`, crases e `=`
literalmente. Inclua `SSL_CERT_FILE=/caminho/CA com espaços.pem` se necessário.

Aspas, barras invertidas, CR/NUL, espaços nas pontas, múltiplas linhas,
continuações e nomes duplicados são recusados com número de linha, sem valor.
Diretórios MACIP_HOME/BIN/CONFIG/STATE/CACHE/LOCK_DIR são configurados fora do
arquivo. Para segredos fora desse subconjunto, use keyring por stdin e o
launcher direto, com TLS no ambiente explícito. Não apare quebras de linha.
O parser não aceita toda a sintaxe do systemd; compartilhe somente o
subconjunto acima entre CLI manual e serviços.

Cadastre tarefa local sintética e agenda futura. Publicar é ação explícita
na central; baixar não equivale a aplicar:

```bash
manual pull-check --central "$MACIP_CONFIG/central.json" --store "$MACIP_STATE/agent.db"
manual pull-tasks --central "$MACIP_CONFIG/central.json" --store "$MACIP_STATE/agent.db" --no-activate
# Inspecione staging e confirme ausência de efeitos operacionais.
# Após aprovação explícita:
manual pull-tasks --central "$MACIP_CONFIG/central.json" --store "$MACIP_STATE/agent.db"
manual schedule-pull --central "$MACIP_CONFIG/central.json" --store "$MACIP_STATE/agent.db" --calendar '*-*-* *:10:00' --enable
manual schedule-sync --central "$MACIP_CONFIG/central.json" --store "$MACIP_STATE/agent.db" --calendar '*-*-* *:15:00' --enable
manual pull-status --central "$MACIP_CONFIG/central.json" --store "$MACIP_STATE/agent.db"
manual history --store "$MACIP_STATE/agent.db" --outbox
```

Registre os nomes exatos retornados; consulte só essas units com systemctl.
Confira confirmação no portal, backup pelo timer, outbox sent, check
--read-data, restore em diretório novo e comparação. Linger exige decisão
administrativa explícita. Não converta tarefas locais em gerenciadas nem
use `--rebind` sem adoção explicitamente autorizada.

## Atualização da central

Último estado principal informado: `439e4f2`; laboratório: `469b356`.
São evidências históricas, não detecção da versão instalada. Antes da janela,
registre SHA, imagens e digests reais, configuração Compose e esquema atual.
Não imprima `docker compose config` completo: pode expandir segredos.

```bash
cd '/instalação atual/deploy/central'
dc() { docker compose --project-directory "$PWD" -f "$PWD/compose.yaml" "$@"; }
dc ps
dc images
dc exec -T db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT version,name FROM schema_migrations ORDER BY version"'
```

1. Registre contagens/IDs e amostras de clientes, agentes, execuções,
   credenciais (sem valores), tarefas/revisões/confirmações. Salve digests e
   imagens antigas para retorno; não dependa de tags mutáveis.
2. Pare escritores (`dc stop web api`), confirme parados e bloqueie acesso
   durante a janela. Pare somente timers identificados que escrevam nessa
   central; preserve nomes/estado para retomada. Não remova volumes.
3. Execute `bash scripts/backup.sh '/cofre/dump pré atualização.pgdump'`.
   Valide catálogo com `dc exec -T db pg_restore -l < '/cofre/dump pré atualização.pgdump'`.
   Faça restauração de ensaio em PG17 separado: listar catálogo sozinho não
   prova recuperação. Guarde `.env`, secrets, TLS, Compose e digests em cofre
   0700/0600; nunca dentro do pacote. A rotina central-backup.sh e
   [recuperação da central](backup-central.md) complementam essa custódia.
4. Prepare fontes candidatas em novo caminho; preserve configurações e
   caminhos TLS efetivamente usados. Preserve **o volume e nome do projeto**
   identificados; mudar diretório/projeto pode criar banco vazio. Não rode
   gen-secrets. Faça build antes de migrar e registre novos IDs/digests.
5. Esquema esperado: 1..6. Se estiver em 4, faltam 5 (distribuição) e
   6 (tentativas e último aplicado). Confira todas as migrações pendentes.
   API executa migração transacional com advisory lock na inicialização.
   Com escritores parados e db saudável, execute na configuração candidata:
   `dc run --rm --no-deps api python -c 'from macip_backup_api.db import engine; from macip_backup_api.migrations import migrate; print(migrate(engine()))'`.
   A função dc acima depende do PWD: redefina/verifique caminhos ao mudar
   diretório. Confira esquema antes de `dc up -d api web`.
6. Execute check.sh com TLS, login, leitura das entidades e comparação de
   contagens/IDs. Confira preservação de histórico, revisões e confirmações.
   Só libere escritores/agentes após aceite e registro da janela.

Interrompa diante de dump inválido, volume divergente, migração inesperada,
TLS inválido, readiness falhando ou perda/divergência de dados. Não repita
ações destrutivas para tentar obter verde.

Retorno: antes de liberar escritas, pode restaurar o dump pré-atualização
em PG17 separado, validar e apontar imagens antigas **junto do esquema
compatível**. O código antigo pode rejeitar migrações 5/6. Reverter tag não
desfaz migração. Se já houve escritas novas, pare escritores, preserve dump
do estado novo e liste diferenças. Decida explicitamente entre correção
progressiva ou recuperação com reconciliação/exportação dos dados novos.
Não restaure dump antigo sobre banco atual silenciosamente. restore.sh pede
confirmação e retoma serviços; numa recuperação para código antigo use uma
stack isolada compatível, evitando reabrir API candidata sobre esquema antigo.

## Atualização e retorno do agente

1. Registre launcher, install.json, destino de current, hash do wheel,
   vínculo, units próprias e seus estados. Confirme `--version`. Identifique
   caminhos absolutos de config/SQLite/cache/locks; não presuma defaults.
2. Desative temporariamente **somente** os timers registrados dessa instalação
   e aguarde serviços terminarem. Confira fila e confirmações pendentes.
   Não apague histórico, outbox, journal gerenciado ou repositórios.
3. Sob lock exclusivo de manutenção, crie snapshot consistente do estado.
   O instalador faz backup SQLite online antes da troca, mas isso não inclui
   config, journals, credenciais ou units. Faça custódia privada conjunta
   desses arquivos e do wheel/launcher anterior. Regra única: MACIP_LOCK_DIR
   registrado em install.json prevalece inclusive sem MACIP_LOCK_DIR. Um
   override divergente ou manifesto inválido recusa execução e manutenção.
   Sem registro, use MACIP_LOCK_DIR absoluto, MACIP_STATE/locks ou default.
   A regra é implementada uma vez em macip_backup.lock_identity e usada pelo
   executor, distribuição, entrada manual e scripts. Exporte o
   mesmo layout em backup, instalação e desinstalação. Instalador fixa a
   escolha no launcher e install.json; divergência posterior é recusada.
   Não exclua/substitua maintenance.lock, pois isso mudaria sua identidade.
   Em instalações antigas sem lock_directory no manifesto, layout customizado
   é recusado pelo backup/instalador/desinstalador. Não tente contornar removendo
   o manifesto ou alterando overrides. Esse caso exige procedimento offline
   específico após inventário e parada dos executores, ainda fora deste runbook.
   O layout padrão legado continua suportado. Backup:

```bash
# Se houver override, exporte MACIP_LOCK_DIR absoluto antes da manutenção.
bash "$PACKAGE/source/deploy/backup-agent-db.sh" '/cofre/agent antes.db'
```

4. Instale wheel com deploy/install.sh e --commit como acima. Migração
   SQLite ocorre ao abrir ExecutionStore; candidata suporta até v7 e migra
   anteriores transacionalmente. Esquema futuro é recusado. Confira versão,
   install.json, `manual history`, outbox e `manual pull-status`. Redefina a
   função manual acima em uma nova sessão; ela usa current após a troca e
   carrega também SSL_CERT_FILE.
5. Retome somente timers antes ativos, respeitando suspensão central. Confirme
   comunicação e ausência de duplicação; mantenha tarefas locais e vínculo.

Retorno do agente: se código anterior aceita SQLite v7 e journals/contrato
atuais, reinstale wheel anterior preservando estado. Caso contrário, mantenha
serviços parados e salve estado novo integral antes de decidir recuperação.
Restaurar banco/journals antigos pode perder resultados e reenviar eventos:
reconcilie IDs, outbox e confirmações da janela com a central antes de retomar.
Não substitua agent.db isoladamente com WAL/SHM ativos. Nunca restaure estado
antigo sobre dados novos sem decisão registrada. Repositórios Restic e
credenciais são preservados; não use purge nem altere vínculo no retorno.
