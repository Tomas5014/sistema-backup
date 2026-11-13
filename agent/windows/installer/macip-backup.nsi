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
; Iniciar de TODOS os usuarios -- inclusive a conta dedicada do backup,
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
InstallDir "${PASTA}"                ; FIXO no piloto: sem tela de diretorio e sem /D=
SetCompressor /SOLID lzma

!include "MUI2.nsh"
!include "LogicLib.nsh"
; Sem MUI_PAGE_DIRECTORY: o piloto instala SEMPRE em Program Files\MACIP-Backup
; (pasta protegida por ACL do Windows contra usuarios comuns).
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_LANGUAGE "PortugueseBR"

Function .onInit
  ; /D= na linha de comando nao pode contornar a pasta fixa do piloto.
  StrCmp "$INSTDIR" "${PASTA}" instdir_ok
    MessageBox MB_ICONSTOP|MB_OK "O piloto instala somente em ${PASTA}.$\n$\nO diretorio informado ($INSTDIR) nao e aceito -- rode o instalador sem /D=."
    Abort
  instdir_ok:
FunctionEnd

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
  File "Desinstalar-Tarefa.ps1"      ; verificacao estruturada da tarefa no uninstall
  File "payload\restic.exe"          ; binario aprovado pela MAC IP (nao versionado)
  File /nonfatal "payload\instalar-como-conta.txt"

  ; Pasta de dados SEM espacos no caminho (limite do sftp.command do Restic).
  ; MESMO caminho da interface: %ProgramData%\MacipBackup. As subpastas de
  ; configuracao/estado recebem ACL propria na primeira gravacao da GUI
  ; (somente a conta do backup) -- aqui fica a heranca padrao do ProgramData.
  CreateDirectory "$APPDATA\MacipBackup"

  ; Atalho no Menu Iniciar COMUM: visivel para a conta dedicada do backup
  ; mesmo quando outro administrador autorizou a instalacao.
  CreateDirectory "$SMPROGRAMS\MAC IP Backup"
  CreateShortcut "$SMPROGRAMS\MAC IP Backup\MAC IP Backup.lnk" \
    "$WINDIR\System32\WindowsPowerShell\v1.0\powershell.exe" \
    '-NoProfile -ExecutionPolicy RemoteSigned -File "$INSTDIR\MacipBackup-Gui.ps1"'

  WriteUninstaller "$INSTDIR\desinstalar.exe"

  ; Registro em "Aplicativos instalados" do Windows (remocao por ali).
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\MACIP-Backup" "DisplayName" "${NOME} ${VERSAO}"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\MACIP-Backup" "DisplayVersion" "${VERSAO}"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\MACIP-Backup" "Publisher" "MAC IP"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\MACIP-Backup" "InstallLocation" "$INSTDIR"
  ; aspas obrigatorias: Program Files tem espaco e o Windows executa o
  ; valor da UninstallString como linha de comando
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\MACIP-Backup" "UninstallString" '"$INSTDIR\desinstalar.exe"'
  WriteRegDWORD HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\MACIP-Backup" "NoModify" 1
  WriteRegDWORD HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\MACIP-Backup" "NoRepair" 1
SectionEnd

Section "Uninstall"
  SetShellVarContext all

  ; A desinstalacao delega TODA a decisao sobre a tarefa MACIP-Backup ao
  ; script estruturado (Schedule.Service/COM -- nada de schtasks/XML/findstr).
  ; Ele confirma: propriedade (marca + acao apontando para $INSTDIR),
  ; desabilita primeiro, encerra instancias se preciso e verifica o estado
  ; final. Saida:
  ;   0 = tarefa do app desabilitada e parada -> remove
  ;   2 = ausente confirmada -> remove
  ;   3 = homonima ALHEIA -> preservada intacta; abortar
  ;   4 = inconclusivo/erro -> preservar executaveis
  ;   5 = instancia ativa/pendente -> preservar executaveis
  nsExec::ExecToStack '"$WINDIR\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "$INSTDIR\Desinstalar-Tarefa.ps1" -PrefixoAcao "$INSTDIR"'
  Pop $0                                        ; codigo de saida do script
  Pop $1                                        ; mensagem estruturada
  ${If} $0 == 0
  ${OrIf} $0 == 2
    Goto remover_arquivos
  ${EndIf}
  ${If} $0 == 3
    MessageBox MB_ICONEXCLAMATION|MB_OK \
      "A tarefa MACIP-Backup no Agendador NAO pertence a este aplicativo.$\n$\nEla foi PRESERVADA intacta e a desinstalacao foi interrompida -- nenhum arquivo de $INSTDIR foi removido."
    Goto fim_uninstall
  ${EndIf}
  ${If} $0 == 5
    MessageBox MB_ICONEXCLAMATION|MB_OK \
      "A tarefa MACIP-Backup ainda tem execucao ATIVA ou PENDENTE.$\n$\nOs arquivos de $INSTDIR foram PRESERVADOS. Aguarde o termino e rode a desinstalacao de novo."
    Goto fim_uninstall
  ${EndIf}
  ; 4 ou outro: consulta/desabilitacao inconclusiva -- preserva os executaveis
  MessageBox MB_ICONEXCLAMATION|MB_OK \
    "Nao foi possivel confirmar a tarefa MACIP-Backup (codigo $0):$\n$1$\n$\nOs arquivos de $INSTDIR foram PRESERVADOS porque a tarefa pode ainda usa-los. Resolva o acesso ao Agendador e rode a desinstalacao de novo."
  Goto fim_uninstall

remover_arquivos:
  Delete "$SMPROGRAMS\MAC IP Backup\MAC IP Backup.lnk"
  RMDir "$SMPROGRAMS\MAC IP Backup"
  ; somente os arquivos instalados pelo pacote, por nome -- nada de curinga
  Delete "$INSTDIR\Backup-MacipCliente.ps1"
  Delete "$INSTDIR\MacipBackup-Gui.ps1"
  Delete "$INSTDIR\Desinstalar-Tarefa.ps1"
  Delete "$INSTDIR\perfil.example.json"
  Delete "$INSTDIR\instalar-como-conta.txt"
  Delete "$INSTDIR\restic.exe"
  Delete "$INSTDIR\desinstalar.exe"
  RMDir "$INSTDIR"
  DeleteRegKey HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\MACIP-Backup"
fim_uninstall:
  ; $APPDATA\MacipBackup (backup.json, estado, preparo.json, senha DPAPI e
  ; resultados) e SEMPRE preservado -- tarefa desabilitada, dados nao.
SectionEnd
