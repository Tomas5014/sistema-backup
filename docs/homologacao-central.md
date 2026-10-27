# Homologação da central MAC IP (HTTPS isolado)

Stack reproduzível da **central** — PostgreSQL + API + portal React —
servida por HTTPS na mesma origem, em `deploy/central/`. Nada é
instalado em servidores do ambiente; a VM `srvdev1` não participa.

## Topologia

```
navegador/agente
      │  https://<host>:${CENTRAL_HTTPS_PORT}   (única porta publicada;
      │                                        loopback por padrão)
      ▼
┌─────────┐   /api/  ┌─────────┐   ┌──────────────┐
│ web     │ ───────▶ │ api     │ ─▶│ db (PG17)    │
│ nginx+  │  estáticos│ uvicorn │   │ volume pgdata│
│ portal  │ ◀──────── │ :8080   │   │              │
└─────────┘          └─────────┘   └──────────────┘
     rede compose "central" — api/db sem portas publicadas
```

- Entrada HTTPS única; sem listener HTTP.
- `pgdata` é o volume operacional — dados de homologação;
  `down` preserva, `down -v` NÃO é usado pelos scripts.
- Cookies `Secure` em HTTPS (esta stack **não** usa
  `MACIP_API_SESSION_INSECURE_COOKIE`).

## Requisitos

- Docker com plugin Compose (ou podman rootless + `DOCKER_HOST`,
  como neste repositório).
- `openssl`, `curl`, `python3`, `node` ≥ 20 só na máquina que executa
  a prova (`prove.sh`) — a build em si é 100% em contêiner.
- `restic` apenas para o fluxo opcional do agente (`MACIP_HOMOLOG_AGENT=1`).
- `certutil` (pacote `libnss3-tools`) só para a confiança do Chrome de
  teste — sem ele a prova de navegador não executa.

## Configuração

```bash
cd deploy/central
cp .env.example .env            # bind, porta, caminhos TLS — não segredo
./scripts/gen-secrets.sh        # secrets/postgres.env + api.env (600,
                                # senha única coerente, nunca impressa)
```

- `CENTRAL_BIND`/`CENTRAL_HTTPS_PORT`: padrão `127.0.0.1:8443` — sem
  80/443, sem expor além do loopback a menos que se mude o bind.
- `CENTRAL_HOSTNAME`: hostname usado na verificação e no SAN do
  certificado (ver abaixo).
- Segredos fora do Git (`.gitignore`) e fora do build do frontend —
  o portal compilado não recebe nenhum valor deste diretório.

### Certificados — dois caminhos

**1. Certificado fornecido pelo administrador** — copie `cert.pem`
(cadeia incluída) e `key.pem` para `deploy/central/tls/`
(ou aponte `CENTRAL_TLS_CERT`/`CENTRAL_TLS_KEY` no `.env`):

```bash
install -m 600 /caminho/servidor.key tls/key.pem
install -m 644 /caminho/servidor.crt tls/cert.pem
```

**2. CA de laboratório** (máquina isolada, sem CA corporativa):

```bash
./scripts/mk-lab-tls.sh <hostname>   # gera tls/{lab-ca,cert,key}.pem
```

O `lab-ca.pem` fica no projeto para **confiança explícita por cliente**
(`curl --cacert`, `NODE_EXTRA_CA_CERTS`, `SSL_CERT_FILE` do agente,
nssdb do navegador de teste). Nada é adicionado à confiança do SO.

> TLS se comprova com verificação de cadeia + hostname
> (`openssl s_client -verify_hostname`, `curl --cacert`).
> `curl -k`/`ignoreHTTPSErrors` não aparecem como prova — servem só de
> diagnóstico e indicam configuração pendente, não sucesso.

## Operação

```bash
./scripts/up.sh        # valida .env/secrets/permissões, builda, sobe
./scripts/check.sh     # containers + TLS real + /readyz + estáticos
./scripts/down.sh      # para — preserva pgdata
```

- `up.sh` falha com mensagem clara se faltar `.env`, `secrets/*.env`,
  certificados, se as permissões forem > 600, se a senha do banco não
  casar entre os envs ou se `MACIP_API_SESSION_INSECURE_COOKIE=1`
  aparecer.
- Projeto Compose explícito: `macip-central` (mesmo nome nos scripts).
- `down.sh` **não** usa `-v` — o volume é dado de homologação.
- `pg_dump`/`pg_restore` só tocam o banco desta stack — nada além do
  projeto é limpo ou alterado.

## Bootstrap do primeiro operador

```bash
./scripts/bootstrap-operator.sh <login> "<Nome Completo>" admin
# senha digitada no prompt (getpass) — nunca por argv nem impressa
```

## Verificação integrada (prova real)

```bash
CERTUTIL=$(command -v certutil) MACIP_HOMOLOG_AGENT=1 ./scripts/prove.sh
```

O orquestrador provisiona operador (CLI), cliente/agente/credencial e
uma execução (Bearer admin + Bearer do agente, tudo via `--cacert`),
executa um **backup real do agente** (`restic` local → outbox → sync
HTTPS, confiança explícita na CA) e dirige o Chrome com HOME
temporário cujo `nssdb` contém só a CA de laboratório. O teste
confirma: login, cookie `HttpOnly`+`Secure`, rotação CSRF, detalhe da
execução, restauração de sessão no reload, revogação no servidor,
cookie local removido e **dados intactos após `compose restart`**.
Sem `certutil`/`restic` a etapa correspondente é informada pendente.

## Backup e restauração do PostgreSQL

```bash
./scripts/backup.sh                       # pg_dump -Fc → backups/*.pgdump
./scripts/restore.sh backups/<arquivo>    # pg_restore --clean --if-exists
```

Homologado neste ambiente: dump → restore no banco da stack preservou
operadores, clientes, agentes e execuções (contagens idênticas).
O restore substitui o conteúdo do banco — exige confirmação "sim".

## Diagnóstico

| Sintoma | Comando |
|---|---|
| Stack saudável? | `./scripts/check.sh` |
| API↔banco | `curl --cacert tls/lab-ca.pem https://$HOST:$PORT/readyz` |
| Logs | `docker compose -p macip-central logs api` |
| Handshake | `openssl s_client -connect $HOST:$PORT -servername $HOST --CAfile tls/lab-ca.pem` |
| Só 443 publicada? | `docker compose -p macip-central ps` — `5432`/`8080` não aparecem |

## Homologado em 27/10/2026

`prove.sh` completo: Chrome + API + PG17 via HTTPS em
`https://localhost:8443` — sessão, CSRF, execuções, detalhe,
revogação, persistência após restart e fluxo do agente descartável —
**1 teste Playwright aprovado**. Recursos temporários do agente
(`/tmp/macip-homolog-*`) removidos; stack permanece sob `docker
compose -p macip-central`, volume `pgdata` preservado.
