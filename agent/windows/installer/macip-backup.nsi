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
InstallDir "${PASTA}"
SetCompressor /SOLID lzma

!include "MUI2.nsh"
!include "LogicLib.nsh"
!include "StrFunc.nsh"
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
  ; (somente a conta do backup) -- aqui fica a heranca padrao do ProgramData.
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

  ; A desinstalacao so remove os executaveis depois de confirmar TRES coisas
  ; sobre a tarefa MACIP-Backup:
  ;   a) ela existe e E do aplicativo (a marca "MACIP Backup" aparece no XML --
  ;      uma homonima estranha permanece intacta);
  ;   b) foi desabilitada (sem novos disparos) e nao ha execucao ativa;
  ;   c) se a consulta falhar, diferenciar "nao existe" (segue) de "erro de
  ;      acesso/consulta" (preserva os arquivos que a tarefa pode executar).
  nsExec::ExecToStack 'schtasks /Query /TN "MACIP-Backup" /XML'
  Pop $0                                        ; codigo de saida
  Pop $1                                        ; saida (XML ou mensagem de erro)
  ${If} $0 == 0
    ; tarefa existe: propriedade pela mesma marca que a interface grava
    ${StrContains} $2 "MACIP Backup" $1
    ${If} $2 == ""
      ; homonima estranha -- permanece intacta, nao executa nossos arquivos
      Goto remover_arquivos
    ${EndIf}

    ; tarefa do app: impede novos disparos e encerra execucao em andamento
    nsExec::ExecToLog 'schtasks /End /TN "MACIP-Backup"'
    Pop $3                                        ; falha no /End e tolerada (pode nao estar rodando)
    nsExec::ExecToLog 'schtasks /Change /TN "MACIP-Backup" /DISABLE'
    Pop $3
    ${If} $3 <> 0
      MessageBox MB_ICONEXCLAMATION|MB_OK \
        "FALHA ao desabilitar a tarefa MACIP-Backup (codigo $3).$\n$\nOs arquivos de $INSTDIR foram PRESERVADOS porque a tarefa ainda pode executa-los. Desabilite-a no Agendador de Tarefas e rode a desinstalacao de novo."
      Goto fim_uninstall
    ${EndIf}
    ; confirma que NAO ha execucao ativa antes de remover os executaveis
    nsExec::ExecToStack 'cmd /c schtasks /Query /TN "MACIP-Backup" /V /FO LIST | findstr /I /C:"Running" /C:"execucao" /C:"executando"'
    Pop $4                                        ; 0 = encontrou execucao ativa
    Pop $5
    ${If} $4 == 0
      MessageBox MB_ICONEXCLAMATION|MB_OK \
        "A tarefa MACIP-Backup ainda esta EM EXECUCAO.$\n$\nOs arquivos de $INSTDIR foram PRESERVADOS. Aguarde o termino ou finalize a tarefa e rode a desinstalacao de novo."
      Goto fim_uninstall
    ${EndIf}
    Goto remover_arquivos
  ${Else}
    ; consulta falhou: separar "nao existe" de "erro de acesso/consulta"
    ${StrContains} $6 "cannot find" $1
    ${StrContains} $7 "encontrar" $1
    ${StrContains} $8 "no such" $1
    ${If} $6 == ""
    ${AndIf} $7 == ""
    ${AndIf} $8 == ""
      ; nem "arquivo nao encontrado": acesso negado ou falha de consulta --
      ; NAO confirmado -> preservar os arquivos que a tarefa pode executar
      MessageBox MB_ICONEXCLAMATION|MB_OK \
        "Nao foi possivel confirmar o estado da tarefa MACIP-Backup (codigo $0):$\n$1$\n$\nOs arquivos de $INSTDIR foram PRESERVADOS. Resolva o acesso ao Agendador e rode a desinstalacao de novo."
      Goto fim_uninstall
    ${EndIf}
    ; ausencia confirmada -> segue para remover
  ${EndIf}

remover_arquivos:
  Delete "$SMPROGRAMS\MAC IP Backup\MAC IP Backup.lnk"
  RMDir "$SMPROGRAMS\MAC IP Backup"
  Delete "$INSTDIR\*.ps1"
  Delete "$INSTDIR\*.json"
  Delete "$INSTDIR\restic.exe"
  Delete "$INSTDIR\desinstalar.exe"
  RMDir "$INSTDIR"
fim_uninstall:
  ; $APPDATA\MacipBackup (backup.json, estado, preparo.json, senha DPAPI e
  ; resultados) e SEMPRE preservado -- tarefa desabilitada, dados nao.
SectionEnd
