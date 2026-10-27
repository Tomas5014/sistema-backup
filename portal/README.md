# Portal operacional — MAC IP Backup

Interface web (React + TypeScript + Vite) para a equipe interna MAC IP
acompanhar clientes, agentes e resultados de backup pela API central.
Pacote independente — não compartilha dependências com o agente nem
com a API.

## Escopo desta etapa

- Login/logout com sessão por cookie HttpOnly (o token nunca toca o
  JavaScript — CSRF via cookie `macip_csrf` + cabeçalho `X-CSRF-Token`).
- Restauração da sessão ao abrir (`GET /auth/me`) e revalidação a cada
  navegação em rota protegida.
- Clientes e agentes (última comunicação quando disponível).
- Execuções com filtros (cliente, agente, tarefa, status, período) e
  paginação `limit`/`offset` — a API não devolve total global, então o
  portal não inventa um.
- Detalhe por UUID central com destinos e tentativas.

Fora do escopo: administração de operadores, distribuição de tarefas,
configuração de backups, comandos remotos.

## Requisitos

- Node.js ≥ 20, npm ≥ 10
- API central (`api/`) com PostgreSQL — ver `api/README.md`

## Instalação (reprodutível, pelo lockfile)

```bash
cd portal
npm ci
```

## Desenvolvimento

```bash
# API local rodando em 127.0.0.1:8080 (ver api/README.md)
npm run dev        # Vite em http://127.0.0.1:5173, /api proxied
```

O proxy de `/api` aponta para `MACIP_API_ORIGIN` (default
`http://127.0.0.1:8080`). Em desenvolvimento sem HTTPS a API precisa
da exceção local `MACIP_API_SESSION_INSECURE_COOKIE=1` (documentada em
`api/README.md`) — em operação, HTTPS e cookie `Secure` são a regra.

Operador de teste via bootstrap existente (senha por prompt/stdin):

```bash
.venv-api/bin/python -m macip_backup_api.operator_cli create \
  --login <login> --name "<Nome>" --role admin
```

## Verificação

```bash
npm run typecheck   # tsc estrito
npm test            # vitest — contrato HTTP, sessão, listas, detalhe
npm run build       # tsc -b + vite build → dist/
```

### Prova real em navegador (Playwright + Chrome do sistema)

Sobe API real (uvicorn + PostgreSQL descartável) e `vite preview`
servindo o build com `/api` encaminhado:

```bash
npm run build
MACIP_API_TEST_DATABASE_URL='postgresql+psycopg://<user>:<senha>@<host>:<porta>/<db-teste>' \
  npm run test:e2e
```

Sem `MACIP_API_TEST_DATABASE_URL` os servidores **não sobem** —
a configuração só cria os `webServer` quando a variável existe —
e o teste é ignorado explicitamente (`1 skipped`). Com a variável
presente mas inacessível, a subida da API falha claramente (não vira
skip). O cenário provisiona um operador pelo endpoint
administrativo, autentica pela interface, consulta uma execução com
destinos/tentativas e sai (revogação comprovada).

## Servir em operação

A build é estática (`dist/`). O contrato exige **mesma origem** para
que o cookie HttpOnly funcione: sirva `dist/` e encaminhe `/api/*` à
API pelo mesmo proxy reverso. Exemplo com Nginx:

```nginx
location /api/ { proxy_pass http://127.0.0.1:8080; }
location /     { root /opt/macip-portal/dist; try_files $uri /index.html; }
```

Para homologação local, `vite preview` (ou `npm run preview`) serve
`dist/` e encaminha `/api` conforme `MACIP_API_ORIGIN`.

## Decisões

- **Sem secrets no frontend**: senha só via formulário → corpo JSON →
  esquecida; sessão e Bearer admin nunca passam pelo JavaScript, URL,
  localStorage ou sessionStorage.
- **Erros distinguíveis**: 401 (sessão/credencial), 403 (permissão/
  CSRF), 429 (rate limit), rede/5xx (indisponibilidade) têm mensagens
  diferentes — falha de rede nunca vira "senha errada".
- **Sem dados inventados**: banco vazio mostra estados vazios; campos
  `null` exibem "—", nunca zero.
- **Detalhe usa o UUID central** (`id` da listagem), distinto do
  `execution_id` que o agente envia.
- **Filtros na URL**: voltar do detalhe preserva a busca; respostas
  antigas são descartadas quando filtros mudam rápido.
