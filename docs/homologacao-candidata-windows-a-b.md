# Homologação integrada da candidata · roteiro único

**Ainda pendente. Não iniciar piloto com dados reais.** Este é o roteiro principal
para aceitar a dupla congelada em [lab-automation-pins.json](lab-automation-pins.json).
A candidata Windows é **0.1.4-dev4-lab**; PostgreSQL **15**; API/portal **0.1.0**;
contratos de ativação/resultados **v1**, atendimento/ZIP **v1**, dados/fila Windows
**v1**. Preserve o aplicativo/instalador 0.1.3 como referência. Não una as linhas Git.

As instruções técnicas antigas do [ensaio A/B](ensaio-windows-a-b.md) são referência
histórica, não um segundo roteiro para executar em paralelo. Use o
[guia curto de operação assistida](operacao-recuperacao-assistidas.md) para os cliques
e o [runbook de atualização](atualizacao-reversao-laboratorio.md) para manutenção.

## Administrador — preparar uma vez

1. Confira commits, matriz, esquema, hashes dos artefatos disponíveis e ausência
   de alterações locais no checkout de build. Não invente hash do instalador.
2. Compile em ambiente Windows de build com NSIS e Restic Windows aprovado,
   seguindo `agent/windows/installer/BUILD.md` da branch Windows fixada. Preserve
   o instalador 0.1.3. Registre comando, payload, versão, assinatura e SHA-256 reais.
3. Prepare área Linux nova, PostgreSQL/volume A/volume B exclusivos e Mailpit,
   conforme [preparação inicial](operacao-automatica-laboratorio.md#administrador--preparar-uma-área-e-iniciar-os-serviços).
   Inclua `--storage-lab` no `init` dessa área nova para capacidade/retenção manual
   (sem política não há exclusão automática). Inicie os cinco processos supervisionados e confira saúde/heartbeats, TLS,
   volume B e espaço. Confirme que a área está marcada como laboratório.
4. Prepare Windows limpo e, para substituição, outro Windows limpo ou uma
   restauração do snapshot da VM **anterior à ativação**. Não clone chave, token,
   diretório de ativação ou DPAPI do PC já configurado para representar o substituto.
5. Defina conta Windows executora, CA confiável, acesso de rede sem depender da
   sessão interativa, fuso, horário, tolerância e responsáveis pelo reconhecimento
   dos avisos. Credenciais reais/destinatários reais são proibidos neste ensaio.
6. Prepare arquivos fictícios conhecidos, inclusive espaços/acentos, um binário,
   um arquivo que depois será alterado/removido e `ignorar.tmp`. Publique referência
   de conteúdo/SHA-256 antes do backup, fora da origem/repositórios, pelo mecanismo
   de ensaio já documentado. Não use a origem atual como referência de um ponto antigo.
7. Registre nomes/identidades de processos, tarefas e recursos próprios antes das
   provas. Não toque em serviços globais, timers/contas anteriores ou srvdev1.

Porta realocada, certificado não confiável, volume errado, versão incompatível,
repositório sem identidade ou recurso homônimo alheio são critérios de parada.
A instalação do payload não está aprovada apenas porque testes Linux passaram.

## Técnico no Windows — fluxo visual

1. No painel, **Novo cliente → Preparar instalação**. Cadastre dois clientes
   fictícios distintos. Confira o bloqueio de perfil/código até a prova vigente B.
   Repita o clique/retome uma falha: a conta e os IDs não podem duplicar.
2. Após liberação, **Gerar código de ativação**. Na VM, **Ativar → Usar código**.
   Confira geração local da chave, TLS, identidade SFTP e ID do repositório.
   A chave nova invalida a autorização anterior e exige nova prova B automática.
   Não use comandos `refresh-b`, `copy-activation` ou `prove-activation` por cliente.
3. Siga para **Pasta e horário**, escolha a origem fictícia, exclua `*.tmp`,
   salve a agenda pela confirmação nativa da conta. Cancele essa confirmação
   uma vez e prove que a configuração/tarefa anterior permanece utilizável.
   A senha Windows não pode aparecer no painel ou nos logs.
4. Pela interface, execute um backup e confira o UUID do novo disparo, snapshot
   e histórico central. Restaure um arquivo e a pasta inteira em locais distintos.
   Confira referência, SHA-256 e ausência da exclusão. Perfil sem monitoramento
   deve continuar funcionando e informar **Monitoramento não configurado**.
5. Confirme pelo menos um disparo com sessão encerrada. Depois, no piloto
   controlado autorizado, registre **sete ocorrências agendadas**: prazo/fuso,
   horário previsto/real, UUID, resultado, snapshot A, cópia B, duração e responsável.
   Execuções manuais ou de testes extras não substituem essas ocorrências.
6. Interrompa só a comunicação HTTPS do laboratório. Backup deve manter seu
   resultado e fila local. Após retorno, a sincronização periódica deve enviar
   os mesmos UUIDs sem novo backup, com interface fechada e sem duplicatas.
7. Provoque e reconheça no Mailpit/painel três ocorrências distintas: **backup
   falhou**, **não confirmado no prazo** e **cópia protegida atrasada**. Confira
   último backup, última cópia e próximo prazo separadamente; prova B antiga
   não confirma o ponto novo. Resolva o problema sem apagar a ocorrência anterior.

## Administrador — substituir e encerrar

1. Pela seção de atendimento, solicite **Substituir computador**. Leia o resumo,
   confirme e registre estados solicitada/confirmada/falhou. A fora do ar ou
   sessão SSH ativa não pode aparecer como revogação já concluída.
2. Teste o token/código antigo: novas ações devem ser recusadas. Reenvie um
   resultado antigo: não pode substituir último backup/agenda do novo computador.
   Histórico antigo deve permanecer identificável por geração.
3. Após confirmação de A, gere código e ative o Windows substituto limpo.
   Nova chave e nova prova B são obrigatórias. Confira mesmos repositórios e
   todos os pontos anteriores; fila, credenciais e pausa do PC antigo não são copiadas.
4. Peça **Encerrar instalação**, com confirmação. Novas ativações devem bloquear;
   histórico, dados A/B e segredos de recuperação devem permanecer. Se A falhar,
   confira acessos pendentes e retome o mesmo atendimento após restaurar a
   infraestrutura original. Revogação não remove dados/segredos já copiados.
5. Exercite **Reabrir instalação** com endereço, chave do servidor, ID e inventário
   originais. Exija nova prova B e nova ativação, sem reativar agenda antiga.
   Uma troca de endereço/identidade, inclusive porta efêmera Docker, deve ser
   recusada; repositório vazio não pode passar como continuidade do anterior.
6. Viewer deve consultar e ser recusado nas ações; um cliente/dispositivo não
   pode atuar nas operações/repositórios do outro. Repetição/perda de resposta
   deve conservar a operação e não executar duas restaurações.

## Recuperação de emergência — sem A

1. Confirme cópia em B do **ID exato** criado pelo Windows, anotando os IDs A/B.
   Guarde o manifesto anterior ao backup e altere/remova um arquivo da origem atual.
2. Na infraestrutura descartável, torne A e a origem Windows inacessíveis.
   Não altere firewall/SSH/contas operacionais. Registre o começo da medição.
3. Pelo painel, **Recuperar usando B → consultar → selecionar ponto → preparar**.
   Não escolha caminho absoluto nem envie comando/credencial de B.
4. Aguarde resultado confirmado; baixe o ZIP pelo painel e importe pelo aplicativo
   em pasta nova. Compare todos os arquivos com a referência histórica, incluindo
   o arquivo removido/alterado, SHA-256 e exclusão. Registre tempo de recuperação.
5. Provoque volume B ausente ou falha de leitura em recurso próprio. B deve
   permanecer íntegro; nenhum arquivo parcial pode ser publicado como sucesso.
   Interrompa uma recuperação controlada: preserve a pasta incompleta e use
   nova consulta/solicitação, sem reutilização silenciosa.
6. Perda da API depois do sucesso local deve reenviar somente o resultado.
   Revogue o download e teste expiração em prova controlada; não significa apagar
   cópias já baixadas. A senha administrativa/de escrita B nunca vai para o Windows.
7. Restauração por B não reconstrói A. Só retome backups após conferir infraestrutura,
   identidade, inventário, vínculos e nova prova/ativação necessárias.

## Atualização, reversão e encerramento da prova

Teste atualização interativa com configuração/fila/DPAPI/chave preservadas e as
pausas de backup/sync mantidas individualmente. Tarefa alheia/ativa e arquivo em
uso devem recusar a troca. Confirme cancelamento/falha/retomada e leitor compatível.
Na Central, faça backup verificável, saúde e preservação das duas instalações.
Esquema 15 recusa leitor 14; não remova tabelas nem altere números de migração.
Restore SQL não recupera snapshots apagados por retenção. Reconcilie antes de
reativar manutenção e não reabra instalações encerradas implicitamente.

Se validar retenção, use somente política manual aprovada, simulação nova e
repositórios próprios. Preserve pontos sem cópia/protegidos. Restaure pontos
mantidos A e B separadamente. **Retenção automática de produção não homologada.**

No fim, pare somente os recursos registrados desta área; não use globs, não
apague evidências/filas/WAL/SHM/snapshots e não mexa no piloto anterior. A confirmação
histórica do operador de timers antigos **disabled/inactive** permanece válida e
não autoriza reativá-los. Guarde apenas evidências sem segredos: commits, versões,
hashes, IDs, horários, estados, capturas, reconhecimentos e responsáveis.

## Resultado e decisão

- **Implementação:** confirmar testes de API, fila, executores e consumidor; registrar
  separadamente componentes reais e simulações.
- **Build:** pendente até existir NSIS/payload Windows aprovado, assinatura e hash reais.
- **Aceite VM:** pendente até cumprir os cliques, DPAPI/COM/UAC/NTFS reais, tarefas sem
  sessão, atualização/retorno e recuperação do ponto Windows somente por B.
- **Produção:** pendem cofre, hosts/volumes separados, TLS/rede, assinatura/distribuição,
  destinatários/responsáveis, capacidade/retenção e dimensionamento representativo.

Falha inexplicada, acesso indevido, perfil liberado antes da prova, recuperação
parcial anunciada como sucesso, aviso sem reconhecimento ou perda de dados impede
aprovação. Infraestrutura ausente é gate pendente. Dados fictícios não representam
volumes de clientes. Não há conclusão do MVP, piloto iniciado, deploy ou aceite de
produção por este documento. Após esta entrega, escopo funcional congelado;
próximo passo é compilar e executar este roteiro na VM.
