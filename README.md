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

```bash
export MACIP_RESTIC_PASSWORD_LOCAL='use-uma-senha-unica-e-segura'
export MACIP_RESTIC_PASSWORD_MACIP='use-outra-senha-unica-e-segura'
macip-backup-poc validate examples/tarefa-dois-destinos.json
macip-backup-poc run examples/tarefa-dois-destinos.json
```

`run` inicializa cada repositório local que não possui `config`, executa backup,
`check` estrutural e lista os pontos. Eventos JSON vão para `stderr`; o resultado
JSON da execução vai para `stdout`. Código `0` é sucesso; `2` representa parcial
ou falha. Senhas perdidas tornam dados restic irrecuperáveis: guarde-as em um
cofre e documente recuperação de chaves antes do piloto.

## Validação

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
```

O teste cria e remove tudo com `TemporaryDirectory`; não acessa dados reais.

## Compatibilidade restic verificada

O adaptador usa repositório local com senha em `RESTIC_PASSWORD`, formato v2,
`--compression auto|off|max`, `--json`, `snapshots`, `check`, `check --read-data`
e `restore --verify`. As capacidades e os comportamentos foram confirmados na
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
