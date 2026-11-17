"""Ponte para a suíte PowerShell do agente Windows.

Executa tests/run-windows-agent-tests.ps1 quando há pwsh/powershell no
PATH; sem eles, registra skip — a prova real continua sendo a VM
Windows descartável documentada no README do agente.
"""
import shutil
import subprocess
import unittest
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parent.parent / "agent" / "windows"
SUITE = AGENT_DIR / "tests" / "run-windows-agent-tests.ps1"
SUITE_GUI = AGENT_DIR / "tests" / "run-gui-tests.ps1"
SCRIPT = AGENT_DIR / "Backup-MacipCliente.ps1"
GUI = AGENT_DIR / "gui" / "MacipBackup-Gui.ps1"


class TestWindowsAgentPs1(unittest.TestCase):
    def test_suite_powershell(self):
        exe = shutil.which("pwsh") or shutil.which("powershell")
        if not exe:
            self.skipTest("pwsh/powershell indisponível — suíte PS adiada")
        proc = subprocess.run(
            [exe, "-NoProfile", "-File", str(SUITE)],
            capture_output=True, text=True, timeout=300,
        )
        self.assertEqual(
            proc.returncode, 0,
            f"suíte PS falhou:\n{proc.stdout}\n{proc.stderr}",
        )

    def test_suite_gui(self):
        exe = shutil.which("pwsh") or shutil.which("powershell")
        if not exe:
            self.skipTest("pwsh/powershell indisponível — suíte GUI adiada")
        proc = subprocess.run(
            [exe, "-NoProfile", "-File", str(SUITE_GUI)],
            capture_output=True, text=True, timeout=300,
        )
        self.assertEqual(
            proc.returncode, 0,
            f"suíte GUI falhou:\n{proc.stdout}\n{proc.stderr}",
        )

    def test_contrato_estatico(self):
        """Checagens de contrato que independem de PowerShell."""
        import json
        exemplo = json.loads((AGENT_DIR / "backup.example.json").read_text())
        self.assertEqual(
            set(exemplo),
            {"origem", "exclusoes", "repositorio", "horario",
             "repositorio_id", "identidade_ssh", "known_hosts"},
        )
        self.assertNotIn("senha", json.dumps(exemplo).lower())
        fonte = SCRIPT.read_text(encoding="utf-8")
        for marca in ("--exclude", "sftp.command", "BatchMode yes",
                      "StrictHostKeyChecking yes", "ultimo-resultado.json",
                      "cat', 'config'", "ConvertFrom-SecureString"):
            self.assertIn(marca, fonte, f"ausente no agente: {marca}")
        # o agente jamais cria repositório
        self.assertNotIn("'init'", fonte)
        self.assertNotIn('"init"', fonte)
        # OpenSSH: resolve o executavel que o sftp.command realmente usa
        # (canonico -> PATH, com verificacao 'ssh -V' de OpenSSH)
        self.assertIn("Select-SshCandidato", fonte)
        self.assertIn("Test-OpenSshExe", fonte)
        self.assertIn("Get-Command ssh.exe", fonte)
        # arquitetura: processo x86 ve o System32 nativo via Sysnative,
        # mas o caminho APROVADO e sempre o canonico (tarefa e 64 bits)
        self.assertIn("Sysnative\\OpenSSH\\ssh.exe", fonte)
        self.assertIn("Caminho = $canonico", fonte)
        # status tipado: ausente != encontrado-mas-incompativel
        self.assertIn("Get-SshStatus", fonte)
        self.assertIn("'incompativel'", fonte)

    def test_contrato_gui(self):
        """Contrato estático da interface e do perfil de exemplo."""
        import json
        perfil = json.loads(
            (AGENT_DIR / "gui" / "perfil.example.json").read_text())
        self.assertEqual(
            set(perfil),
            {"nome", "repositorio", "repositorio_id", "known_hosts",
             "horario_sugerido"},
        )
        self.assertNotIn("senha", json.dumps(perfil).lower())
        self.assertNotIn("private", json.dumps(perfil).lower())
        fonte = GUI.read_text(encoding="utf-8")
        for marca in ("Schedule.Service", "TASK_LOGON_PASSWORD",
                      "ultimo-resultado.json", "sem_resultado",
                      "RegisterTaskDefinition", "PromptForCredential"):
            self.assertIn(marca, fonte, f"ausente na GUI: {marca}")
        # a interface nunca chama init nem grava senha em arquivo/log
        self.assertNotIn("'init'", fonte)
        self.assertNotIn('"init"', fonte)
        # TASK_LOGON_PASSWORD = 1 (PASSWORD), nao 2 (S4U)
        self.assertRegex(fonte, r"LogonPassword\s*=\s*1\b")
        # listagem estruturada e caminho exato, nunca texto livre
        self.assertIn("'ls', '--json'", fonte)
        self.assertIn("type -ceq 'file'", fonte)
        # conjunto novo (config-N/estado-N) nao toca o conjunto ativo
        self.assertIn("config-$n", fonte)
        self.assertIn("estado-$n", fonte)
        # ultimo backup confirmado: evidencia separada do ultimo resultado
        self.assertIn("ultimo-confirmado.json", fonte)
        # prova de validacao vinculada a perfil+chave+senha capturados
        self.assertIn("Get-TokenAtivacao", fonte)
        self.assertIn("Test-AtivacaoVigente", fonte)
        # horario: seletor DateTimePicker HH:mm (bug da linha na VM),
        # sem TextBox livre para o horario
        self.assertIn("DateTimePicker", fonte)
        self.assertIn("ShowUpDown = $true", fonte)
        self.assertIn("CustomFormat = 'HH:mm'", fonte)
        self.assertNotIn("$tbHora", fonte)
        # previa do agendamento antes de salvar
        self.assertIn("Format-PreviaHorario", fonte)
        # situacao: auto-atualizacao + detalhes de suporte + abertura direta
        self.assertIn("System.Windows.Forms.Timer", fonte)
        self.assertIn("Get-DetalhesSuporte", fonte)
        self.assertIn("Test-ConfiguracaoPronta", fonte)
        # exclusoes na secao opcional (rotulo direto); janela de credencial
        # nativa mantida
        self.assertIn("Exclusões (opcional)", fonte)
        # falha SFTP: mensagem curta classificada + detalhes recolhidos
        self.assertIn("Get-MensagemFalha", fonte)
        self.assertIn("Mostrar detalhes técnicos", fonte)
        # restauracao: abrir pasta restaurada apos sucesso
        self.assertIn("Abrir pasta restaurada", fonte)
        # lancamento falho -> diagnostico sem segredos
        self.assertIn("Write-Diagnostico", fonte)
        self.assertIn("diagnostico-macip.txt", fonte)
        # "Executar agora" vinculado a execucao NOVA: referencia antes do
        # Run (ultimo LastRunTime + GUIDs de instancia), estados proprios
        # e nunca o sucesso anterior anunciado como do disparo novo
        self.assertIn("New-PedidoDisparo", fonte)
        self.assertIn("Update-PedidoDisparo", fonte)
        self.assertIn("UltimaRef", fonte)
        self.assertIn("InstRef", fonte)
        self.assertIn("nao_confirmado", fonte)
        self.assertIn("Instancias", fonte)
        # clique repetido bloqueado enquanto ha pedido aberto
        self.assertIn("if ($script:Disparo) { return }", fonte)
        # layout que acompanha a escala de DPI
        self.assertIn("AutoScaleMode", fonte)
        # refinamento visual: sem coordenadas fixas, um acento, a11y,
        # tres passos do primeiro uso e UTF-8 com BOM (acentos no PS 5.1)
        self.assertIn("TableLayoutPanel", fonte)
        self.assertIn("FlowLayoutPanel", fonte)
        self.assertNotIn(".Location = ", fonte)
        self.assertIn("AccessibleName", fonte)
        self.assertIn("TabIndex", fonte)
        self.assertIn("Passo 1 de 3", fonte)
        self.assertIn("Executar backup agora", fonte)
        self.assertIn("Último backup bem-sucedido", fonte)
        self.assertTrue(fonte.startswith("\ufeff"),
                        "GUI deve ser UTF-8 com BOM para o PS 5.1 ler os acentos")

    def test_contrato_instalador(self):
        """Contrato estatico do instalador NSIS."""
        nsi = (AGENT_DIR / "installer" / "macip-backup.nsi").read_text(
            encoding="utf-8")
        # $PROGRAMDATA nao e variavel nativa do NSIS — nao pode aparecer em
        # codigo (comentarios que explicam a ausencia sao ignorados).
        codigo = "\n".join(
            ln for ln in nsi.splitlines() if not ln.lstrip().startswith(";"))
        self.assertNotIn("$PROGRAMDATA", codigo)
        # SetShellVarContext all: $APPDATA = %ProgramData% (mesmo caminho da GUI)
        self.assertIn("SetShellVarContext all", nsi)
        self.assertIn('$APPDATA\\MacipBackup', nsi)
        # desinstalacao delega a verificacao estruturada ao script COM
        # (Desinstalar-Tarefa.ps1) — nada de schtasks/XML/findstr/StrContains.
        self.assertIn('Desinstalar-Tarefa.ps1', nsi)
        self.assertIn('nsExec::ExecToStack', nsi)
        self.assertNotIn('StrContains', codigo)
        self.assertNotIn('StrFunc.nsh', codigo)
        self.assertNotIn('/XML', codigo)
        self.assertNotIn('findstr', codigo)
        self.assertNotIn('schtasks', codigo)
        self.assertIn('MACIP-Backup', nsi)
        # piloto: instalacao FIXA em Program Files -- sem pagina de diretorio,
        # e .onInit barra contorno por /D= na linha de comando
        self.assertNotIn('MUI_PAGE_DIRECTORY', codigo)
        self.assertIn('Function .onInit', nsi)
        self.assertIn('$PROGRAMFILES64\\MACIP-Backup', nsi)
        # registro em "Aplicativos instalados" do Windows
        self.assertIn('CurrentVersion\\Uninstall\\MACIP-Backup', nsi)
        self.assertIn('UninstallString', nsi)
        # UninstallString COM aspas no caminho completo (Program Files tem
        # espaco): sem elas, "C:\Program" seria executado como programa
        self.assertIn('\'"$INSTDIR\\desinstalar.exe"\'', nsi)
        self.assertIn('DeleteRegKey HKLM "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\MACIP-Backup"', nsi)
        # OpenSSH: pre-verificacao so de EXISTENCIA, ciente de
        # arquitetura -- Sysnative antes (x86 ve o System32 nativo),
        # System32 (x86 real) e PATH; nada e executado com privilegio
        # de administrador, e a verificacao definitiva fica na ativacao
        self.assertIn('$WINDIR\\Sysnative\\OpenSSH\\ssh.exe', nsi)
        self.assertIn('$WINDIR\\System32\\OpenSSH\\ssh.exe', nsi)
        self.assertIn('SearchPath', nsi)
        self.assertIn('"ssh.exe"', nsi)
        self.assertNotIn('ExecWait', nsi.split('ssh_falta')[0].split('OpenSSH')[0])
        self.assertIn('locais verificados', nsi)
        # o script estruturado verifica marca + acao + instancias via COM
        desinst = (AGENT_DIR / "installer" / "Desinstalar-Tarefa.ps1").read_text(
            encoding="utf-8")
        self.assertIn('Schedule.Service', desinst)
        self.assertIn('GetInstances', desinst)
        self.assertIn('RegistrationInfo.Description', desinst)
        self.assertIn('MACIP Backup', desinst)


if __name__ == "__main__":
    unittest.main()
