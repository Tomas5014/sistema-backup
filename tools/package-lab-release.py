#!/usr/bin/env python3
"""Artefato central de commit aprovado. Não inclui dependências nem segredos."""
import argparse
import sys
import os
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tarfile
import tomllib
import tempfile
import shutil

ORIGIN='Tomas5014/sistema-backup'
BASE='fd9a82adc295ec66a652a3d28dc69fe6842be11b'


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def build(repo,commit,output,windows):
    if any(p.exists() for p in (output,output.with_name(output.name+'.manifest.json'),output.with_name(output.name+'.sha256'),output.with_name(output.name+'.incomplete'))):raise RuntimeError('saída/manifesto/checksum já existe')
    if len(commit)!=40 or len(windows)!=40:raise RuntimeError('use SHAs completos')
    subprocess.run(['git','merge-base','--is-ancestor',BASE,commit],cwd=repo,check=True,capture_output=True)
    raw=subprocess.check_output(['git','ls-tree','-r','--name-only',commit],cwd=repo,text=True).splitlines()
    wanted=[p for p in raw if p.startswith(('api/','portal/','tools/')) or p=='deploy/package-candidate.py']
    spec=importlib.util.spec_from_file_location('package_filter',repo/'deploy/package-candidate.py')
    filters=importlib.util.module_from_spec(spec);spec.loader.exec_module(filters)
    with tempfile.TemporaryDirectory(prefix='macip-release-') as temp:
        stage=Path(temp);source=stage/'source';source.mkdir()
        archive=stage/'tracked.tar'
        with archive.open('wb') as out:subprocess.run(['git','archive',commit,*wanted],cwd=repo,stdout=out,check=True)
        with tarfile.open(archive) as tar:
            members=[]
            for item in tar.getmembers():
                path=Path(item.name)
                if item.issym() or item.islnk() or path.is_absolute() or '..' in path.parts or not (item.isfile() or item.isdir()):
                    raise RuntimeError('entrada insegura')
                if any(x in {'node_modules','dist','__pycache__'} for x in path.parts):continue
                if item.isfile() and filters.operational_file(path,tar.extractfile(item).read()):raise RuntimeError('material operacional recusado')
                members.append(item)
            tar.extractall(source,members=members,filter='data')
        # Usa só o cache correspondente ao lockfile exato; nada de baixar/executar
        # um instalador enviado pelo navegador. O build tem origem no commit.
        if sha(source/'portal/package-lock.json')!=sha(repo/'portal/package-lock.json'):
            raise RuntimeError('cache Node não corresponde ao lockfile; prepare build descartável compatível')
        (source/'portal/node_modules').symlink_to(repo/'portal/node_modules',target_is_directory=True)
        try:subprocess.run(['npm','run','build'],cwd=source/'portal',check=True,capture_output=True,timeout=180)
        finally:(source/'portal/node_modules').unlink()
        for cache in source.rglob('*.tsbuildinfo'):cache.unlink()
        result=subprocess.check_output([sys.executable,'-c',
            "import sys;sys.path.insert(0,sys.argv[1]);from macip_backup_api.migrations import MIGRATIONS;print(MIGRATIONS[-1][0])",str(source/'api/src')],text=True,env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1'})
        manifest=dict(reconciliation=(source/'api/src/macip_backup_api/lab_updates.py').exists(),format=1,origin=ORIGIN,commit=commit,windows=windows,activation_contract=1,results_contract=1,
            postgres_min=int(result),postgres_max=int(result),controller_protocol=1,
            components=dict(api=tomllib.loads((source/'api/pyproject.toml').read_text())['project']['version'],portal=json.loads((source/'portal/package.json').read_text())['version']),
            dependencies='use validated Python environment; dependencies and images not byte-reproducible',
            installer=dict(available=False,sha256=None),
            files={p.relative_to(source).as_posix():sha(p) for p in sorted(source.rglob('*')) if p.is_file()})
        (stage/'MANIFEST.json').write_text(json.dumps(manifest,sort_keys=True,indent=2)+'\n')
        output.parent.mkdir(parents=True,exist_ok=True)
        temporary=output.with_name(output.name+'.incomplete')
        if temporary.exists():raise RuntimeError('temporário já existe')
        try:
            with tarfile.open(temporary,'w:gz') as tar:
                tar.add(stage/'MANIFEST.json',arcname='MANIFEST.json');tar.add(source,arcname='source')
            temporary.rename(output)
        finally:temporary.unlink(missing_ok=True)
        external=output.with_name(output.name+'.manifest.json')
        if external.exists():raise RuntimeError('manifesto externo já existe')
        external.write_text(json.dumps(manifest,sort_keys=True,indent=2)+'\n')
        output.with_name(output.name+'.sha256').write_text(sha(output)+'  '+output.name+'\n')
        return manifest


def main():
    p=argparse.ArgumentParser();p.add_argument('commit');p.add_argument('output',type=Path);p.add_argument('--windows',required=True)
    a=p.parse_args();build(Path(__file__).resolve().parents[1],a.commit,a.output.resolve(),a.windows)
    print(a.output)
if __name__=='__main__':main()
