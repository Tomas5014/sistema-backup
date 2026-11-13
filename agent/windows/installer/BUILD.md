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
- Desinstalação segura, nesta ordem: consulta `MACIP-Backup` via
  `schtasks /Query /XML` — **ausência confirmada** ("não encontrado")
  segue; **erro de acesso/consulta** informa a falha e preserva os
  executáveis; tarefa **homônima sem a marca** `MACIP Backup` no XML
  permanece intacta. Para a tarefa do app: `/End` para encerrar
  execução, `/Change /DISABLE` conferido pelo código de retorno e uma
  segunda consulta que rejeita remover arquivos enquanto houver
  execução ativa. `%ProgramData%\MacipBackup` (configuração, estado,
  resultados, senha protegida) e o repositório remoto são sempre
  preservados.

## Status de compilação

**Instalador ainda não compilado.** Neste ambiente de desenvolvimento
(Linux) não há `makensis` nem o `restic.exe` homologado pela MAC IP —
o binário de teste do repositório **não** é instalador aprovado e nunca
deve ser distribuído como tal. Quando o build acontecer no Windows,
registrar aqui: versão (`/DVERSAO`), comando exato, caminho do `.exe`
gerado e o SHA-256 — um pacote só é homologável com o `restic.exe`
aprovado e a validação da VM documentada no roteiro do piloto.
