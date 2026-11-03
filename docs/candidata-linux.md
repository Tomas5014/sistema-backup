# Candidata Linux 0.3.0rc1 — piloto controlado

Identidade da distribuição composta: `0.3.0rc1`, definida em
`deploy/CANDIDATE`. Não é uma versão estável nem o MVP completo.
O SHA exato do código vem no `MANIFEST.json` externo e dentro do pacote,
gerados **depois** do commit. A versão do agente isoladamente não distingue
esta candidata do piloto antigo: confira sempre SHA e checksum do wheel.

## Componentes e compatibilidade

| Componente | Versão/contrato | Evidência e requisito |
|---|---|---|
| Agente | 0.2.1 | Python >=3.11 declarado; Linux Python 3.14.4 testado |
| API | 0.1.0 | Python >=3.11 declarado; imagem Python 3.13-slim |
| Portal | 0.1.0 | Node >=20 no roteiro; imagem de build Node 22-alpine |
| PostgreSQL | 17 | imagem 17-alpine; migrações 1..6; provas históricas PG17 |
| SQLite agente | user_version 7 | migrações locais transacionais; rejeita esquema futuro |
| Contrato de tarefas | 1 | central e agente devem concordar |
| Restic | 0.18.1 testado | formato de repositório 2 e compressão; versões anteriores não homologadas |
| OpenSSH | 10.2p1 histórico | SFTP com chave e host key estrita |
| systemd | 259 (259.5) testado | --user funcional e systemd-analyze; mínimo numérico não definido |
| Docker/Compose | CLI 29.1.3 / plugin 2.40.3 observados | não comprovam acesso ao daemon nem build desta candidata |
| Node/npm local | 24.19.0 / 11.17.0 observados | build do portal não reexecutado nesta consolidação |
| Empacotamento | Python >=3.12, setuptools >=68 | filtro seguro de tar; setuptools 78.1.1 observado |

Os números de imagens são bases declaradas, não digests imutáveis nem prova
de reprodução byte a byte. As versões patch de Python/Node/Docker usadas
nas provas históricas não foram integralmente coletadas; não inferir.

Execução do agente não exige bibliotecas Python externas, Node ou Docker.
Exige Python com venv, Restic, OpenSSH, flock e sha256sum; agendamento exige
systemd --user. Keyctl é alternativa para segredos; CIFS só para SMB futuro.
API requer FastAPI, uvicorn, SQLAlchemy, psycopg e argon2-cffi, com constraints
em `deploy/central/api-constraints.txt`. Portal executa como estáticos nginx
1.27; Node/TypeScript/Vite são ferramentas de build. Pytest/httpx, unittest,
Vitest/Playwright e Docker dos fixtures são dependências de testes.

## Alcance incluído

- Backup, snapshots, check e restore por CLI; histórico SQLite e outbox.
- Agendamento Linux e sincronização independente.
- Autenticação humana e de agentes; clientes, agentes e credenciais.
- Portal de histórico, cadastro/revisões de tarefas, publicação, aplicação
  automática, confirmação e suspensão.
- Procedimentos de backup e recuperação da central.

A homologação gerenciada de 03/11/2026 usou destino **local** em `469b356`.
SFTP real anterior é evidência separada, descrita no registro histórico;
não comprova o fluxo gerenciado SFTP nesta candidata. Os comandos públicos
de consulta foram validados depois em systemd descartável com `ef3ff75`.
Ver [evidências](homologacao-gerenciamento.md).

Pendências do produto: Windows; restauração pelo portal; alertas; execução
imediata e cancelamento remoto; distribuição de segredos; atualização
automática autenticada; conclusão de SMB e de um provedor S3; permissões
humanas por cliente, MFA e auditoria completa; dimensionamento e volumes
representativos. Provas antigas pontuais SMB/S3 não encerram essas pendências.

## Artefato e conferência

O gerador usa `git archive COMMIT` e uma lista de diretórios versionados;
remove runtime de TLS/backups/caches e recusa arquivos operacionais. Não
inclui working tree, `.claude`, `.trail`, tarefa-teste ou scripts legados da
raiz. Exemplos de secrets são modelos, nunca valores de operação.

```bash
python3 deploy/package-candidate.py COMMIT_EXATO '/caminho/saída nova'
cd '/caminho/saída nova'
sha256sum -c SHA256SUMS
tar -tzf 'macip-backup-linux-0.3.0rc1-SHA12.tar.gz'
```

Antes de extrair, confira SHA completo no manifesto contra a referência
aprovada, versão e origem dos checksums por canal confiável. Checksums não
são assinatura nem autenticação de release. Extraia para diretório vazio
privado; confira manifesto interno e hash do wheel contra o externo.
Pacote contém fontes da central/portal, wheel do agente e runbooks; imagens
da central precisam de build, acesso às dependências e registro de digests.

Siga [instalação, atualização e retorno](instalacao-atualizacao-retorno.md).
