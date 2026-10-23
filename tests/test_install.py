"""Fluxo de instalação/atualização/desinstalação — isolado por HOME fake.

O wheel é construído uma vez (pip wheel --no-build-isolation) e cada
teste instala num HOME temporário novo — sem root, sem systemd real,
sem dados operacionais existentes. HOME vazio prova que o binário
instalado roda fora do checkout (sem PYTHONPATH para src).
"""

from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
INSTALL_SH = ROOT / "deploy" / "install.sh"
UNINSTALL_SH = ROOT / "deploy" / "uninstall.sh"


def _build_wheel(directory: Path) -> Path:
    """Wheel sem pip: chama o backend setuptools diretamente — o python
    do sistema pode não ter `pip` instalado."""
    subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import os; "
                f"os.chdir({str(ROOT)!r}); "
                "from setuptools.build_meta import build_wheel; "
                f"build_wheel({str(directory)!r})"
            ),
        ],
        check=True,
        capture_output=True,
        cwd=str(ROOT),
    )
    wheels = list(directory.glob("macip_backup_poc-*.whl"))
    if len(wheels) != 1:
        raise RuntimeError(f"wheel único não produzido: {wheels}")
    return wheels[0]


def _task_config(source: Path, repository: Path, task_id: str) -> dict:
    return {
        "id": task_id,
        "name": "Instalação",
        "client": {"id": "client-inst", "name": "Cliente"},
        "device_id": "device-inst",
        "sources": [str(source)],
        "exclusions": [],
        "compression": "auto",
        "destinations": [
            {
                "id": "local",
                "name": "Local",
                "kind": "local",
                "repository": str(repository),
                "password_environment": "MACIP_RESTIC_PASSWORD",
            }
        ],
    }


@unittest.skipUnless(
    Path("/usr/bin/flock").exists() or subprocess.run(
        ["sh", "-c", "command -v flock"], capture_output=True
    ).returncode
    == 0,
    "flock ausente",
)
class InstallFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.dist_dir = Path(
            tempfile.mkdtemp(prefix=".test-dist-", dir=Path.cwd())
        )
        cls.wheel = _build_wheel(cls.dist_dir)

    @classmethod
    def tearDownClass(cls) -> None:
        import shutil

        shutil.rmtree(cls.dist_dir, ignore_errors=True)

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix=".test-inst-", dir=Path.cwd())
        self.root = Path(self.temp_dir.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.env = {
            "HOME": str(self.home),
            "PATH": os.environ.get("PATH", ""),
            "MACIP_RESTIC_PASSWORD": "pw-teste",
        }
        self.bin = self.home / ".local" / "bin" / "macip-backup"
        self.macip_home = self.home / ".local" / "share" / "macip-backup"
        self.config_dir = self.home / ".config" / "macip-backup"
        self.state_dir = self.home / ".local" / "state" / "macip-backup"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _install(self, *args: object) -> subprocess.CompletedProcess:
        """install.sh <wheel> [flags...] — se o 1º arg for .whl vira o
        artefato; o resto vai como flags."""
        args = [str(arg) for arg in args]
        wheel = str(self.wheel)
        if args and args[0].endswith(".whl"):
            wheel = args.pop(0)
        return subprocess.run(
            ["bash", str(INSTALL_SH), wheel, *args],
            env=self.env,
            capture_output=True,
            text=True,
        )

    def _uninstall(self, *extra: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", str(UNINSTALL_SH), *extra],
            env=self.env,
            capture_output=True,
            text=True,
        )

    def _run_cli(self, *args: str, timeout: int = 120) -> subprocess.CompletedProcess:
        return subprocess.run(
            [str(self.bin), *args],
            env=self.env,
            cwd=str(self.root),  # fora do checkout — sem PYTHONPATH
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def _write_task(self) -> tuple[Path, Path]:
        source = self.root / "origem"
        source.mkdir()
        (source / "documento.txt").write_text("conteúdo original\n")
        config = self.root / "tarefa.json"
        config.write_text(
            json.dumps(_task_config(source, self.root / "repo", "task-inst"))
        )
        return source, config

    def test_first_install_and_cli_backup_restore(self) -> None:
        installed = self._install("--commit", "abc123")
        self.assertEqual(installed.returncode, 0, installed.stderr)

        manifest = json.loads(
            (self.macip_home / "install.json").read_text()
        )
        self.assertEqual(manifest["commit"], "abc123")
        self.assertTrue(manifest["version"])
        self.assertTrue(
            (self.config_dir / "agent.env").stat().st_mode & 0o777 == 0o600
        )

        source, config = self._write_task()

        run = self._run_cli("run", str(config))
        self.assertEqual(run.returncode, 0, run.stderr + run.stdout)

        snapshots = self._run_cli("snapshots", str(config), "local")
        self.assertEqual(snapshots.returncode, 0)
        snapshot_id = json.loads(snapshots.stdout)["snapshots"][0]["id"]

        check = self._run_cli("check", str(config), "local", "--read-data")
        self.assertEqual(check.returncode, 0, check.stderr)

        target = self.root / "restaurado"
        restore = self._run_cli(
            "restore", str(config), "local", snapshot_id, str(target)
        )
        self.assertEqual(restore.returncode, 0, restore.stderr)
        restored = list(target.rglob("documento.txt"))
        self.assertEqual(len(restored), 1)
        self.assertEqual(restored[0].read_text(), "conteúdo original\n")

    def test_reinstall_preserves_state_config_and_history(self) -> None:
        self.assertEqual(self._install("--commit", "aaa").returncode, 0)
        marker = self.config_dir / "marcador.txt"
        marker.write_text("não apagar")
        db = self.state_dir / "agent.db"
        db.write_bytes(b"historico-fake")

        again = self._install("--commit", "bbb")
        self.assertEqual(again.returncode, 0, again.stderr)

        self.assertEqual(marker.read_text(), "não apagar")
        self.assertEqual(db.read_bytes(), b"historico-fake")
        manifest = json.loads(
            (self.macip_home / "install.json").read_text()
        )
        self.assertEqual(manifest["commit"], "bbb")

    def test_update_fails_controlled_and_previous_install_survives(self) -> None:
        self.assertEqual(self._install("--commit", "v1").returncode, 0)
        _, config = self._write_task()
        self.assertEqual(self._run_cli("run", str(config)).returncode, 0)

        broken = self.root / "macip_backup_poc-9.9.9-py3-none-any.whl"
        broken.write_bytes(b"isto nao e um wheel")
        failed = self._install(broken)
        self.assertNotEqual(failed.returncode, 0)

        # Instalação anterior continua utilizável.
        still = self._run_cli("run", str(config))
        self.assertEqual(still.returncode, 0, still.stderr + still.stdout)
        self.assertEqual(
            self.macip_home.joinpath("current").resolve().name, "0.2.0"
        )

        recovered = self._install("--commit", "v2")
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        self.assertEqual(self._run_cli("validate", str(config)).returncode, 0)

    def test_install_refuses_during_active_execution(self) -> None:
        self.assertEqual(self._install().returncode, 0)
        locks = self.state_dir / "locks"
        locks.mkdir(parents=True, exist_ok=True)
        lock_file = (locks / "task-inst.lock").open("a")
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            refused = self._install()
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn("execução ativa", refused.stderr)
        finally:
            lock_file.close()

    def test_home_with_spaces_installs_and_runs(self) -> None:
        spaced_home = self.root / "dir com espaços" / "home"
        spaced_home.mkdir(parents=True)
        self.env["HOME"] = str(spaced_home)
        self.bin = spaced_home / ".local" / "bin" / "macip-backup"

        installed = self._install("--commit", "esp")
        self.assertEqual(installed.returncode, 0, installed.stderr)

        source, config = self._write_task()
        run = self._run_cli("run", str(config))
        self.assertEqual(run.returncode, 0, run.stderr + run.stdout)

    def test_uninstall_preserves_operational_data(self) -> None:
        self.assertEqual(self._install().returncode, 0)
        _, config = self._write_task()
        self.assertEqual(self._run_cli("run", str(config)).returncode, 0)
        (self.state_dir / "logs" / "registro.jsonl").write_text("{}\n")

        removed = self._uninstall()
        self.assertEqual(removed.returncode, 0, removed.stderr)

        self.assertFalse(self.bin.exists())
        self.assertFalse((self.macip_home / "install.json").exists())
        self.assertFalse((self.macip_home / "releases").exists())
        self.assertTrue((self.config_dir / "agent.env").exists())
        self.assertTrue((self.state_dir / "logs" / "registro.jsonl").exists())
        self.assertIn("Preservado", removed.stdout)

    def test_uninstall_refuses_during_active_execution(self) -> None:
        self.assertEqual(self._install().returncode, 0)
        locks = self.state_dir / "locks"
        locks.mkdir(parents=True, exist_ok=True)
        lock_file = (locks / "task-inst.lock").open("a")
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            refused = self._uninstall()
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn("execução ativa", refused.stderr)
        finally:
            lock_file.close()
