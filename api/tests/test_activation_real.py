"""Resgate HTTPS + chave gerada por PowerShell + SFTP/Restic/B reais em Linux."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
from threading import Thread
import time
import uuid

import pytest
from sqlalchemy import text
import uvicorn

from conftest import requires_db
from macip_backup_api.activation_executor import LabActivationExecutor, create_app
from macip_backup_api.provisioner import LabFileVault
from macip_backup_api.protector import LabProtector
from macip_backup_api.db import engine
from test_activation import enabled_activation, issue
from test_protected_copy import (
    enabled_protection, InProcessProtector, _make_point, _a_read_password, _private, _run,
)
from test_provisioning import PROVISIONER, lab_sftp_profile

pytestmark = requires_db


@contextmanager
def https_server(app, certificate, private):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        server = uvicorn.Server(uvicorn.Config(
            app, lifespan="off", log_level="warning", access_log=False,
            ssl_certfile=str(certificate), ssl_keyfile=str(private),
        ))
        thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.02)
        else:
            raise RuntimeError("HTTPS não iniciou")
        try:
            yield f"https://localhost:{listener.getsockname()[1]}"
        finally:
            server.should_exit = True
            thread.join(timeout=10)


class ExecutorTransport:
    def __init__(self, client):
        self.client = client

    def request(self, path, body):
        response = self.client.post(path, json=body, headers=PROVISIONER)
        if response.status_code != 200:
            raise RuntimeError("recusado pela central")
        return response.json()


def test_real_https_powershell_key_and_B_reproof(
    client, tmp_path, enabled_activation, enabled_protection, monkeypatch, caplog,
):
    monkeypatch.setenv("MACIP_API_LAB_ALERTS", "1")
    pwsh = os.environ.get("MACIP_TEST_POWERSHELL")
    windows = os.environ.get("MACIP_TEST_WINDOWS_WORKTREE")
    if not pwsh or not windows:
        pytest.skip("defina PowerShell e worktree Windows da ativação")
    certificate, private = tmp_path / "cert.pem", tmp_path / "tls-key.pem"
    _run("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
         "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost",
         "-keyout", str(private), "-out", str(certificate))
    source_root = tmp_path / "a"
    source_root.mkdir(mode=0o700)
    state = tmp_path / "b-state"
    state.mkdir(mode=0o700)
    with (
        lab_sftp_profile(client, source_root) as (sftp, installation, read_key, _),
        tempfile.TemporaryDirectory(prefix="macip-activation-b-", dir="/dev/shm") as directory,
    ):
        volume = Path(directory)
        volume_id = uuid.uuid4()
        _private(volume / ".macip-volume", str(volume_id))
        b = LabProtector(Path("/dev/shm"), volume, volume_id, state, reserve_bytes=1024**2)
        b_api = InProcessProtector(client)
        password_a = _a_read_password(sftp, source_root)
        point, expected = _make_point(sftp, source_root / "dados")
        source = {"source_key": read_key, "source_password_file": password_a}
        b.execute(b_api, installation, "prepare", **source)
        b.execute(b_api, installation, "copy", snapshot=point, **source)
        manifest = _private(tmp_path / "manifest.json", json.dumps(expected))
        b.execute(b_api, installation, "prove", manifest=manifest)
        executor = LabActivationExecutor(
            ExecutorTransport(client), source_root / "primary-a", LabFileVault(source_root / "vault"),
            lab_container=sftp[-1],
        )
        original = _run("docker", "exec", sftp[-1], "cat", f"/home/{sftp[1]['repositorio'].split('@')[0][7:]}/.ssh/authorized_keys")
        with (
            https_server(client.app, certificate, private) as central_url,
            https_server(create_app(executor), certificate, private) as delivery_url,
        ):
            monkeypatch.setenv("MACIP_API_ACTIVATION_PUBLIC_URL", central_url)
            monkeypatch.setenv("MACIP_API_ACTIVATION_DELIVERY_URL", delivery_url + "/v1/activate")
            code = issue(client, str(installation))
            code_file = _private(tmp_path / "code.json", json.dumps(code))
            win_base = tmp_path / "windows"
            win_base.mkdir()
            # Restic real é executado por um shim PowerShell no Linux.
            (win_base / "restic-real.ps1").write_text("& /usr/bin/restic @args\nexit $LASTEXITCODE")
            harness = tmp_path / "activate.ps1"
            harness.write_text("""
param($Windows,$Base,$CodeFile)
$ErrorActionPreference='Stop'
. (Join-Path $Windows 'agent/windows/gui/MacipBackup-Gui.ps1') -BaseDir $Base -AgentePath (Join-Path $Windows 'agent/windows/Backup-MacipCliente.ps1')
function Protect-ResticPassword([securestring]$Senha,[string]$Destino) {
    $ptr=[Runtime.InteropServices.Marshal]::SecureStringToBSTR($Senha)
    try { $v=[Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) }
    [IO.File]::WriteAllText($Destino,[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($v)))
}
function Read-ResticPassword([string]$Caminho) {
    ConvertTo-SecureString ([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String([IO.File]::ReadAllText($Caminho)))) -AsPlainText -Force
}
function Get-SshExe { '/usr/bin/ssh' }
$script:ResticExeNome='restic-real.ps1'
$code=(Get-Content -LiteralPath $CodeFile -Raw | ConvertFrom-Json).activation_code
$result=Invoke-CodeActivation -Code $code -Directory (Join-Path $Base 'activation-code') -WaitSeconds 0
[pscustomobject]@{state=$result.State} | ConvertTo-Json -Compress
""")

            def activate(trusted=True):
                environment = {**os.environ, "NO_PROXY": "localhost,127.0.0.1"}
                if trusted:
                    environment["SSL_CERT_FILE"] = str(certificate)
                else:
                    environment.pop("SSL_CERT_FILE", None)
                return subprocess.run(
                    [pwsh, "-NoProfile", "-File", str(harness), windows, str(win_base), str(code_file)],
                    env=environment, capture_output=True, timeout=100,
                )

            refused = activate(trusted=False)
            assert refused.returncode != 0  # CA desconhecida é recusada
            pending = activate()
            assert pending.returncode == 0, pending.stderr.decode()
            assert json.loads(pending.stdout)["state"] == "pending"
            registered = _run("docker", "exec", sftp[-1], "cat",
                              f"/home/{sftp[1]['repositorio'].split('@')[0][7:]}/.ssh/authorized_keys")
            assert registered.startswith(original)
            assert registered.count(b'internal-sftp -R') == original.count(b'internal-sftp -R') == 1
            assert json.loads(activate().stdout)["state"] == "pending"
            b.execute(b_api, installation, "prepare", **source)
            b.execute(b_api, installation, "copy", snapshot=point, **source)
            b.execute(b_api, installation, "prove", manifest=manifest)
            ready = activate()
            assert ready.returncode == 0, ready.stderr.decode()
            assert json.loads(ready.stdout)["state"] == "ready"
            assert json.loads(activate().stdout)["state"] == "ready"
            common = client.get(f"/api/v1/installations/{installation}/preparation",
                                headers={"Authorization": "Bearer test-admin-9f8e7d6c5b4a3210"})
            assert sftp[4] not in common.text
            assert code["activation_code"] not in common.text
            assert sftp[4] not in caplog.text
            assert code["activation_code"] not in caplog.text
            with engine().connect() as db:
                rows = str(db.execute(text("SELECT * FROM activation_codes")).all())
                assert sftp[4] not in rows
            assert not (win_base / "gui.json").exists()
            # Envio Windows por HTTPS real. DPAPI continua explicitamente simulado no harness Linux.
            telemetry = tmp_path / "telemetry.ps1"
            telemetry.write_text(harness.read_text().split("$code=(Get-Content")[0] + r'''
$directory=Join-Path $Base 'activation-code'
$monitorPath=Join-Path $directory 'monitoring.json'
$monitor=Get-Content -LiteralPath $monitorPath -Raw | ConvertFrom-Json
$queue=Join-Path $Base 'queue'
New-Item -ItemType Directory -Path $queue | Out-Null
$monitor | Add-Member NoteProperty queue $queue
Write-MonitorJson $monitorPath $monitor
$config=Join-Path $directory 'backup.json'
Set-MonitorSchedule $config '10:00' $false
$event=Start-MonitorResult $config $args[0] 'manual' ([DateTime]::UtcNow.AddMinutes(-1).ToString('o'))
Complete-MonitorResult $event @{fim_utc=[DateTime]::UtcNow.ToString('o');status='sucesso';codigo_restic=0;snapshot=$args[1]}
$realHttp=${function:Invoke-MonitorHttp}
$script:lost=$true
function Invoke-MonitorHttp($Monitor,$Path,$Body) {
    & $realHttp $Monitor $Path $Body
    if ($Path -eq 'executions' -and $script:lost) { $script:lost=$false; throw 'resposta perdida simulada apos aceite real' }
}
try { [void](Sync-MonitorQueue $config) } catch { }
if (@(Get-ChildItem $queue -Filter 'event-*.json').Count -ne 1) { throw 'perdeu resultado nao confirmado' }
[void](Sync-MonitorQueue $config)
if (@(Get-ChildItem $queue -Filter 'event-*.json').Count -ne 0) { throw 'reenvio falhou' }
''')
            sent = subprocess.run([pwsh,"-NoProfile","-File",str(telemetry),windows,str(win_base),str(code_file),sftp[1]["repositorio_id"],point],
                                  env={**os.environ,"SSL_CERT_FILE":str(certificate),"NO_PROXY":"localhost,127.0.0.1"},capture_output=True,timeout=60)
            assert sent.returncode == 0, sent.stderr.decode()
            with engine().connect() as db:
                assert db.scalar(text("SELECT count(*) FROM windows_results WHERE agent_id=:id"), {"id":installation}) == 1
            received = client.get(f"/api/v1/installations/{installation}/alerts", headers={"Authorization":"Bearer test-admin-9f8e7d6c5b4a3210"})
            assert received.json()["last_backup"]["snapshot_id"] == point
            assert sftp[4] not in received.text
