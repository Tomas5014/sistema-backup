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
- Copia `Backup-MacipCliente.ps1`, `MacipBackup-Gui.ps1`, exemplo de
  perfil e `restic.exe` para `%ProgramFiles%\MACIP-Backup`.
- Cria `%ProgramData%\MacipBackup` (sem espaços — configuração e estado).
- Cria atalho no Menu Iniciar. **A GUI deve ser aberta na conta dedicada
  do backup** — o preparo e a tarefa ficam vinculados a essa conta.
- Desinstalação: desabilita a tarefa `MACIP-Backup` e preserva
  `%ProgramData%\MacipBackup` (configuração, estado e senha protegida).
