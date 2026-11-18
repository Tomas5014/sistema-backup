"""Integração de B com PostgreSQL, OpenSSH, Restic e seccomp reais."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import secrets
import shlex
import socket
import subprocess
import sys
import tempfile
from threading import Thread
from urllib.parse import urlsplit
import uuid

import pytest
from sqlalchemy import text

from conftest import ADMIN_TOKEN, make_operator, requires_db
from macip_backup_api.db import engine
from macip_backup_api.protector import LabProtector, ProtectionError
from test_provisioning import PROVISIONER, claim, finish, lab_sftp_profile, prepare, request_body, success_for

pytestmark = requires_db
TOKEN = "test-protector-token-distinct-123456789012345"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
ADMIN = {"Authorization": f"Bearer {ADMIN_TOKEN}"}


@pytest.fixture
def enabled_protection(monkeypatch):
    monkeypatch.setenv("MACIP_API_LAB_PROTECTION", "1")
    monkeypatch.setenv("MACIP_API_PROTECTOR_TOKEN", TOKEN)


def _endpoint(installation):
    return f"/api/v1/protector/installations/{installation}"


def _profile_route(installation):
    return f"/api/v1/installations/{installation}/profile"


def _public(client, installation):
    response = client.get(f"/api/v1/installations/{installation}/preparation", headers=ADMIN)
    assert response.status_code == 200
    return response.json()


@pytest.fixture
def browser_gate(client, monkeypatch):
    if os.environ.get("MACIP_TEST_PROTECTED_BROWSER") != "1":
        yield None
        return
    import uvicorn
    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles

    portal = Path(__file__).resolve().parents[2] / "portal"
    dist = portal / "dist"
    monkeypatch.setenv("MACIP_API_SESSION_INSECURE_COOKIE", "1")
    operator = make_operator(
        client, ADMIN, login=f"proof-b-{uuid.uuid4().hex[:10]}",
        name="Operador de prova B", role="admin", password=secrets.token_urlsafe(24),
    )
    client.app.mount("/assets", StaticFiles(directory=dist / "assets"))
    client.app.add_api_route("/{path:path}", lambda: FileResponse(dist / "index.html"))
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        origin = f"http://127.0.0.1:{listener.getsockname()[1]}"
        server = uvicorn.Server(uvicorn.Config(client.app, lifespan="off", log_level="warning"))
        thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()

        def check(client_id, state, repo):
            _run(
                "node", str(portal / "e2e/protected-copy.browser.cjs"), origin, client_id, state,
                os.environ.get("MACIP_TEST_PROTECTED_SCREENSHOTS", ""),
                env={
                    **os.environ, "MACIP_TEST_BROWSER_LOGIN": operator["login"],
                    "MACIP_TEST_BROWSER_PASSWORD": operator["password"], "MACIP_TEST_EXPECTED_REPO_ID": repo,
                },
            )

        try:
            yield check, origin
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            assert not thread.is_alive()


class InProcessProtector:
    def __init__(self, client):
        self.client = client

    def request(self, path, body=None):
        response = (
            self.client.get(path, headers=AUTH) if body is None
            else self.client.post(path, headers=AUTH, json=body)
        )
        assert response.status_code == 200, response.text
        return response.json()


def test_api_scopes_binding_copy_and_simulated_proof(client, enabled_protection, monkeypatch):
    prepared = prepare(client, request_body())
    operation = claim(client)
    assert finish(client, operation, success_for(operation)).status_code == 200
    installation = prepared["installation"]["id"]
    base = _endpoint(installation)
    assert client.get(base, headers=PROVISIONER).status_code == 403
    assert client.get(base, headers=ADMIN).status_code == 403
    assert client.post(
        "/api/v1/provisioner/operations/claim", headers=AUTH, json={"executor_id": "lab-b"}
    ).status_code == 403
    api = InProcessProtector(client)
    source = api.request(base)
    values = {
        "previous_binding": None, "source": source["source"], "volume_id": str(uuid.uuid4()),
        "destination": "a" * 64, "repository_id": "c" * 64,
    }
    bound = api.request(base + "/binding", values)
    point = {"binding_id": bound["binding_id"], "snapshot_a": "d" * 64, "snapshot_b": "e" * 64}
    assert _public(client, installation)["protection_state"] == "prepared"
    copy = api.request(base + "/copy", point)
    assert api.request(base + "/copy", point) == copy
    assert client.get(_profile_route(installation), headers=ADMIN).status_code == 409
    evidence = {
        **point, "method": "simulation", "files": 1,
        "manifest_sha256": "b" * 64, "restored_sha256": "b" * 64,
    }
    assert client.post(base + "/proof", headers=AUTH, json=evidence).status_code == 422
    evidence["method"] = "restic-offline-sha256-v1"
    assert client.post(base + "/proof", headers=AUTH, json={
        **evidence, "restored_sha256": "f" * 64,
    }).status_code == 409
    relocated = api.request(base + "/binding", {
        **values, "previous_binding": bound["binding_id"], "destination": "f" * 64,
    })
    assert relocated["binding_id"] != point["binding_id"]
    assert client.post(base + "/proof", headers=AUTH, json=evidence).status_code == 409
    assert client.get(_profile_route(installation), headers=ADMIN).status_code == 409
    monkeypatch.delenv("MACIP_API_LAB_PROTECTION")
    assert client.get(base, headers=AUTH).status_code == 404


def _private(path, content):
    path.write_text(content, encoding="utf-8")
    path.chmod(0o600)
    return path


def _run(*args, **kwargs):
    result = subprocess.run(args, timeout=60, capture_output=True, **kwargs)
    assert result.returncode == 0, result.stderr.decode()
    return result.stdout


def _sftp_prefix(profile, key, hosts):
    uri = urlsplit(profile["repositorio"])
    return (
        "sftp", "-F", "/dev/null", "-oIdentityAgent=none", "-oIdentitiesOnly=yes",
        "-oGlobalKnownHostsFile=/dev/null", "-oStrictHostKeyChecking=yes",
        f"-oUserKnownHostsFile={hosts}", "-i", str(key), "-P", str(uri.port),
        "-b", "-", f"{uri.username}@{uri.hostname}",
    )


def _make_point(sftp, path):
    path.mkdir()
    sample = path / "dados ç.txt"
    sample.write_bytes(b"arquivo sintetico macip\n")
    (path / "ignorar.tmp").write_bytes(b"exclusao de teste\n")
    reference = {sample.as_posix().lstrip("/"): hashlib.sha256(sample.read_bytes()).hexdigest()}
    output = _client_restic(sftp, "backup", "--json", "--exclude", "*.tmp", str(path))
    summary = next(row for row in map(json.loads, output.splitlines()) if row["message_type"] == "summary")
    return summary["snapshot_id"], reference


def _client_restic(sftp, *command):
    _, profile, windows_key, hosts, password, _ = sftp
    args = shlex.join([
        "-F", "/dev/null", "-oIdentityAgent=none", "-oIdentitiesOnly=yes",
        "-oGlobalKnownHostsFile=/dev/null", "-oStrictHostKeyChecking=yes",
        f"-oUserKnownHostsFile={hosts}", "-i", str(windows_key),
    ])
    return _run(
        "restic", "--no-cache", "-r", profile["repositorio"], "-o", f"sftp.args={args}",
        *command,
        env={"PATH": "/usr/bin:/bin", "RESTIC_PASSWORD": password},
    )


def _a_read_password(sftp, directory):
    # Administração descartável de A: chave Restic exclusiva para B.
    path = _private(directory / "a-reader-pass", secrets.token_urlsafe(48))
    _client_restic(sftp, "key", "add", "--new-password-file", str(path))
    return path


def _sftp_refused(prefix, command):
    result = subprocess.run(prefix, input=command.encode(), capture_output=True, timeout=15)
    assert result.returncode != 0
    assert b"Permission denied (publickey)" not in result.stderr


def test_real_protected_copy_and_offline_restore(
    client, enabled_protection, tmp_path, monkeypatch, browser_gate,
):
    mount = Path("/dev/shm")
    first_dir, second_dir, state = (tmp_path / name for name in ("first", "second", "state-b"))
    for path in (first_dir, second_dir, state):
        path.mkdir(mode=0o700)
    with (
        lab_sftp_profile(client, first_dir) as first,
        lab_sftp_profile(client, second_dir) as second,
        tempfile.TemporaryDirectory(prefix="macip-protected-test-", dir=mount) as mounted,
    ):
        volume = Path(mounted)
        volume_id = uuid.uuid4()
        _private(volume / ".macip-volume", str(volume_id))
        api = InProcessProtector(client)
        b = LabProtector(mount, volume, volume_id, state, reserve_bytes=1024**2)
        sftp, installation, reader_key, stop_a = first
        sftp2, second_id, reader2, _ = second
        snapshot, manifest = _make_point(sftp, first_dir / "amostra-1")
        second_snapshot, _ = _make_point(sftp2, second_dir / "amostra-2")
        a_secret = _a_read_password(sftp, first_dir)
        a_secret2 = _a_read_password(sftp2, second_dir)
        source = {"source_password_file": a_secret, "source_key": reader_key}
        source2 = {"source_password_file": a_secret2, "source_key": reader2}
        initial = b.execute(api, installation, "prepare", **source)
        assert initial["state"] == "prepared"
        second_binding = b.execute(api, second_id, "prepare", **source2)
        assert len({initial["repository_id"], second_binding["repository_id"], sftp[1]["repositorio_id"]}) == 3
        password_b = b.vault.get(f"installation/{installation}/bundle")
        assert password_b != sftp[4].encode()
        assert password_b != a_secret.read_bytes()
        assert password_b != b.vault.get(f"installation/{second_id}/bundle")
        assert client.get(_profile_route(installation), headers=ADMIN).status_code == 409
        with b.lock(installation):
            with pytest.raises(ProtectionError, match="já em execução"):
                b.execute(api, installation, "copy", **source)
        without_mount = LabProtector(tmp_path, volume, volume_id, state, reserve_bytes=1024**2)
        with pytest.raises(ProtectionError, match="montagem.*ausente"):
            without_mount.execute(api, installation, "copy", **source)
        too_full = LabProtector(mount, volume, volume_id, state, reserve_bytes=2**63)
        with pytest.raises(ProtectionError, match="espaço insuficiente"):
            too_full.execute(api, installation, "copy", **source)
        with pytest.raises(ProtectionError, match="chave do Windows"):
            b.execute(api, installation, "copy", **{**source, "source_key": sftp[2]})
        with pytest.raises(ProtectionError, match="origem A"):
            b.execute(api, installation, "copy", **{**source, "source_key": reader2})
        _sftp_refused(_sftp_prefix(sftp[1], reader_key, sftp[3]), "mkdir /repository/tentativa-b\n")

        copy = b.execute(api, installation, "copy", snapshot=snapshot, **source)
        assert copy["snapshot_a"] == snapshot
        assert copy["snapshot_a"] != copy["snapshot_b"]
        assert b.execute(api, installation, "copy", snapshot=snapshot, **source) == copy
        copy2 = b.execute(api, second_id, "copy", snapshot=second_snapshot, **source2)

        def inventory(agent_id, password):
            return json.loads(_run(
                "restic", "--no-cache", "-r", str(volume / str(agent_id)), "snapshots", "--json",
                env={"PATH": "/usr/bin:/bin", "RESTIC_PASSWORD": password.decode()},
            ))

        assert [point["id"] for point in inventory(installation, password_b)] == [copy["snapshot_b"]]
        assert [point["id"] for point in inventory(
            second_id, b.vault.get(f"installation/{second_id}/bundle"),
        )] == [copy2["snapshot_b"]]
        win_prefix = _sftp_prefix(sftp[1], sftp[2], sftp[3])
        _run(*win_prefix, input=b"ls /repository\n")  # mesma chave de escrita em A
        b_point = volume / str(installation) / "snapshots" / copy["snapshot_b"]
        _sftp_refused(win_prefix, f"put {sftp[0]} {b_point}\n")
        _sftp_refused(win_prefix, f"rm {b_point}\n")
        assert [point["id"] for point in inventory(installation, password_b)] == [copy["snapshot_b"]]
        assert client.get(_profile_route(installation), headers=ADMIN).status_code == 409

        later, manifest = _make_point(sftp, first_dir / "amostra-posterior")
        latest = b.execute(api, installation, "copy", **source)
        assert latest["snapshot_a"] == later
        assert {point["id"] for point in inventory(installation, password_b)} == {
            copy["snapshot_b"], latest["snapshot_b"],
        }

        stop_a()
        (first_dir / "amostra-posterior" / "dados ç.txt").unlink()
        (first_dir / "amostra-posterior" / "ignorar.tmp").unlink()
        (first_dir / "amostra-posterior").rmdir()
        with pytest.raises(ProtectionError, match="origem A"):
            b.execute(api, installation, "copy", **source)
        assert _public(client, installation)["last_protected_copy"] == latest
        public = _public(client, installation)
        if browser_gate:
            browser_gate[0](public["client_id"], "prepared", public["repository_id"])
        wrong = _private(first_dir / "wrong.json", json.dumps({name: "0" * 64 for name in manifest}))
        with pytest.raises(ProtectionError, match="conteúdo restaurado diverge"):
            b.execute(api, installation, "prove", manifest=wrong)
        assert client.get(_profile_route(installation), headers=ADMIN).status_code == 409
        truth = _private(first_dir / "manifest.json", json.dumps(manifest))
        if browser_gate:
            proof = json.loads(_run(
                sys.executable, "-m", "macip_backup_api.protector", "prove", str(installation),
                "--manifest", str(truth),
                env={
                    **os.environ, "MACIP_PROTECTOR_API_URL": browser_gate[1],
                    "MACIP_PROTECTOR_LAB_ALLOW_HTTP": "1", "MACIP_PROTECTOR_TOKEN": TOKEN,
                    "MACIP_PROTECTOR_MOUNT": str(mount), "MACIP_PROTECTOR_VOLUME": str(volume),
                    "MACIP_PROTECTOR_VOLUME_ID": str(volume_id), "MACIP_PROTECTOR_STATE": str(state),
                    "MACIP_PROTECTOR_RESERVE_BYTES": str(1024**2),
                },
            ))
        else:
            proof = b.execute(api, installation, "prove", manifest=truth)
        assert proof["files"] == len(manifest)
        assert proof["manifest_sha256"] == proof["restored_sha256"]
        assert (proof["snapshot_a"], proof["snapshot_b"]) == (later, latest["snapshot_b"])
        assert _public(client, installation)["protection_state"] == "proven"
        downloaded = client.get(_profile_route(installation), headers=ADMIN)
        assert downloaded.json() == sftp[1]
        assert downloaded.headers["Cache-Control"] == "no-store"
        assert client.get(_profile_route(second_id), headers=ADMIN).status_code == 409
        if browser_gate:
            browser_gate[0](public["client_id"], "proven", public["repository_id"])
        monkeypatch.delenv("MACIP_API_LAB_PROTECTION")
        assert client.get(_profile_route(installation), headers=ADMIN).status_code == 409
        monkeypatch.setenv("MACIP_API_LAB_PROTECTION", "1")

        with tempfile.TemporaryDirectory(prefix="macip-protected-replacement-", dir=mount) as new_path:
            new_volume = Path(new_path)
            new_id = uuid.uuid4()
            _private(new_volume / ".macip-volume", str(new_id))
            replacement = LabProtector(mount, new_volume, new_id, state, reserve_bytes=1024**2)
            new_binding = replacement.execute(api, installation, "prepare", **source)
            assert new_binding["repository_id"] != initial["repository_id"]
            assert new_binding["state"] == "prepared"
            assert client.get(_profile_route(installation), headers=ADMIN).status_code == 409
            stale_proof = {**proof, "binding_id": initial["binding_id"]}
            stale_proof.pop("verified_at")
            assert client.post(_endpoint(installation) + "/proof", headers=AUTH, json=stale_proof).status_code == 409
            assert {point["id"] for point in inventory(installation, password_b)} == {
                copy["snapshot_b"], latest["snapshot_b"],
            }

        # Alteração da identidade de A invalida a prova anterior, mesmo sem comando em B.
        with engine().begin() as connection:
            connection.execute(text("UPDATE installation_preparations SET repository_id=:id WHERE agent_id=:agent"),
                               {"id": "f" * 64, "agent": installation})
        assert client.get(_profile_route(installation), headers=ADMIN).status_code == 409
        assert _public(client, installation)["protection_state"] == "pending"
        print(json.dumps({
            "installation": str(installation), "repository_a": sftp[1]["repositorio_id"],
            "repository_b": initial["repository_id"], "volume_id": str(volume_id),
            "binding_id": initial["binding_id"], "copy": latest, "proof": proof,
            "second_installation": str(second_id), "second_repository_b": second_binding["repository_id"],
            "second_copy": copy2,
        }))
