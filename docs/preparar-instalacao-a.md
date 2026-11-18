# Preparar instalação — entrega do servidor primário A

O primário A usa cadastro, fila, executor fixo e reconciliação idempotente.
O ensaio de cópia e restauração está em [Cópia protegida em B](copia-protegida-b.md).

## Garantias

- Identificador único dentro do cliente; vários clientes podem usar o mesmo.
- Uma operação `pending`/`running` por instalação.
- A migração 9 acrescenta reservas de cinco minutos. Após o prazo, o próximo
  executor pode assumir a mesma operação com outro `claim_id`. Um resultado
  da reserva vencida recebe HTTP 409. A repetição idêntica de um resultado já
  confirmado continua idempotente.
- Retry reutiliza bundle do cofre, conta, chave e repositório.
- Segredo ausente depois do início, identidade alheia ou repository ID
  divergente interrompem o fluxo.
- Perfil interno segue o consumidor Windows 0.1.3 (`7d62a80`), sem senha ou
  chave privada. A forma exportada é
  `sftp://conta@host[:porta]//repository`. O caminho visto pelo SFTP é
  `/repository` dentro do chroot da conta. Em porta diferente de 22, a linha
  `known_hosts` usa `[host]:porta`.
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
MACIP_PROVISIONER_SFTP_PORT=22
MACIP_PROVISIONER_KNOWN_HOSTS=backup.lab ssh-ed25519 <chave pública fictícia>
```

Cada chamada processa no máximo uma operação:

```bash
macip-backup-provisioner
```

O processo não imprime segredo. Em HTTPS, `SSL_CERT_FILE` pode apontar para
a CA pública do laboratório. Para testar com a API local deste roteiro, use
`MACIP_PROVISIONER_API_URL=http://127.0.0.1:8080` e
`MACIP_PROVISIONER_LAB_ALLOW_HTTP=1` no ambiente do executor. A exceção vale
para o host literal `127.0.0.1` e desativa proxies HTTP. O transporte recusa
credenciais na URL e redirecionamentos.

Use a mesma raiz A e o mesmo cofre em todas as retomadas. O executor usa um
lock exclusivo na raiz de A para impedir duas reconciliações no mesmo
laboratório. Ao atualizar do esquema 8, encerre os executores antigos antes
de migrar. Reservas antigas sem prazo ficam disponíveis para nova reserva.
Um perfil legado relativo permanece registrado para auditoria e a exportação
o recusa. Para outro ensaio, prepare uma instalação descartável nova.
O código anterior recusa o esquema 9. Para retorno integral, preserve um
backup pré-migração e considere as escritas posteriores antes de restaurá-lo.

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
../.venv-api/bin/python -m pytest -o tmp_path_retention_policy=none \
  tests/test_provisioning.py tests/test_provisioner.py tests/test_migrations.py -q
```

Para incluir as duas provas do perfil, defina
`MACIP_TEST_PROVISIONING_SFTP_IMAGE` com uma imagem OpenSSH local, por exemplo
uma já construída com `tests/sftp/Dockerfile`. O teste usa porta livre em
`127.0.0.1`, gera chaves temporárias e remove o contêiner ao final. Para o
consumidor Windows, disponibilize `pwsh` no PATH ou indique seu caminho em
`MACIP_TEST_POWERSHELL`. O teste executa o `Read-Perfil` original sobre o JSON
devolvido pela API. A prova PowerShell no Linux cobre o leitor de perfil.
