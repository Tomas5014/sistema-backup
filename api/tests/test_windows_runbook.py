"""Provas dos comandos fornecidos no roteiro, com arquivos fictícios."""

import json
import importlib.util
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time

import pytest

from macip_backup_api.provisioner import ApiTransport

ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "tools/windows-lab-server.py"
DATA = ROOT / "tools/windows-lab-data.ps1"
PWSH = os.environ.get("MACIP_TEST_POWERSHELL") or shutil.which("pwsh")


def test_manifest_windows_root_matches_snapshot():
    spec = importlib.util.spec_from_file_location("windows_lab", HELPER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source = "C:\\Area fictícia\\origem"
    reference = {"source": source, "files": {"sub/arquivo ç.txt": "a" * 64}}
    assert module.windows_manifest(reference, [source]) == {
        "C/Area fictícia/origem/sub/arquivo ç.txt": "a" * 64,
    }
    with pytest.raises(RuntimeError):
        module.windows_manifest(reference, ["D:\\outra"])
    with pytest.raises(RuntimeError):
        module.windows_manifest({**reference, "files": {"../escape": "a" * 64}}, [source])


@pytest.mark.skipif(not PWSH, reason="PowerShell indisponível")
def test_windows_manifest_commands(tmp_path):
    area = tmp_path / "dados ficticios"

    def invoke(*args):
        return subprocess.run([PWSH, "-NoProfile", "-File", str(DATA), *args],
                              capture_output=True, timeout=30)

    assert invoke("-Action", "Create", "-Area", str(area)).returncode == 0
    reference = json.loads((area / "reference.json").read_text())
    assert set(reference["files"]) == {"documento.txt", "subpasta/binario.bin"}
    assert invoke("-Action", "Create", "-Area", str(area)).returncode != 0
    restored = tmp_path / "recuperado"
    shutil.copytree(area / "origem", restored)
    arguments = ["-Reference", str(area / "reference.json"), "-Target", str(restored)]
    assert invoke("-Action", "CheckFolder", *arguments).returncode != 0  # excluído presente
    (restored / "ignorar.tmp").unlink()
    assert invoke("-Action", "CheckFolder", *arguments).returncode == 0
    assert invoke("-Action", "CheckFile", "-Reference", str(area / "reference.json"),
                  "-Target", str(restored / "documento.txt")).returncode == 0
    (restored / "documento.txt").write_text("alterado")
    assert invoke("-Action", "CheckFolder", *arguments).returncode != 0
    linked = tmp_path / "atalho"
    linked.symlink_to(area, target_is_directory=True)
    assert invoke("-Action", "Create", "-Area", str(linked / "novo")).returncode != 0


@pytest.mark.skipif(
    not os.environ.get("MACIP_TEST_PROVISIONING_SFTP_IMAGE"),
    reason="OpenSSH Docker descartável não configurado",
)
def test_server_helper_real_flow(tmp_path):
    area = tmp_path / "laboratório com espaços"
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    env = {**os.environ, "PYTHONPATH": str(ROOT / "api/src")}

    def invoke(action, *args):
        result = subprocess.run(
            [sys.executable, str(HELPER), action, str(area), *args],
            env=env, capture_output=True, timeout=120,
        )
        if result.returncode and (area / "private/last-restic-error").exists():
            pytest.fail((area / "private/last-restic-error").read_text())
        assert result.returncode == 0, result.stdout.decode() + result.stderr.decode()
        return result

    invoke("init", "--sftp-ip", "127.0.0.1", "--sftp-image",
           os.environ["MACIP_TEST_PROVISIONING_SFTP_IMAGE"], "--postgres-image",
           os.environ.get("MACIP_TEST_POSTGRES_IMAGE", "postgres:16-alpine"), "--api-port", str(port))
    cfg = json.loads((area / "private/config.json").read_text())
    process = subprocess.Popen([sys.executable, str(HELPER), "api", str(area)],
                               env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    volume = None
    try:
        admin = ApiTransport(cfg["api"], cfg["admin"], "test-admin", allow_lab_http=True)
        for _ in range(40):
            try:
                admin.request("/healthz")
                break
            except RuntimeError:
                time.sleep(0.25)
        else:
            pytest.fail("API descartável não iniciou")
        prepared = admin.request("/api/v1/clients/prepare", {
            "id": "4ab6c4ef-0c9a-4d7d-86be-9004f8aca2ca", "client_name": "Ficticio runbook",
            "installation_identifier": "vm-runbook-01", "machine_name": "VM-LAB",
            "suggested_time": "22:30",
        })
        install_id = prepared["installation"]["id"]
        invoke("prepare-a", "--installation", install_id)
        invoke("seed")
        invoke("prepare-b")
        volume = Path((area / "private/volume").read_text())
        invoke("copy-seed")
        with pytest.raises(RuntimeError):
            admin.request(f"/api/v1/installations/{install_id}/profile")
        invoke("prove-seed")
        exported = admin.request(f"/api/v1/installations/{install_id}/profile")
        assert exported["repositorio_id"] == json.loads((area / "evidence/primary.json").read_text())["repository_id"]
        invoke("export-credentials")
        assert (area / "private/delivery/id_windows").read_text() == (area / "private/windows-key").read_text()
        evidence = json.loads((area / "evidence/seed-proof.json").read_text())
        assert evidence["manifest_sha256"] == evidence["restored_sha256"]
        assert evidence["duration_seconds"] > 0
        invoke("stop-a")
        # Mesma prova permanece possível após desligamento de A.
        (area / "evidence/seed-proof.json").unlink()
        invoke("prove-seed")
        serialized = "".join(path.read_text() for path in (area / "evidence").iterdir())
        for secret in (cfg["admin"], cfg["a_token"], cfg["b_token"],
                       (area / "private/windows-password").read_text()):
            assert secret not in serialized
    finally:
        process.terminate()
        process.wait(timeout=10)
        invoke("stop")
        if volume and volume.name == "macip-windows-" + install_id.replace("-", ""):
            shutil.rmtree(volume)
