; macip-backup.nsi - instalador NSIS do MAC IP Backup (piloto Windows).
; Gera MacipBackupSetup-<versao>.exe. Compilar no Windows:
;   makensis /DVERSAO=0.1.0 macip-backup.nsi
; ou sem /DVERSAO para usar o padrao abaixo. Saida: MacipBackupSetup-0.1.0.exe
;
; O pacote instala: agente PS1, interface PS1 e o restic.exe APROVADO
; (colocado em payload\ antes de compilar  -  nunca commitado no repo).
; Nao instala chaves, senhas ou backup.json: a ativacao acontece na interface.
;
; Diretorio comum de dados: NSIS nao tem $PROGRAMDATA nativo. Com
; "SetShellVarContext all", $APPDATA resolve para %ProgramData% (= a mesma
; pasta que a interface usa via $env:ProgramData) e $SMPROGRAMS vira o Menu
; Iniciar de TODOS os usuarios — inclusive a conta dedicada do backup,
; mesmo quando outro administrador autorizou a instalacao.

!ifndef VERSAO
  !define VERSAO "0.1.0"
!endif
!define NOME "MAC IP Backup"
!define PASTA "$PROGRAMFILES64\MACIP-Backup"

Name "${NOME} ${VERSAO}"
OutFile "MacipBackupSetup-${VERSAO}.exe"
Unicode True
RequestExecutionLevel admin          ; instalar em Program Files exige elevacao (UAC nativo)
InstallDir "${PASTA}"
SetCompressor /SOLID lzma

!include "MUI2.nsh"
!include "LogicLib.nsh"
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_LANGUAGE "PortugueseBR"

Section "Instalar"
  SetShellVarContext all             ; $APPDATA = %ProgramData%, $SMPROGRAMS = todos os usuarios
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
  ; MESMO caminho da interface: %ProgramData%\MacipBackup. As subpastas de
  ; configuracao/estado recebem ACL propria na primeira gravacao da GUI
  ; (somente a conta do backup) — aqui fica a heranca padrao do ProgramData.
  CreateDirectory "$APPDATA\MacipBackup"

  ; Atalho no Menu Iniciar COMUM: visivel para a conta dedicada do backup
  ; mesmo quando outro administrador autorizou a instalacao.
  CreateDirectory "$SMPROGRAMS\MAC IP Backup"
  CreateShortcut "$SMPROGRAMS\MAC IP Backup\MAC IP Backup.lnk" \
    "$WINDIR\System32\WindowsPowerShell\v1.0\powershell.exe" \
    '-NoProfile -ExecutionPolicy RemoteSigned -File "$INSTDIR\MacipBackup-Gui.ps1"'

  WriteUninstaller "$INSTDIR\desinstalar.exe"
SectionEnd

Section "Uninstall"
  SetShellVarContext all

  ; Antes de remover os arquivos que a tarefa executa, desabilita SOMENTE a
  ; tarefa do aplicativo e confere o resultado. Sem essa confirmacao, os
  ; executaveis ficam (a tarefa continuaria apontando para eles).
  nsExec::ExecToLog 'schtasks /Query /TN "MACIP-Backup"'
  Pop $0
  ${If} $0 == 0
    nsExec::ExecToLog 'schtasks /End /TN "MACIP-Backup"'
    Pop $1                                   ; encerrar instancia em execucao e melhor esforco
    nsExec::ExecToLog 'schtasks /Change /TN "MACIP-Backup" /DISABLE'
    Pop $2
    ${If} $2 <> 0
      MessageBox MB_ICONEXCLAMATION|MB_OK \
        "FALHA ao desabilitar a tarefa MACIP-Backup (codigo $2).$\n$\nOs arquivos de $INSTDIR foram PRESERVADOS porque a tarefa ainda pode executa-los. Desabilite-a no Agendador de Tarefas e rode a desinstalacao de novo."
      Goto fim_uninstall                       ; nao remove o que a tarefa executa
    ${EndIf}
  ${EndIf}

  Delete "$SMPROGRAMS\MAC IP Backup\MAC IP Backup.lnk"
  RMDir "$SMPROGRAMS\MAC IP Backup"
  Delete "$INSTDIR\*.ps1"
  Delete "$INSTDIR\*.json"
  Delete "$INSTDIR\restic.exe"
  Delete "$INSTDIR\desinstalar.exe"
  RMDir "$INSTDIR"
fim_uninstall:
  ; $APPDATA\MacipBackup (backup.json, estado, preparo.json, senha DPAPI e
  ; resultados) e SEMPRE preservado — a tarefa fica desabilitada, os dados nao.
SectionEnd
