#!/usr/bin/env python3
"""Empacota somente conteúdo versionado de um commit, em staging descartável."""

import argparse
import hashlib
import json
import subprocess
import tarfile
import tempfile
import tomllib
from pathlib import Path


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
    output.mkdir(parents=True, exist_ok=False)
    paths = ["src", "api", "portal", "deploy", "docs", "tests", "examples",
             "README.md", "registro-trabalho.md", "pyproject.toml", ".dockerignore"]
    with tempfile.TemporaryDirectory(prefix="macip-candidate-") as directory:
        stage = Path(directory)
        archive = stage / "source.tar"
        with archive.open("wb") as stream:
            subprocess.run(["git", "archive", sha, *paths], cwd=repo, stdout=stream, check=True)
        source = stage / "source"
        source.mkdir()
        with tarfile.open(archive) as tar:
            members = []
            for member in tar.getmembers():
                path = Path(member.name)
                # Runtime nunca entra, mesmo se alguém o tiver versionado.
                if any(part in {"node_modules", "dist", "__pycache__", "backups", "tls"}
                       for part in path.parts):
                    continue
                if member.issym() or member.islnk() or path.is_absolute() or ".." in path.parts:
                    raise RuntimeError(f"entrada insegura: {member.name}")
                if not member.isdir() and (
                    path.suffix in {".db", ".sqlite", ".sqlite3", ".pem", ".key", ".log", ".pyc"}
                    or path.name == ".env"
                    or ("secrets" in path.parts and not path.name.endswith(".example"))
                ):
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
            "candidate": version, "status": "Linux controlled pilot candidate",
            "commit": sha, "agent": agent["version"], "api": api["version"],
            "portal": portal["version"], "postgresql": "17", "migrations": [1, 2, 3, 4, 5, 6],
            "sqlite_schema": 7, "task_contract": 1,
            "scope": "docs/candidata-linux.md", "runbook": "docs/instalacao-atualizacao-retorno.md",
            "build_dependencies": "Python >=3.12 (tar filter), setuptools >=68",
            "reproducibility": "commit-pinned source; no byte-for-byte image/dependency guarantee",
            "wheels": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in wheels.iterdir()},
        }
        manifest_text = json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"
        (bundle / "MANIFEST.json").write_text(manifest_text)
        (output / "MANIFEST.json").write_text(manifest_text)
        package = output / f"{bundle.name}-{sha[:12]}.tar.gz"
        with tarfile.open(package, "w:gz") as tar:
            tar.add(bundle, arcname=bundle.name)
        files = [package, output / "MANIFEST.json"]
        (output / "SHA256SUMS").write_text("".join(
            f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}\n" for p in files
        ))
        print(package)


if __name__ == "__main__":
    main()
