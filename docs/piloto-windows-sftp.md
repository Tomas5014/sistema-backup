# Piloto Windows + SFTP — roteiro do operador

Um operador deve conseguir seguir este roteiro do preparo ao
encerramento sem editar o agente. Escopo: **uma** máquina Windows, **uma**
pasta local não crítica, um repositório SFTP **exclusivo** na MAC IP e
**7 dias** de acompanhamento. Os backups atuais do cliente **permanecem
em operação** — o piloto não substitui nada.

Agente homologado em laboratório (ver seção Evidência): commit
`5a2f3f882fb11ecb33b34e5c63ad70c967d1d027`, pacote
`agente-windows-5a2f3f882fb1.zip`, SHA-256
`1320310ac065b641b21212ab22190a200289534f2348ea115a36954f82f4cff6`.

Status do piloto: **não iniciado — depende da autorização do cliente e
das pendências marcadas abaixo.**

## 1. Ficha do piloto (preencher ANTES da instalação)

| Campo | Valor | Exemplo (fictício) |
|---|---|---|
| Autorização do cliente (quem/data) | ______ | "João Silva, 10/11" |
| Máquina Windows | ______ | "SRV-ARQ-01, Windows Server 2019" |
| Pasta autorizada | ______ | "D:\Documentos\Setor" |
| Volume da pasta | ______ | "D: (disco local, NTFS)" |
| Exclusões aprovadas | ______ | "*.tmp, cache local\*.dat" |
| Horário do backup (HH:mm) | ______ | "02:30" |
| Janela de tolerância | ______ | "até 06:00 do dia seguinte" |
| Retenção pretendida | ______ | "manter tudo durante o piloto" |
| Responsável pelos alertas | ______ | "monitoramento@macip" |
| Meta de recuperação | ______ | "pasta restaurada em < 2h" |

**Regra:** a pasta precisa estar definida e autorizada **antes** da
instalação — o `backup.json` é preenchido com ela.

## 2. Evidência já comprovada em laboratório

Homologação de laboratório do agente `5a2f3f8` — **não é** aceite de
cliente nem validação de produção:

- 11/11/2026, 14h22 (Brasília): o Agendador disparou a tarefa em
  **Windows PowerShell 5.1**, com a sessão encerrada. Agendador e Restic
  retornaram `0`; `ultimo-resultado.json` registrou `status: sucesso`.
- Snapshot: `f0d283eed89cc54322dc22e2d1900c0ef8e3ba30f5ba9d9b41fffb80f3aa823a`.
- `restic check --read-data` no servidor SFTP: terminou **sem erros**.
- Restauração feita **sem depender da VM**: apenas `amostra.txt` (43
  bytes), SHA-256
  `18c4b63c718f062e9da0717912869c9364197fd75242e446021a334b64e3fc56`.
- Origem continha espaço e acento; as exclusões `ignorar.tmp` e
  `cache temporário.log` foram respeitadas (ausentes do snapshot).

## 3. Preparação pela MAC IP (antes da visita)

1. Provisionar repositório SFTP **exclusivo** do piloto (não reutilizar
   o do laboratório nem o de outro cliente): criar a conta SFTP restrita
   ao diretório do piloto, rodar `restic init` remotamente e anotar o
   `repositorio_id` (`restic cat config`).
2. Conferir capacidade (espaço ≥ volume estimado + margem) e
   conectividade (porta SFTP alcançável a partir da rede do cliente).
3. Conferir a **chave do servidor** contra o `known_hosts` que vai no
   kit — a entrega inclui a chave esperada, nunca a relaxada.
4. Custodiar a **senha Restic** no cofre da MAC IP; ela só entra na
   máquina do cliente via `Preparar` (DPAPI), nunca no JSON.
5. Gerar a chave SSH **exclusiva** do piloto e o `backup.json`
   preenchido (repositorio, repositorio_id, caminhos, horário).
6. **Cópia protegida** do repositório, imune a alteração/exclusão pela
   credencial do cliente (ex.: credencial somente-append, snapshot do
   lado do servidor ou sincronização para destino fora do alcance da
   chave do cliente). Exigência de teste: **antes** de usar dados
   reais, provar com dados fictícios que (a) a credencial do cliente
   não altera nem apaga a cópia e (b) uma restauração completa a partir
   dessa cópia funciona.
   ⏳ **Pendência se a infraestrutura ainda não oferecer a cópia
   protegida verificada** — registrar como bloqueio para dados reais.

Kit entregue: `Backup-MacipCliente.ps1`, `backup.json`, `restic.exe`,
chave SSH, `known_hosts`, senha via cofre.

## 4. Instalação no Windows (administrador do cliente)

1. Conferir o pacote:
   `Get-FileHash agente-windows-5a2f3f882fb1.zip -Algorithm SHA256` →
   `1320310ac065b641b21212ab22190a200289534f2348ea115a36954f82f4cff6`.
2. Criar a **conta dedicada** (ex.: `macip-backup`) com leitura na pasta
   e **logon em lote**; criar pastas sem espaço (ex.: `C:\MACIP\agente`,
   `C:\MACIP\id`, `C:\MACIP\estado`).
3. **Operador MAC IP + administrador** preenchem o `backup.json` juntos:
   origem/exclusões/horário do cliente + campos MAC IP.
4. Na sessão da **conta dedicada**:

   ```powershell
   powershell -NoProfile -File C:\MACIP\agente\Backup-MacipCliente.ps1 `
     -Modo Preparar -Config C:\MACIP\agente\backup.json `
     -DiretorioEstado C:\MACIP\estado -ResticExe C:\MACIP\agente\restic.exe
   ```

   Recusas (config, credencial, destino divergente) abortam sem deixar
   pasta; sucesso imprime os campos exatos do Agendador.
5. Cadastrar a tarefa **nova** (nome exclusivo, ex.:
   `MACIP-Backup-Piloto`) com os campos impressos — conta dedicada,
   "executar estando o usuário conectado ou não", "não iniciar nova
   instância". **Primeiro disparo manual único**, sem agendamento:

   ```powershell
   schtasks /Create /TN "MACIP-Backup-Piloto" /SC ONCE /ST 23:59 `
     /RU "macip-backup" /RP * /RL LIMITED `
     /TR "powershell -NoProfile -NonInteractive -File C:\MACIP\agente\Backup-MacipCliente.ps1 -Modo Backup -Config C:\MACIP\agente\backup.json -DiretorioEstado C:\MACIP\estado"
   schtasks /Run /TN "MACIP-Backup-Piloto"
   ```

6. Conferir o disparo de prova **com a sessão encerrada**: horário,
   `LastTaskResult=0`, `ultimo-resultado.json` com `sucesso`, snapshot
   listado no destino (`restic snapshots`), restauração em **pasta
   separada** (`restic restore <id> --target C:\MACIP\restore-teste`)
   conferida por hash e ausência dos excluídos.
7. **Só depois** ajustar o disparador para o horário diário aprovado:
   `schtasks /Change /TN "MACIP-Backup-Piloto" /SC DAILY /ST 02:30`.

## 5. Monitoramento — 7 dias

Ficha de acompanhamento (uma linha por disparo):

| Dia | Esperado | Disparou | Resultado | Snapshot | Duração | Espaço livre destino | Cópia protegida |
|---|---|---|---|---|---|---|---|
| 1–7 | 02:30 | ___ | sucesso/falha | ___ | ___ | ___ | íntegra/verificar |

- **Falha ou incompleto:** `LastTaskResult` ≠ 0 ou `ultimo-resultado.json`
  com `falha`/`codigo_restic=3` → investigar no dia.
- **Ausência de execução:** `inicio_utc` anterior ao disparo esperado ao
  fim da **janela de tolerância** → a tarefa não rodou (Agendador/conta,
  não Restic). Alerta distinto.
- **Alertas MAC IP:** o monitoramento central deve disparar em (a)
  execução com falha e (b) nenhuma execução nova até o fim da janela.
  Procedimento de prova **com dados fictícios antes dos dados reais**:
  forçar `falha` (ex.: origem temporariamente indisponível numa cópia de
  teste) e deixar um disparo não acontecer — confirmar que os dois
  alertas chegam ao responsável da ficha.
  ⏳ **Pendência:** a integração do agente Windows com o monitoramento
  do portal **não está comprovada** — se ainda não houver ingestão do
  `ultimo-resultado.json`, os alertas são verificação manual na ficha;
  registrar como pendência, não como cobertura existente.
- **Snapshots:** todos preservados durante o piloto — sem
  `forget`/`prune`.

## 6. Aceite

Aprovação exige, registrado nesta ficha:

- Evidência dos **7 disparos** (tabela acima completa, sem execução
  ausente ou com explicação aceita).
- Evidência dos **dois alertas** entregues ao responsável.
- Restauração de um **ponto recente** e de um **anterior**; pelo menos
  uma delas a partir da **cópia protegida**, sem depender da máquina
  original — SHA-256 dos arquivos comparado à origem.
- Comparar o tempo de restauração com a **meta de recuperação** da
  ficha; registrar quem aprova (nome/data do cliente + MAC IP).

## 7. Interrupção

Para interromper: desabilitar **somente** a tarefa do piloto —
`Disable-ScheduledTask -TaskName "MACIP-Backup-Piloto"`. **Preservar**
o repositório, os `ultimo-resultado.json`/logs e todos os snapshots —
o encerramento decide o destino dos dados, não o operador de plantão.
