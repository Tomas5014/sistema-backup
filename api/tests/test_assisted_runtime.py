"""Filesystem real; transporte e Restic controlados para interrupção/reenvio."""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest

from macip_backup_api.assisted_runtime import AssistedWorker
from macip_backup_api.protector import ProtectionError
from macip_backup_api.storage_runtime import StorageFailure


def setup_worker(tmp_path):
    for name in ('private','private/services','private/assisted','private/recovery-exports'):
        (tmp_path/name).mkdir(mode=0o700,exist_ok=True)
    worker=object.__new__(AssistedWorker)
    worker.root=tmp_path;worker.state=tmp_path/'private/assisted';worker.exports=tmp_path/'private/recovery-exports'
    worker.prefix='/api/v1/protector/assisted';worker.side='b'
    operation=dict(id=str(uuid.uuid4()),claim_id=str(uuid.uuid4()),kind='recovery_b',
        work=dict(action='restore',context=dict(installation_id=str(uuid.uuid4()),repository_a='a'*64,repository_b='b'*64),
                  point=dict(snapshot_a='c'*64,snapshot_b='d'*64,time='2026-11-23T10:00:00Z')))
    return worker,operation


def test_durable_success_replayed_without_second_restore(tmp_path):
    worker,operation=setup_worker(tmp_path);calls=[];attempts=[]
    class API:
        def request(self,path,body=None):
            if path.endswith('/claim'):
                operation['claim_id']=str(uuid.uuid4());return {'operation':operation}
            attempts.append(body)
            if len(attempts)==1:raise RuntimeError('offline controlado')
            return {'accepted':True}
    worker.api=API()
    def recover(op,journal):
        calls.append(op['id']);return {'files':1,'archive_sha256':'f'*64}
    worker.recover=recover
    with pytest.raises(RuntimeError):worker.tick()
    assert json.loads((worker.state/(operation['id']+'.json')).read_text())['phase']=='completed'
    worker.tick()
    assert calls==[operation['id']]
    assert attempts[0]['result']==attempts[1]['result']
    assert attempts[0]['claim_id']!=attempts[1]['claim_id']


def test_interruption_preserves_partial_and_refuses_reuse(tmp_path):
    worker,operation=setup_worker(tmp_path);ctx=operation['work']['context'];calls=[]
    @contextmanager
    def locked(installation):yield 42
    worker.actor=SimpleNamespace(executor=SimpleNamespace(lock=locked,check_volume=lambda:None))
    class API:
        def request(self,path,body=None):return dict(context=ctx,pairs=[dict(snapshot_a='c'*64,snapshot_b='d'*64)])
    worker.api=API()
    def run(*args):
        if 'snapshots' in args:return json.dumps([{'id':'d'*64}]).encode()
        if 'stats' in args:return b'{"total_size":4}'
        if 'ls' in args:return b'{"struct_type":"node","type":"file","path":"/file.txt"}'
        if 'dump' in args:return b'good'
        if 'restore' in args:
            target=Path(args[args.index('--target')+1]);(target/'file.txt').write_bytes(b'partial')
            calls.append(1);raise ProtectionError('interrupção controlada')
        return b'{}'
    worker.storage=SimpleNamespace(backend=lambda ctx,fd,offline:(run,None,None),inventory=lambda ctx,run:[{'id':'d'*64}])
    journal=worker.state/(operation['id']+'.json')
    with pytest.raises(ProtectionError):worker.recover(operation,journal)
    target=worker.state/('restore-'+operation['id'])
    assert (target/'file.txt').read_bytes()==b'partial'
    assert not list(worker.exports.iterdir())
    with pytest.raises(StorageFailure,match='interrupted'):worker.recover(operation,journal)
    assert len(calls)==1


def test_ssh_session_prevents_false_revocation_confirmation(tmp_path):
    worker,_=setup_worker(tmp_path)
    def stat(pid,state='S',start='123'):
        fields=[state]+['0']*18+[start]
        return str(pid)+' (sshd) '+' '.join(fields)+'\n'
    observations=[stat(10),stat(10)+stat(11)]
    worker.actor=SimpleNamespace(container=lambda *args:(observations.pop(0) if len(observations)>1 else observations[0]).encode())
    with pytest.raises(StorageFailure,match='busy'):
        with worker.idle_ssh('own-fixture'):
            pass
    worker.actor=SimpleNamespace(container=lambda *args:(stat(10)+stat(11)).encode())
    with pytest.raises(StorageFailure,match='busy'):
        with worker.idle_ssh('own-fixture'):
            raise AssertionError('não deve alterar chaves com sessão ativa')


def test_activation_poll_preserves_reader_and_never_rewrites_live_key_with_wrong_uid(tmp_path):
    from macip_backup_api.activation_executor import LabActivationExecutor,DeliveryIn
    accounts=tmp_path/'accounts';accounts.mkdir(mode=0o700)
    account='macip'+'a'*20;folder=accounts/account;folder.mkdir(mode=0o700)
    ssh=folder/'.ssh';ssh.mkdir(mode=0o700)
    reader='restrict,command="internal-sftp -R" ssh-ed25519 READER'
    live=(reader+'\n').encode();local=ssh/'authorized_keys';local.write_bytes(live);local.chmod(0o600)
    calls=[];files={};unsafe=[]
    class Central:
        def request(self,path,body):return dict(installation_id=str(uuid.uuid4()),account_name=account,
            public_key='ssh-ed25519 WINDOWS',registered=True,ready=False,profile={})
    executor=LabActivationExecutor(Central(),accounts,None,lab_container='own-fixture')
    def command(*args):
        nonlocal live
        calls.append(args)
        if args[0]=='inspect':return str(accounts).encode()+b'\n'
        if args[0]=='cp':
            target=args[2].split(':',1)[1];files[target]=Path(args[1]).read_bytes()
            if target.endswith('/authorized_keys'):unsafe.append('cp alterou UID da chave efetiva')
            return b''
        if args[2]=='cat':return live
        if args[2]=='mv':live=files[args[-2]]
        return b''
    executor.container_command=command
    payload=DeliveryIn(activation_id=uuid.uuid4(),resume_secret='x'*43)
    assert executor.activate(payload)['state']=='awaiting_protection'
    assert live.splitlines()[0]==reader.encode()
    assert executor.activate(payload)['state']=='awaiting_protection'
    assert len([c for c in calls if c[0]=='cp'])==1
    assert not unsafe
    mutation=[c[2] for c in calls if c[0]=='exec' and c[2] in ('chown','chmod','mv')]
    assert mutation==['chown','chmod','mv']


def test_restic_diagnostic_uses_categories_without_stderr_secret(tmp_path):
    import os,sys
    from macip_backup_api.protector import _run
    script=tmp_path/'failing.py'
    script.write_text("import sys;sys.stderr.write('SYNTHETIC_SECRET Permission denied (publickey)');sys.exit(1)")
    fd=os.open(tmp_path/'owned.lock',os.O_CREAT|os.O_RDWR,0o600)
    try:
        with pytest.raises(ProtectionError) as failed:
            _run([sys.executable,str(script)],{'PATH':os.environ['PATH']},operation='cat',lock_fd=fd)
        assert failed.value.transport_error=='ssh_denied' and failed.value.returncode==1
        assert 'SYNTHETIC_SECRET' not in str(failed.value)
    finally:os.close(fd)


def test_timeout_reports_interruption_without_success_or_secret(tmp_path):
    worker,operation=setup_worker(tmp_path);reported=[]
    class API:
        def request(self,path,body=None):
            if path.endswith('/claim'):return {'operation':operation}
            reported.append(body);return {'accepted':True}
    worker.api=API()
    def timeout(op,journal):raise ProtectionError('SYNTHETIC_SECRET',code='timeout')
    worker.recover=timeout;worker.tick()
    assert reported[0]['state']=='failed' and reported[0]['error_code']=='interrupted'
    assert 'result' not in reported[0] and 'SYNTHETIC_SECRET' not in json.dumps(reported)
