# Como gerar o instalador do MAC IP Backup

O instalador é um `.exe` NSIS que instala o agente, a interface e o
`restic.exe` aprovado pela MAC IP. **Não** instala chaves, senhas ou
`backup.json` — a ativação acontece dentro da interface.

## Pré-requisitos do build (Windows)

1. NSIS 3.x — `winget install NSIS.NSIS` ou https://nsis.sourceforge.io
2. `restic.exe` aprovado pela MAC IP em `agent/windows/installer/payload/restic.exe`
   (não versionado — ver `agent/windows/.gitignore`)

## Gerar

```powershell
cd agent\windows\installer
makensis /DVERSAO=0.1.0 macip-backup.nsi
```

Saída: `MacipBackupSetup-0.1.0.exe`. Registre a versão e o hash:

```powershell
Get-FileHash .\MacipBackupSetup-0.1.0.exe -Algorithm SHA256
```

Distribua o `.exe` + o perfil `.json` do cliente + a chave privada +
a senha Restic (chave e senha por canal seguro, nunca junto ao pacote).

## O que o instalador faz

- Verifica o Cliente OpenSSH do Windows e avisa se faltar (instalação
  continua; backup só funciona depois de ativado).
- Copia `Backup-MacipCliente.ps1`, `MacipBackup-Gui.ps1`,
  `Desinstalar-Tarefa.ps1`, exemplo de perfil e `restic.exe` para
  `%ProgramFiles%\MACIP-Backup`.
- Cria `%ProgramData%\MacipBackup` (sem espaços — configuração e estado)
  via `SetShellVarContext all`: `$APPDATA` resolve para `%ProgramData%`,
  **o mesmo caminho que a interface usa** (`$env:ProgramData`). NSIS não
  tem `$PROGRAMDATA` nativo — não use essa variável.
- Cria atalho no **Menu Iniciar comum** (`$SMPROGRAMS` com contexto
  `all`): aparece para a conta dedicada do backup mesmo quando outro
  administrador autorizou a instalação. **A GUI deve ser aberta na conta
  dedicada** — preparo, tarefa e DPAPI ficam nessa conta.
- Permissões: as pastas `config-N`/`estado-N` e a pasta temporária do
  teste de conexão recebem ACL própria na primeira gravação da GUI
  (somente a conta do backup lê/escreve — inclusive a chave privada
  copiada e o `restic.dpapi`). A raiz `%ProgramData%\MacipBackup` fica
  com a herança padrão do ProgramData para que a conta dedicada consiga
  criar suas subpastas.
- Desinstalação segura: o desinstalador chama `Desinstalar-Tarefa.ps1`
  (consulta **estruturada** via `Schedule.Service`/COM — sem `schtasks`,
  XML textual ou `findstr`). O script confirma a propriedade nos campos
  certos — a marca `MACIP Backup` em `RegistrationInfo.Description` **e**
  a ação apontando para a instalação — e devolve um código: `0` tarefa do
  app desabilitada e parada / `2` ausente confirmada → remove; `3`
  homônima alheia → preservada intacta e desinstalação abortada; `4`
  consulta inconclusiva ou falha ao desabilitar → preserva executáveis;
  `5` instância ativa/pendente → preserva executáveis. Para a tarefa do
  app ele **desabilita primeiro** (`Enabled = $false`, impedindo novos
  disparos), encerra instâncias via `Stop()` se necessário e só libera
  com o estado final confirmado. `%ProgramData%\MacipBackup`
  (configuração, estado, resultados, senha protegida) e o repositório
  remoto são sempre preservados.

## Status de compilação

**Instalador ainda não compilado.** Neste ambiente de desenvolvimento
(Linux) não há `makensis` nem o `restic.exe` homologado pela MAC IP —
o binário de teste do repositório **não** é instalador aprovado e nunca
deve ser distribuído como tal. Quando o build acontecer no Windows,
registrar aqui: versão (`/DVERSAO`), comando exato, caminho do `.exe`
gerado e o SHA-256 — um pacote só é homologável com o `restic.exe`
aprovado e a validação da VM documentada no roteiro do piloto.
