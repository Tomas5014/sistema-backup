# Monitoramento central Windows · Laboratório v1

A branch `feature/windows-backup-central-alerts` parte da ativação
`1a00be647d55109c31dd503ad4826cf738aabbc7`. Use a central
`feature/backup-central-alerts`, migração 12, com
`MACIP_API_LAB_ALERTS=1`. O instalador 0.1.3 original fica preservado.

Ativação por código registra, depois da entrega de A e da nova prova B,
uma credencial `windows-results` exclusiva. Só o hash está no PostgreSQL;
o Windows protege o token com o mecanismo DPAPI existente, na conta do
backup. `monitoring.json` e `monitoring.dpapi` acompanham cada conjunto
candidato sem mudar o conjunto ativo em caso de erro/cancelamento.
O token não permite baixar tarefas, ler dados ou administrar a central.

A alternativa por perfil continua sem exigir central e mostra
**Monitoramento não configurado**. Não copie credenciais entre clientes.
Configure a confiança na CA da central antes da ativação; não ignore TLS.
O transporte recusa HTTP, URL com credenciais e redirecionamento, usando
a origem HTTPS autorizada capturada no código.

A tarefa nova inclui `-Disparo "$(Arg0)"`. Vazio ou marcador literal indica
disparo automático; **Executar backup agora** passa `manual` via COM.
A tarefa legada continua funcionando e permanece manual para monitoramento
até ser salva novamente. A janela nativa continua pedindo a senha da conta
Windows; essa senha não passa pela central.

A agenda diária é publicada com fuso IANA, revisão, hora e tolerância.
UTC e America/Sao_Paulo são os fusos locais explicitamente suportados no
laboratório Windows. A central reutiliza cálculo IANA/DST; não aceita
resultado manual/antigo como confirmação de janela. Mudança local do fuso
não confirma silenciosamente a agenda antiga: exige nova publicação.
Pausar/retomar pela GUI persiste uma nova revisão para envio; confira sua
confirmação na central. Alterações externas no Agendador não são detectadas.

O agente cria UUID e registro `.running` antes do backup. Lock livre é
necessário para converter abandono em `interrupted`. Registro terminal
usa flush e troca atômica; envio não muda o resultado do backup. A fila
`monitoring-<installation_id>` fica na área privada do aplicativo, fora
do perfil público. Falta de espaço/permissão deve ser resolvida antes
do piloto. Não apague fila ou arquivos DPAPI para contornar erro de envio.

Cada backup tenta reenviar agendas em ordem e até 50 resultados. Para
reenviar sem repetir backup, na conta executora:

```powershell
& $AgentScript -Modo Sincronizar -Config $ConfigPath
```

Use os caminhos absolutos reais registrados na tarefa instalada.
O comando não exige GUI nem senha em argumento. A máquina desligada não
executa reenvio; a cobrança continua no servidor. Conexão recusada ou
resposta perdida preserva o mesmo UUID e conteúdo. A central trata o
reenvio como a mesma execução. Não há envio de nomes de arquivos ou stderr.

No cadastro da instalação no painel, confira Último backup, Última cópia
protegida e Próximo prazo. Os três tipos de alerta ficam no histórico com
estado de envio, reconhecimento pelo operador e resolução. A cópia exige
par exato A/B no vínculo atual; prova seed não confirma snapshot posterior.
A rotina central usa somente SMTP local capturado em Mailpit, sem envio
a destinatários reais. Roteiro completo: `docs/alertas-centrais-laboratorio.md`
na branch central; aceite integrado: `docs/ensaio-windows-a-b.md`.

Testes no Linux:

```powershell
& $Pwsh -NoProfile -File agent/windows/tests/run-central-alerts-tests.ps1
& $Pwsh -NoProfile -File agent/windows/tests/run-activation-tests.ps1
& $Pwsh -NoProfile -File agent/windows/tests/run-gui-tests.ps1
& $Pwsh -NoProfile -File agent/windows/tests/run-windows-agent-tests.ps1
```

O teste de fila usa Python/POSIX para seu executável Restic simulado;
exercita também a entrada pública em processo separado sem GUI. Rede e
DPAPI são explicitamente simulados nas unidades. A integração separada
na central executa HTTPS, OpenSSH e Restic reais com PowerShell no Linux,
sem chamar essa prova de homologação Windows.

Pendente: compilação e hash/assinatura do novo instalador, WinForms,
DPAPI real, registro COM, disparo com sessão encerrada, restauração de
arquivo/pasta e ponto Windows restaurado somente por B. A candidata não
está homologada em VM nem autorizada para dados reais.
