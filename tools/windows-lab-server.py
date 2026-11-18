"""Suporte exclusivo ao roteiro descartável Windows → A → B."""

import argparse
import base64
import hashlib
import ipaddress
import json
import os
from pathlib import Path
from pathlib import PurePosixPath
import secrets
import socket
import subprocess
import sys
import time
import uuid

from macip_backup_api.provisioner import ApiTransport, LabFileVault, LabPrimaryAdapter, reconcile_operation
from macip_backup_api.protector import LabProtector, ProtectionError, sftp_args_option


def run(*args, **kwargs):
    completed = subprocess.run(args, capture_output=True, timeout=120, **kwargs)
    if completed.returncode:
        # Diagnóstico apenas da categoria e código; saída bruta permanece privada.
        if args[0] == "restic":
            (DIAGNOSTIC_ROOT / "private/last-restic-error").write_bytes(completed.stderr)
            (DIAGNOSTIC_ROOT / "private/last-restic-error").chmod(0o600)
        raise RuntimeError(f"comando de laboratório falhou: {args[0]} (código {completed.returncode})")
    return completed.stdout.decode()


def write(path, value):
    with path.open("x", encoding="utf-8") as stream:
        stream.write(value)
    path.chmod(0o600)


def record(root, name, value):
    write(root / "evidence" / name, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def api(cfg, role):
    return ApiTransport(cfg["api"], cfg[role], "windows-lab", allow_lab_http=True)


def api_env(cfg):
    environment = {
        **os.environ, "MACIP_API_DATABASE_URL": cfg["database"],
        "MACIP_API_ADMIN_TOKEN": cfg["admin"], "MACIP_API_PROVISIONER_TOKEN": cfg["a_token"],
        "MACIP_API_PROTECTOR_TOKEN": cfg["b_token"], "MACIP_API_LAB_PROFILE_EXPORT": "1",
        "MACIP_API_LAB_PROTECTION": "1", "MACIP_API_SESSION_INSECURE_COOKIE": "1",
    }
    if cfg.get("activation_public"):
        environment.update({
            "MACIP_API_LAB_ACTIVATION": "1",
            "MACIP_API_SESSION_INSECURE_COOKIE": "0",
            "MACIP_API_ACTIVATION_PUBLIC_URL": cfg["activation_public"],
            "MACIP_API_ACTIVATION_DELIVERY_URL": cfg["activation_delivery"],
        })
    return environment


def check_container(name, root):
    label = run("docker", "inspect", "-f", '{{index .Config.Labels "macip.windows-lab"}}', name).strip()
    if label != str(root):
        raise RuntimeError("contêiner não pertence a este laboratório")


def windows_manifest(reference, paths):
    if paths != [reference["source"]]:
        raise RuntimeError("origem registrada no snapshot difere do manifesto Windows")
    source = reference["source"].replace("\\", "/")
    if len(source) < 3 or source[1:3] != ":/" or ".." in source.split("/"):
        raise RuntimeError("referência Windows exige unidade absoluta normalizada")
    prefix = source[0].upper() + source[2:].rstrip("/")
    result = {}
    for name, digest in reference["files"].items():
        if (
            PurePosixPath(name).is_absolute()
            or any(part in {"", ".", ".."} for part in name.split("/"))
            or "\\" in name or len(digest) != 64
            or any(c not in "0123456789abcdef" for c in digest)
        ):
            raise RuntimeError("caminho ou SHA-256 inválido na referência")
        result[prefix + "/" + name] = digest
    if not result:
        raise RuntimeError("referência vazia")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=(
        "init", "api", "operator", "prepare-a", "seed", "prepare-b", "copy-seed",
        "prove-seed", "export-credentials", "windows-point", "copy-windows", "stop-a", "prove-windows", "stop",
        "configure-activation", "activation-api", "activation-a", "refresh-b", "copy-activation", "prove-activation",
    ))
    parser.add_argument("area", type=Path)
    parser.add_argument("--sftp-ip")
    parser.add_argument("--sftp-image", default="macip-protection-lab-sshd")
    parser.add_argument("--postgres-image", default="postgres:16-alpine")
    parser.add_argument("--api-port", type=int, default=8187)
    parser.add_argument("--installation", type=uuid.UUID)
    parser.add_argument("--snapshot")
    parser.add_argument("--reference", type=Path)
    parser.add_argument("--public-url")
    parser.add_argument("--delivery-url")
    parser.add_argument("--certificate")
    parser.add_argument("--key")
    parser.add_argument("--ca")
    parser.add_argument("--delivery-port", type=int, default=9447)
    args = parser.parse_args()
    root = args.area
    global DIAGNOSTIC_ROOT
    DIAGNOSTIC_ROOT = root
    if not root.is_absolute() or root.resolve() != root:
        parser.error("area deve ser absoluta e sem links")
    if args.action == "init":
        if (
            not args.sftp_ip or ipaddress.ip_address(args.sftp_ip).version != 4
            or ipaddress.ip_address(args.sftp_ip).is_unspecified
        ):
            parser.error("informe IPv4 do servidor descartável acessível pela VM")
        # Exige endereço local explícito e recusa uma área já existente.
        with socket.socket() as probe:
            probe.bind((args.sftp_ip, 0))
        root.mkdir(mode=0o700)
        for name in ("evidence", "private", "a", "b-state"):
            (root / name).mkdir(mode=0o700)
        stamp = uuid.uuid4().hex[:12]
        cfg = {
            "api": f"http://127.0.0.1:{args.api_port}", "api_port": args.api_port,
            "ip": args.sftp_ip, "sftp_image": args.sftp_image,
            "pg": "macip-win-pg-" + stamp, "sshd": "macip-win-a-" + stamp,
            **{key: secrets.token_urlsafe(40) for key in ("admin", "a_token", "b_token")},
        }
        # Registra identidade antes de criar recursos para permitir limpeza após falha.
        write(root / "private/config.json", json.dumps(cfg))
        run("docker", "run", "--pull=never", "-d", "--rm", "--name", cfg["pg"],
            "--label", f"macip.windows-lab={root}", "-e", "POSTGRES_HOST_AUTH_METHOD=trust",
            "-e", "POSTGRES_DB=macip_windows_test", "-p", "127.0.0.1::5432", args.postgres_image)
        port = run("docker", "port", cfg["pg"], "5432").strip().rsplit(":", 1)[1]
        cfg["database"] = f"postgresql+psycopg://postgres@127.0.0.1:{port}/macip_windows_test"
        for _ in range(30):
            if subprocess.run(["docker", "exec", cfg["pg"], "pg_isready", "-h", "127.0.0.1"],
                              capture_output=True).returncode == 0:
                break
            time.sleep(0.5)
        else:
            raise RuntimeError("PostgreSQL não ficou pronto")
        # init ainda é a única escrita desta configuração privada.
        (root / "private/config.json").write_text(json.dumps(cfg))
        record(root, "base.json", {
            "central": run("git", "rev-parse", "HEAD").strip(),
            "windows": "7d62a800325146d1357f8832b1df5c4c3b2eff8d",
            "restic": run("restic", "version").strip(),
        })
        print("Laboratório criado. Inicie API e portal em terminais separados.")
        return
    if root.stat().st_uid != os.getuid() or root.stat().st_mode & 0o077:
        raise RuntimeError("area privada com propriedade ou modo divergente")
    cfg = json.loads((root / "private/config.json").read_text())
    if args.action == "configure-activation":
        from urllib.parse import urlsplit
        for url in (args.public_url, args.delivery_url):
            parsed = urlsplit(url or "")
            if parsed.scheme != "https" or not parsed.hostname or "@" in parsed.netloc or parsed.query or parsed.fragment:
                raise RuntimeError("configure endereços HTTPS do laboratório")
        cfg.update(activation_public=args.public_url.rstrip("/"), activation_delivery=args.delivery_url,
                   api=args.public_url.rstrip("/"))
        (root / "private/config.json").write_text(json.dumps(cfg))
        return
    if args.action in {"activation-api", "activation-a"}:
        if not args.certificate or not args.key or not args.ca:
            raise RuntimeError("informe certificate, key e ca do laboratório")
        environment = {**api_env(cfg), "SSL_CERT_FILE": args.ca}
        if args.action == "activation-api":
            os.environ.update(environment)
            import uvicorn
            from fastapi import HTTPException
            from fastapi.responses import FileResponse
            from fastapi.staticfiles import StaticFiles
            from macip_backup_api.app import create_app
            app = create_app()
            dist = Path(__file__).resolve().parents[1] / "portal/dist"
            app.mount("/assets", StaticFiles(directory=dist / "assets"))

            @app.get("/{path:path}")
            def frontend(path: str):
                if path.startswith("api/"):
                    raise HTTPException(404, "rota inexistente")
                return FileResponse(dist / "index.html")

            uvicorn.run(app, host=cfg["ip"], port=cfg["api_port"], ssl_certfile=args.certificate,
                        ssl_keyfile=args.key)
            return
        else:
            # A recebe somente sua identidade. Tokens de B, administração e
            # acesso ao banco permanecem no processo da central.
            environment = {key: value for key, value in os.environ.items() if not key.startswith("MACIP_")}
            environment.update({
                "SSL_CERT_FILE": args.ca, "MACIP_API_LAB_ACTIVATION": "1",
                "MACIP_PROVISIONER_API_URL": cfg["api"], "MACIP_PROVISIONER_TOKEN": cfg["a_token"],
                "MACIP_PROVISIONER_LAB_ROOT": str(root / "a"),
                "MACIP_PROVISIONER_LAB_VAULT": str(root / "private/vault-a"),
                "MACIP_ACTIVATION_LAB_CONTAINER": cfg["sshd"],
            })
            command = [sys.executable, "-m", "macip_backup_api.activation_executor",
                       "--host", cfg["ip"], "--port", str(args.delivery_port),
                       "--certificate", args.certificate, "--key", args.key]
        os.execve(sys.executable, command, environment)
    if args.action == "api":
        os.execve(sys.executable, [sys.executable, "-m", "uvicorn", "macip_backup_api.app:app",
                                  "--host", "127.0.0.1", "--port", str(cfg["api_port"])], api_env(cfg))
    if args.action == "operator":
        subprocess.run([sys.executable, "-m", "macip_backup_api.operator_cli", "create",
                        "--login", "operador-lab", "--name", "Operador Windows Lab",
                        "--role", "admin"], env=api_env(cfg), check=True)
        return
    if args.action == "stop":
        for name in (cfg["sshd"], cfg["pg"]):
            exists = subprocess.run(["docker", "inspect", name], capture_output=True).returncode == 0
            if exists:
                check_container(name, root)
                run("docker", "stop", "-t", "2", name)
        print("Contêineres encerrados. Arquivos e evidências preservados.")
        return
    a_api, b_api = api(cfg, "a_token"), api(cfg, "b_token")
    if args.action == "prepare-a":
        if (root / "private/installation.json").exists() or not args.installation:
            raise RuntimeError("informe instalação nova e execute prepare-a somente uma vez")
        operation = a_api.claim()
        if operation is None or operation["installation_id"] != str(args.installation):
            raise RuntimeError("fila difere da instalação selecionada; pare e inspecione")
        write(root / "private/installation.json", json.dumps(operation))
        run("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(root / "private/hostkey"))
        run("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(root / "private/b-reader"))
        run("docker", "run", "--pull=never", "--rm", "-d", "--name", cfg["sshd"],
            "--label", f"macip.activation-accounts={root / 'a'}",
            "--label", f"macip.windows-lab={root}", "-p", f'{cfg["ip"]}::22',
            "--entrypoint", "/bin/sh", cfg["sftp_image"], "-c", "exec sleep infinity")
        port = int(run("docker", "port", cfg["sshd"], "22").strip().rsplit(":", 1)[1])
        key = (root / "private/hostkey.pub").read_text().split()[:2]
        hosts = f'[{cfg["ip"]}]:{port} ' + " ".join(key)
        primary = LabPrimaryAdapter(root / "a", host=cfg["ip"], port=port, known_hosts=hosts)
        vault = LabFileVault(root / "private/vault-a")
        result = reconcile_operation(operation, vault, primary)
        account = operation["account_name"]
        bundle = json.loads(vault.get(f'installation/{args.installation}/bundle'))
        write(root / "private/windows-key", base64.b64decode(bundle["private_key"]).decode())
        write(root / "private/windows-password", bundle["restic_password"])
        write(root / "private/known_hosts", hosts + "\n")
        write(root / "private/authorized_keys",
              'restrict,command="internal-sftp" ' + bundle["public_key"] + "\n" +
              'restrict,command="internal-sftp -R" ' + (root / "private/b-reader.pub").read_text())
        run("docker", "exec", cfg["sshd"], "mkdir", "-p", "/jail", "/run/sshd")
        for source, target in (
            (root / "a" / account / "repository", "/jail/repository"),
            (root / "private/authorized_keys", "/authorized_keys"),
            (root / "private/hostkey", "/hostkey"),
        ):
            run("docker", "cp", str(source), cfg["sshd"] + ":" + target)
        run("docker", "exec", cfg["sshd"], "/bin/sh", "-eu", "-c", """
            useradd --create-home --shell /usr/sbin/nologin "$1"
            passwd --delete "$1"
            mkdir -p "/home/$1/.ssh" "/jail/home/$1"
            mv /authorized_keys "/home/$1/.ssh/authorized_keys"
            chown -R "$1:$1" "/home/$1/.ssh" /jail/repository
            chmod 700 "/home/$1/.ssh"
            chmod 600 "/home/$1/.ssh/authorized_keys" /hostkey
        """, "setup", account)
        run("docker", "exec", "-d", cfg["sshd"], "/usr/sbin/sshd", "-D", "-e", "-f", "/dev/null",
            "-h", "/hostkey", "-oUsePAM=no", "-oPasswordAuthentication=no",
            "-oKbdInteractiveAuthentication=no", "-oPermitRootLogin=no", "-oDisableForwarding=yes",
            "-oAuthorizedKeysFile=.ssh/authorized_keys", f"-oAllowUsers={account}",
            "-oChrootDirectory=/jail", "-oSubsystem=sftp internal-sftp")
        a_api.result(operation["id"], {
            "claim_id": operation["claim_id"], "state": "succeeded", "result": result,
        })
        write(root / "private/profile.json", json.dumps(result["profile"]))
        record(root, "primary.json", result)
        return
    operation = json.loads((root / "private/installation.json").read_text())
    installation = uuid.UUID(operation["installation_id"])
    profile = json.loads((root / "private/profile.json").read_text())

    def restic_a(*command):
        ssh = sftp_args_option([
            "-F", "/dev/null", "-oIdentityAgent=none", "-oBatchMode=yes", "-oIdentitiesOnly=yes",
            "-oStrictHostKeyChecking=yes", "-oGlobalKnownHostsFile=/dev/null",
            "-oUserKnownHostsFile=" + json.dumps(str(root / "private/known_hosts"), ensure_ascii=False),
            "-i", str(root / "private/windows-key"),
        ])
        return run("restic", "--no-cache", "-r", profile["repositorio"],
                   "-o", ssh, *command,
                   env={"PATH": os.environ["PATH"],
                        "RESTIC_PASSWORD": (root / "private/windows-password").read_text()})

    if args.action == "seed":
        source = root / "seed"
        source.mkdir()
        write(source / "ficticio.txt", "Ponto fictício anterior à liberação do perfil\n")
        expected = {str(source / "ficticio.txt").lstrip("/"):
                    hashlib.sha256((source / "ficticio.txt").read_bytes()).hexdigest()}
        write(root / "private/seed-manifest.json", json.dumps(expected))
        output = restic_a("backup", "--json", str(source))
        snapshot = next(json.loads(line)["snapshot_id"] for line in output.splitlines()
                        if json.loads(line)["message_type"] == "summary")
        record(root, "seed.json", {"snapshot_a": snapshot})
        write(root / "private/a-read-password", secrets.token_urlsafe(48))
        restic_a("key", "add", "--new-password-file", str(root / "private/a-read-password"))
        return
    if args.action == "export-credentials":
        status = api(cfg, "admin").request(f"/api/v1/installations/{installation}/preparation")
        if not status["profile_available"]:
            raise RuntimeError("perfil ainda bloqueado")
        target = root / "private/delivery"
        target.mkdir(mode=0o700)
        for source, name in (("windows-key", "id_windows"), ("windows-password", "restic-password.txt")):
            write(target / name, (root / "private" / source).read_text())
        print("Credenciais separadas em private/delivery. Não registrar conteúdo em evidências.")
        return
    if args.action == "stop-a":
        check_container(cfg["sshd"], root)
        run("docker", "stop", "-t", "2", cfg["sshd"])
        record(root, "a-off.json", {"container": cfg["sshd"], "stopped_at": time.time()})
        return
    if args.action == "windows-point":
        if not args.reference or not args.snapshot:
            raise RuntimeError("informe manifesto e ID completo do Windows")
        if len(args.snapshot) != 64 or any(c not in "0123456789abcdef" for c in args.snapshot):
            raise RuntimeError("ID Restic deve ter 64 caracteres hexadecimais")
        reference = json.loads(args.reference.read_text(encoding="utf-8-sig"))
        points = json.loads(restic_a("snapshots", "--json"))
        point = next(p for p in points if p["id"] == args.snapshot)
        manifest = windows_manifest(reference, point["paths"])
        write(root / "private/windows-manifest.json", json.dumps(manifest))
        record(root, "windows-point.json", {
            "snapshot_a": args.snapshot, "source": reference["source"],
            "manifest_sha256": hashlib.sha256(args.reference.read_bytes()).hexdigest(),
        })
        return
    if args.action == "prepare-b":
        volume = Path("/dev/shm") / ("macip-windows-" + installation.hex)
        volume.mkdir(mode=0o700)
        write(volume / ".macip-volume", str(uuid.uuid4()))
        write(root / "private/volume", str(volume))
    volume = Path((root / "private/volume").read_text())
    protector = LabProtector(Path("/dev/shm"), volume, uuid.UUID((volume / ".macip-volume").read_text()),
                             root / "b-state", reserve_bytes=64 * 1024**2)
    if args.action in {"prove-seed", "prove-windows", "prove-activation"}:
        kind = "windows" if args.action == "prove-windows" else "seed"
        copy_file = "copy-activation.json" if args.action == "prove-activation" else "copy-" + kind + ".json"
        expected_copy = json.loads((root / "evidence" / copy_file).read_text())
        current = b_api.request(f"/api/v1/protector/installations/{installation}")["protection"]
        if current is None or current.get("last_copy") != expected_copy:
            raise RuntimeError("último ponto de B difere da cópia escolhida para a prova")
        started = time.monotonic()
        result = protector.execute(b_api, installation, "prove",
                                   manifest=root / f"private/{kind}-manifest.json")
        proof_name = "activation-proof.json" if args.action == "prove-activation" else f"{kind}-proof.json"
        record(root, proof_name, {**result, "duration_seconds": time.monotonic() - started})
    else:
        snapshot = None
        if args.action in {"copy-seed", "copy-activation"}:
            snapshot = json.loads((root / "evidence/seed.json").read_text())["snapshot_a"]
        elif args.action == "copy-windows":
            snapshot = json.loads((root / "evidence/windows-point.json").read_text())["snapshot_a"]
        action = "prepare" if args.action in {"prepare-b", "refresh-b"} else "copy"
        result = protector.execute(
            b_api, installation, action, snapshot=snapshot,
            source_key=root / "private/b-reader", source_password_file=root / "private/a-read-password",
        )
        record(root, f"{args.action}.json", result)
    print("Resultado público registrado em evidence.")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ProtectionError) as error:
        raise SystemExit(str(error)) from None
    except (OSError, ValueError, KeyError, StopIteration):
        raise SystemExit("Etapa recusada ou falhou. Preserve a área e confira o roteiro e o estado público.") from None
