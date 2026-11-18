"""Executor de B para volume local e origem SFTP em laboratório."""

from __future__ import annotations

import argparse
import csv
from contextlib import contextmanager
from datetime import datetime
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import uuid

from .provisioner import ApiTransport, LabFileVault

_ID = re.compile(r"[0-9a-f]{64}")
_PROOF_METHOD = "restic-offline-sha256-v1"


def sftp_args_option(arguments: list[str]) -> str:
    # Cobra interpreta -o como CSV antes de o Restic interpretar os argumentos SSH.
    output = io.StringIO()
    csv.writer(output, lineterminator="").writerow(["sftp.args=" + shlex.join(arguments)])
    return output.getvalue()


class ProtectionError(RuntimeError):
    pass


def _directory(path: Path, *, create=False) -> None:
    if not path.is_absolute() or path.resolve() != path:
        raise ProtectionError("diretório deve ser absoluto e sem links")
    if create:
        path.mkdir(mode=0o700, exist_ok=True)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ProtectionError("diretório deve pertencer ao executor e ser privado")


def _private_file(path: Path) -> bytes:
    if not path.is_absolute() or path.resolve() != path:
        raise ProtectionError("arquivo de credencial deve ser absoluto e sem links")
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ProtectionError("arquivo de credencial sem proteção")
    return path.read_bytes()


def _run(args: list[str], env: dict[str, str], *, operation: str, lock_fd: int) -> bytes:
    # O Restic mantém o lock de B até sair, mesmo se o executor morrer.
    with subprocess.Popen(
        args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=env, start_new_session=True, pass_fds=(lock_fd,),
    ) as process:
        try:
            output, _ = process.communicate(timeout=600)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate()
            raise ProtectionError(f"{operation}: prazo excedido") from None
        if process.returncode:
            raise ProtectionError(f"{operation}: falhou (código {process.returncode})")
        return output


def _digest(value) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ProtectionError("identidade Restic inválida")
    return value


def _manifest_digest(files: dict[str, str]) -> str:
    encoded = json.dumps(files, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _manifest(path: Path) -> dict[str, str]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not value:
        raise ProtectionError("manifesto vazio ou inválido")
    for name, digest in value.items():
        parts = name.split("/")
        if PurePosixPath(name).is_absolute() or any(p in {"", ".", ".."} for p in parts):
            raise ProtectionError("caminho inválido no manifesto")
        _digest(digest)
    return value


def _hash_tree(root: Path) -> dict[str, str]:
    files = {}
    for path in root.rglob("*"):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise ProtectionError("restauração contém link ou arquivo especial")
        if path.is_file():
            with path.open("rb") as stream:
                files[path.relative_to(root).as_posix()] = hashlib.file_digest(stream, "sha256").hexdigest()
    return files


class LabProtector:
    def __init__(self, mount: Path, volume: Path, volume_id: uuid.UUID, state: Path,
                 *, reserve_bytes: int):
        if reserve_bytes < 1:
            raise ProtectionError("reserva de espaço deve ser positiva")
        self.mount = mount
        self.volume = volume
        self.volume_id = str(volume_id)
        self.state = state
        self.reserve_bytes = reserve_bytes
        if state == volume or state.is_relative_to(volume) or volume.is_relative_to(state):
            raise ProtectionError("estado e volume devem usar diretórios separados")
        _directory(state)
        self.vault = LabFileVault(state / "vault")
        self.restic = shutil.which("restic")
        if not self.restic:
            raise ProtectionError("Restic indisponível")

    def check_volume(self, needed=0):
        if self.mount.resolve() != self.mount or not self.mount.is_mount():
            raise ProtectionError("montagem de B ausente")
        if self.volume == self.mount or not self.volume.is_relative_to(self.mount):
            raise ProtectionError("volume deve ser uma área privada da montagem de B")
        _directory(self.volume)
        if (
            self.volume.stat().st_dev != self.mount.stat().st_dev
            or _private_file(self.volume / ".macip-volume").decode().strip() != self.volume_id
        ):
            raise ProtectionError("identidade do volume de B divergente")
        if shutil.disk_usage(self.volume).free < self.reserve_bytes + needed:
            raise ProtectionError("espaço insuficiente em B")

    @contextmanager
    def lock(self, installation: uuid.UUID):
        fd = os.open(self.state / f"{installation}.lock", os.O_CREAT | os.O_NOFOLLOW | os.O_RDWR, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ProtectionError("operação de B já em execução") from None
            yield fd
        finally:
            os.close(fd)

    def execute(self, api, installation: uuid.UUID, action: str, *,
                source_password_file: Path | None = None,
                source_key: Path | None = None, snapshot: str | None = None,
                manifest: Path | None = None) -> dict:
        with self.lock(installation) as fd:
            self.check_volume()
            endpoint = f"/api/v1/protector/installations/{installation}"
            context = api.request(endpoint)
            repository = self.volume / str(installation)
            destination = hashlib.sha256(os.fsencode(repository)).hexdigest()
            binding = context["protection"]
            same_location = binding and (binding["volume_id"], binding["destination"]) == (
                self.volume_id, destination,
            )
            vault_ref = f"installation/{installation}/bundle"
            password = self.vault.get(vault_ref)
            source_password = None
            if action != "prove":
                if source_password_file is None or source_key is None:
                    raise ProtectionError("informe as credenciais de leitura de A")
                source_password = _private_file(source_password_file).strip()
                if not source_password:
                    raise ProtectionError("senha de leitura de A ausente")

            def invalidate():
                if binding and not binding.get("invalid"):
                    updated = api.request(endpoint + "/invalidate", {"binding_id": binding["binding_id"]})
                    context["binding_id"] = updated["binding_id"]

            if binding and not same_location:
                # Fence the old proof before attempting a different destination.
                invalidate()

            has_config = (repository / "config").exists()
            if not has_config and (same_location or action != "prepare"):
                invalidate()
                raise ProtectionError("repositório de B ausente ou incompleto")
            _directory(repository, create=action == "prepare")
            if password is None:
                if has_config or any(repository.iterdir()):
                    invalidate()
                    raise ProtectionError("senha original de B ausente")
                password = secrets.token_urlsafe(48).encode()
                self.vault.put_once(vault_ref, password)
            if source_password == password:
                raise ProtectionError("A e B devem usar senhas distintas")
            env_b = {"PATH": os.environ.get("PATH", os.defpath), "RESTIC_PASSWORD": password.decode()}

            def local(*args, offline=False):
                command = [self.restic, "--no-cache", "-r", str(repository), *args]
                if offline:
                    command = [sys.executable, "-m", "macip_backup_api.protection_offline", *command]
                    # Preserve apenas a resolução do módulo no checkout de laboratório.
                    return _run(command, {**env_b, "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
                                operation=args[0], lock_fd=fd)
                return _run(command, env_b, operation=args[0], lock_fd=fd)

            if not has_config:
                if any(repository.iterdir()):
                    raise ProtectionError("repositório de B incompleto requer inspeção")
                local("init")
            try:
                repository_id = _digest(json.loads(local("cat", "config"))["id"])
            except ProtectionError:
                if same_location:
                    invalidate()
                raise
            self.check_volume()
            bound = api.request(endpoint + "/binding", {
                "source": context["source"],
                "previous_binding": context["binding_id"],
                "volume_id": self.volume_id,
                "destination": destination,
                "repository_id": repository_id,
            })
            if action == "prepare":
                return {"binding_id": bound["binding_id"], "repository_id": repository_id,
                        "state": "proven" if bound.get("proof") else "prepared"}
            if action == "copy":
                pair = self._copy(
                    context, repository, local, source_key, source_password, password, snapshot, fd,
                )
                self.check_volume()
                return api.request(endpoint + "/copy", {"binding_id": bound["binding_id"], **pair})
            if action != "prove":
                raise ProtectionError("ação de B incompatível")
            if manifest is None or not bound.get("last_copy"):
                raise ProtectionError("prova exige manifesto e ponto copiado")
            last = bound["last_copy"]
            expected = _manifest(manifest)
            # Verifique até a identidade pelo processo sem acesso à rede.
            if json.loads(local("cat", "config", offline=True))["id"] != repository_id:
                raise ProtectionError("identidade de B mudou antes da restauração")
            local("check", "--read-data", offline=True)
            with tempfile.TemporaryDirectory(prefix="restore-b-", dir=self.state) as restored:
                local("restore", last["snapshot_b"], "--target", restored, "--verify", offline=True)
                actual = _hash_tree(Path(restored))
            if actual != expected:
                raise ProtectionError("conteúdo restaurado diverge do manifesto")
            self.check_volume()
            return api.request(endpoint + "/proof", {
                "binding_id": bound["binding_id"], "snapshot_a": last["snapshot_a"],
                "snapshot_b": last["snapshot_b"], "method": _PROOF_METHOD,
                "files": len(expected), "manifest_sha256": _manifest_digest(expected),
                "restored_sha256": _manifest_digest(actual),
            })

    def _copy(self, context, repository, local, key, source_password, password, snapshot, fd):
        _private_file(key)
        # Key fingerprint works directly on the private key. Only the public fingerprint is used.
        fingerprint = _run(["ssh-keygen", "-lf", str(key), "-E", "sha256"], {},
                           operation="identidade SSH de B", lock_fd=fd).split()[1].decode()
        if fingerprint == context["windows_key_fingerprint"]:
            raise ProtectionError("a chave do Windows não pode ser usada por B")
        with tempfile.TemporaryDirectory(prefix="ssh-b-", dir=self.state) as directory:
            hosts = Path(directory) / "known_hosts"
            hosts.write_text(context["known_hosts"] + "\n", encoding="utf-8")
            options = ["-o", sftp_args_option([
                "-F", "/dev/null", "-oBatchMode=yes", "-oStrictHostKeyChecking=yes",
                "-oIdentitiesOnly=yes", "-oIdentityAgent=none", "-oGlobalKnownHostsFile=/dev/null",
                "-oUserKnownHostsFile=" + json.dumps(str(hosts), ensure_ascii=False),
                "-oConnectTimeout=5", "-i", str(key),
            ])]
            source = context["repository"]
            env = {"PATH": os.environ.get("PATH", os.defpath), "RESTIC_PASSWORD": source_password.decode()}

            def remote(*args):
                return _run([self.restic, "--no-cache", "--no-lock", "-r", source, *options, *args],
                            env, operation=f"origem A ({args[0]})", lock_fd=fd)

            if json.loads(remote("cat", "config"))["id"] != context["repository_id"]:
                raise ProtectionError("identidade do repositório de A divergente")
            points = json.loads(remote("snapshots", "--json"))
            if snapshot is None:
                if not points:
                    raise ProtectionError("nenhum snapshot disponível em A")
                snapshot = max(
                    points, key=lambda point: (datetime.fromisoformat(point["time"]), point["id"]),
                )["id"]
            original = next((point for point in points if point["id"] == _digest(snapshot)), None)
            if not original:
                raise ProtectionError("snapshot não pertence ao repositório de A")
            size = json.loads(remote("stats", snapshot, "--mode", "restore-size", "--json"))["total_size"]
            self.check_volume(size)
            env_copy = {"PATH": env["PATH"], "RESTIC_PASSWORD": password.decode(),
                        "RESTIC_FROM_PASSWORD": source_password.decode()}
            _run(
                [self.restic, "--no-cache", "--no-lock", "-r", str(repository),
                 *options, "copy", "--from-repo", source, snapshot],
                env_copy, operation="cópia A → B", lock_fd=fd,
            )
            if json.loads(remote("cat", "config"))["id"] != context["repository_id"]:
                raise ProtectionError("identidade de A mudou durante a cópia")
        matching = [
            point for point in json.loads(local("snapshots", "--json"))
            if point.get("original", point["id"]) == original.get("original", snapshot)
            and all(point.get(field) == original.get(field)
                    for field in ("tree", "paths", "time", "hostname", "tags"))
        ]
        if len(matching) != 1:
            raise ProtectionError("ponto correspondente em B ausente ou ambíguo")
        return {"snapshot_a": snapshot, "snapshot_b": _digest(matching[0]["id"])}


def main() -> int:
    parser = argparse.ArgumentParser(description="Executor de cópia protegida em laboratório")
    parser.add_argument("action", choices=("prepare", "copy", "prove"))
    parser.add_argument("installation", type=uuid.UUID)
    parser.add_argument("--source-password-file", type=Path)
    parser.add_argument("--source-key", type=Path)
    parser.add_argument("--snapshot")
    parser.add_argument("--manifest", type=Path)
    args = parser.parse_args()
    if args.snapshot is not None and not _ID.fullmatch(args.snapshot):
        parser.error("snapshot exige ID Restic completo")
    try:
        transport = ApiTransport(
            os.environ["MACIP_PROTECTOR_API_URL"], os.environ["MACIP_PROTECTOR_TOKEN"],
            "protected-b-lab", allow_lab_http=os.environ.get("MACIP_PROTECTOR_LAB_ALLOW_HTTP") == "1",
        )
        executor = LabProtector(
            Path(os.environ["MACIP_PROTECTOR_MOUNT"]), Path(os.environ["MACIP_PROTECTOR_VOLUME"]),
            uuid.UUID(os.environ["MACIP_PROTECTOR_VOLUME_ID"]), Path(os.environ["MACIP_PROTECTOR_STATE"]),
            reserve_bytes=int(os.environ.get("MACIP_PROTECTOR_RESERVE_BYTES", str(64 * 1024**2))),
        )
        result = executor.execute(
            transport, args.installation, args.action, source_password_file=args.source_password_file,
            source_key=args.source_key, snapshot=args.snapshot, manifest=args.manifest,
        )
    except (ProtectionError, RuntimeError) as error:
        parser.exit(1, f"{error}\n")
    except (OSError, ValueError, KeyError, TypeError):
        parser.exit(1, "configuração ou resposta inválida no executor de B\n")
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
