# Candidata central 0.4.0dev2 — monitoramento de tarefas

Identidade da distribuição composta: `0.4.0dev2`, definida em
`deploy/CANDIDATE`. **Não é versão estável, não é o MVP completo e não
está homologada.** O SHA exato do código vem no `MANIFEST.json`
externo e dentro do pacote, gerados depois do commit — confira sempre
SHA e checksum do pacote, nunca só o nome do arquivo.

Distingue-se do piloto congelado (`ea74d5d`, distribuição `0.4.0dev1`)
por conter o acompanhamento de tarefas na API e no portal. O piloto em
andamento NÃO usa esta candidata.

## Componentes e compatibilidade

| Componente | Versão/contrato | Fonte |
|---|---|---|
| Agente | 0.2.1 | `pyproject.toml` |
| API | 0.1.0 | `api/pyproject.toml` |
| Portal | 0.1.0 | `portal/package.json` |
| PostgreSQL | 17 (imagem 17-alpine) | `deploy/central/compose.yaml` |
| Migrações PG | 1..7 | `api/src/macip_backup_api/migrations.py` |
| SQLite agente | user_version 7 | migrações locais transacionais |
| Contrato de tarefas | 1 | `src/macip_backup/distribution.py` |

**Nenhuma migração nova nesta candidata** — o esquema é o mesmo de
`ea74d5d` (1..7). O monitoramento é leitura calculada sobre sinais já
persistidos; atualização do piloto não exigiria migração, mas isso fica
fora do escopo desta homologação. Confirme o esquema real antes de
qualquer instalação — não presuma.

## Alcance incluído

- Acompanhamento na lista e no detalhe das tarefas do portal.
- Situação operacional derivada da distribuição confirmada
  (`unpublished`, `pending_apply`, `manual`, `scheduled`,
  `pending_suspend`, `suspended`) — nunca do POST administrativo.
- Resultado da ocorrência: `no_history`, `awaiting`, `pending`, `ok`,
  `partial`, `failed`, `missing` — ausência de resultado NÃO é falha
  de backup nem agente offline.
- Agenda avaliada = revisão efetivamente aplicada
  (`last_applied_revision`), com fuso IANA e DST do contrato.
- Último efeito confirmado (`apply`/`suspend`) separado da decisão
  administrativa pendente: suspensão confirmada vigora até retomada
  confirmada; agenda automática aplicada segue acompanhada durante
  publicação/suspensão pendentes; configuração manual nunca anuncia
  timer automático.
- Execuções isoladas por par (task_id, agente dono) — histórico de
  outro agente com mesmo task_id local não contamina.
- Coerência temporal: `started_at_utc <= received_at + 120 s`;
  linhas inválidas são não-evidência permanente para o cálculo
  (histórico preservado; avançar `now` não revalida).
- Leitura por viewer; escrita segue restrita a admin/sessão+CSRF
  existente.

Fora do escopo: notificações externas, veredito de agente offline,
Windows, novos destinos, alterações no piloto.

## Artefato e conferência

- Pacote `macip-backup-linux-0.4.0dev2-<sha12>.tar.gz` + `MANIFEST.json`
  + `SHA256SUMS` no diretório de publicação.
- `sha256sum -c SHA256SUMS` confere pacote e manifesto.
- `MANIFEST.json` traz: candidata, `commit` (SHA completo), versões
  dos componentes, migrações, contratos, documentos de escopo/runbook/
  homologação e hash SHA-256 de cada wheel gerado.
- `deploy/central/VERSION` no pacote carrega SHA+data do commit
  (expansão `git archive`) — `up.sh` recusa instalação sem SHA válido.
- Helpers de laboratório incluídos em `deploy/central/scripts/`:
  - `preflight-lab.sh` — pré-verificação BLOQUEANTE do laboratório
    (porta, projeto/volumes, diretório, daemon, overrides herdados) e
    conferência pós-configuração (porta efetiva = aprovada, `name:` e
    `PROJECT=` aplicados);
  - `verify-identity.sh` — cadeia MANIFEST→label OCI da imagem→
    contêiner em execução; ausência/divergência/falha de consulta
    recusam (arquivo local não é substituto); `db` conferido por
    política explícita de equivalência (`postgres:17[-*]`, com ou
    sem prefixos `library/`/`docker.io/` — `postgres:170`, outras
    majors e outros repositórios recusados);
  - `lab-curl.sh` — chamadas autenticadas com Bearer por stdin
    (`curl -H @-`), TLS verificado, redirects desativados, HTTP≠2xx
    com diagnóstico sem segredos;
  - `up.sh` — além do SHA em VERSION, recusa overrides Compose
    (`COMPOSE_PROJECT_NAME`/`COMPOSE_FILE`/`DOCKER_CONTEXT` no `.env`
    ou herdados divergentes) e sempre compõe com `-p "$PROJECT"`;
  - `portal/e2e/monitoring-fault.spec.ts` — prova C15: aborto
    seletivo SIMULADO só em `/api/v1/monitoring/*`, controle de
    recuperação por resposta 200 real.
  - Imagens `api`/`web` carregam `org.opencontainers.image.revision`
    = SHA da fonte (arg `SOURCE_COMMIT` no build via `up.sh`).
- Rodar o empacotador exige Python ≥3.12 (filtro `data` de tar) e
  setuptools ≥68. Build das imagens ocorre na instalação — sem promessa
  de reprodução byte a byte de imagens ou de downloads.

## Evidências reutilizadas (executadas nas branches de correção, não
## reexecutadas para esta consolidação)

- API/PostgreSQL descartável: 250/250. Monitoramento: 60/60.
- E2E real (Chrome + uvicorn + PostgreSQL): 3/3 — login, lista/detalhe,
  `missing`≠`failed`, explicação de suspensão coerente, recusa do
  viewer provada como RBAC com CSRF válido, controle positivo admin.
- Portal: Vitest 94/94, `tsc -b`, `vite build` aprovados.
- Revisão independente: 42 testes puros aprovados.

Evidência histórica ≠ homologação operacional — esta depende do plano
em [homologacao-monitoramento.md](homologacao-monitoramento.md),
executado pelo operador em laboratório isolado.
