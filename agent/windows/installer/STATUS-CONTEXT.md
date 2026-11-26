# Situação da configuração e retomada da candidata

Correção sobre `44e9ccf8b3217661084deaf329efb64d56c34969`, versão
**0.1.4-dev4-lab**. Escopo funcional congelado. Nenhum formato central mudou.

Causa demonstrada por código e reprodução isolada: `Classify-Resultado` aceitava
qualquer `LastRunTime` da tarefa como prova do disparo atual. Atualizar a mesma
tarefa não elimina esse histórico. Configuração nova podia mostrar falha/ausência
de resultado de uma execução anterior à sua publicação.

A publicação agora grava exclusivamente `config-N/situacao.json` com hash e
fronteira UTC antes do registro COM. O agente registra no resultado local caminho,
hash da configuração efetiva, UUID próprio e tipo de disparo. A tela confere essas
referências e o início da execução. Pedidos manuais exigem resultado manual novo;
respostas/resoluções de outra configuração não confirmam o pedido atual.
Para configuração legada sem sidecar, a fronteira é a data de gravação do arquivo;
resultado antigo sem identidade permanece histórico, sem atribuição automática à
configuração atual. Nenhum histórico, credencial, resultado ou fila é apagado.
`ultimo-confirmado.json` continua separado e preserva o último sucesso do repositório.
Sidecar inválido é diagnóstico, não prova de execução.

A chamada `Run([string]'manual')`, tarefa, conta, SID, propriedade, concorrência e
solicitação única de 44e9ccf estão preservados. O helper COM isolado e os testes ZIP
não foram alterados. **Marshaling ainda é hipótese; exige Windows PowerShell 5.1.**

No checkout novo do SHA aprovado, execute (somente recursos temporários):

```powershell
$ErrorActionPreference = 'Stop'
& '.\agent\windows\tests\run-status-context-tests.ps1'
& '.\agent\windows\tests\run-gui-tests.ps1'
& '.\agent\windows\tests\run-windows-agent-tests.ps1'
& '.\agent\windows\tests\run-assisted-tests.ps1'
& '.\agent\windows\tests\run-update-tests.ps1'
```

Siga o roteiro único da Central `docs/homologacao-candidata-windows-a-b.md`:
transferir os SHAs fixados → gate COM descartável → gate NTFS → payload novo de
Git com bytes conferidos → NSIS 3.11 → atualização preservando dados e pausas →
casos de situação/sessão e restauração. Os comandos completos de build estão em
[REBUILD-dev4.md](REBUILD-dev4.md); não use payload anterior nem referências de
arquivos instalados desconhecidos. Hash do instalador só após compilação real.

Aceite nativo: antes da primeira execução da configuração nova, histórico antigo
não aparece como tentativa dela. Depois, primeiro disparo atual é reconhecido.
Recusa manual permanece separada do último sucesso; chamada aceita aguarda prova
nova. Repetir restaurações por A/B e conferir os hashes históricos do laboratório.
A prova já relatada pelo operador (A pausado/origem ausente, dois hashes por B)
permanece válida em seu alcance; não substitui o aceite desta candidata recompilada.
