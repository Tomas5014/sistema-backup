# POC: núcleo de execução de backup MAC IP

Primeira entrega para validar execução direta e independente por destino usando
`restic`. O protótipo legado (`backup-full.py`, `backup-diferencial.py`,
`backup-incremental.py`, manifestos e `restore.py`) foi preservado para manter
a restauração no formato anterior. Esta POC não escreve nesse formato.

## Escopo entregue

- Modelo de tarefa: cliente, equipamento, IDs estáveis, origens, exclusões,
  compressão, destinos e retenção declarada por destino.
- Destino local inicializado como repositório restic v2, quando ainda não existe.
- Backup para cada destino selecionado, sem cópia ou cadeia implícita entre eles.
- Histórico por repositório, verificação estrutural e resultado por destino.
- Resultado global `success`, `partial` ou `failed`.
- Logs JSON com UTC, cliente, equipamento, tarefa, execução, destino, tentativa,
  fase, duração, arquivos e bytes quando informados pelo restic.
- Adaptador com `init`, `backup`, `snapshots`, `restore --verify` e `check`;
  comandos são listas de argumentos, nunca shell concatenado.
- Limites configuráveis por operação; timeout encerra e aguarda o processo restic
  antes de registrar falha do destino.
- Testes reais em diretórios temporários: backup, exclusão, lista, restauração,
  `check --read-data` e continuidade após falha de outro destino.

## Premissas e limites

- Ambiente homologado nesta entrega: Linux, Python 3.11+ e restic 0.18.1.
- Foram testados somente repositórios locais temporários. SMB, SFTP, S3/nuvem,
  Windows e service/systemd ainda não foram homologados.
- `retention.keep_last` é armazenado por destino, mas o `forget/prune` não foi
  executado nesta POC. Isso evita apagar pontos restauráveis antes da política.
- A verificação após cada execução é estrutural. O teste também executa
  `check --read-data` e uma restauração real; agendamento dessa verificação
  completa será etapa posterior.
- Senhas nunca entram nos arquivos de configuração, argumentos, resultados ou
  logs. Cada destino lê sua senha da variável indicada em `password_environment`.
- A POC executa destinos em série e não possui fila, cancelamento, repetição,
  banco central, API ou portal.
- Retenção permanece somente declarada. `forget` e `prune` continuam fora desta
  entrega.

## Instalação

Em Debian/Ubuntu sem `venv`, instale antes o pacote correspondente à versão de
Python exibida por `python3 --version`; neste ambiente seria `python3.14-venv`.

```bash
sudo apt install python3.14-venv
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
restic version
```

Não há dependências Python externas. `pyproject.toml` fixa o empacotamento e a
faixa mínima de Python; instale um restic 0.18.x para repetir a validação desta
entrega.

## Configuração e execução

Use [tarefa-um-destino.json](examples/tarefa-um-destino.json) ou
[tarefa-dois-destinos.json](examples/tarefa-dois-destinos.json) como modelo.
Troque as origens e repositórios pelos caminhos de teste. O repositório não pode
ficar dentro de uma origem.

### Formato aceito

`sources`, `exclusions` e `destinations` são sempre listas JSON. Uma string não
é convertida em lista. `sources` e `destinations` exigem ao menos um item;
`exclusions` pode ser `[]`. Cliente, cada destino, `retention` e `timeouts`,
quando presentes, são objetos.

```json
{
  "sources": ["/dados/financeiro"],
  "exclusions": ["*.tmp", "cache/**"],
  "compression": "auto",
  "timeouts": {
    "initialize_seconds": 120,
    "backup_seconds": 14400,
    "snapshots_seconds": 120,
    "check_seconds": 3600,
    "restore_seconds": 14400
  },
  "destinations": [
    {
      "id": "destino-local",
      "name": "Rede local",
      "kind": "local",
      "repository": "/srv/macip-poc/cliente/local",
      "password_environment": "MACIP_RESTIC_PASSWORD_LOCAL",
      "retention": {"keep_last": 14}
    }
  ]
}
```

`retention` pode ser omitido; se existir, deve ser objeto. `keep_last` pode ser
omitido ou `null`, ou ser inteiro positivo. Booleanos, texto, frações, zero e
números negativos são inválidos. A política ainda não remove snapshots.

Os limites acima são padrão, em segundos. Para backups grandes, ajuste somente
o campo necessário no objeto `timeouts`; todos exigem inteiro positivo. O agente
encerra e coleta o processo restic quando qualquer limite expira.

```bash
export MACIP_RESTIC_PASSWORD_LOCAL='use-uma-senha-unica-e-segura'
export MACIP_RESTIC_PASSWORD_MACIP='use-outra-senha-unica-e-segura'
macip-backup-poc validate examples/tarefa-dois-destinos.json
macip-backup-poc run examples/tarefa-dois-destinos.json
```

`run` valida toda configuração antes de inicializar repositórios. Depois executa
cada destino: `init`, `backup`, `check` estrutural e `snapshots`. Eventos JSON
vão para `stderr`; resultado JSON vai para `stdout`. Cada destino informa sucesso
ou falha, incluindo operação e limite em timeout. Resultado global é `success`
quando todos funcionam, `partial` quando ao menos um funciona e outro falha, e
`failed` quando nenhum funciona. Código `0` é sucesso; `2` representa parcial
ou falha. Senhas perdidas tornam dados restic irrecuperáveis: guarde-as em cofre.

## Validação

```bash
# Testes unitários: validação, timeout, estados e credenciais.
PYTHONPATH=src python -m unittest discover -s tests -p 'test_*.py' -v

# Testes reais: restic, dois destinos, restauração, exclusões e compressão.
PYTHONPATH=src python -m unittest discover -s tests -p 'test_integration_restic.py' -v

# Executa a validação real; falha se restic não existir ou nenhum teste rodar.
macip-backup-poc verify-integration
```

Testes reais criam e removem dados temporários; não acessam dados reais. Testes
ignorados não são homologação: `verify-integration` retorna falha sem `restic`.

## Compatibilidade restic verificada

O adaptador foi testado em Linux com restic 0.18.1 e repositórios locais
temporários: formato v2, senha em `RESTIC_PASSWORD`, `--compression auto|off|max`,
`--json`, `snapshots`, `check`, `check --read-data` e `restore --verify` em ambos
os destinos. As capacidades e os comportamentos foram confirmados na
documentação oficial: [inicialização e backends](https://restic.readthedocs.io/en/stable/030_preparing_a_new_repo.html)
e [fluxo de backup, check e restore](https://restic.readthedocs.io/en/stable/010_introduction.html).
S3 e SFTP são suportados pelo restic, mas não são homologados por esta POC.

## Próximas etapas

1. Homologar SMB, SFTP e S3 com TLS, identidade, imutabilidade e credenciais de menor privilégio.
2. Criar serviço do agente: SQLite para política, fila de eventos e retomada após reinicialização.
3. Adicionar agenda com fuso, limites, prevenção de sobreposição, repetição e cancelamento seguro.
4. Criar API FastAPI e PostgreSQL com isolamento obrigatório por cliente, contas, auditoria e MFA.
5. Criar portal React em português, alertas, retenção segura, verificação periódica e restauração controlada.
6. Executar piloto supervisionado com medições de volume, alteração diária, banda e teste de recuperação.
