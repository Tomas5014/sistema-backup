"""Integridade e barreira HTTP simulada; sem equipamentos operacionais."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tarfile

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from macip_backup_api.lab_updates import stage,selected,UpdateError
from macip_backup_api.lab_supervisor import atomic_json
from macip_backup_api.auth import require_viewer

REPO=Path(__file__).resolve().parents[2]


def bundle(path,*,unsafe=False):
    source=path.parent/'source';source.mkdir()
    (source/'code.py').write_text('# synthetic\n')
    manifest=dict(format=1,origin='Tomas5014/sistema-backup',commit='a'*40,windows='b'*40,
        activation_contract=1,results_contract=1,controller_protocol=1,postgres_min=14,postgres_max=14,
        files={'code.py':hashlib.sha256((source/'code.py').read_bytes()).hexdigest()})
    m=path.parent/'MANIFEST.json';m.write_text(json.dumps(manifest))
    with tarfile.open(path,'w:gz') as tar:
        tar.add(source,arcname='source');tar.add(m,arcname='MANIFEST.json')
        if unsafe:
            entry=tarfile.TarInfo('../escape');entry.size=0;tar.addfile(entry)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_stage_integrity_and_no_overwrite(tmp_path):
    root=tmp_path/'área';root.mkdir(mode=0o700)
    package=tmp_path/'p.tar.gz';digest=bundle(package)
    with pytest.raises(UpdateError):stage(root,package,'0'*64,'a'*40)
    rid=stage(root,package,digest,'a'*40)
    assert stage(root,package,digest,'a'*40)==rid
    release,m=selected(root,rid)
    (release/'source/code.py').chmod(0o600);(release/'source/code.py').write_text('alterado')
    with pytest.raises(UpdateError):selected(root,rid)


def test_stage_refuses_traversal(tmp_path):
    root=tmp_path/'area';root.mkdir(mode=0o700)
    p=tmp_path/'p.tar.gz';digest=bundle(p,unsafe=True)
    with pytest.raises(UpdateError):stage(root,p,digest,'a'*40)
    assert not (tmp_path/'escape').exists()
    assert list((root/'updates/releases').iterdir())==[]


def test_guard_freeze_drain_reconcile_and_readonly_version(tmp_path):
    spec=importlib.util.spec_from_file_location('lab_guard',REPO/'tools/lab_guard.py')
    guard=importlib.util.module_from_spec(spec);spec.loader.exec_module(guard)
    app=FastAPI();count=[]
    for path in ('/api/v1/windows-results/executions','/api/v1/clients/prepare','/api/v1/protector/operations/claim'):
        app.add_api_route(path,lambda:count.append(1) or {'ok':True},methods=['POST'])
    guard.attach(app,tmp_path);app.dependency_overrides[require_viewer]=lambda:None
    with TestClient(app) as client:
        for phase,operator,agent in (('open',200,200),('drain',503,200),('freeze',503,503),('reconcile',503,200)):
            atomic_json(tmp_path/'updates/gate.json',dict(phase=phase))
            assert client.post('/api/v1/clients/prepare').status_code==operator
            assert client.post('/api/v1/windows-results/executions').status_code==agent
            assert client.get('/api/v1/lab/version').json()['phase']==phase
        atomic_json(tmp_path/'updates/gate.json',dict(phase='open'))
        assert client.post('/api/v1/lab/version',json={'url':'https://arbitrary','command':'sh'}).status_code==405


def test_inconsistent_queue_refuses_before_stopping_or_touching_code(tmp_path):
    from macip_backup_api.lab_updates import LaboratoryUpdate
    update=object.__new__(LaboratoryUpdate);update.root=tmp_path;update.directory=tmp_path/'updates'
    update.directory.mkdir(mode=0o700);update.tx={};update.pending=lambda:1
    sentinel=tmp_path/'active-code';sentinel.write_bytes(b'keep')
    with pytest.raises(UpdateError,match='fila não ficou ociosa'):
        with update.quiet(.01):raise AssertionError('não deve parar serviços')
    assert sentinel.read_bytes()==b'keep'
    assert json.loads((update.directory/'gate.json').read_text())['phase']=='open'


def test_gate_waits_for_existing_request_without_forcing_unlock(tmp_path):
    import fcntl,os,threading,time
    from macip_backup_api.lab_updates import LaboratoryUpdate
    update=object.__new__(LaboratoryUpdate);update.directory=tmp_path
    fd=os.open(tmp_path/'writers.lock',os.O_CREAT|os.O_RDWR,0o600);fcntl.flock(fd,fcntl.LOCK_SH)
    def finish():time.sleep(.05);os.close(fd)
    thread=threading.Thread(target=finish);thread.start()
    update.gate('freeze');thread.join()
    assert json.loads((tmp_path/'gate.json').read_text())['phase']=='freeze'


def test_reused_or_dead_updater_pid_is_not_reported_as_active(tmp_path):
    spec=importlib.util.spec_from_file_location('lab_guard',REPO/'tools/lab_guard.py')
    guard=importlib.util.module_from_spec(spec);spec.loader.exec_module(guard)
    (tmp_path/'updates').mkdir(mode=0o700)
    atomic_json(tmp_path/'updates/transaction.json',dict(state='running',executor='1:0',step='migration'))
    assert guard.public_state(tmp_path)['state']=='needs-recovery'
