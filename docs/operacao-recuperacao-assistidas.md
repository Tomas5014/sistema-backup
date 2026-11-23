# Operação e recuperação assistidas · laboratório

Candidata Windows **0.1.4-dev4-lab**, Central com PostgreSQL **15**. Matriz em
[lab-automation-pins.json](lab-automation-pins.json). Linhas Git separadas,
sem merge. Este procedimento usa somente clientes fictícios e área descartável.
O instalador 0.1.3 e os artefatos anteriores permanecem referências separadas.

O roteiro único de aceite da candidata é
[homologacao-candidata-windows-a-b.md](homologacao-candidata-windows-a-b.md).
Provas Linux/PowerShell não aprovam DPAPI, COM, WinForms ou tarefa sem sessão.
Nenhuma ação descrita aqui foi executada em instalações operacionais.

## Administrador

Prepare uma área nova e inicie os serviços conforme
[operação automática — preparação inicial](operacao-automatica-laboratorio.md#administrador--preparar-uma-área-e-iniciar-os-serviços).
A preparação, HTTPS/CA, conta Windows executora, cofre descartável, volume B,
portas e capturador Mailpit são decisões iniciais do administrador. Não reutilize
áreas congeladas, contas, chaves, volumes ou bancos de outros ensaios.

Numa área nova, `services-start` configura o atendimento automaticamente.
Para habilitar somente a nova capacidade numa área descartável anteriormente
configurada, já atualizada para esta candidata, na raiz Central aprovada:

```bash
set -euo pipefail
umask 077
CENTRAL="$(pwd -P)"
PY="$CENTRAL/.venv-api/bin/python"
TOOL="$CENTRAL/tools/windows-lab-server.py"
read -r -p 'Área absoluta do laboratório descartável: ' LAB
unset DOCKER_CONTEXT
export DOCKER_HOST=unix:///var/run/docker.sock
"$PY" "$TOOL" services-stop "$LAB"
"$PY" "$TOOL" enable-assisted "$LAB"
"$PY" "$TOOL" services-start "$LAB"
"$PY" "$TOOL" services-status "$LAB"
```

`services-stop` pausa somente processos dessa área; **não** use o comando
`stop`, que encerra definitivamente os contêineres efêmeros. O novo comando
não muda credenciais, repositórios ou tarefas. Cria apenas o canal privado de
arquivos de recuperação, com caminho fixo derivado da área. Área anterior sem
habilitação conserva seus fluxos existentes. Não edite configurações internas.

A migração 15 é expansiva: mantém identidades, tokens, histórico e filas,
acrescentando geração do dispositivo, ciclo da instalação e evidências das
operações. O leitor Central 14 não aceita o esquema 15. Use o
[procedimento de atualização/reversão](atualizacao-reversao-laboratorio.md),
backup verificado e artefatos aprovados; não force downgrade. A verificação
compara as colunas anteriores durante expansão e continua recusando alteração
dos seus valores. Restore SQL não recria snapshots apagados por retenção.

### Substituir computador

1. Abra **Clientes → instalação → Operação e recuperação assistidas**.
2. Escolha **Substituir computador**. Confira a instalação e o resumo.
3. Marque a autorização e confirme. Histórico, repositórios e pontos permanecem.
4. Aguarde a confirmação de A. Códigos e tokens centrais antigos já ficam
   revogados; autorização SSH permanece **solicitada** até confirmação do executor.
   Servidor indisponível ou sessão ativa não vira revogação concluída.
5. Em falha, abra os detalhes técnicos e use **Tentar novamente este atendimento**.
   A mesma operação reconcilia o estado; não cria outra conta ou repositório.
6. Com **Aguardando novo computador**, use **Gerar código de ativação** no painel.
   Entregue o código pelo canal privado do laboratório. A nova máquina cria sua
   própria chave. Registrar essa chave exige nova prova B automática antes da
   entrega da configuração/segredo e da liberação.

A operação exige identidades registradas e fila ociosa. Trabalho em andamento
produz recusa clara; aguarde sua conclusão, sem apagar filas/locks ou encerrar
backups pelo terminal. Autorizações do dispositivo anterior são removidas de A;
a leitura restrita de B é preservada. Sessões ainda presentes impedem confirmar
revogação. O listener não é pausado pelo atendimento; a troca de chaves é atômica.

**Revogar acesso não elimina senha, chave ou arquivos já copiados.** A agenda
local do PC antigo também não é apagada remotamente. Se disponível, pause-a
pelo aplicativo e preserve suas evidências. Não transfira a chave, DPAPI ou fila
antigas para o substituto. Gerações antigas ficam no histórico, com rótulos
distintos; seus tokens e resultados atrasados não alteram o estado do novo PC.

### Encerrar ou reabrir

1. Escolha **Encerrar instalação**, leia o resumo e confirme explicitamente.
2. Novas ativações ficam bloqueadas. Códigos/tokens centrais são revogados;
   A deve confirmar a remoção de suas autorizações SSH, inclusive do leitor B.
3. A tela distingue **solicitado**, **não confirmado** e **encerrado**.
   Se A estiver indisponível, existem acessos pendentes. Use a mesma tentativa
   quando a infraestrutura original voltar.
4. Histórico, dados A/B, contas com dados e segredos de recuperação permanecem.
   Não há exclusão definitiva. A recuperação por B continua disponível ao admin.
5. Para voltar, use **Reabrir instalação**. O executor confere conta, repositório,
   inventário, chave do servidor e endereço SFTP registrados. Repositório vazio,
   ID, chave ou endereço divergentes são recusados. Não substitua JSON manualmente.
6. Com a estrutura original conferida, aguarde nova prova B e gere outra
   ativação para o computador. Não há retomada automática da agenda antiga.

Docker pode realocar uma porta efêmera depois de `stop/start`. Isso foi observado
na prova: reabrir nesse endereço diferente deve falhar. O administrador precisa
reconstituir a infraestrutura/endereço aprovados ou planejar outra instalação;
não basta dizer que o novo listener representa continuidade. Recuperar arquivos
por B não reconstrói A. Esta entrega não automatiza reconstrução de servidor.

## Técnico no Windows

1. Instale a candidata aprovada na conta Windows executora.
2. Em **Ativar**, cole o código do computador substituto. Retome a mesma tentativa
   se a prova B ainda estiver pendente; não descarte a chave para acelerar o fluxo.
3. Só depois de validar identidade SFTP e ID do repositório, siga para
   **Pasta e horário**. Confirme a conta na janela nativa do Windows.
4. Cancelamento/falha conserva configuração, fila e pausas individuais anteriores.
   Importar perfil continua como alternativa autorizada; não substitui revogação
   administrativa de um dispositivo antigo.
5. Para arquivos de emergência, abra **Restaurar → Abrir recuperação de B**,
   escolha o ZIP baixado pelo administrador e uma pasta existente fora da origem
   e dos dados do app. Confirme. O aplicativo cria uma subpasta **nova**, confere
   SHA-256 de todos os arquivos e informa o destino. Não ativa agenda ou configuração.

O ZIP não executa scripts e o serviço não injeta credenciais de B. Pacote
alterado, arquivo extra/ausente, colisão de nomes, caminho inválido, link/junction,
local protegido ou pasta já existente são recusados. Falha de escrita preserva
pasta parcial marcada como incompleta; nunca é anunciada como êxito. Não reutilize
essa pasta numa nova tentativa. O comprovante local é para suporte, sem edição.

## Recuperação de emergência — A indisponível

1. No painel da instalação, escolha **Recuperar usando B** e confirme a consulta.
2. Aguarde **consulta concluída**. O catálogo vem do Restic real em B, cruzado
   com os pares A/B confirmados da instalação; histórico SQL sozinho não é catálogo.
   Consulta pendente, lista vazia e falha são estados distintos.
3. Selecione a data/ponto. Os detalhes permitem conferir instalação e IDs A/B,
   que podem ser diferentes. Escolha **Preparar arquivos para recuperação**,
   leia o resumo e confirme.
4. B confere volume, espaço, identidade e ponto; executa `check --read-data`,
   restaura com `--verify` em pasta nova e compara SHA-256 com o conteúdo do ponto.
   Os processos Restic usam o bloqueio de rede do laboratório: não acessam A/Windows.
5. Somente após o resultado confirmado aparece **Baixar arquivos recuperados**.
   O download exige admin autenticado, confere hash, expira em 24 horas e pode ser
   revogado pelo painel. Viewer só consulta. Forneça o ZIP ao técnico por canal
   privado, ou use o navegador da VM autorizada; não entregue senhas/chaves de B.
6. Abra-o pelo aplicativo e confira o resultado. Link revogado/expirado não apaga
   arquivos já baixados. Os dados de B e a pasta privada da operação são preservados.

O transporte de laboratório aceita até **1 GiB**, **1.000 arquivos regulares**, manifesto até **2 MiB**,
sem links/arquivos especiais e sem renomear silenciosamente nomes incompatíveis
com Windows. Limite, volume ausente, ponto removido, hash divergente, manutenção
inconclusiva ou concorrência bloqueiam a entrega. Uma falha não libera ZIP parcial.

O resultado terminal local é durável: reenvio não executa outra restauração.
Após interrupção antes desse resultado, a pasta parcial não é reutilizada:
faça nova consulta/solicitação. A reserva vencida é substituída e resultados
atrasados são recusados. Reiniciar o worker supervisionado libera sua reserva
somente depois de confirmar o encerramento dos filhos; perda da API mantém a
expiração normal, sem apagar a operação. Repetir a confirmação de um mesmo
catálogo/ponto devolve a mesma recuperação, inclusive após perda da resposta.

Arquivos privados/journals/exportações não devem ser anexados a logs públicos
nem entrar em Git/pacote. Não há limpeza automática de dados de recuperação,
contas, snapshots ou segredos. O administrador planeja descarte separado.

## Contratos e gates

Operação assistida v1: admin com CSRF registra UUID, ação fixa e geração esperada;
API responde identificador/estado. Reservas/resultados A e B têm autenticação
separada, contexto imutável e confirmação idempotente. Nenhuma requisição aceita
shell, caminho de servidor, branch, comando Restic ou credencial livre.

Rotas administrativas: `/api/v1/installations/{id}/assisted`, operações `retry`,
`download` e `revoke-export`. Executores: `/api/v1/provisioner/assisted` e
`/api/v1/protector/assisted`, com `claim`, contexto, `result` e `release`.
Ativação/resultados Windows permanecem v1; geração é atribuída pela credencial
servidor, não por um campo livre enviado pelo dispositivo. ZIP de recuperação v1
contém identidades, ponto, data e manifesto SHA-256. Esquema local/fila Windows v1
não mudou. Recuperação não altera pontos ou identidade de B nem usa `init`, `forget`, `prune` ou `unlock`.

Implementação não é aceite operacional. Build NSIS/payload aprovado, DPAPI/COM/
WinForms/UAC, arquivos em uso no NTFS e disparos sem sessão exigem VM. Cofre,
hosts, rede, assinatura, destinatários e volumes de produção continuam decisões
pendentes. O escopo funcional congela aqui para o aceite integrado.
