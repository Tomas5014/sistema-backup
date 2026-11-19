# Ativação por código · candidata de laboratório

Para a candidata atual, siga [Automação do laboratório](LAB-AUTOMATION.md).
B verifica automaticamente a chave nova, e a GUI registra o envio periódico.
As instruções manuais abaixo permanecem como referência da etapa anterior.

Esta branch parte de `7d62a800325146d1357f8832b1df5c4c3b2eff8d`.
Preserve o instalador Windows 0.1.3 como referência. Para gerar outra
candidata, use uma versão distinta no NSIS, por exemplo 0.1.4-dev1, e
inclua o módulo `Activation-Code.ps1` listado pelo instalador.

Na central, use a branch `feature/backup-activation-code-central` e siga
`docs/activation-code-v1.md`. O código para copiar e colar é
`https://central-do-laboratorio#macip-activation=<segredo>`.
O técnico confere o endereço e instala a CA pública confiável do laboratório
na sua conta. O aplicativo exige HTTPS e recusa redirecionamentos.

Na aba Ativar, cole o código e clique **Usar código de ativação**. A chave
Ed25519 nasce localmente, sob a ACL da conta Windows. A senha Restic e a
credencial aleatória de retomada usam DPAPI. Somente a chave pública sai.
O primeiro clique registra a chave em A e pode mostrar **preparação
pendente**. O operador refaz a prova de B e o técnico clica novamente com o
mesmo código. A tentativa conserva IDs e chave após perda de resposta ou
reinício. **Descartar tentativa** remove só os arquivos locais conhecidos
da tentativa para permitir outro código.

O retorno pronto exige a validação existente da host key SFTP e do ID
Restic. Depois, o app avança para **Pasta e horário**. Salvar mantém a
confirmação nativa da conta Windows e a publicação transacional da tarefa.
O caminho **Importar perfil** continua disponível.

O contrato v1 envia ao resgate `activation_code`, `activation_id`,
`device_id`, `public_key` e `resume_secret` por POST HTTPS. A central
retorna a URL HTTPS de A. O app envia a A apenas o ID e a credencial de
retomada. A retorna HTTP 202/`awaiting_protection` ou versão 1,
`ready`, `profile` e `restic_password`. O segredo nunca vai ao banco
central, ao perfil ou aos logs.

Rode em PowerShell:

```powershell
& .\agent\windows\tests\run-activation-tests.ps1
& .\agent\windows\tests\run-gui-tests.ps1
& .\agent\windows\tests\run-windows-agent-tests.ps1
```

Os testes Linux simulam DPAPI/COM. O ensaio integrado da central usa
PowerShell, HTTPS, OpenSSH e Restic reais. A homologação na VM exige
cliques, DPAPI real, execução agendada sem sessão, restore de arquivo e
pasta e recuperação do ponto Windows por B com A/Windows indisponíveis.
