# Alertas centrais de laboratório · Windows v1

Esta entrega é experimental. Use clientes fictícios e infraestrutura descartável.
Central e Windows continuam em linhas separadas:
`feature/backup-central-alerts` parte de `0a25d30149ba43beaf598d652831384736eccaef`;
`feature/windows-backup-central-alerts` parte de `1a00be647d55109c31dd503ad4826cf738aabbc7`.
O instalador 0.1.3 permanece referência do fluxo por perfil, sem telemetria nova.

## Credencial e contrato

O código de ativação v1 permanece compatível com seu consumidor anterior.
A candidata Windows desta entrega adiciona um passo após a entrega de A:

| Operação | Regra |
| --- | --- |
| `POST /api/v1/activations/monitoring` | HTTPS, `activation_id` e `resume_secret`. Exige ativação entregue, não revogada, retomada dentro de 24 horas e prova vigente de B. Retorna `contract_version:1`, `installation_id` e `token`, com `Cache-Control: no-store`. |
| `POST /api/v1/windows-results/schedule` | HTTPS + Bearer exclusivo. Envia `previous_schedule_id`, novo `schedule_id`, `schedule` e `paused`. Atualização compara a identidade anterior; replay idêntico é aceito, atualização atrasada é recusada. |
| `POST /api/v1/windows-results/executions` | HTTPS + mesmo Bearer. Envia somente execução UUID, agenda UUID quando aplicável, `trigger`, ocorrência UTC, início/fim com fuso, resultado, motivo enumerado, ID do repositório e snapshot completo quando disponível. |
| `GET /api/v1/installations/{id}/alerts` | Operador autenticado, admin ou viewer. Último backup, última cópia no vínculo atual, prazo e histórico de ocorrências. |
| `POST /api/v1/alerts/{id}/ack` | Admin; exige CSRF para sessão por cookie. Reconhecimento idempotente, com identidade e horário do operador. |

Todas essas rotas exigem `MACIP_API_LAB_ALERTS=1`. A migração **12** é
expansiva. Preserva dados e credenciais anteriores com escopo `agent`.
A credencial nova tem escopo `windows-results`: não lê distribuição,
recuperação, cadastros, alertas ou credenciais. A instalação é derivada do
Bearer; o corpo não aceita cliente/agente livres. Reutiliza o cadastro e a
revogação de credenciais existentes. O token tem 256 bits e somente seu
hash fica no PostgreSQL. A retomada autenticada deriva o mesmo token;
revogar a credencial impede nova entrega dessa credencial.

Não há senha Restic, chave SSH, origem nem nomes de arquivos nesse contrato.
Erros são motivos fixos (`restic_failed`, `incomplete`, `validation_failed`,
`interrupted`), sem enviar stderr do Restic. A execução aparece também no
histórico já existente, na mesma transação, com tarefa lógica
`windows:<installation_id>` e destino `primary-a`. Não há repetição após
reenvio; conteúdo divergente para o mesmo UUID recebe 409.

No Windows, `monitoring.json` guarda a origem HTTPS autorizada no resgate;
`monitoring.dpapi` guarda o Bearer na conta executora. O aplicativo copia
ambos para o conjunto candidato sem substituir a configuração ativa antes
do registro confirmado da tarefa. Transporte usa certificado confiável,
recusa HTTP, credenciais na URL e redirecionamentos. Não desabilite TLS.
Não importe arquivos de monitoramento de outra instalação.

**Importar perfil continua funcionando**, sem exigir vínculo central.
Nesse caso, a aba Situação mostra **Monitoramento não configurado**.
Ativar por código na candidata requer a central atualizada e o modo de
alertas habilitado; falha nessa etapa preserva a configuração anterior.

## Fila e agenda

A fila fica em `monitoring-<installation_id>` na área privada do aplicativo,
fora do perfil público. Cada execução recebe UUID antes do backup. Um
arquivo `.running` e lock de execução protegem o registro em andamento;
a conclusão publica JSON terminal por substituição atômica com flush.
Após abandono, somente um processo que consiga o lock livre converte o
registro em `interrupted`; não inventa sucesso. Isso não é prova de
restauração nem de encerramento de órfãos no Windows.

A execução agendada envia resultados mesmo com a GUI fechada. No fim de
cada backup, a fila tenta transmitir agendas pendentes em ordem e até
50 resultados. A resposta confirmada remove a mensagem; resposta perdida
preserva UUID e conteúdo para reenvio idempotente. Falha de comunicação
não muda o resultado local do backup. Falta de espaço/permissão para
persistir a fila é diagnosticada localmente e impede o aceite do piloto.

Reenvio sem novo backup, na conta executora, com caminhos absolutos do
conjunto instalado conferidos na tarefa:

```powershell
& $AgentScript -Modo Sincronizar -Config $ConfigPath
```

`$AgentScript` e `$ConfigPath` devem ser os caminhos efetivos dessa
instalação, não arquivos de outro laboratório. Nenhum segredo vai em
argumentos. A GUI pode ficar fechada. O próximo backup também reenvia.
Não há promessa de reenvio enquanto a máquina estiver desligada ou sem
novas invocações do agente.

A tarefa nova usa `-Disparo "$(Arg0)"`. O agente reconhece vazio ou o marcador literal
quando não há parâmetro no disparo automático; **Executar backup agora** passa `manual` via COM.
Invocação CLI sem esse marcador é manual. Reconhece também a definição
legada, que permanece manual para monitoramento até ser salva novamente.
Não basta abrir a GUI antiga para transformar uma tarefa já existente.
A confirmação nativa da conta Windows continua obrigatória ao salvar.

Agenda central diária: `kind=daily`, `timezone` IANA, `hour`, `minute`,
`tolerance_seconds` (60–86400; padrão 1800) e `copy_seconds` (60–604800;
padrão 86400). O Windows candidato usa 30 minutos e 24 horas. Nesta fatia,
a conversão local aceita explicitamente UTC e America/Sao_Paulo
(`E. South America Standard Time`); outros fusos são recusados ao preparar
um conjunto monitorado. API reutiliza o cálculo IANA existente, inclusive
salto/ambiguidade de horário de verão.

A agenda passa a ser cobrada depois da confirmação recebida pela central.
Salvar, pausar e retomar geram revisões locais sequenciais, reenviadas em
ordem. Pausa offline ainda não recebida não pode suspender uma cobrança
que a central desconhece: confira a fila e o estado no painel antes do
aceite. Não há sincronização automática de mudanças feitas diretamente
no Agendador fora do aplicativo. Alteração do fuso do sistema exige nova
conferência e publicação da agenda.

## Ocorrências e entrega

- **Backup falhou:** nova execução `failed`, `partial` ou `interrupted`.
  Mantém status e motivo nos detalhes. Sucesso posterior resolve o problema;
  falha antiga recebida fora de ordem permanece no histórico como resolvida.
- **Backup não confirmado no prazo:** ocorrência esperada após a aplicação
  da agenda, sem resultado associado quando termina a tolerância. A central
  calcula mesmo sem contato com o PC. Exige agenda/ocorrência exatas e início
  dentro daquela janela. Resultado antigo, de outra agenda ou manual não
  satisfaz. Recebimento tardio resolve a ocorrência, preservando histórico.
  Pausa confirmada elimina cobrança nova; retomada inicia nova âncora.
- **Cópia protegida atrasada:** snapshot confirmado em A pelo resultado,
  após prazo contado do recebimento central, sem par exato A/B confirmado
  pelo executor B no vínculo atual. Não usa a prova antiga como cópia nova.
  `protected_points` preserva pares idempotentes; mudança de vínculo exige
  novas confirmações. Não inventa inventário retroativo na migração.

O avaliador deve rodar regularmente, independentemente do agente e do
navegador. Cada ocorrência possui chave única. Reconhecimento e resolução
não apagam o histórico. Leitura do painel não dispara e-mail nem avaliação.
A UI mostra quando não há ocorrência registrada sem afirmar que o worker
está ativo.

O canal desta entrega é **somente SMTP em 127.0.0.1** para Mailpit
capturador. Remetente e destinatário fixos usam `example.invalid`.
Não há integração com SMTP operacional nem destinatário configurável.
Estados: `pending`, `sending`, `sent`, `uncertain`. Conexão recusada permite
nova tentativa. Após falha ambígua ou crash durante envio, não repete
automaticamente: confira o Message-ID da ocorrência no Mailpit. Esse limite
impede apresentar entrega ambígua como recebida e evita duplicar avisos.
Escalonamento e recuperação administrativa dessa ambiguidade ficam fora
desta fatia. Reconhecer no painel não equivale a marcar SMTP como enviado.

## Rodar o laboratório

1. Use o roteiro A/B e o caminho de ativação por código com as branches
   desta entrega. O helper `activation-api` habilita os dois modos de
   laboratório. Suba somente os serviços descartáveis da área escolhida.
2. Na raiz central, com `$PY`, `$LAB_TOOL` e `$LAB` definidos conforme o
   roteiro A/B, crie um capturador exclusivo e registre seu ID:

```bash
set -euo pipefail
MAIL_NAME="macip-alerts-$(python3 -c 'import uuid; print(uuid.uuid4().hex[:12])')"
docker run --pull=never --rm -d --name "$MAIL_NAME" \
  -p 127.0.0.1::1025 -p 127.0.0.1::8025 axllent/mailpit:v1.29.7
MAIL_ID="$(docker inspect -f '{{.Id}}' "$MAIL_NAME")"
SMTP_PORT="$(docker port "$MAIL_ID" 1025/tcp | sed 's/.*://')"
WEB_PORT="$(docker port "$MAIL_ID" 8025/tcp | sed 's/.*://')"
printf 'Mailpit local: http://127.0.0.1:%s\n' "$WEB_PORT"
"$PY" "$LAB_TOOL" alerts "$LAB" --smtp-port "$SMTP_PORT"
```

Se a imagem não estiver presente, obtenha-a no laboratório antes de
executar; não use outra infraestrutura para contornar essa verificação.
O comando `alerts` lê a configuração privada existente, mas entrega ao
subprocesso somente a URL do banco e a flag de laboratório, sem tokens A/B.
Para acompanhar janelas durante a prova, em terminal separado execute:

```bash
while "$PY" "$LAB_TOOL" alerts "$LAB" --smtp-port "$SMTP_PORT"; do
  sleep 30
done
```

O loop termina em erro; preserve o diagnóstico. Pare-o com Ctrl+C antes
da limpeza. Não crie timers globais ou unidades do piloto nesta entrega.

3. Ative uma instalação fictícia. Conclua nova prova B após a chave Windows,
   salve a tarefa e confira sua agenda recebida. No painel do cliente,
   monitore os três campos separados e o histórico de execuções.
4. Consulte a seção de aceite do roteiro A/B para provocar falha, ausência
   e atraso da cópia com recursos fictícios. Abra o Mailpit local, confira
   o Message-ID e clique **Reconhecer recebimento** como operador autorizado.
   Não fotografe código, credenciais, senha nem nomes de arquivos reais.
5. Após preservar evidências não sensíveis, pare o loop e remova somente
   o capturador pelo ID registrado. A área A/B tem limpeza própria:

```bash
docker stop "$MAIL_ID"
```

## Validação e retorno

Regressões central: PostgreSQL real, relógio injetado, dois clientes,
concorrência/deduplicação, eventos fora de ordem, manual versus agendado,
pausa/retomada, escopos, CSRF, prova/cópia/vínculo e SMTP real capturado.
`test_activation_real.py` acrescenta envio HTTPS via PowerShell, com
perda de resposta simulada **após aceite real** e verificação de uma só
execução. Sua fixture já exerce OpenSSH, Restic e recuperação de B reais.
DPAPI é simulado nessa prova Linux; não é aceite Windows.

Com banco exclusivamente de testes e Mailpit próprios, execute na pasta API:

```bash
PYTHONPATH=src ../.venv-api/bin/python -m pytest -o tmp_path_retention_policy=none \
  tests/test_central_alerts.py tests/test_activation.py tests/test_activation_real.py \
  tests/test_migrations.py tests/test_monitoring.py tests/test_protected_copy.py -q
```

Defina `MACIP_API_TEST_DATABASE_URL`, `MACIP_TEST_SMTP_PORT`,
`MACIP_TEST_MAILPIT_URL`; a prova HTTPS usa também
`MACIP_TEST_POWERSHELL`, `MACIP_TEST_WINDOWS_WORKTREE` e
`MACIP_TEST_PROVISIONING_SFTP_IMAGE`, conforme os testes A/B.
Infraestrutura ausente gera skip explícito. Não conte skips como aprovação.
Portal: `npm run typecheck`, `npm test`, `npm run build` na pasta portal;
`node e2e/central-alerts.browser.cjs` confere Chrome com API simulada.
Windows no Linux: `run-central-alerts-tests.ps1` (inclui entrada pública,
fixtures POSIX/Python, sem GUI), `run-activation-tests.ps1`, `run-gui-tests.ps1`
e `run-windows-agent-tests.ps1`. Não são prova de DPAPI/COM/WinForms reais.

Antes de retorno da API, não rode código antigo que ignore o escopo das
credenciais novas. Revogue as credenciais `windows-results` e preserve um
backup consistente do banco; avalie escritas posteriores antes de restaurar.
Migração 12 não possui downgrade destrutivo automático. Código anterior
com guarda de migrações recusa esquema futuro. No Windows preserve fila,
configurações, segredos DPAPI e tarefas; não apague resultados para voltar
uma versão. Não converta a referência 0.1.3 em uma instalação monitorada
por mera cópia de arquivos.

Permanecem pendentes compilação/assinatura/hash do instalador candidato,
VM com DPAPI real, cliques, tarefa sem sessão e recuperação do ponto
Windows somente por B. Nenhum dado real foi autorizado por esta entrega.
