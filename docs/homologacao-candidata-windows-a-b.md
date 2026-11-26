# Homologação integrada da candidata · roteiro único

**Ainda pendente. Não iniciar piloto com dados reais.** Este é o roteiro principal
para aceitar a dupla congelada em [lab-automation-pins.json](lab-automation-pins.json).
A candidata Windows é **0.1.4-dev4-lab**; PostgreSQL **16**, migrações **1–15**; API/portal **0.1.0**;
contratos de ativação/resultados **v1**, atendimento/ZIP **v1**, dados/fila Windows
**v1**. Preserve o aplicativo/instalador 0.1.3 como referência. Não una as linhas Git.

As instruções técnicas antigas do [ensaio A/B](ensaio-windows-a-b.md) são referência
histórica, não um segundo roteiro para executar em paralelo. Use o
[guia curto de operação assistida](operacao-recuperacao-assistidas.md) para os cliques
e o [runbook de atualização](atualizacao-reversao-laboratorio.md) para manutenção.

## Retomada desta candidata — ordem única

Bases aprovadas: Central `653b73008214df6ce7a373aee097589b0ca3b8b7` e Windows
`44e9ccf8b3217661084deaf329efb64d56c34969`. Use os **SHAs finais do relatório e
os pins compatíveis**, sem juntar as linhas. O pin Central identifica o commit
funcional; o commit documental final é informado no relatório para evitar SHA
circular. Não regenere referências a partir da instalação desconhecida.

1. **Transferir:** obtenha somente as branches `fix/backup-integrated-candidate`
   e `fix/windows-integrated-candidate` nos clones correspondentes. Confira SHA
   completo, ancestralidade às bases e matriz. Preserve instalador 0.1.3, builds
   anteriores, configurações, DPAPI, tarefas, pausas e journal de recuperação.
   Faça checkout de build novo pelo procedimento Windows abaixo, sem alterar a
   instalação. Não reaplique preparação, init ou migrações de outro roteiro ao
   laboratório existente: não há migração nova nesta correção (migrações PostgreSQL 1–15).
2. **TLS antes de HTTPS:** confira relógios, certificado público realmente servido,
   SAN, emissor, fingerprint e validade. No terminal Linux **do operador**, somente
   para os arquivos públicos e URL autorizados do laboratório:

   ```bash
   (
     set -euo pipefail
     read -r -p 'Certificado publico PEM servido pelo laboratorio: ' CERT
     read -r -p 'CA PEM confiavel do laboratorio: ' CA
     read -r -p 'URL HTTPS autorizada, sem credenciais: ' URL
     [[ -f "$CERT" && -f "$CA" && "$URL" == https://* && "$URL" != *@* ]] || exit 1
     openssl x509 -in "$CERT" -noout -subject -issuer -dates -ext subjectAltName -fingerprint -sha256
     openssl x509 -in "$CERT" -checkend 86400 -noout || {
       printf '%s\n' 'PARAR: certificado vencido ou com menos de 24h. Administrador deve renovar.' >&2
       exit 1
     }
     curl --fail --silent --show-error --cacert "$CA" "${URL%/}/healthz"
   )
   ```

   Compare o fingerprint com o certificado apresentado pelo navegador. O arquivo
   local sozinho não comprova o certificado servido. Se faltar validade para a
   janela, interrompa o ensaio: o administrador deve renovar **só o laboratório**,
   conferir SAN/CA/fingerprint novo, atualizar a confiança nas contas necessárias
   e repetir a validação TLS. Não use `-k`, SkipCertificateCheck, data falsa ou
   aceitação cega de certificado. Não reinicialize repositórios/banco para renovar.
3. **Gate COM e NTFS:** no Windows PowerShell **5.1**, checkout novo do SHA Windows,
   execute `agent/windows/tests/probe-manual-run-com.ps1` pelo bloco completo de
   `agent/windows/installer/MANUAL-RUN.md`. Ele cria/remove somente tarefa sintética
   própria e registra nomes/UUID/SID. Execute também o gate de
   `agent/windows/installer/DIRECTORY-SWAP.md` e `run-status-context-tests.ps1`.
   Recusa/infraestrutura ausente é pendência. A hipótese de marshaling só pode
   ser avaliada pela saída COM nativa; não é conclusão dos testes Linux.
4. **Compilar → atualizar → conferir:** siga **somente**
   `agent/windows/installer/REBUILD-dev4.md` do SHA Windows final. Esse procedimento
   fornece comandos completos para worktree novo (`core.autocrlf=false`, LF),
   exportação e conferência de cada blob Git, payload novo, Restic Windows aprovado,
   referência 0.1.3 `7d62a800325146d1357f8832b1df5c4c3b2eff8d` e NSIS **3.11**.
   Registre hashes reais dos dois manifestos e do EXE após compilar. Atualização
   interativa segue `agent/windows/UPDATE-ROLLBACK.md`: não apague next/previous,
   journal, fila ou DPAPI. Antes de executar, confirme o estado preservado da VM;
   depois confira recibo, módulos, referências efetivas, conta e pausas individuais.
   Atualize a Central **somente por decisão do operador**, no procedimento existente
   de backup e atualização do laboratório, sem encerramento destrutivo. Esta entrega
   não fez deploy. Confira saúde e versão em ambas as pontas antes dos testes.
5. **Repetir os dois defeitos:** salve nova configuração válida sem recriar a tarefa.
   Antes do primeiro disparo, exija “Configuração atual ainda sem execução comprovada”;
   LastRunTime antigo não prova tentativa dela. O último sucesso deve permanecer
   separado. Solicite uma vez **Executar backup agora**: aceite aguarda UUID/resultado
   novo da configuração, recusa não vira sucesso histórico. Confira depois a primeira
   execução agendada atual. Os casos de recusa/resultado atrasado também estão nas
   suítes descartáveis. No painel, use a sessão normal restaurada: consultar e
   **Recuperar usando B → Confirmar atendimento**. Se faltar proteção, use
   **Verificar sessão**; se expirou, **Entrar**. A página deve consultar novamente a
   instalação/geração e exigir nova confirmação. Não repetir ação de resultado
   incerto sem conferir acompanhamento/mesmo identificador. Viewer continua sem
   ações administrativas. Nenhum uso de janela anônima, DevTools ou limpeza manual
   de cookies é parte do fluxo normal.
6. **Preservar regressões operacionais:** repetir backup sem sessão, restore de um
   arquivo/pasta por A e recuperação por B de ponto exato com A pausado/origem
   ausente, comparando referências/SHA-256 e exclusão. Use somente recursos fictícios
   autorizados. Registre pontos A/B, geração, pedido/execução, duração e resultado.
   O operador já comprovou esses fluxos na candidata anterior; a lista abaixo
   continua o aceite integrado mais amplo, não recursos novos.

### Matriz congelada

| Componente | Referência da candidata |
|---|---|
| Central, código funcional | `e9a6769b717e006073409d9d386c8f8c8752604d` |
| Windows | `b841192bc49339e8b5d9e6c71e0b052f318ef6f2` |
| API / portal | 0.1.0 / 0.1.0 |
| Aplicativo Windows | 0.1.4-dev4-lab; referência 0.1.3 preservada |
| PostgreSQL | engine 16 nas provas; migrações 1–15, sem migração nesta correção |
| Ativação / resultados / atendimento / ZIP | v1 / v1 / v1 / v1 |
| Dados / fila / controlador Windows | v1 / v1 / v1; campos locais adicionais de evidência compatíveis |
| Execução das regressões | Python 3.14.4, Node 24.19.0, pwsh 7.4.6 Linux, Restic 0.18.1 Linux, Chrome real |
| Aceite Windows exigido | Windows PowerShell 5.1, NSIS 3.11, Restic Windows previamente aprovado e hash conferido |
| Instalador desta dupla | build nativo e SHA-256 pendentes; não reutilizar EXE de SHA anterior |

SHAs e artefatos finais são informados no relatório; o commit documental não
se referencia por seu próprio SHA. Nenhuma compatibilidade Windows adicional
foi inferida dos testes Linux.

### Evidência e limites desta consolidação

Causas reproduzidas localmente: a base Windows classificava LastRunTime antigo como
`sem_resultado` sem vínculo à configuração; a base Central respondia `/auth/me`
com sessão válida sem repor CSRF ausente. Sessão tinha Max-Age; CSRF era cookie de
sessão. Agora os cookies têm o mesmo prazo configurado, Secure/SameSite=Lax,
Path=/ e sem Domain; só sessão é HttpOnly. `/auth/me` autenticado repõe somente
CSRF ausente, sem prolongar sessão nem rotacionar o token de outras abas, com
`Cache-Control: no-store`. Escrita continua exigindo cookie + cabeçalho.

O frontend interrompe a intenção, oferece revalidação explícita e desmonta a
confirmação antiga; não reenvia POST. Respostas de sessão/visita anterior são
invalidadas; 401, 403 de permissão e recusa CSRF são distintos. Backend de backup,
recuperação B, identidade dos repositórios e contratos não foram afrouxados.

**Histórico do operador, não execução deste agente:** backup agendado sem sessão,
restore de arquivo/pasta por A e ZIP por B com A pausado e origem ausente; dois
SHA-256 conferidos. Ainda não é homologação desta recompilação, produção ou dados
reais. A prova COM nativa, o novo instalador e o aceite integrado seguem pendentes.
O escopo funcional está congelado; novas mudanças somente para defeitos demonstrados.

<details>
<summary>Checklist integrado ampliado (detalhes já previstos, sem novos recursos)</summary>

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

</details>
