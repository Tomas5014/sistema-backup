"""Decisões puras e provas Restic locais, sem Docker nem sistema operacional Windows."""
from contextlib import nullcontext
from datetime import UTC,datetime,timedelta
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace
import uuid

import pytest
from pydantic import ValidationError
from macip_backup_api.storage import Policy,plan,capacity_public
from macip_backup_api.storage_runtime import LabStorage,exclusive
from macip_backup_api.lab_runtime import json_once


def point(n):return dict(id=f'{n:064x}',time=f'2026-11-{n:02d}T12:00:00Z')


def context():
    return dict(installation_id=str(uuid.uuid4()),account='macip'+'a'*20,source='f'*64,
        repository_a='a'*64,repository_b='b'*64,protection=dict(binding_id=str(uuid.uuid4()),proof=None),
        policy=Policy().model_dump(),pairs={},seed=None)


@pytest.mark.parametrize('value',[True,False,0,-1,1.5,'7',10001])
def test_invalid_counts(value):
    for field in ('keep_a','keep_b'):
        with pytest.raises(ValidationError):Policy.model_validate({field:value})


def test_no_policy_pending_proof_pins_and_last_point():
    ctx=context();points=[point(n) for n in (1,2,3)]
    assert all(p['reason']=='no_policy' for p in plan(ctx,'a',points,[])['points'])
    ctx['policy']['keep_a']=1
    assert {p['reason'] for p in plan(ctx,'a',points,[])['points']}=={'keep_last','copy_unproven'}
    ctx['protection']['proof']=dict(snapshot_a=point(1)['id'],snapshot_b=point(4)['id'])
    ctx['pairs']={point(2)['id']:point(5)['id']}
    rows=plan(ctx,'a',points,[point(5)['id']])['points']
    assert [r['id'] for r in rows if r['action']=='remove']==[point(2)['id']]
    ctx['policy']['pins_a']=[point(2)['id']]
    assert all(r['action']=='keep' for r in plan(ctx,'a',points,[point(5)['id']])['points'])
    ctx['policy']['pins_a']=[point(9)['id']]
    with pytest.raises(ValueError):plan(ctx,'a',points,[])


def test_b_preserves_copy_of_published_source_and_proof():
    ctx=context();ctx['policy']['keep_b']=1
    ctx['pairs']={point(7)['id']:point(1)['id']}
    assert plan(ctx,'b',[point(1),point(2)],[point(7)['id']])['points'][1]['reason']=='source_present'


def test_capacity_stale_failed_measurement_and_low_space():
    now=datetime.now(UTC)
    value=dict(last_good=dict(measured_at=now.isoformat(),free=5),error=None,last_attempt=now.isoformat())
    assert capacity_public(value,now=now,threshold=10)['low']
    assert not capacity_public(value,now=now,threshold=10)['stale']
    for kwargs in (dict(now=now+timedelta(seconds=121)),dict(now=now,worker_stale=True)):
        result=capacity_public(value,threshold=10,**kwargs);assert result['stale'] and not result['low']
    value['error']='unavailable'
    assert capacity_public(value,now=now)['last_good']==value['last_good']
    assert capacity_public(value,now=now)['stale']


def test_shared_lock_recuses_second_operation_with_spaces(tmp_path):
    path=tmp_path/'área com espaços.lock'
    with exclusive(path):
        with pytest.raises(RuntimeError,match='busy'):
            with exclusive(path):pass
    with exclusive(path):pass


class LocalStorage(LabStorage):
    """Somente o transporte/cerca de SSH são simulados; todos os comandos Restic são reais."""
    def __init__(self,root,side,repository,password,ctx,peer):
        actor=SimpleNamespace(root=root,api=SimpleNamespace(request=lambda path:ctx))
        super().__init__(actor,side)
        self.repo=repository;self.password=password;self.ctx=ctx;self.peer=peer;self.fail=False;self.commands=[]
    def guarded(self,ctx):return exclusive(self.state/'local.lock')
    def fence(self,container,ctx=None,operation=None):return nullcontext()
    def receipt(self,ctx):return self.peer
    def save_receipt(self,ctx,points,valid=True):pass
    def backend(self,ctx,fd):
        def run(*args):
            self.commands.append(args)
            if self.fail and args[0]=='forget':
                self.fail=False
                self.real('forget',args[1]);raise RuntimeError('falha sintética após uma remoção')
            return self.real(*args)
        return run,lambda:None,None
    def real(self,*args):
        result=subprocess.run(['restic','--no-cache','-r',str(self.repo),*args],
            env={**os.environ,'RESTIC_PASSWORD':self.password},capture_output=True,timeout=25)
        if result.returncode:raise RuntimeError('restic falhou: '+str(result.returncode)+' '+result.stderr.decode())
        return result.stdout


def operation(ctx,action,expected=None):
    return dict(id=str(uuid.uuid4()),work=dict(context=ctx,action=action,expected=expected))


@pytest.mark.skipif(not shutil.which('restic'),reason='Restic real ausente')
def test_real_restic_partial_retry_historical_restore_and_two_installations(tmp_path):
    root=tmp_path/'área isolada';root.mkdir(mode=0o700);(root/'private').mkdir(mode=0o700)
    ctx=context();a=root/'a';b=root/'b';other=root/'outro';src=root/'origem com acentos á'
    src.mkdir();payloads={}
    runner=LocalStorage(root,'a',a,'senha A fictícia',ctx,[])
    runner.real('init')
    ctx['repository_a']=json.loads(runner.real('cat','config'))['id']
    for n in range(4):
        content=os.urandom(1024+n)
        (src/'arquivo.bin').write_bytes(content)
        output=runner.real('backup','--json',str(src))
        snap=json.loads(output.decode().splitlines()[-1])['snapshot_id'];payloads[snap]=content
    other_run=LocalStorage(root,'a',other,'outra senha',ctx,[]);other_run.real('init');other_run.real('backup',str(src))
    other_before=other_run.real('--no-lock','snapshots','--json')
    rb=LocalStorage(root,'b',b,'senha B diferente',ctx,[]);rb.real('init')
    ctx['repository_b']=json.loads(rb.real('cat','config'))['id']
    for snap in payloads:
        result=subprocess.run(['restic','--no-cache','-r',str(b),'copy','--from-repo',str(a),snap],
            env={**os.environ,'RESTIC_PASSWORD':rb.password,'RESTIC_FROM_PASSWORD':runner.password},capture_output=True,timeout=25)
        assert result.returncode==0
    pairs={p.get('original',p['id']):p['id'] for p in json.loads(rb.real('--no-lock','snapshots','--json'))}
    ctx['pairs']=pairs;ctx['policy'].update(keep_a=1,keep_b=1)
    oldest=list(payloads)[0]
    ctx['protection']['proof']=dict(snapshot_a=oldest,snapshot_b=pairs[oldest])
    runner.peer=list(pairs.values());rb.peer=list(payloads)
    before=runner.real('--no-lock','snapshots','--json')
    sim=runner.perform(operation(ctx,'simulate'));assert sim['state']=='succeeded',sim
    assert before==runner.real('--no-lock','snapshots','--json')
    assert not any('unlock' in cmd or 'forget' in cmd for cmd in runner.commands)
    execute=operation(ctx,'execute',sim['plan']);runner.fail=True
    failed=runner.perform(execute)
    assert failed['state']=='failed' and len(failed['removed'])==1,failed
    retry=runner.perform(execute);assert retry['state']=='succeeded' and len(retry['removed'])==2,retry
    assert other_run.real('--no-lock','snapshots','--json')==other_before
    rb.peer=[p['id'] for p in json.loads(runner.real('--no-lock','snapshots','--json'))]
    bsim=rb.perform(operation(ctx,'simulate'));assert bsim['state']=='succeeded',bsim
    assert rb.perform(operation(ctx,'execute',bsim['plan']))['state']=='succeeded'
    for r,snap in ((runner,oldest),(rb,pairs[oldest])):
        r.real('check','--read-data')
        target=root/('restore-'+r.side)
        r.real('restore',snap,'--target',str(target),'--verify')
        actual=(target/str(src).lstrip('/')/'arquivo.bin').read_bytes()
        assert hashlib.sha256(actual).digest()==hashlib.sha256(payloads[oldest]).digest()
    # Snapshot novo após simulação: nenhuma exclusão pela lista antiga.
    newsim=runner.perform(operation(ctx,'simulate'))
    (src/'arquivo.bin').write_bytes(b'novo ponto ficticio');runner.real('backup',str(src))
    previous=runner.real('--no-lock','snapshots','--json')
    rejected=runner.perform(operation(ctx,'execute',newsim['plan']))
    assert rejected['error']=='inventory_changed'
    assert runner.real('--no-lock','snapshots','--json')==previous


def test_ssh_zombie_is_exited_but_stopped_or_live_session_is_not():
    from macip_backup_api.storage_runtime import ssh_processes
    def row(pid,state):return str(pid)+' (sshd) '+state+' '+' '.join(['0']*18)+' 123'
    parsed=ssh_processes('\n'.join(row(i,s) for i,s in enumerate(('S','Z','T','t','R'),1)))
    assert [p['pid'] for p in parsed if p['state'] not in ('Z','X')]==['1','3','4','5']
    assert all(p['starttime']=='123' for p in parsed)
    with pytest.raises(RuntimeError):ssh_processes('não é /proc/stat')


def test_abandoned_a_intent_blocks_a_maintenance_and_b_before_effects(tmp_path):
    from macip_backup_api.protector import LabProtector,ProtectionError
    root=tmp_path/'área interrompida';root.mkdir(mode=0o700)
    for name in ('private','b-state'):(root/name).mkdir(mode=0o700)
    ctx=context();aid=uuid.UUID(ctx['installation_id'])
    marker=root/'b-state'/f'{aid}.maintenance-a.json'
    marker.write_text('{"synthetic":"interrupted"}');marker.chmod(0o600)
    a=LabStorage(SimpleNamespace(root=root,api=None),'a')
    with pytest.raises(RuntimeError,match='interrupted'):
        with a.guarded(ctx):raise AssertionError('não deve executar')
    b=LabProtector(tmp_path,tmp_path/'volume ausente',uuid.uuid4(),root/'b-state',reserve_bytes=1)
    with pytest.raises(ProtectionError,match='manutenção A interrompida'):
        b.execute(None,aid,'prepare')
    assert marker.exists() and not (tmp_path/'volume ausente').exists()


@pytest.mark.skipif(not shutil.which('restic'),reason='Restic real ausente')
def test_real_native_lock_preserves_points_during_backup(tmp_path):
    import time
    root=tmp_path/'lab';root.mkdir(mode=0o700);(root/'private').mkdir(mode=0o700)
    ctx=context();r=LocalStorage(root,'a',root/'repository','senha sintética',ctx,[])
    r.real('init');ctx['repository_a']=json.loads(r.real('cat','config'))['id']
    for i in range(2):
        file=root/'arquivo.txt';file.write_text(str(i));r.real('backup',str(file))
    points=json.loads(r.real('--no-lock','snapshots','--json'))
    ctx['policy']['keep_a']=1;ctx['protection']['proof']={'synthetic':True}
    ctx['pairs']={p['id']:p['id'] for p in points};r.peer=list(ctx['pairs'])
    sim=r.perform(operation(ctx,'simulate'));assert sim['state']=='succeeded'
    before=r.real('--no-lock','snapshots','--json')
    process=subprocess.Popen(['restic','--no-cache','-r',str(r.repo),'backup','--stdin','--stdin-filename','dados.txt'],
        env={**os.environ,'RESTIC_PASSWORD':r.password},stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    try:
        deadline=time.monotonic()+10
        while not r.real('--no-lock','list','locks').strip():
            assert process.poll() is None and time.monotonic()<deadline
            time.sleep(.03)
        result=r.perform(operation(ctx,'execute',sim['plan']))
        assert result['state']=='failed' and not result['removed']
        assert r.real('--no-lock','snapshots','--json')==before
    finally:
        process.communicate(b'conteudo ficticio',timeout=10)
        assert process.returncode==0


def test_a_requires_b_mount_marker_and_config_attestation(tmp_path):
    import tempfile
    from macip_backup_api.storage_runtime import digest_path
    from macip_backup_api.lab_supervisor import atomic_json
    if not Path('/dev/shm').is_mount():pytest.skip('/dev/shm montado indisponível')
    root=tmp_path/'lab';root.mkdir(mode=0o700)
    (root/'private/services').mkdir(parents=True,mode=0o700)
    ctx=context()
    actor=SimpleNamespace(root=root,api=None)
    a=LabStorage(actor,'a')
    with tempfile.TemporaryDirectory(prefix='macip-retention-test-',dir='/dev/shm') as temp:
        volume=Path(temp);vid=str(uuid.uuid4());repo=volume/ctx['installation_id'];repo.mkdir(mode=0o700)
        marker=volume/'.macip-volume';marker.write_text(vid);marker.chmod(0o600)
        config=repo/'config';config.write_bytes(b'config sintetica cifrada');config.chmod(0o600)
        ctx['protection'].update(volume_id=vid,destination=digest_path(repo))
        atomic_json(root/'private/services/installation.json',dict(volume=str(volume),volume_id=vid))
        receipt=dict(at=datetime.now(UTC).isoformat(),valid=True,source=ctx['source'],protection=ctx['protection']['binding_id'],
             repository_id=ctx['repository_b'],points=[point(1)],config_sha256=hashlib.sha256(config.read_bytes()).hexdigest())
        atomic_json(a.receipt_path(ctx,'b'),receipt)
        assert a.receipt(ctx)==[point(1)['id']]
        marker.unlink()
        with pytest.raises((OSError,RuntimeError)):a.receipt(ctx)
        marker.write_text(vid);marker.chmod(0o600)
        config.write_bytes(b'identidade substituida')
        with pytest.raises(RuntimeError,match='reference_stale'):a.receipt(ctx)


def test_successful_command_without_confirmed_deletion_is_failure(tmp_path):
    root=tmp_path/'lab';root.mkdir(mode=0o700);(root/'private').mkdir(mode=0o700)
    ctx=context();ctx['policy']['keep_a']=1;ctx['protection']['proof']={'synthetic':True}
    pts=[point(1),point(2)];ctx['pairs']={point(1)['id']:point(3)['id']}
    r=LocalStorage(root,'a',root/'unused','sintética',ctx,[point(3)['id']])
    r.inventory=lambda c,run:pts
    r.backend=lambda c,fd:(lambda *args:b'',lambda:None,None)
    simulated=r.perform(operation(ctx,'simulate'))
    result=r.perform(operation(ctx,'execute',simulated['plan']))
    assert result['state']=='failed' and result['removed']==[]
