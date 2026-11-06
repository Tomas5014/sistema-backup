#!/usr/bin/env python3
"""Empacota somente conteúdo versionado de um commit, em staging descartável."""

import argparse
import hashlib
import json
import re
import subprocess
import tarfile
import tempfile
import tomllib
from pathlib import Path


ENV_MODELS = {
    "deploy/central/.env.example",
    "deploy/central/secrets/api.env.example",
    "deploy/central/secrets/postgres.env.example",
    "deploy/central/secrets/central-backup.env.example",
}


def operational_file(path, data):
    """Controles conhecidos; não é um detector universal de segredos."""
    name = path.name.lower()
    if ((str(path) not in ENV_MODELS and (
            re.search(r"(^|\.)env($|\.)", name) or name == ".envrc"
            or "secrets" in path.parts))
            or re.search(r"\.(db|sqlite|sqlite3)(-(wal|shm|journal))?$", name)
            or name.endswith(("-wal", "-shm", "-journal"))
            or name in {"id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "identity"}
            or path.suffix.lower() in {".pem", ".key", ".log", ".pyc"}):
        return True
    if data.startswith((b"SQLite format 3\x00", b"\x37\x7f\x06\x82", b"\x37\x7f\x06\x83")):
        return True
    header = rb"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----"
    # Um arquivo de chave sem extensão começa no cabeçalho; fontes que
    # somente citam a string não começam nele. Também recusar blocos reais.
    return bool(re.match(header, data.lstrip()) or re.search(
        header + rb"\r?\n[A-Za-z0-9+/=\r\n]+-----END (?:[A-Z0-9]+ )*PRIVATE KEY-----", data
    ))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("commit", help="commit exato aprovado")
    parser.add_argument("output", type=Path, help="diretório novo de saída")
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    sha = subprocess.check_output(
        ["git", "rev-parse", "--verify", f"{args.commit}^{{commit}}"], cwd=repo, text=True
    ).strip()
    output = args.output.resolve()
    if output.exists():
        raise FileExistsError("diretório de saída já existe")
    output.parent.mkdir(parents=True, exist_ok=True)
    paths = ["src", "api", "portal", "deploy", "docs", "tests", "examples",
             "README.md", "registro-trabalho.md", "pyproject.toml", ".dockerignore"]
    with tempfile.TemporaryDirectory(prefix=".macip-candidate-", dir=output.parent) as directory:
        stage = Path(directory)
        publication = stage / "publication"
        publication.mkdir()
        archive = stage / "source.tar"
        with archive.open("wb") as stream:
            subprocess.run(["git", "archive", sha, *paths], cwd=repo, stdout=stream, check=True)
        source = stage / "source"
        source.mkdir()
        with tarfile.open(archive) as tar:
            members = []
            for member in tar.getmembers():
                path = Path(member.name)
                if member.issym() or member.islnk() or path.is_absolute() or ".." in path.parts:
                    raise RuntimeError(f"entrada insegura: {member.name}")
                # Runtime nunca entra, mesmo se alguém o tiver versionado.
                if any(part in {"node_modules", "dist", "__pycache__", "backups", "tls"}
                       for part in path.parts):
                    continue
                if member.isfile() and operational_file(path, tar.extractfile(member).read()):
                    raise RuntimeError(f"material operacional recusado: {member.name}")
                members.append(member)
            tar.extractall(source, members=members, filter="data")
        version = (source / "deploy/CANDIDATE").read_text().strip()
        bundle = stage / f"macip-backup-linux-{version}"
        bundle.mkdir()
        source.rename(bundle / "source")
        source = bundle / "source"
        wheels = bundle / "wheels"
        wheels.mkdir()
        subprocess.run(
            ["python3", "-c", "from setuptools.build_meta import build_wheel; "
             f"build_wheel({str(wheels)!r})"], cwd=source, check=True
        )
        # Backend de build pode criar intermediários; não os distribuímos.
        import shutil
        shutil.rmtree(source / "build", ignore_errors=True)
        for egg in (source / "src").glob("*.egg-info"):
            shutil.rmtree(egg)
        agent = tomllib.loads((source / "pyproject.toml").read_text())["project"]
        api = tomllib.loads((source / "api/pyproject.toml").read_text())["project"]
        portal = json.loads((source / "portal/package.json").read_text())
        manifest = {
            "candidate": version,
            "status": "experimental central task monitoring; not homologated",
            "commit": sha, "agent": agent["version"], "api": api["version"],
            "portal": portal["version"], "postgresql": "17", "migrations": [1, 2, 3, 4, 5, 6, 7],
            "sqlite_schema": 7, "task_contract": 1,
            "scope": "docs/candidata-monitoramento.md",
            "runbook": "docs/instalacao-monitoramento.md",
            "homologation": "docs/homologacao-monitoramento.md",
            "build_dependencies": "Python >=3.12 (tar filter), setuptools >=68",
            "reproducibility": "commit-pinned source; no byte-for-byte image/dependency guarantee",
            "wheels": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in wheels.iterdir()},
        }
        manifest_text = json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"
        (bundle / "MANIFEST.json").write_text(manifest_text)
        (publication / "MANIFEST.json").write_text(manifest_text)
        package = publication / f"{bundle.name}-{sha[:12]}.tar.gz"
        with tarfile.open(package, "w:gz") as tar:
            tar.add(bundle, arcname=bundle.name)
        files = [package, publication / "MANIFEST.json"]
        (publication / "SHA256SUMS").write_text("".join(
            f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}\n" for p in files
        ))
        publication.rename(output)
        print(output / package.name)


if __name__ == "__main__":
    main()
