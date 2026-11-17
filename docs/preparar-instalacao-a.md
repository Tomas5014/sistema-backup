# Preparar instalação — entrega do servidor primário A

Escopo: cadastro, fila, executor fixo, reconciliação idempotente e painel.
Não inclui B, ativação por código, alertas ou infraestrutura de produção.

## Garantias

- Identificador único dentro do cliente; vários clientes podem usar o mesmo.
- Uma operação `pending`/`running` por instalação.
- Retry reutiliza bundle do cofre, conta, chave e repositório.
- Segredo ausente depois do início, identidade alheia ou repository ID
  divergente interrompem o fluxo.
- Perfil interno segue o consumidor Windows 0.1.3 (`7d62a80`), sem senha ou
  chave privada.
- API e botão de download permanecem bloqueados enquanto B estiver pendente.

## Laboratório descartável

O adaptador desta entrega não executa `useradd` e não configura OpenSSH. Ele
materializa uma árvore equivalente privada e usa Restic/ssh-keygen reais.
Não use com dados reais.

Variáveis do executor:

```text
MACIP_PROVISIONER_API_URL=https://central-lab.example
MACIP_PROVISIONER_TOKEN=<token exclusivo, mínimo 32 caracteres>
MACIP_PROVISIONER_ID=primary-a-lab
MACIP_PROVISIONER_LAB_ROOT=/área descartável/primary-a
MACIP_PROVISIONER_LAB_VAULT=/área descartável/vault
MACIP_PROVISIONER_SFTP_HOST=backup.lab
MACIP_PROVISIONER_KNOWN_HOSTS=backup.lab ssh-ed25519 <chave pública fictícia>
```

Cada chamada processa no máximo uma operação:

```bash
macip-backup-provisioner
```

O processo não imprime segredo. `SSL_CERT_FILE` pode apontar a CA pública do
laboratório. HTTP só é aceito em `127.0.0.1`; demais endereços exigem HTTPS.

## Abrir o painel local

Use PostgreSQL de teste cujo nome contenha `test`. Não use banco operacional.

Terminal da API:

```bash
export MACIP_API_DATABASE_URL='postgresql://postgres@127.0.0.1:55439/macip_provisioning_test'
export MACIP_API_ADMIN_TOKEN='<token administrativo de laboratório>'
export MACIP_API_PROVISIONER_TOKEN='<token diferente, mínimo 32 caracteres>'
export MACIP_API_SESSION_INSECURE_COOKIE=1
.venv-api/bin/python -m uvicorn macip_backup_api.app:app --app-dir api/src --host 127.0.0.1 --port 8080
```

Terminal do portal:

```bash
cd portal
MACIP_API_ORIGIN=http://127.0.0.1:8080 npm run dev -- --host 127.0.0.1
```

Abra a URL exibida pelo Vite. Entre com operador criado pelo procedimento já
documentado. Em **Clientes**, use **Novo cliente → Preparar instalação**.

## Testes

```bash
PYTHONPATH=api/src python3 -m unittest api.tests.test_provisioner -v

cd portal
npm run typecheck
npm test
npm run build
```

Com PostgreSQL descartável:

```bash
cd api
PYTHONPATH=src \
MACIP_API_TEST_DATABASE_URL='postgresql+psycopg://postgres@127.0.0.1:55439/macip_provisioning_test' \
../.venv-api/bin/python -m pytest tests/test_provisioning.py tests/test_migrations.py -q
```

## Próxima entrega B

Manter estados distintos:

- `prepared`: repositório B e rotina existem;
- `proven`: ponto fictício copiado, A/Windows indisponíveis durante restore e
  conteúdo comparado.

Somente `proven` poderá liberar o perfil ao operador. A prova usará dados
fictícios e não dependerá de entregar previamente o perfil ao cliente.
