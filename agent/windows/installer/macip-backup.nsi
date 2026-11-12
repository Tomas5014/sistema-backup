; macip-backup.nsi - instalador NSIS do MAC IP Backup (piloto Windows).
; Gera MacipBackupSetup-<versao>.exe. Compilar no Windows:
;   makensis /DVERSAO=0.1.0 macip-backup.nsi
; ou sem /DVERSAO para usar o padrao abaixo. Saida: MacipBackupSetup-0.1.0.exe
;
; O pacote instala: agente PS1, interface PS1 e o restic.exe APROVADO
; (colocado em payload\ antes de compilar  -  nunca commitado no repo).
; Nao instala chaves, senhas ou backup.json: a ativacao acontece na interface.

!ifndef VERSAO
  !define VERSAO "0.1.0"
!endif
!define NOME "MAC IP Backup"
!define PASTA "$PROGRAMFILES64\MACIP-Backup"
!define DADOS "$PROGRAMDATA\MacipBackup"

Name "${NOME} ${VERSAO}"
OutFile "MacipBackupSetup-${VERSAO}.exe"
Unicode True
RequestExecutionLevel admin          ; instalar em Program Files exige elevacao (UAC nativo)
InstallDir "${PASTA}"
SetCompressor /SOLID lzma

!include "MUI2.nsh"
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_LANGUAGE "PortugueseBR"

Section "Instalar"
  SetOutPath "$INSTDIR"

  ; Pre-requisito: Cliente OpenSSH do Windows (recurso opcional do SO).
  IfFileExists "$WINDIR\System32\OpenSSH\ssh.exe" ssh_ok
    MessageBox MB_ICONEXCLAMATION|MB_OK \
      "Cliente OpenSSH nao encontrado em $WINDIR\System32\OpenSSH.$\n$\nAtive em: Configuracoes > Aplicativos > Recursos opcionais > Adicionar recurso > Cliente OpenSSH.$\nA instalacao continua, mas o backup so funcionara depois disso."
  ssh_ok:

  File "..\Backup-MacipCliente.ps1"
  File "..\gui\MacipBackup-Gui.ps1"
  File "..\gui\perfil.example.json"
  File "payload\restic.exe"          ; binario aprovado pela MAC IP (nao versionado)
  File /nonfatal "payload\instalar-como-conta.txt"

  ; Pasta de dados SEM espacos no caminho (limite do sftp.command do Restic).
  CreateDirectory "${DADOS}"

  ; Atalho: a GUI roda na CONTA que abri-la  -  preparo e tarefa ficam nessa conta.
  CreateDirectory "$SMPROGRAMS\MAC IP Backup"
  CreateShortcut "$SMPROGRAMS\MAC IP Backup\MAC IP Backup.lnk" \
    "$WINDIR\System32\WindowsPowerShell\v1.0\powershell.exe" \
    '-NoProfile -ExecutionPolicy RemoteSigned -File "$INSTDIR\MacipBackup-Gui.ps1"'

  WriteUninstaller "$INSTDIR\desinstalar.exe"
SectionEnd

Section "Uninstall"
  ; Desabilita a tarefa do piloto (nao a remove nem toca no repositorio).
  nsExec::ExecToLog 'schtasks /Change /TN "MACIP-Backup" /DISABLE'
  Delete "$SMPROGRAMS\MAC IP Backup\MAC IP Backup.lnk"
  RMDir "$SMPROGRAMS\MAC IP Backup"
  Delete "$INSTDIR\*.ps1"
  Delete "$INSTDIR\*.json"
  Delete "$INSTDIR\restic.exe"
  Delete "$INSTDIR\desinstalar.exe"
  RMDir "$INSTDIR"
  ; ${DADOS} (backup.json, estado, preparo.json, senha DPAPI) e preservado.
SectionEnd
