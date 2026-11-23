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
  !define VERSAO "0.1.4-dev4-lab"
!endif
!ifndef MANIFEST_SHA256
  !error "Forneca /DMANIFEST_SHA256 do payload congelado"
!endif
!ifndef LEGACY_SHA256
  !define LEGACY_SHA256 ""
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
  IfSilent refuse_silent interactive
  refuse_silent:
    SetErrorLevel 1
    Abort
  interactive:
  MessageBox MB_YESNO|MB_DEFBUTTON2|MB_ICONQUESTION "Instalar ou atualizar este laboratorio? Feche o aplicativo e aguarde as tarefas. Configuracao, credenciais e fila serao preservadas." IDYES approved
    Abort
  approved:
  ; /D= na linha de comando nao pode contornar a pasta fixa do piloto.
  StrCmp "$INSTDIR" "${PASTA}" instdir_ok
    MessageBox MB_ICONSTOP|MB_OK "O piloto instala somente em ${PASTA}.$\n$\nO diretorio informado ($INSTDIR) nao e aceito -- rode o instalador sem /D=."
    Abort
  instdir_ok:
FunctionEnd

Section "Instalar"
  SetShellVarContext all             ; $APPDATA = %ProgramData%, $SMPROGRAMS = todos os usuarios
  InitPluginsDir
  SetOutPath "$PLUGINSDIR\payload"

  ; Pre-verificacao de existencia do Cliente OpenSSH -- CONSULTA
  ; INCONCLUSIVA por natureza: este instalador e um processo de 32
  ; bits, entao $WINDIR\System32 redireciona para SysWOW64 (o ssh nativo
  ; de 64 bits NAO aparece la -- foi o falso aviso na VM do piloto).
  ; Por isso consultamos primeiro o alias Sysnative, que um processo
  ; x86 resolve para o System32 real em Windows x64. NADA aqui e
  ; executado com privilegio administrativo: so existencia, nunca
  ; "ssh -V" de candidato do PATH. A verificacao DEFINITIVA e a
  ; ativacao, na conta do backup, no mesmo criterio do sftp.command.
  IfFileExists "$WINDIR\Sysnative\OpenSSH\ssh.exe" ssh_ok
  IfFileExists "$WINDIR\System32\OpenSSH\ssh.exe" ssh_ok
  SearchPath $0 "ssh.exe"
  StrCmp $0 "" ssh_falta ssh_ok
  ssh_falta:
    MessageBox MB_ICONEXCLAMATION|MB_OK \
      "O Cliente OpenSSH nao foi encontrado nos locais verificados.$\n$\nSe ele ja estiver instalado, a ativacao do aplicativo confirmara na conta do backup. Se nao estiver, ative em: Configuracoes > Aplicativos > Recursos opcionais > Adicionar recurso > Cliente OpenSSH.$\nA instalacao continua; o backup so funciona depois que a ativacao confirmar o OpenSSH."
  ssh_ok:

  ; Somente bytes do commit congelado pelo gerador de manifesto.
  File "payload\Backup-MacipCliente.ps1"
  File "payload\Central-Alerts.ps1"
  File "payload\MacipBackup-Gui.ps1"
  File "payload\Activation-Code.ps1"
  File "payload\Assisted-Recovery.ps1"
  File "payload\Monitoring-Task.ps1"
  File "payload\Update-Installation.ps1"
  File "payload\perfil.example.json"
  File "payload\Desinstalar-Tarefa.ps1"
  File "payload\restic.exe"
  File "payload\MANIFEST.json"
  File /nonfatal "payload\LEGACY-MANIFEST.json"
  nsExec::ExecToStack '"$WINDIR\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "$PLUGINSDIR\payload\Update-Installation.ps1" -Action Apply -InstallRoot "$INSTDIR" -DataRoot "$APPDATA\MacipBackup" -Payload "$PLUGINSDIR\payload" -ManifestHash "${MANIFEST_SHA256}" -LegacyManifest "$PLUGINSDIR\payload\LEGACY-MANIFEST.json" -LegacyManifestHash "${LEGACY_SHA256}"'
  Pop $0
  Pop $1
  ${If} $0 != 0
    MessageBox MB_ICONSTOP|MB_OK "Atualizacao recusada ou incompleta. Dados e codigo anterior preservados. Feche o aplicativo, aguarde tarefas e confira o roteiro de recuperacao.$\n$1"
    Abort
  ${EndIf}

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

  CreateShortcut "$SMPROGRAMS\MAC IP Backup\Reverter atualizacao.lnk" \
    "$WINDIR\System32\WindowsPowerShell\v1.0\powershell.exe" \
    '-NoProfile -ExecutionPolicy RemoteSigned -File "$PROGRAMFILES64\.MACIP-Backup-update\Update-Installation.ps1" -Action Rollback -InstallRoot "$INSTDIR" -DataRoot "$APPDATA\MacipBackup"'

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
  nsExec::ExecToStack '"$WINDIR\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "$INSTDIR\Desinstalar-Tarefa.ps1" -PrefixoAcao "$INSTDIR" -Nome "MACIP-Backup-Sync" -DescricaoEsperada "MACIP Backup - envio central do piloto" -Monitoramento'
  Pop $0
  Pop $1
  ${If} $0 != 0
  ${AndIf} $0 != 2
    MessageBox MB_ICONEXCLAMATION|MB_OK "A tarefa de envio nao foi confirmada como propria e parada. Arquivos preservados. $1"
    Goto fim_uninstall
  ${EndIf}
  Delete "$SMPROGRAMS\MAC IP Backup\MAC IP Backup.lnk"
  Delete "$SMPROGRAMS\MAC IP Backup\Reverter atualizacao.lnk"
  RMDir "$SMPROGRAMS\MAC IP Backup"
  ; somente os arquivos instalados pelo pacote, por nome -- nada de curinga
  Delete "$INSTDIR\Backup-MacipCliente.ps1"
  Delete "$INSTDIR\Central-Alerts.ps1"
  Delete "$INSTDIR\MacipBackup-Gui.ps1"
  Delete "$INSTDIR\Activation-Code.ps1"
  Delete "$INSTDIR\Assisted-Recovery.ps1"
  Delete "$INSTDIR\Monitoring-Task.ps1"
  Delete "$INSTDIR\Desinstalar-Tarefa.ps1"
  Delete "$INSTDIR\perfil.example.json"
  Delete "$INSTDIR\instalar-como-conta.txt"
  Delete "$INSTDIR\restic.exe"
  Delete "$INSTDIR\MANIFEST.json"
  Delete "$INSTDIR\Update-Installation.ps1"
  Delete "$INSTDIR\desinstalar.exe"
  RMDir "$INSTDIR"
  DeleteRegKey HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\MACIP-Backup"
fim_uninstall:
  ; $APPDATA\MacipBackup (backup.json, estado, preparo.json, senha DPAPI e
  ; resultados) e SEMPRE preservado -- tarefa desabilitada, dados nao.
SectionEnd
