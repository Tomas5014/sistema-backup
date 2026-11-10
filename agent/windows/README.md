# Agente de backup Windows — MAC IP (piloto)

Primeira versão reutilizável: **um mesmo script** para qualquer cliente.
Tudo que muda de um cliente para outro está no `backup.json`, preenchido
pela MAC IP. Compatível com **Windows PowerShell 5.1**, Restic e OpenSSH
do Windows. Sem integração com o portal, sem retenção automática, sem
SMB/VSS/backup de aplicações, sem instalador — esses ficam fora desta
entrega de propósito.

Arquivos desta pasta:

- `Backup-MacipCliente.ps1` — o agente (modos `Preparar` e `Backup`).
- `backup.example.json` — modelo de configuração **sem segredos**.
- `tests/` — regressões (stub de restic; nada real é acessado).

## backup.json

Campos ajustados por cliente (exemplo em `backup.example.json`):

| Campo | Dono | Regra |
|---|---|---|
| `origem` | operador | pasta local **absoluta**, existente e legível |
| `exclusoes` | operador | lista de padrões; `[]` = nenhuma exclusão implícita; cada padrão vira um `--exclude` separado |
| `repositorio` | operador | `sftp:usuario@servidor[:porta]/caminho` |
| `horario` | operador | `HH:mm`; mudar exige atualizar o disparador no Agendador |
| `repositorio_id` | MAC IP | ID Restic do repositório (64 hex); o backup é **recusado** se o destino divergir |
| `identidade_ssh` | MAC IP | caminho absoluto da chave privada exclusiva deste cliente |
| `known_hosts` | MAC IP | caminho absoluto do `known_hosts` aprovado |

A senha Restic **nunca** vai no JSON: é digitada uma vez no `Preparar` e
gravada protegida por DPAPI (só abre na mesma conta Windows da mesma
máquina) em `restic.dpapi`, dentro do DiretorioEstado com ACL privada.

Limite real: `DiretorioEstado` **não pode conter espaços** (o
`sftp.command` do Restic não cita caminhos). Origem e exclusões podem
ter espaços e acentos normalmente.

## Quem faz o quê

**MAC IP (antes da visita):** provisiona o repositório SFTP (`init`
remoto), entrega `restic.exe`, a chave `*_ed25519` exclusiva, o
`known_hosts` aprovado, a senha Restic pelo cofre e o `backup.json`
preenchido (host/usuário/caminho, `repositorio_id`, horário).

**Administrador Windows (no cliente):**

1. Crie a conta dedicada (ex.: `macip-backup`) com **leitura na origem**
   e direito de **logon em lote** (SecPol → Atribuição de direitos →
   "Fazer logon como um trabalho em lotes").
2. Copie `Backup-MacipCliente.ps1`, `backup.json`, `restic.exe`,
   `identidade_ssh` e `known_hosts` para pastas **sem espaços**
   (ex.: `C:\MACIP\agente\`, `C:\MACIP\id\`).
3. Abra uma sessão **na conta dedicada** e execute o preparo:

   ```powershell
   powershell -NoProfile -File C:\MACIP\agente\Backup-MacipCliente.ps1 `
     -Modo Preparar -Config C:\MACIP\agente\backup.json `
     -DiretorioEstado C:\MACIP\estado -ResticExe C:\MACIP\agente\restic.exe
   ```

   O preparo valida a configuração, os executáveis, a chave e o
   `known_hosts`; digita a senha uma vez (oculta); confirma que o
   repositório **existe e tem o ID aprovado** — nunca cria nem
   reinicializa repositório. Ao final imprime **os campos exatos do
   Agendador** (programa, argumentos absolutos, conta e horário).
4. Cadastre a tarefa com os campos impressos — programa
   `powershell.exe`, argumentos `-NoProfile -NonInteractive
   -ExecutionPolicy Bypass -File "...ps1" -Modo Backup -Config "..."
   -DiretorioEstado "..."`, disparador **diário no `horario`**, conta
   dedicada, opção **"executar estando o usuário conectado ou não"**
   e **"não iniciar nova instância"** (impede sobreposição). O
   `-ExecutionPolicy Bypass` vale só para aquele processo e não altera
   a política do Windows; se a organização preferir, remova-o e ajuste
   `RemoteSigned` na conta — decidido pelo administrador, não pelo
   script.

   Equivalente por linha de comando (confira com os campos impressos):

   ```powershell
   schtasks /Create /TN "MACIP-Backup-Restic" /SC DAILY /ST 02:30 `
     /RU "macip-backup" /RP * /RL LIMITED /F `
     /TR "powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File C:\MACIP\agente\Backup-MacipCliente.ps1 -Modo Backup -Config C:\MACIP\agente\backup.json -DiretorioEstado C:\MACIP\estado"
   ```

## Verificar

- `Get-ScheduledTaskInfo "MACIP-Backup-Restic"` → `LastTaskResult`
  (`0` = sucesso; `1`, `3`… = falha).
- `DiretorioEstado\ultimo-resultado.json` — `inicio_utc`, `fim_utc`,
  `conta`, `status` (`em_execucao`/`sucesso`/`falha`), `codigo_restic`
  e `snapshot`. **Falha de execução vs. sem execução nova:** confira
  `inicio_utc` — se o horário é anterior ao disparo esperado, a tarefa
  não rodou (problema de Agendador/conta, não de backup). O Restic só
  gera `status: sucesso` com código `0` **e** snapshot válido; código
  `3` (leitura incompleta) retorna erro ao Agendador.
- Localizar snapshots: `restic -r <repositorio> snapshots` com o mesmo
  `ssh_config` (a senha sai de `restic.dpapi` — comando no cofre).
- Restaurar em **pasta separada** (nunca sobre a origem):
  `restic -r <repo> restore <snapshot_id> --target C:\MACIP\restore-teste`.

## Interromper o piloto

Desabilite **somente** a tarefa do piloto —
`Disable-ScheduledTask -TaskName "MACIP-Backup-Restic"`. **Preserve os
snapshots**: não apague o repositório nem rode `forget`/`prune` durante
o piloto.

## Testes e limites da prova

`tests/run-windows-agent-tests.ps1` (pwsh ou PS 5.1) cobre: validação
de `backup.json`, exclusões vazia/preenchida/com espaço e acento,
destino divergente recusado **antes** do `restic backup`, códigos
0/1/3, senha ausente de argv/logs/resultado, duas configurações com o
mesmo script e os campos impressos do Agendador. Fora do Windows a
fronteira DPAPI é substituída — a prova real com DPAPI + Agendador +
sessão encerrada + SFTP pertence à VM Windows descartável (pendente
nesta entrega).
