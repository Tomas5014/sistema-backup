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
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
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


def _build_variant_wheel(work_dir: Path, dist_dir: Path, version: str) -> Path:
    """Wheel de outra release: copia src+pyproject, injeta a versão
    em ambos — distinguível por __version__."""
    dist_dir.mkdir(parents=True, exist_ok=True)
    shutil.copytree(ROOT / "src", work_dir / "src")
    (work_dir / "pyproject.toml").write_text(
        (ROOT / "pyproject.toml")
        .read_text()
        .replace('version = "0.2.1"', f'version = "{version}"')
    )
    init = work_dir / "src" / "macip_backup" / "__init__.py"
    init.write_text(
        init.read_text().replace(
            '__version__ = "0.2.1"', f'__version__ = "{version}"'
        )
    )
    subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import os; "
                f"os.chdir({str(work_dir)!r}); "
                "from setuptools.build_meta import build_wheel; "
                f"build_wheel({str(dist_dir)!r})"
            ),
        ],
        check=True,
        capture_output=True,
    )
    wheels = list(dist_dir.glob("macip_backup_poc-*.whl"))
    if len(wheels) != 1:
        raise RuntimeError(f"wheel variante não produzido: {wheels}")
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
        connection = sqlite3.connect(db)
        connection.execute("CREATE TABLE t (x TEXT)")
        connection.execute("INSERT INTO t VALUES ('historico')")
        connection.commit()
        connection.close()

        again = self._install("--commit", "bbb")
        self.assertEqual(again.returncode, 0, again.stderr)

        self.assertEqual(marker.read_text(), "não apagar")
        connection = sqlite3.connect(db)
        row = connection.execute("SELECT x FROM t").fetchone()
        connection.close()
        self.assertEqual(row[0], "historico")
        # update criou backup consistente do banco
        self.assertTrue(list((self.state_dir / "db-backups").iterdir()))
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
        self.assertTrue(
            self.macip_home.joinpath("current")
            .resolve()
            .name.startswith("0.2.1-")
        )

        recovered = self._install("--commit", "v2")
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        self.assertEqual(self._run_cli("validate", str(config)).returncode, 0)

    def test_install_refused_during_active_task(self) -> None:
        """Executor segura SH em maintenance.lock → install recusa."""
        self.assertEqual(self._install().returncode, 0)
        locks = self.state_dir / "locks"
        locks.mkdir(parents=True, exist_ok=True)
        lock_file = (locks / "maintenance.lock").open("a")
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
            refused = self._install()
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn("em andamento", refused.stderr)
        finally:
            lock_file.close()

    def test_run_refused_during_maintenance(self) -> None:
        """Manutenção com EX → `run` da mesma instalação recusa."""
        self.assertEqual(self._install().returncode, 0)
        _, config = self._write_task()
        locks = self.state_dir / "locks"
        lock_file = (locks / "maintenance.lock").open("a")
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            refused = self._run_cli("run", str(config))
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn("manutenção", refused.stderr + refused.stdout)
        finally:
            lock_file.close()

    def test_two_tasks_share_maintenance_outside_it(self) -> None:
        """Dois locks SH de tarefas diferentes coexistem fora de manutenção."""
        self.assertEqual(self._install().returncode, 0)
        locks = self.state_dir / "locks"
        a = (locks / "task-a.lock").open("a")
        b = (locks / "task-b.lock").open("a")
        m1 = (locks / "maintenance.lock").open("a")
        m2 = (locks / "maintenance.lock").open("a")
        try:
            for lock in (a, b, m1, m2):
                fcntl.flock(lock.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
            fcntl.flock(a.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(b.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            for lock in (a, b, m1, m2):
                lock.close()

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

    def test_uninstall_refuses_during_active_task(self) -> None:
        self.assertEqual(self._install().returncode, 0)
        locks = self.state_dir / "locks"
        locks.mkdir(parents=True, exist_ok=True)
        lock_file = (locks / "maintenance.lock").open("a")
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
            refused = self._uninstall()
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn("em andamento", refused.stderr)
        finally:
            lock_file.close()

    def test_invalid_wheel_same_version_preserves_previous(self) -> None:
        """Wheel quebrado da MESMA versão: falha antes da ativação e a
        CLI anterior continua funcionando."""
        self.assertEqual(self._install().returncode, 0)
        _, config = self._write_task()
        self.assertEqual(self._run_cli("run", str(config)).returncode, 0)

        broken = self.root / "macip_backup_poc-0.2.1-py3-none-any.whl"
        broken.write_bytes(b"zip corrompido, mesmo nome")
        failed = self._install(broken)
        self.assertNotEqual(failed.returncode, 0)

        still = self._run_cli("run", str(config))
        self.assertEqual(still.returncode, 0, still.stderr + still.stdout)

    def test_same_artifact_reinstall_is_idempotent(self) -> None:
        self.assertEqual(self._install().returncode, 0)
        release = self.macip_home.joinpath("current").resolve()
        again = self._install()
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(
            self.macip_home.joinpath("current").resolve(), release
        )
        self.assertIn("reutilizando", again.stdout)

    def test_same_version_different_artifact_distinguished(self) -> None:
        """Mesmo nome de wheel, conteúdo diferente → releases distintos."""
        self.assertEqual(self._install().returncode, 0)
        first = self.macip_home.joinpath("current").resolve()

        variant = self.root / "macip_backup_poc-0.2.1-py3-none-any.whl"
        with zipfile.ZipFile(self.wheel) as zin, zipfile.ZipFile(
            variant, "w", zipfile.ZIP_DEFLATED
        ) as zout:
            for item in zin.namelist():
                zout.writestr(item, zin.read(item))
            zout.writestr("macip_backup/_variant.txt", "distinto")
        updated = self._install(variant)
        self.assertEqual(updated.returncode, 0, updated.stderr)
        second = self.macip_home.joinpath("current").resolve()

        self.assertNotEqual(first, second)
        self.assertTrue(first.name.startswith("0.2.1-"))
        self.assertTrue(second.name.startswith("0.2.1-"))
        self.assertTrue(first.exists(), "release anterior preservada")

    def test_service_generated_by_old_release_runs_new_code(self) -> None:
        """Unit gerada na release A executa a release B após update —
        ExecStart no launcher, sem PYTHONPATH preso."""
        self.assertEqual(self._install().returncode, 0)
        _, config = self._write_task()
        scheduled = self._run_cli(
            "schedule", str(config),
            "--store", str(self.state_dir / "agent.db"),
            "--calendar", "daily",
        )
        self.assertEqual(scheduled.returncode, 0, scheduled.stderr + scheduled.stdout)
        service = (
            self.home / ".config" / "systemd" / "user"
            / "macip-backup-agent-task-inst.service"
        ).read_text()
        self.assertIn(str(self.bin), service)          # ExecStart = launcher
        self.assertNotIn("PYTHONPATH", service)         # sem pin de release
        self.assertIn(
            f"# macip-backup: install={self.macip_home}", service
        )                                              # identidade persistida

        before = self._run_cli("--version")
        self.assertIn("0.2.1", before.stdout)

        wheel_021 = _build_variant_wheel(
            self.root / "pkg-021", self.dist_dir / "v021", "0.2.2"
        )
        updated = self._install(wheel_021)
        self.assertEqual(updated.returncode, 0, updated.stderr)

        after = self._run_cli("--version")
        self.assertIn("0.2.2", after.stdout)  # o que o service vai executar

    def test_uninstall_preserves_foreign_and_unmanaged(self) -> None:
        """Unit com prefixo semelhante mas sem marcador + binário alheio:
        tudo permanece."""
        self.assertEqual(self._install().returncode, 0)
        unit_dir = self.home / ".config" / "systemd" / "user"
        unit_dir.mkdir(parents=True)
        foreign = unit_dir / "macip-backup-alien.timer"
        foreign.write_text("[Timer]\nOnCalendar=daily\n")  # sem marcador
        other_install = unit_dir / "macip-backup-agent-outro.timer"
        other_install.write_text(
            # B compartilha PREFIXO com A — substring grep marcaria errado;
            # só comparação exata distingue.
            f"# macip-backup: install={self.macip_home}-teste\n"
            "[Timer]\nOnCalendar=daily\n"
        )
        ours = unit_dir / "macip-backup-agent-task-inst.timer"
        ours.write_text(
            f"# macip-backup: install={self.macip_home}\n"
            "[Timer]\nOnCalendar=daily\n"
        )
        self.env["MACIP_SYSTEMCTL"] = str(self._stub_systemctl(0))

        removed = self._uninstall()
        self.assertEqual(removed.returncode, 0, removed.stderr)
        self.assertTrue(foreign.exists())
        self.assertTrue(
            other_install.exists(), "unit de OUTRA instalação MAC IP preservada"
        )
        self.assertFalse(ours.exists())
        self.assertIn("macip-backup-alien.timer", removed.stdout)

    def test_uninstall_aborts_on_systemctl_failure(self) -> None:
        """Falha ao desativar unit nossa → aborta ANTES de remover programa."""
        self.assertEqual(self._install().returncode, 0)
        unit_dir = self.home / ".config" / "systemd" / "user"
        unit_dir.mkdir(parents=True)
        ours = unit_dir / "macip-backup-task-inst.service"
        ours.write_text(
            f"# macip-backup: install={self.macip_home}\n"
            "[Service]\nType=oneshot\n"
        )
        self.env["MACIP_SYSTEMCTL"] = str(self._stub_systemctl(1))

        refused = self._uninstall()
        self.assertNotEqual(refused.returncode, 0)
        self.assertTrue(
            (self.macip_home / "releases").exists(),
            "programa removido apesar da falha do systemctl",
        )

    def _stub_systemctl(self, returncode: int) -> Path:
        stub = self.root / f"systemctl-stub-{returncode}"
        stub.write_text(f"#!/bin/sh\nexit {returncode}\n")
        stub.chmod(0o755)
        return stub

    def test_foreign_executable_at_bin_path_refused_and_preserved(self) -> None:
        """Executável alheio em ~/.local/bin/macip-backup → instalação
        recusa; conteúdo byte a byte intacto e nada criado."""
        bin_dir = self.home / ".local" / "bin"
        bin_dir.mkdir(parents=True)
        foreign = bin_dir / "macip-backup"
        payload = b"#!/bin/sh\necho programa-alheio\n"
        foreign.write_bytes(payload)
        foreign.chmod(0o755)

        refused = self._install()
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("propriedade", refused.stderr)
        self.assertEqual(foreign.read_bytes(), payload)
        self.assertFalse((self.macip_home / "current").exists())

    def test_custom_layout_keeps_maintenance_exclusion(self) -> None:
        """MACIP_STATE personalizado: o lock de manutenção mora no
        layout configurado — propagado ao launcher e ao agente."""
        custom_state = self.root / "estado-custom"
        self.env["MACIP_STATE"] = str(custom_state)
        self.env["MACIP_CONFIG"] = str(self.root / "config-custom")
        self.env["MACIP_CACHE"] = str(self.root / "cache-custom")
        self.assertEqual(self._install().returncode, 0)

        _, config = self._write_task()
        maintenance = custom_state / "locks" / "maintenance.lock"
        lock_file = maintenance.open("a")
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            refused = self._run_cli("run", str(config))
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn("manutenção", refused.stderr + refused.stdout)
        finally:
            lock_file.close()

        # Fora da manutenção: run funciona no mesmo layout customizado.
        ok = self._run_cli("run", str(config))
        self.assertEqual(ok.returncode, 0, ok.stderr + ok.stdout)

    def test_install_and_agent_use_same_locks_in_custom_layout(self) -> None:
        """Executor SH em MACIP_STATE/locks/maintenance.lock bloqueia
        install.sh — prova que os dois resolvem o mesmo caminho."""
        custom_state = self.root / "estado-custom"
        self.env["MACIP_STATE"] = str(custom_state)
        self.assertEqual(self._install().returncode, 0)

        lock_file = (custom_state / "locks" / "maintenance.lock").open("a")
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
            refused = self._install()
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn("em andamento", refused.stderr)
        finally:
            lock_file.close()

    def test_install_over_other_install_launcher_refused(self) -> None:
        """Launcher de B (prefixo compartilhado) no caminho de A →
        install recusa e o arquivo segue byte a byte igual."""
        bin_dir = self.home / ".local" / "bin"
        bin_dir.mkdir(parents=True)
        b_home = self.root / "b-macip-backup-teste"  # prefixo comum com A
        payload = (
            "#!/bin/sh\n"
            f"# macip-backup: install={b_home}\n"
            'exec /bin/true\n'
        ).encode()
        foreign = bin_dir / "macip-backup"
        foreign.write_bytes(payload)
        foreign.chmod(0o755)

        refused = self._install()
        self.assertNotEqual(refused.returncode, 0)
        self.assertEqual(foreign.read_bytes(), payload)

    def test_reinstall_over_own_launcher_works(self) -> None:
        """Reinstalar A sobre seu próprio launcher segue funcionando."""
        self.assertEqual(self._install().returncode, 0)
        marker = (
            f"# macip-backup: install={self.macip_home}"
        ).encode()
        self.assertIn(marker, self.bin.read_bytes())
        again = self._install()
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertIn(marker, self.bin.read_bytes())
