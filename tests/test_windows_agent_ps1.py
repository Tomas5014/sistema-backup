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
                      "ultimo-resultado.json", "sem_resultado_novo",
                      "RegisterTaskDefinition", "PromptForCredential"):
            self.assertIn(marca, fonte, f"ausente na GUI: {marca}")
        # a interface nunca chama init nem grava senha em arquivo/log
        self.assertNotIn("'init'", fonte)
        self.assertNotIn('"init"', fonte)


if __name__ == "__main__":
    unittest.main()
