"""Executor controlado do servidor A e adaptador de laboratório.

O adaptador incluído cria somente uma árvore privada descartável e um
repositório Restic local. Ele não cria usuário do sistema e é proibido para
dados reais. A API recebe somente resultado tipado e sanitizado.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import tempfile
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class ProvisioningFailure(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class Vault(Protocol):
    def get(self, reference: str) -> bytes | None: ...
    def put_once(self, reference: str, value: bytes) -> None: ...


class LabFileVault:
    """Cofre explícito de laboratório. Diretório privado, publicação atômica."""

    def __init__(self, root: Path):
        root = Path(root)
        if not root.is_absolute() or root.resolve() != root:
            raise ValueError("raiz do cofre deve ser absoluta e sem links")
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = root.stat()
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError("cofre de laboratório deve pertencer ao executor e ter modo 0700")
        self.root = root

    def _path(self, reference: str) -> Path:
        if not re.fullmatch(r"installation/[0-9a-f-]{36}/bundle", reference):
            raise ValueError("referência de segredo incompatível")
        return self.root / (hashlib.sha256(reference.encode()).hexdigest() + ".secret")

    def get(self, reference: str) -> bytes | None:
        path = self._path(reference)
        if path.is_symlink():
            raise ProvisioningFailure("access_denied")
        if not path.exists():
            return None
        info = path.stat()
        if (
            not path.is_file()
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise ProvisioningFailure("access_denied")
        return path.read_bytes()

    def put_once(self, reference: str, value: bytes) -> None:
        path = self._path(reference)
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            if self.get(reference) != value:
                raise ProvisioningFailure("key_mismatch") from None
            return
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(value)
                stream.flush()
                os.fsync(stream.fileno())
        except Exception:
            path.unlink(missing_ok=True)
            raise


class LabPrimaryAdapter:
    """Servidor A descartável: layout isolado + Restic real, sem useradd."""

    def __init__(
        self,
        root: Path,
        *,
        host: str,
        known_hosts: str,
        restic_bin: str = "restic",
    ):
        root = Path(root)
        if not root.is_absolute() or root.resolve() != root:
            raise ValueError("raiz A deve ser absoluta e sem links")
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = root.stat()
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError("raiz A deve pertencer ao executor e ter modo 0700")
        if not re.fullmatch(r"[A-Za-z0-9.-]+", host):
            raise ValueError("host SFTP incompatível")
        match = re.fullmatch(
            r"([^ ]+) (ssh-(?:ed25519|rsa)|ecdsa-[^ ]+) ([A-Za-z0-9+/=]+)",
            known_hosts,
        )
        if not match or match.group(1) != host:
            raise ValueError("known_hosts não corresponde ao host")
        self.root = root
        self.host = host
        self.known_hosts = known_hosts
        self.restic_bin = restic_bin

    def account_path(self, account_name: str) -> Path:
        if not re.fullmatch(r"macip[a-f0-9]{20}", account_name):
            raise ProvisioningFailure("invalid_state")
        return self.root / account_name

    def has_state(self, account_name: str) -> bool:
        path = self.account_path(account_name)
        if path.exists() or path.is_symlink():
            self._private_directory(path)
            return True
        return False

    @staticmethod
    def _private_directory(path: Path) -> None:
        if path.is_symlink():
            raise ProvisioningFailure("access_denied")
        if path.exists():
            info = path.stat()
            if (
                not path.is_dir()
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o700
            ):
                raise ProvisioningFailure("access_denied")
            return
        try:
            path.mkdir(mode=0o700)
        except OSError:
            raise ProvisioningFailure("access_denied") from None

    @staticmethod
    def _validate_private_file(path: Path) -> None:
        if path.is_symlink() or not path.is_file():
            raise ProvisioningFailure("access_denied")
        info = path.stat()
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise ProvisioningFailure("access_denied")

    def reconcile(
        self,
        *,
        installation_id: str,
        account_name: str,
        public_key: str,
        password: str,
    ) -> tuple[str, str]:
        account = self.account_path(account_name)
        marker = account / "installation.json"
        if account.exists():
            self._private_directory(account)
            try:
                self._validate_private_file(marker)
                identity = json.loads(marker.read_text())
            except (OSError, ValueError, ProvisioningFailure):
                raise ProvisioningFailure("access_denied") from None
            if identity != {"installation_id": installation_id, "account": account_name}:
                raise ProvisioningFailure("access_denied")
        else:
            self._private_directory(account)
            marker.write_text(
                json.dumps(
                    {"installation_id": installation_id, "account": account_name},
                    sort_keys=True,
                )
                + "\n"
            )
            marker.chmod(0o600)

        ssh = account / ".ssh"
        self._private_directory(ssh)
        authorized = ssh / "authorized_keys"
        wanted = public_key.strip() + "\n"
        if authorized.exists():
            self._validate_private_file(authorized)
            if authorized.read_text() != wanted:
                raise ProvisioningFailure("key_mismatch")
        else:
            authorized.write_text(wanted)
            authorized.chmod(0o600)

        repository = account / "repository"
        self._private_directory(repository)
        environment = dict(os.environ, RESTIC_PASSWORD=password)
        cache = account / ".cache"
        self._private_directory(cache)
        base = [self.restic_bin, "--repo", str(repository), "--cache-dir", str(cache)]
        if (repository / "config").exists():
            command = [*base, "cat", "config"]
        else:
            command = [*base, "init"]
        try:
            completed = subprocess.run(
                command,
                env=environment,
                capture_output=True,
                text=True,
                timeout=60,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise ProvisioningFailure("repository_error") from None
        if completed.returncode != 0:
            lowered = completed.stderr.lower()
            code = "access_denied" if "permission denied" in lowered else "repository_error"
            raise ProvisioningFailure(code)
        try:
            checked = subprocess.run(
                [*base, "cat", "config"],
                env=environment,
                capture_output=True,
                text=True,
                timeout=60,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise ProvisioningFailure("repository_error") from None
        if checked.returncode != 0:
            raise ProvisioningFailure("repository_error")
        try:
            repository_id = json.loads(checked.stdout)["id"]
        except (KeyError, TypeError, json.JSONDecodeError):
            raise ProvisioningFailure("repository_error") from None
        if not re.fullmatch(r"[a-f0-9]{64}", repository_id):
            raise ProvisioningFailure("repository_error")
        identity_file = account / ".repository-id"
        if identity_file.exists():
            self._validate_private_file(identity_file)
            if identity_file.read_text().strip() != repository_id:
                raise ProvisioningFailure("repository_mismatch")
        else:
            identity_file.write_text(repository_id + "\n")
            identity_file.chmod(0o600)
        try:
            fingerprint = subprocess.run(
                ["ssh-keygen", "-lf", str(authorized), "-E", "sha256"],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise ProvisioningFailure("key_mismatch") from None
        if fingerprint.returncode != 0:
            raise ProvisioningFailure("key_mismatch")
        parts = fingerprint.stdout.split()
        if len(parts) < 2 or not parts[1].startswith("SHA256:"):
            raise ProvisioningFailure("key_mismatch")
        return repository_id, parts[1]


def _create_bundle(installation_id: str) -> dict:
    with tempfile.TemporaryDirectory(prefix="macip-provision-key-") as directory:
        private = Path(directory) / "id_ed25519"
        try:
            completed = subprocess.run(
                [
                    "ssh-keygen",
                    "-q",
                    "-t",
                    "ed25519",
                    "-N",
                    "",
                    "-C",
                    f"macip:{installation_id}",
                    "-f",
                    str(private),
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise ProvisioningFailure("repository_error") from None
        if completed.returncode != 0:
            raise ProvisioningFailure("repository_error")
        return {
            "private_key": base64.b64encode(private.read_bytes()).decode(),
            "public_key": private.with_suffix(".pub").read_text().strip(),
            "restic_password": secrets.token_urlsafe(48),
        }


def reconcile_operation(operation: dict, vault: Vault, primary: LabPrimaryAdapter) -> dict:
    installation_id = operation["installation_id"]
    account_name = operation["account_name"]
    reference = f"installation/{installation_id}/bundle"
    encoded = vault.get(reference)
    if encoded is None:
        if primary.has_state(account_name):
            raise ProvisioningFailure("secret_missing")
        bundle = _create_bundle(installation_id)
        encoded = json.dumps(bundle, sort_keys=True).encode()
        vault.put_once(reference, encoded)
    try:
        bundle = json.loads(encoded)
        public_key = bundle["public_key"]
        password = bundle["restic_password"]
        base64.b64decode(bundle["private_key"], validate=True)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise ProvisioningFailure("secret_missing") from None
    repository_id, fingerprint = primary.reconcile(
        installation_id=installation_id,
        account_name=account_name,
        public_key=public_key,
        password=password,
    )
    return {
        "account_name": account_name,
        "repository_id": repository_id,
        "public_key_fingerprint": fingerprint,
        "profile": {
            "nome": operation["client_name"],
            "repositorio": f"sftp:{account_name}@{primary.host}:repository",
            "repositorio_id": repository_id,
            "known_hosts": primary.known_hosts,
            "horario_sugerido": operation["suggested_time"],
        },
    }


class ApiTransport:
    def __init__(self, url: str, token: str, executor_id: str):
        if not url.startswith("https://") and not url.startswith("http://127.0.0.1"):
            raise ValueError("API exige HTTPS; HTTP permitido somente em 127.0.0.1")
        self.url = url.rstrip("/")
        self.token = token
        self.executor_id = executor_id

    def request(self, path: str, body: dict) -> dict:
        request = Request(
            self.url + path,
            data=json.dumps(body).encode(),
            method="POST",
            headers={
                "Authorization": "Bearer " + self.token,
                "Content-Type": "application/json",
            },
        )
        try:
            with urlopen(request, timeout=30) as response:
                return json.loads(response.read())
        except (HTTPError, URLError, TimeoutError) as error:
            raise RuntimeError("API de preparação indisponível") from error

    def claim(self) -> dict | None:
        return self.request(
            "/api/v1/provisioner/operations/claim",
            {"executor_id": self.executor_id},
        )["operation"]

    def result(self, operation_id: str, body: dict) -> dict:
        return self.request(
            f"/api/v1/provisioner/operations/{operation_id}/result", body
        )


def run_once(transport: ApiTransport, vault: Vault, primary: LabPrimaryAdapter) -> dict:
    operation = transport.claim()
    if operation is None:
        return {"status": "idle"}
    try:
        result = reconcile_operation(operation, vault, primary)
        body = {
            "claim_id": operation["claim_id"],
            "state": "succeeded",
            "result": result,
            "error_code": None,
        }
    except ProvisioningFailure as error:
        body = {
            "claim_id": operation["claim_id"],
            "state": "failed",
            "result": None,
            "error_code": error.code,
        }
    transport.result(operation["id"], body)
    return {"status": body["state"], "operation_id": operation["id"]}


def main() -> int:
    """Executa uma operação. Adaptador é deliberadamente LAB_ONLY."""
    required = {
        name: os.environ.get(name, "")
        for name in (
            "MACIP_PROVISIONER_API_URL",
            "MACIP_PROVISIONER_TOKEN",
            "MACIP_PROVISIONER_ID",
            "MACIP_PROVISIONER_LAB_ROOT",
            "MACIP_PROVISIONER_LAB_VAULT",
            "MACIP_PROVISIONER_SFTP_HOST",
            "MACIP_PROVISIONER_KNOWN_HOSTS",
        )
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise SystemExit("configuração de laboratório ausente: " + ", ".join(missing))
    transport = ApiTransport(
        required["MACIP_PROVISIONER_API_URL"],
        required["MACIP_PROVISIONER_TOKEN"],
        required["MACIP_PROVISIONER_ID"],
    )
    vault = LabFileVault(Path(required["MACIP_PROVISIONER_LAB_VAULT"]))
    primary = LabPrimaryAdapter(
        Path(required["MACIP_PROVISIONER_LAB_ROOT"]),
        host=required["MACIP_PROVISIONER_SFTP_HOST"],
        known_hosts=required["MACIP_PROVISIONER_KNOWN_HOSTS"],
    )
    print(json.dumps(run_once(transport, vault, primary), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
