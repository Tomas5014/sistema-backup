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
        self.assertIn('DeleteRegKey HKLM "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\MACIP-Backup"', nsi)
        # o script estruturado verifica marca + acao + instancias via COM
        desinst = (AGENT_DIR / "installer" / "Desinstalar-Tarefa.ps1").read_text(
            encoding="utf-8")
        self.assertIn('Schedule.Service', desinst)
        self.assertIn('GetInstances', desinst)
        self.assertIn('RegistrationInfo.Description', desinst)
        self.assertIn('MACIP Backup', desinst)


if __name__ == "__main__":
    unittest.main()
