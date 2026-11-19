"""Endpoint HTTPS do executor A. O segredo Restic nunca passa pela central."""

import argparse
import fcntl
import json
import os
from pathlib import Path
import subprocess
import secrets
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .activation_routes import digest
from .provisioner import ApiTransport, LabFileVault, LabPrimaryAdapter


class DeliveryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    activation_id: uuid.UUID
    resume_secret: str = Field(min_length=43, max_length=128)


class LabActivationExecutor:
    def __init__(self, central, accounts: Path, vault, *, lab_container=None, dynamic_containers=False):
        self.central, self.accounts, self.vault = central, accounts, vault
        self.lab_container = lab_container
        self.dynamic_containers = dynamic_containers

    def container_command(self, *args):
        result = subprocess.run(["docker", *args], capture_output=True, timeout=30)
        if result.returncode:
            raise RuntimeError("fixture A indisponível")
        return result.stdout

    def check_container(self, container):
        label = self.container_command(
            "inspect", "-f", '{{index .Config.Labels "macip.activation-accounts"}}', container,
        ).decode().strip()
        if label != str(self.accounts):
            raise RuntimeError("contêiner não pertence ao executor A")

    def activate(self, request):
        authorization = {"activation_id": str(request.activation_id), "resume_hash": digest(request.resume_secret)}
        context = self.central.request("/api/v1/activation-executor/authorize", authorization)
        installation = context["installation_id"]
        # Conta e caminho derivados exclusivamente da identidade conhecida de A.
        account = context["account_name"]
        path = self.accounts / account / ".ssh/authorized_keys"
        container = self.lab_container
        if self.dynamic_containers:
            registry = self.accounts / account / "sftp-runtime.json"
            LabPrimaryAdapter._validate_private_file(registry)
            value = json.loads(registry.read_text())
            if value.get("account") != account or value.get("installation_id") != installation:
                raise RuntimeError("registro SFTP divergente")
            container = value["container"]
        LabPrimaryAdapter._private_directory(self.accounts / account)
        LabPrimaryAdapter._private_directory(path.parent)
        LabPrimaryAdapter._validate_private_file(path)
        fd = os.open(self.accounts / account, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            if container:
                self.check_container(container)
                # Lê a política efetiva de A antes de acrescentar a chave Windows.
                live = self.container_command("exec", container, "cat",
                                              f"/home/{account}/.ssh/authorized_keys")
                path.write_bytes(live)
            lines = path.read_text().splitlines()
            public = context["public_key"]
            # Preserve todas as regras existentes, incluindo a chave de leitura de B.
            if not any(line.split()[-2:] == public.split() for line in lines):
                temporary = path.with_name("authorized_keys." + secrets.token_hex(12))
                with temporary.open("x") as stream:
                    temporary.chmod(0o600)
                    stream.write("\n".join(lines) + '\nrestrict,command="internal-sftp" ' + public + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
            if container:
                target = f"/home/{account}/.ssh/authorized_keys"
                self.container_command("cp", str(path), container + ":" + target)
                self.container_command("exec", container, "chown", f"{account}:{account}", target)
                self.container_command("exec", container, "chmod", "600", target)
            if not context["registered"]:
                self.central.request("/api/v1/activation-executor/registered", authorization)
                return {"state": "awaiting_protection"}
            if not context["ready"]:
                return {"state": "awaiting_protection"}
            bundle = self.vault.get(f"installation/{installation}/bundle")
            if bundle is None:
                raise RuntimeError("segredo original ausente")
            password = json.loads(bundle)["restic_password"]
            # Última autorização central imediatamente antes da entrega.
            context = self.central.request("/api/v1/activation-executor/delivered", authorization)
            return {"contract_version": 1, "state": "ready", "profile": context["profile"],
                    "restic_password": password}
        finally:
            os.close(fd)


def create_app(executor):
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def bounded_request(request: Request, call_next):
        size = 0
        chunks = []
        async for chunk in request.stream():
            size += len(chunk)
            if size > 4096:
                return JSONResponse({"detail": "requisição muito grande"}, status_code=413)
            chunks.append(chunk)
        request._body = b"".join(chunks)
        return await call_next(request)

    @app.exception_handler(RequestValidationError)
    async def invalid(_request, _error):
        return JSONResponse({"detail": "requisição de ativação inválida"}, status_code=422)

    @app.post("/v1/activate")
    def activate(payload: DeliveryIn):
        try:
            result = executor.activate(payload)
        except (RuntimeError, OSError, ValueError, KeyError):
            raise HTTPException(409, "ativação pendente ou recusada; confira a central") from None
        return JSONResponse(result, status_code=200 if result["state"] == "ready" else 202,
                            headers={"Cache-Control": "no-store"})
    return app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9447)
    parser.add_argument("--certificate", required=True)
    parser.add_argument("--key", required=True)
    args = parser.parse_args()
    if os.environ.get("MACIP_API_LAB_ACTIVATION") != "1":
        parser.error("modo de laboratório deve ser explícito")
    central = ApiTransport(os.environ["MACIP_PROVISIONER_API_URL"],
                           os.environ["MACIP_PROVISIONER_TOKEN"], "activation-a",
                           allow_lab_http=os.environ.get("MACIP_PROVISIONER_LAB_ALLOW_HTTP") == "1")
    executor = LabActivationExecutor(central, Path(os.environ["MACIP_PROVISIONER_LAB_ROOT"]),
                                    LabFileVault(Path(os.environ["MACIP_PROVISIONER_LAB_VAULT"])),
                                    lab_container=os.environ.get("MACIP_ACTIVATION_LAB_CONTAINER"),
                                    dynamic_containers=os.environ.get("MACIP_ACTIVATION_LAB_DYNAMIC") == "1")
    import uvicorn
    uvicorn.run(create_app(executor), host=args.host, port=args.port,
                ssl_certfile=args.certificate, ssl_keyfile=args.key, access_log=False)


if __name__ == "__main__":
    main()
