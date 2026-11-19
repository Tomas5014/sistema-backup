"""Processos, PostgreSQL, dump/restore, migrações, A/SFTP e B reais em área nova.

O candidato durante desenvolvimento é um pacote de fixture com rótulo sintético;
origem de Git é testada separadamente pelo empacotador e no artefato final.
Não há Windows/DPAPI/COM nesta prova.
"""
from contextlib import contextmanager
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import sys
import tarfile
import time
from urllib.request import Request,urlopen
from urllib.error import HTTPError
import uuid

import pytest
from sqlalchemy import create_engine,text

from macip_backup_api.lab_updates import LaboratoryUpdate,stage,selected,UpdateError
from macip_backup_api.lab_supervisor import atomic_json,control
from macip_backup_api.lab_runtime import command
from test_lab_services_real import wait_for,port

REPO=Path(__file__).resolve().parents[2]
BASE='fd9a82adc295ec66a652a3d28dc69fe6842be11b'
WINDOWS='97de94ba563feeccb24fabea11d12abada67e247'


def fixture_bundle(directory,commit,migration=None):
    source=directory/'source';source.mkdir(parents=True)
    shutil.copytree(REPO/'api/src',source/'api/src',ignore=shutil.ignore_patterns('__pycache__'))
    shutil.copytree(REPO/'portal/dist',source/'portal/dist')
    (source/'tools').mkdir()
    for name in ('windows-lab-server.py','lab_guard.py','lab-update.py'):
        shutil.copyfile(REPO/'tools'/name,source/'tools'/name)
    if migration:
        p=source/'api/src/macip_backup_api/migrations.py'
        with p.open('a') as out:out.write("\nMIGRATIONS.append((15,'update-fixture',"+repr(migration)+"))\n")
    files={p.relative_to(source).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in source.rglob('*') if p.is_file()}
    m=dict(reconciliation=True,format=1,origin='Tomas5014/sistema-backup',commit=commit,windows='493a5651fb5bf19869a87c925c8f946bbad6d71b',activation_contract=1,results_contract=1,
           controller_protocol=1,postgres_min=15 if migration else 14,postgres_max=15 if migration else 14,files=files)
    (directory/'MANIFEST.json').write_text(json.dumps(m))
    package=directory/'package.tgz'
    with tarfile.open(package,'w:gz') as tar:
        tar.add(source,arcname='source');tar.add(directory/'MANIFEST.json',arcname='MANIFEST.json')
    return package,hashlib.sha256(package.read_bytes()).hexdigest()


@contextmanager
def area(tmp_path,baseline):
    if os.environ.get('MACIP_TEST_LAB_UPDATE_REAL')!='1':pytest.skip('prova de atualização Docker não habilitada explicitamente')
    image=os.environ.get('MACIP_TEST_PROVISIONING_SFTP_IMAGE');pg=os.environ.get('MACIP_TEST_UPDATE_PG_IMAGE')
    if not image or not pg:pytest.fail('configure as imagens locais descartáveis de SSH e PostgreSQL')
    root=tmp_path/'laboratório atualização'
    helper=baseline/'tools/windows-lab-server.py';api_port=port();delivery=port()
    command(sys.executable,str(helper),'init',str(root),'--sftp-ip','127.0.0.1','--sftp-image',image,
            '--postgres-image',pg,'--api-port',str(api_port),'--storage-lab','--windows-commit',WINDOWS,
            env={**os.environ,'PYTHONPATH':str(baseline/'api/src'),'PYTHONDONTWRITEBYTECODE':'1'})
    # Registro inicial da instalação de fixture: os bytes vêm do artefato BASE.
    evidence=json.loads((root/'evidence/base.json').read_text());evidence['central']=BASE
    atomic_json(root/'evidence/base.json',evidence)
    try:
        cert=root/'private/cert.pem';key=root/'private/key.pem'
        command('openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1','-subj','/CN=localhost',
            '-addext','subjectAltName=IP:127.0.0.1','-keyout',str(key),'-out',str(cert));key.chmod(0o600)
        command(sys.executable,str(helper),'configure-activation',str(root),'--public-url',f'https://127.0.0.1:{api_port}',
                '--delivery-url',f'https://127.0.0.1:{delivery}/v1/activate',env={**os.environ,'PYTHONPATH':str(baseline/'api/src'),'PYTHONDONTWRITEBYTECODE':'1'})
        command(sys.executable,str(helper),'services-start',str(root),'--certificate',str(cert),'--key',str(key),'--ca',str(cert),'--delivery-port',str(delivery),env={**os.environ,'PYTHONPATH':str(baseline/'api/src'),'PYTHONDONTWRITEBYTECODE':'1'})
        cfg=json.loads((root/'private/config.json').read_text());context=ssl.create_default_context(cafile=str(cert))
        def request(path,body=None,token=None):
            with urlopen(Request(cfg['api']+path,data=json.dumps(body).encode() if body is not None else None,
                headers={'Content-Type':'application/json','Authorization':'Bearer '+(token or cfg['admin'])},method='POST' if body is not None else 'GET'),
                context=context,timeout=15) as response:return json.load(response)
        wait_for(lambda:request('/healthz'))
        yield root,cfg,request
    finally:
        command(sys.executable,str(helper),'stop',str(root))
        installed=root/'private/services/installation.json'
        if installed.exists():
            settings=json.loads(installed.read_text());volume=Path(settings['volume'])
            if volume.exists():
                assert (volume/'.macip-volume').read_text()==settings['volume_id'];shutil.rmtree(volume)
        for folder in root.rglob('*'):
            if folder.is_dir():folder.chmod(0o700)
        shutil.rmtree(root)


def test_real_update_rollback_failed_migration_restore_and_reconciliation(tmp_path):
    spec=importlib.util.spec_from_file_location('lab_pack',REPO/'tools/package-lab-release.py')
    pack=importlib.util.module_from_spec(spec);spec.loader.exec_module(pack)
    previous=tmp_path/'previous.tgz';pack.build(REPO,BASE,previous,WINDOWS)
    base_dir=tmp_path/'baseline';base_dir.mkdir()
    with tarfile.open(previous) as tar:tar.extractall(base_dir,filter='data')
    with area(tmp_path,base_dir/'source') as (root,cfg,request):
        identities=[]
        for name in ('Cliente fictício 1','Cliente fictício 2'):
            prep=request('/api/v1/clients/prepare',dict(id=str(uuid.uuid4()),client_name=name,
                installation_identifier='pc-unico',machine_name='Windows fictício',suggested_time='11:00'))
            aid=prep['installation']['id']
            wait_for(lambda:request(f'/api/v1/installations/{aid}/preparation')['profile_available'],180)
            prep=request(f'/api/v1/installations/{aid}/preparation')
            identities.append((aid,prep['client_id'],prep['repository_id'],prep['last_protected_copy']))
            credential=request(f'/api/v1/agents/{aid}/credentials',{'label':'produtor fictício'})
            from conftest import execution_payload
            payload=execution_payload(task_id='backup-de-ensaio-'+aid)
            payload['destinations'][0].update(destination_id='sftp-fixture',snapshot_id=prep['last_protected_copy']['snapshot_a'])
            payload['destinations'][0]['attempts'][0]['snapshot_id']=prep['last_protected_copy']['snapshot_a']
            request('/api/v1/agent/executions',payload,token=credential['token'])

        new_pkg,new_hash=fixture_bundle(tmp_path/'candidate','2'*40)
        controller=stage(root,new_pkg,new_hash,'2'*40)
        old=stage(root,previous,hashlib.sha256(previous.read_bytes()).hexdigest(),BASE)
        cli=REPO/'tools/lab-update.py'
        command(sys.executable,str(cli),'adopt',str(root),'--artifact',old,'--controller',controller)
        update=LaboratoryUpdate(root)
        assert update.preflight(controller)['current_schema']==14
        with update.engine.connect() as db:
            assert db.scalar(text('SELECT count(DISTINCT agent_id) FROM executions'))==2
        result=json.loads(command(sys.executable,str(cli),'apply',str(root),'--artifact',controller,'--confirm','--wait','60',timeout=180))
        assert result['state']=='succeeded';backup=result['backup']
        version=request('/api/v1/lab/version');assert version['commit']=='2'*40 and version['schema']==14
        # Escrita posterior: reversão compatível não a perde.
        later=request('/api/v1/clients',{'name':'Escrita posterior preservada'})
        rolled=subprocess.run([sys.executable,str(cli),'rollback',str(root),'--artifact',old,'--confirm','--wait','60'],capture_output=True,timeout=180)
        diagnostic=json.loads((root/'updates/transaction.json').read_text())
        assert rolled.returncode==0,(rolled.stderr.decode(),{k:diagnostic.get(k) for k in ('state','step','error')})
        result=json.loads(rolled.stdout)
        assert result['state']=='succeeded'
        with create_engine(cfg['database']).connect() as db:assert db.scalar(text('SELECT name FROM clients WHERE id=:id'),dict(id=later['id']))
        # Falha de migração real: a tabela criada antes do erro também reverte.
        bad,dh=fixture_bundle(tmp_path/'migration-failure','3'*40,'CREATE TABLE migration_probe(id int); SELECT 1/0;')
        bad_id=stage(root,bad,dh,'3'*40)
        with pytest.raises(UpdateError):update.apply(bad_id,wait=60)
        assert update.schema()==14
        with update.engine.connect() as db:assert db.scalar(text("SELECT to_regclass('public.migration_probe')")) is None
        assert update.read('transaction.json')['step']=='migration'
        assert update.recover()['state']=='recovered'
        # Retomada: pacote válido consegue usar os mesmos dados e vínculos.
        assert update.apply(controller,wait=60)['state']=='succeeded'
        for aid,cid,repo,last in identities:
            current=request(f'/api/v1/installations/{aid}/preparation')
            assert (current['client_id'],current['repository_id'],current['last_protected_copy'])==(cid,repo,last)
        # Migração expansiva real 15; voltar ao leitor 14 fica proibido.
        future,fh=fixture_bundle(tmp_path/'future','4'*40,'CREATE TABLE update_future_probe(id int);')
        future_id=stage(root,future,fh,'4'*40)
        assert update.apply(future_id,wait=60)['state']=='succeeded';assert update.schema()==15
        with pytest.raises(UpdateError,match='esquema incompatível'):update.preflight(controller,rollback=True)
        preview=update.restore_preview(backup);assert 'clients' in preview['changed_tables']
        retained_url=update.cfg['database']
        restored=update.restore(backup,wait=60);assert restored['maintenance']=='blocked'
        assert request('/api/v1/lab/version')['needs_reconciliation']
        with create_engine(retained_url).connect() as db:assert db.scalar(text('SELECT name FROM clients WHERE id=:id'),dict(id=later['id']))
        with update.engine.connect() as db:assert db.scalar(text('SELECT name FROM clients WHERE id=:id'),dict(id=later['id'])) is None
        for aid,cid,repo,last in identities:
            current=request(f'/api/v1/installations/{aid}/preparation');assert current['repository_id']==repo and current['client_id']==cid
        assert update.reconcile()['state']=='reconciling'
        wait_for(lambda:update.reconcile()['state']=='recovered',180)
        assert request('/api/v1/lab/version')['phase']=='open'
        with update.engine.connect() as db:assert db.scalar(text('SELECT count(DISTINCT agent_id) FROM executions'))==2
        for aid,cid,repo,last in identities:
            current=request(f'/api/v1/installations/{aid}/preparation')
            assert current['client_id']==cid and current['repository_id']==repo
            assert current['last_protected_copy']['snapshot_a']==last['snapshot_a']
            assert current['last_protected_copy']['snapshot_b']==last['snapshot_b']
        if os.environ.get('MACIP_TEST_UPDATE_BROWSER')=='1':
            import secrets
            login='update-visual';password=secrets.token_urlsafe(24)
            request('/api/v1/operators',dict(login=login,name='Operador fictício',password=password,role='admin'))
            visual=subprocess.run(['node',str(REPO/'portal/e2e/update-version.browser.cjs'),cfg['api']],
                env={**os.environ,'MACIP_TEST_BROWSER_LOGIN':login,'MACIP_TEST_BROWSER_PASSWORD':password},capture_output=True,timeout=60)
            assert visual.returncode==0,visual.stderr.decode()
