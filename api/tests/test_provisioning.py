"""Preparação primária real no PostgreSQL; executor externo é tipado."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import base64
import json
from pathlib import Path
import fcntl
import os
import shlex
import shutil
import socket
import subprocess
import tempfile
import time
import uuid

import pytest
from sqlalchemy import text

from conftest import (
    ADMIN_TOKEN,
    csrf_headers,
    login,
    make_operator,
    requires_db,
)
from macip_backup_api.provisioning_routes import account_name
from macip_backup_api.db import engine, migrate_now
from macip_backup_api.provisioner import (
    LabFileVault, LabPrimaryAdapter, reconcile_operation, run_once,
)

pytestmark = requires_db

PROVISIONER = {
    "Authorization": "Bearer test-provisioner-token-1234567890abcdef"
}


def request_body(**overrides):
    body = {
        "id": str(uuid.uuid4()),
        "client_name": f"Cliente {uuid.uuid4().hex[:8]}",
        "installation_identifier": "pc-financeiro-01",
        "machine_name": "PC-FINANCEIRO",
        "suggested_time": "22:30",
    }
    body.update(overrides)
    return body


def prepare(client, body, headers=None):
    response = client.post(
        "/api/v1/clients/prepare",
        json=body,
        headers=headers or {"Authorization": f"Bearer {ADMIN_TOKEN}"},
    )
    assert response.status_code == 202, response.text
    return response.json()


def claim(client):
    response = client.post(
        "/api/v1/provisioner/operations/claim",
        json={"executor_id": "primary-a-lab"},
        headers=PROVISIONER,
    )
    assert response.status_code == 200, response.text
    return response.json()["operation"]


def success_for(operation):
    repository_id = "a" * 64
    account = account_name(
        uuid.UUID(operation["client_id"]), operation["installation_identifier"]
    )
    return {
        "claim_id": operation["claim_id"],
        "state": "succeeded",
        "error_code": None,
        "result": {
            "account_name": account,
            "repository_id": repository_id,
            "public_key_fingerprint": "SHA256:" + "A" * 43,
            "profile": {
                "nome": operation["client_name"],
                "repositorio": f"sftp://{account}@backup.lab//repository",
                "repositorio_id": repository_id,
                "known_hosts": "backup.lab ssh-ed25519 " + "A" * 44,
                "horario_sugerido": operation["suggested_time"],
            },
        },
    }


def finish(client, operation, body):
    return client.post(
        f"/api/v1/provisioner/operations/{operation['id']}/result",
        json=body,
        headers=PROVISIONER,
    )


class InProcessTransport:
    """Transporte de prova: mesma API real, sem abrir socket ou esconder o contrato."""

    def __init__(self, client):
        self.client = client

    def claim(self):
        return claim(self.client)

    def result(self, operation_id, body):
        response = self.client.post(
            f"/api/v1/provisioner/operations/{operation_id}/result",
            json=body,
            headers=PROVISIONER,
        )
        assert response.status_code == 200, response.text
        return response.json()


def test_two_clients_are_isolated_and_profile_stays_blocked(client):
    first = prepare(client, request_body(client_name="Cliente Alfa"))
    second = prepare(
        client,
        request_body(
            client_name="Cliente Beta",
            installation_identifier="pc-financeiro-01",
            machine_name="PC-BETA",
        ),
    )
    assert first["client_id"] != second["client_id"]
    assert first["installation"]["identifier"] == second["installation"]["identifier"]

    operations = [claim(client), claim(client)]
    for operation in operations:
        response = finish(client, operation, success_for(operation))
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["state"] == "primary_ready"
        assert body["protection_state"] == "pending"
        assert body["profile_available"] is False
        assert "profile" not in body and "vault_reference" not in body
        blocked = client.get(
            f"/api/v1/installations/{operation['installation_id']}/profile",
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        )
        assert blocked.status_code == 409
        assert "pendente" in blocked.json()["detail"]
        lab = client.get(
            f"/api/v1/lab/installations/{operation['installation_id']}/profile",
            headers=PROVISIONER,
        )
        assert lab.status_code == 200
        serialized = lab.text.lower()
        assert "password" not in serialized and "private" not in serialized


def test_two_simultaneous_requests_share_one_active_operation(client):
    base = request_body(client_name="Cliente Concorrente")
    bodies = [{**base, "id": str(uuid.uuid4())} for _ in range(2)]

    def send(body):
        return client.post(
            "/api/v1/clients/prepare",
            json=body,
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(send, bodies))
    assert [response.status_code for response in responses] == [202, 202]
    documents = [response.json() for response in responses]
    assert len({item["installation"]["id"] for item in documents}) == 1
    assert len({item["operation"]["id"] for item in documents}) == 1


def test_request_id_cannot_be_reused_for_different_input(client):
    body = request_body(client_name="Cliente Idempotente")
    prepare(client, body)
    changed = client.post(
        "/api/v1/clients/prepare",
        json={**body, "client_name": "Outro Cliente"},
        headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
    )
    assert changed.status_code == 409


def test_partial_failure_retry_and_late_results_do_not_regress(client):
    prepared = prepare(client, request_body(client_name="Cliente Retry"))
    first = claim(client)
    failed = {
        "claim_id": first["claim_id"],
        "state": "failed",
        "result": None,
        "error_code": "secret_missing",
    }
    response = finish(client, first, failed)
    assert response.status_code == 200
    assert response.json()["state"] == "failed"

    retry_id = str(uuid.uuid4())
    response = client.post(
        f"/api/v1/installations/{prepared['installation']['id']}/preparation/retry",
        json={"id": retry_id},
        headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
    )
    assert response.status_code == 202
    assert response.json()["operation"]["attempt"] == 2

    duplicate = finish(client, first, failed)
    assert duplicate.status_code == 200
    assert duplicate.json()["state"] == "pending"
    changed_late = finish(client, first, success_for(first))
    assert changed_late.status_code == 409

    second = claim(client)
    completed = finish(client, second, success_for(second))
    assert completed.status_code == 200
    assert completed.json()["state"] == "primary_ready"


def test_divergent_repository_identity_is_rejected_without_terminal_success(client):
    prepare(client, request_body(client_name="Cliente Divergência"))
    operation = claim(client)
    result = success_for(operation)
    result["result"]["profile"]["repositorio_id"] = "b" * 64
    response = finish(client, operation, result)
    assert response.status_code == 422
    current = client.get(
        f"/api/v1/installations/{operation['installation_id']}/preparation",
        headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
    )
    assert current.json()["state"] == "running"


@pytest.mark.parametrize("uri,host_line", [
    ("sftp:{account}@backup.lab:repository", "backup.lab"),
    ("sftp://{account}@backup.lab/repository", "backup.lab"),
    ("sftp://{account}@backup.lab//other", "backup.lab"),
    ("sftp://{account}@backup.lab:2222//repository", "backup.lab"),
    ("sftp://{account}@backup.lab:65536//repository", "[backup.lab]:65536"),
])
def test_profile_requires_absolute_fixed_path_and_matching_host_port(client, uri, host_line):
    prepare(client, request_body())
    operation = claim(client)
    result = success_for(operation)
    account = operation["account_name"]
    profile = result["result"]["profile"]
    profile["repositorio"] = uri.format(account=account)
    profile["known_hosts"] = host_line + " ssh-ed25519 " + "A" * 44
    assert finish(client, operation, result).status_code == 422


def test_migration_reclaims_v8_reservation_preserving_operation(client):
    queued = prepare(client, request_body())
    previous = claim(client)
    # Isolated test DB: reproduce the shape of a reservation written by v8.
    with engine().begin() as connection:
        connection.execute(text("ALTER TABLE provisioning_operations DROP COLUMN claim_expires_at"))
        connection.execute(text("DELETE FROM schema_migrations WHERE version = 9"))
    assert migrate_now() == [9]
    assert migrate_now() == []
    assert finish(client, previous, success_for(previous)).status_code == 409
    with ThreadPoolExecutor(max_workers=2) as executor:
        claims = list(executor.map(claim, (client, client)))
    assert claims.count(None) == 1
    resumed = next(operation for operation in claims if operation is not None)
    assert resumed["id"] == queued["operation"]["id"]
    assert resumed["claim_id"] != previous["claim_id"]
    assert resumed["attempt"] == previous["attempt"]
    assert resumed["installation_id"] == previous["installation_id"]
    assert finish(client, resumed, success_for(resumed)).status_code == 200


def test_viewer_cannot_prepare_or_retry_and_cookie_admin_needs_csrf(
    client, admin_headers, monkeypatch
):
    monkeypatch.setenv("MACIP_API_SESSION_INSECURE_COOKIE", "1")
    viewer = make_operator(
        client,
        admin_headers,
        login="viewer-provision",
        role="viewer",
        password="senha-viewer-provision-1",
    )
    assert login(client, viewer["login"], viewer["password"]).status_code == 200
    response = client.post("/api/v1/clients/prepare", json=request_body())
    assert response.status_code == 403

    client.cookies.clear()
    admin = make_operator(
        client,
        admin_headers,
        login="admin-provision",
        role="admin",
        password="senha-admin-provision-1",
    )
    assert login(client, admin["login"], admin["password"]).status_code == 200
    no_csrf = client.post("/api/v1/clients/prepare", json=request_body())
    assert no_csrf.status_code == 403
    allowed = client.post(
        "/api/v1/clients/prepare",
        json=request_body(client_name="Cliente CSRF"),
        headers=csrf_headers(client),
    )
    assert allowed.status_code == 202


def test_executor_auth_and_invalid_error_do_not_echo_input(client):
    prepare(client, request_body(client_name="Cliente Seguro"))
    assert client.post(
        "/api/v1/provisioner/operations/claim",
        json={"executor_id": "primary-a-lab"},
    ).status_code == 401
    operation = claim(client)
    secret = "SEGREDO-SINTETICO-NAO-ECOAR"
    response = finish(
        client,
        operation,
        {
            "claim_id": operation["claim_id"],
            "state": "failed",
            "result": None,
            "error_code": secret,
        },
    )
    assert response.status_code == 422
    assert secret not in response.text
    success = success_for(operation)
    success["result"]["password"] = secret
    response = finish(client, operation, success)
    assert response.status_code == 422
    assert secret not in response.text


@pytest.mark.skipif(
    not shutil.which("restic") or not shutil.which("ssh-keygen"),
    reason="Restic/ssh-keygen locais indisponíveis",
)
def test_full_api_executor_restic_flow_resumes_abandoned_claim(client):
    body = request_body(
        client_name="Cliente Integrado",
        installation_identifier="pc-integrado-01",
        machine_name="PC-INTEGRADO",
    )
    queued = prepare(client, body)
    with tempfile.TemporaryDirectory(
        prefix=".test-provisioning-e2e-", dir=Path.cwd()
    ) as directory:
        root = Path(directory)
        root.chmod(0o700)
        vault = LabFileVault(root / "vault")
        primary = LabPrimaryAdapter(
            root / "primary-a",
            host="backup.lab",
            known_hosts="backup.lab ssh-ed25519 " + "A" * 44,
        )
        class AbandonedExecutor(InProcessTransport):
            def claim(self):
                self.abandoned = super().claim()
                return self.abandoned

            def result(self, operation_id, body):
                self.unsent = body
                raise ConnectionError("executor encerrou antes de enviar")

        executor = AbandonedExecutor(client)
        with pytest.raises(ConnectionError):
            run_once(executor, vault, primary)
        previous = executor.abandoned
        repository_id = executor.unsent["result"]["repository_id"]
        assert claim(client) is None
        with engine().begin() as connection:
            connection.execute(text(
                "UPDATE provisioning_operations SET claim_expires_at = clock_timestamp() "
                "WHERE id = :id"
            ), {"id": previous["id"]})
        assert finish(client, previous, executor.unsent).status_code == 409

        lock = os.open(primary.root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
            assert run_once(InProcessTransport(client), vault, primary) == {"status": "busy"}
        finally:
            os.close(lock)

        class ResumedExecutor(InProcessTransport):
            def result(self, operation_id, body):
                assert operation_id == previous["id"]
                assert body["claim_id"] != previous["claim_id"]
                assert finish(client, previous, executor.unsent).status_code == 409
                assert body["result"] == executor.unsent["result"]
                return super().result(operation_id, body)

        result = run_once(ResumedExecutor(client), vault, primary)
        assert result["status"] == "succeeded"
        assert finish(client, previous, executor.unsent).status_code == 409

        completed = client.get(
            f"/api/v1/installations/{queued['installation']['id']}/preparation",
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        ).json()
        assert completed["state"] == "primary_ready"
        assert completed["repository_id"] == repository_id

        repeated = prepare(client, {**body, "id": str(uuid.uuid4())})
        assert repeated["operation"]["id"] == queued["operation"]["id"]
        assert repeated["repository_id"] == repository_id
        assert client.post(
            "/api/v1/provisioner/operations/claim",
            json={"executor_id": "primary-a-lab"},
            headers=PROVISIONER,
        ).json() == {"operation": None}

        accounts = [path for path in (root / "primary-a").iterdir() if path.is_dir()]
        assert len(accounts) == 1
        marker = (accounts[0] / ".repository-id").read_text().strip()
        assert marker == repository_id


@pytest.fixture
def sftp_profile(client, tmp_path):
    image = os.environ.get("MACIP_TEST_PROVISIONING_SFTP_IMAGE")
    if not image:
        pytest.skip("defina MACIP_TEST_PROVISIONING_SFTP_IMAGE com uma imagem OpenSSH local")

    def run(*args, **kwargs):
        return subprocess.run(args, check=True, capture_output=True, timeout=30, **kwargs)

    queued = prepare(client, request_body())
    operation = claim(client)
    account = operation["account_name"]
    name = f"macip-profile-test-{uuid.uuid4().hex[:12]}"
    hostkey = tmp_path / "hostkey"
    run("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(hostkey))
    run("docker", "run", "--pull=never", "--rm", "-d", "--name", name,
        "-p", "127.0.0.1::22", "--entrypoint", "/bin/sh", image, "-c", "exec sleep infinity")
    try:
        port = int(run("docker", "port", name, "22/tcp").stdout.decode().strip().rsplit(":", 1)[1])
        hostname = "127.0.0.1"
        host_public = hostkey.with_suffix(".pub").read_text().split()[:2]
        vault = LabFileVault(tmp_path / "vault")
        primary = LabPrimaryAdapter(
            tmp_path / "primary-a", host=hostname, port=port,
            known_hosts=f"[{hostname}]:{port} " + " ".join(host_public),
        )
        result = reconcile_operation(operation, vault, primary)
        result_body = {"claim_id": operation["claim_id"], "state": "succeeded", "result": result}
        assert finish(client, operation, result_body).status_code == 200
        installation = queued["installation"]["id"]
        exported = client.get(f"/api/v1/lab/installations/{installation}/profile", headers=PROVISIONER)
        assert exported.status_code == 200
        assert client.get(
            f"/api/v1/installations/{installation}/profile",
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        ).status_code == 409
        profile_path = tmp_path / "perfil.json"
        profile_path.write_bytes(exported.content)
        profile = exported.json()
        assert profile["repositorio"] == f"sftp://{account}@{hostname}:{port}//repository"

        bundle = json.loads(vault.get(f"installation/{installation}/bundle"))
        private = tmp_path / "id_ed25519"
        private.write_bytes(base64.b64decode(bundle["private_key"]))
        private.chmod(0o600)
        known_hosts = tmp_path / "known_hosts"
        known_hosts.write_text(profile["known_hosts"] + "\n")
        account_dir = primary.account_path(account)
        run("docker", "exec", name, "mkdir", "-p", "/jail", "/run/sshd")
        run("docker", "cp", str(account_dir / "repository"), f"{name}:/jail/repository")
        run("docker", "cp", str(account_dir / ".ssh/authorized_keys"), f"{name}:/authorized_keys")
        run("docker", "cp", str(hostkey), f"{name}:/hostkey")
        run("docker", "exec", name, "/bin/sh", "-eu", "-c", """
            useradd --create-home --shell /usr/sbin/nologin "$1"
            passwd --delete "$1"
            mkdir -p "/jail/home/$1" "/home/$1/.ssh"
            mv /authorized_keys "/home/$1/.ssh/authorized_keys"
            chown -R "$1:$1" "/home/$1/.ssh" /jail/repository
            chmod 700 "/home/$1/.ssh"
            chmod 600 "/home/$1/.ssh/authorized_keys"
            chmod 600 /hostkey
        """, "setup", account)
        run("docker", "exec", "-d", name, "/usr/sbin/sshd", "-D", "-E", "/sshd-test.log", "-f", "/dev/null",
            "-h", "/hostkey", "-oUsePAM=no", "-oPasswordAuthentication=no",
            "-oKbdInteractiveAuthentication=no", "-oPermitRootLogin=no",
            "-oDisableForwarding=yes", "-oAuthorizedKeysFile=.ssh/authorized_keys",
            f"-oAllowUsers={account}", "-oChrootDirectory=/jail", "-oForceCommand=internal-sftp",
            "-oSubsystem=sftp internal-sftp")
        for _ in range(30):
            try:
                with socket.create_connection((hostname, port), timeout=0.25) as conn:
                    if conn.recv(64).startswith(b"SSH-"):
                        break
            except OSError:
                pass
            time.sleep(0.1)
        else:
            pytest.fail("sshd descartável não respondeu")
        yield profile_path, profile, private, known_hosts, bundle["restic_password"], name
    finally:
        run("docker", "stop", "-t", "1", name)


def assert_exported_sftp_id(sftp_profile, tmp_path):
    _, profile, private, known_hosts, password, name = sftp_profile
    ssh_args = shlex.join([
        "-F", "/dev/null", "-oIdentityAgent=none", "-oGlobalKnownHostsFile=/dev/null",
        "-oBatchMode=yes", "-oStrictHostKeyChecking=yes", "-oIdentitiesOnly=yes",
        f"-oUserKnownHostsFile={known_hosts}", "-i", str(private),
    ])
    completed = subprocess.run(
        ["restic", "-r", profile["repositorio"], "--cache-dir", str(tmp_path / "remote-cache"),
         "-o", f"sftp.args={ssh_args}", "cat", "config"],
        env={**os.environ, "RESTIC_PASSWORD": password},
        capture_output=True, timeout=30,
    )
    if completed.returncode != 0:
        sshd_log = subprocess.check_output(
            ["docker", "exec", name, "cat", "/sshd-test.log"], timeout=10,
        )
        pytest.fail(completed.stderr.decode() + sshd_log.decode())
    assert json.loads(completed.stdout)["id"] == profile["repositorio_id"]


def test_exported_profile_opens_repository_over_real_chroot_sftp(sftp_profile, tmp_path):
    assert_exported_sftp_id(sftp_profile, tmp_path)


_POWERSHELL = os.environ.get("MACIP_TEST_POWERSHELL") or shutil.which("pwsh")


@pytest.mark.skipif(not _POWERSHELL, reason="PowerShell indisponível para executar Read-Perfil")
def test_windows_013_read_perfil_consumes_exported_json(sftp_profile, tmp_path):
    assert_exported_sftp_id(sftp_profile, tmp_path)
    profile_path, profile, *_ = sftp_profile
    windows_base = "7d62a800325146d1357f8832b1df5c4c3b2eff8d"
    for relative in ("gui/MacipBackup-Gui.ps1", "Backup-MacipCliente.ps1"):
        content = subprocess.check_output(["git", "show", f"{windows_base}:agent/windows/{relative}"])
        (tmp_path / Path(relative).name).write_bytes(content)
    harness = tmp_path / "read-profile.ps1"
    harness.write_text("""
param([string]$ProfilePath)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'MacipBackup-Gui.ps1') `
    -BaseDir (Join-Path $PSScriptRoot 'base') `
    -AgentePath (Join-Path $PSScriptRoot 'Backup-MacipCliente.ps1')
Read-Perfil -Caminho $ProfilePath | ConvertTo-Json -Compress
""")
    completed = subprocess.run(
        [_POWERSHELL, "-NoProfile", "-NonInteractive", "-File", str(harness), str(profile_path)],
        check=True, capture_output=True, timeout=30,
    )
    parsed = json.loads(completed.stdout)
    assert Path(parsed.pop("caminho")) == profile_path
    assert parsed == profile
