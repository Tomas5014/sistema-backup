"""PostgreSQL e SMTP reais; relógio e emissor B simulados explicitamente."""
from datetime import UTC, datetime
import json
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from urllib.request import urlopen
import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session
from conftest import ADMIN_TOKEN, requires_db, make_operator, login, csrf_headers
from macip_backup_api.central_alerts import evaluate, deliver
from macip_backup_api.db import engine
from macip_backup_api.auth import token_hash
from macip_backup_api.models import AgentCredential, InstallationPreparation
from test_activation import ready_installation, fake_proof, enabled_activation, issue, body_for, redeem

pytestmark = requires_db
ADMIN = {"Authorization": f"Bearer {ADMIN_TOKEN}"}

@pytest.fixture(autouse=True)
def enabled(monkeypatch):
    monkeypatch.setenv('MACIP_API_LAB_ALERTS','1')

def installation(client):
    aid = ready_installation(client)
    token = uuid.uuid4().hex + uuid.uuid4().hex
    with Session(engine()) as db:
        db.add(AgentCredential(id=uuid.uuid4(),agent_id=uuid.UUID(aid),token_hash=token_hash(token),scope='windows-results'))
        prep = db.get(InstallationPreparation,uuid.UUID(aid))
        repo = uuid.uuid4().hex * 2
        prep.repository_id = repo
        db.commit()
    return aid, {'Authorization':f'Bearer {token}'}, repo

def post(client,path,body,auth):
    return client.post('https://testserver/api/v1/windows-results/'+path,json=body,headers=auth)

def settings():
    return dict(schedule_id=str(uuid.uuid4()),schedule=dict(kind='daily',timezone='America/Sao_Paulo',hour=10,minute=0,tolerance_seconds=60,copy_seconds=60),paused=False)

def result(repo,**updates):
    return {**dict(execution_id=str(uuid.uuid4()),trigger='manual',started_at='2026-01-02T13:00:01Z',finished_at='2026-01-02T13:00:03Z',status='success',result_code='ok',repository_id=repo,snapshot_id='c'*64),**updates}

def anchor(aid):
    with engine().begin() as db:
        db.execute(text("UPDATE windows_monitors SET effective_at='2026-01-02T12:00:00Z' WHERE agent_id=:id"),dict(id=aid))
        db.execute(text("UPDATE windows_results SET received_at='2026-01-02T13:00:10Z' WHERE agent_id=:id"),dict(id=aid))

def tick(now='2026-01-02T13:02:00+00:00'):
    with Session(engine()) as db:
        evaluate(db,datetime.fromisoformat(now))

def view(client,aid):
    response=client.get(f'/api/v1/installations/{aid}/alerts',headers=ADMIN)
    assert response.status_code==200,response.text
    return response.json()

def test_scope_https_isolation_and_deduplication(client):
    a,auth,repo=installation(client); b,other,repo_b=installation(client)
    body=result(repo)
    assert client.post('/api/v1/windows-results/executions',json=body,headers=auth).status_code==400
    with ThreadPoolExecutor(2) as pool:
        responses=list(pool.map(lambda _:post(client,'executions',body,auth),range(2)))
    assert [r.status_code for r in responses]==[200,200]
    assert post(client,'executions',{**body,'status':'failed','result_code':'restic_failed'},auth).status_code==409
    assert post(client,'executions',body,other).status_code==409
    error=post(client,'executions',{**body,'password':'synthetic-secret'},auth)
    assert error.status_code==422 and 'synthetic-secret' not in error.text
    assert client.get('/api/v1/agent/tasks/desired',headers=auth).status_code==403
    assert client.post('/api/v1/agent/executions',json={},headers=auth).status_code==403
    assert client.get(f'/api/v1/installations/{a}/alerts',headers=auth).status_code==403
    assert view(client,b)['last_backup'] is None
    assert view(client,a)['last_backup']['execution_id']==body['execution_id']
    history=client.get('/api/v1/executions',params={'agent_id':a},headers=ADMIN).json()
    assert len(history)==1 and history[0]['execution_id']==body['execution_id']
    assert auth['Authorization'] not in json.dumps(view(client,a))
    with engine().connect() as db:
        assert db.scalar(text('SELECT count(*) FROM windows_results'))==1

def test_manual_old_event_late_result_pause_and_resume(client):
    aid,auth,repo=installation(client); schedule=settings()
    assert post(client,'schedule',schedule,auth).status_code==200
    assert post(client,'schedule',schedule,auth).status_code==200
    assert post(client,'schedule',{**schedule,'paused':True},auth).status_code==409
    assert post(client,'executions',result(repo),auth).status_code==200
    old=result(repo,schedule_id=schedule['schedule_id'],trigger='scheduled',occurrence='2026-01-01T13:00:00Z',started_at='2026-01-01T13:00:01Z',finished_at='2026-01-01T13:00:02Z')
    assert post(client,'executions',old,auth).status_code==200
    anchor(aid); tick(); tick()
    assert len([a for a in view(client,aid)['alerts'] if a['kind']=='backup_missing'])==1
    correct=result(repo,schedule_id=schedule['schedule_id'],trigger='scheduled',occurrence='2026-01-02T13:00:00Z')
    assert post(client,'executions',correct,auth).status_code==200
    tick(); assert next(a for a in view(client,aid)['alerts'] if a['kind']=='backup_missing')['resolved_at']
    paused={**schedule,'previous_schedule_id':schedule['schedule_id'],'schedule_id':str(uuid.uuid4()),'paused':True}
    assert post(client,'schedule',paused,auth).status_code==200
    assert post(client,'schedule',schedule,auth).status_code==409
    tick('2026-01-03T15:00:00+00:00')
    assert len([a for a in view(client,aid)['alerts'] if a['kind']=='backup_missing'])==1
    assert view(client,aid)['paused']
    resumed={**paused,'previous_schedule_id':paused['schedule_id'],'schedule_id':str(uuid.uuid4()),'paused':False}
    assert post(client,'schedule',resumed,auth).status_code==200
    assert not view(client,aid)['paused']

def test_partial_failure_and_out_of_order_preserve_history(client):
    aid,auth,repo=installation(client)
    assert post(client,'executions',result(repo,status='partial',result_code='incomplete',snapshot_id=None),auth).status_code==200
    tick(); assert view(client,aid)['alerts'][0]['detail']['status']=='partial'
    good=result(repo,started_at='2026-01-02T14:00:00Z',finished_at='2026-01-02T14:01:00Z')
    assert post(client,'executions',good,auth).status_code==200
    tick(); assert view(client,aid)['alerts'][0]['resolved_at']
    assert post(client,'executions',result(repo,status='failed',result_code='validation_failed',snapshot_id=None,started_at='2026-01-01T14:00:00Z',finished_at='2026-01-01T14:01:00Z'),auth).status_code==200
    tick(); data=view(client,aid)
    assert len(data['alerts'])==2 and all(a['resolved_at'] for a in data['alerts'])
    assert data['last_backup']['execution_id']==good['execution_id']

def test_copy_requires_exact_point_and_current_binding(client):
    aid,auth,repo=installation(client); binding=str(uuid.uuid4())
    from macip_backup_api.protection_routes import source_fingerprint
    with Session(engine()) as db:
        row=db.get(InstallationPreparation,uuid.UUID(aid))
        row.protected_copy=dict(source=source_fingerprint(row),binding_id=binding,proof={'snapshot_a':'a'*64},last_copy=dict(snapshot_a='a'*64,snapshot_b='b'*64))
        db.commit()
    assert post(client,'schedule',settings(),auth).status_code==200
    assert post(client,'executions',result(repo),auth).status_code==200
    anchor(aid); tick()
    assert 'copy_late' in [a['kind'] for a in view(client,aid)['alerts']]
    assert view(client,aid)['last_protected_copy'] is None
    # Emissor B simulado; autenticação real de B coberta em test_protected_copy.
    with engine().begin() as db:
        db.execute(text('INSERT INTO protected_points(agent_id,binding_id,snapshot_a,snapshot_b) VALUES(:id,:binding,:a,:b)'),dict(id=aid,binding=binding,a='c'*64,b='d'*64))
    tick()
    assert next(a for a in view(client,aid)['alerts'] if a['kind']=='copy_late')['resolved_at']
    assert view(client,aid)['last_protected_copy']['snapshot_b']=='d'*64
    with Session(engine()) as db:
        row=db.get(InstallationPreparation,uuid.UUID(aid)); row.protected_copy={**row.protected_copy,'binding_id':str(uuid.uuid4())}; db.commit()
    tick()
    assert view(client,aid)['last_protected_copy'] is None
    assert len([a for a in view(client,aid)['alerts'] if a['kind']=='copy_late'])==2

def test_enrollment_binding_revocation_and_lab_gate(client,enabled_activation,monkeypatch):
    aid=ready_installation(client); fake_proof(aid); code=issue(client,aid); body=body_for(code)
    assert redeem(client,body).status_code==200
    data=dict(activation_id=body['activation_id'],resume_secret=body['resume_secret'])
    path='https://testserver/api/v1/activations/monitoring'
    assert client.post(path,json=data).status_code==403
    with engine().begin() as db:
        db.execute(text("UPDATE activation_codes SET state='delivered' WHERE activation_id=:id"),dict(id=body['activation_id']))
    first=client.post(path,json=data)
    assert first.status_code==200,first.text
    assert client.post(path,json=data).json()==first.json()
    assert first.headers['cache-control']=='no-store'
    assert client.post(path,json={**data,'resume_secret':'x'*43}).status_code==403
    with engine().begin() as db:
        db.execute(text("UPDATE agent_credentials SET revoked_at=now() WHERE scope='windows-results'"))
    assert client.post(path,json=data).status_code==403
    monkeypatch.delenv('MACIP_API_LAB_ALERTS')
    assert client.get(f'/api/v1/installations/{aid}/alerts',headers=ADMIN).status_code==404

def test_ack_permissions_csrf_and_mailpit_once(client,monkeypatch):
    aid,auth,repo=installation(client)
    from macip_backup_api.protection_routes import source_fingerprint
    with Session(engine()) as db:
        prep=db.get(InstallationPreparation,uuid.UUID(aid))
        prep.protected_copy=dict(source=source_fingerprint(prep),binding_id=str(uuid.uuid4()),proof={'simulated': True})
        db.commit()
    assert post(client,'schedule',settings(),auth).status_code==200
    assert post(client,'executions',result(repo,status='partial',result_code='incomplete'),auth).status_code==200
    anchor(aid); tick()
    assert {a['kind'] for a in view(client,aid)['alerts']} == {'backup_failed','backup_missing','copy_late'}
    alert_id=next(a['id'] for a in view(client,aid)['alerts'] if a['kind']=='backup_failed')
    monkeypatch.setenv('MACIP_API_SESSION_INSECURE_COOKIE','1')
    op=make_operator(client,ADMIN,login='alerts-viewer',role='viewer'); assert login(client,op['login'],op['password']).status_code==200
    assert client.post(f'/api/v1/alerts/{alert_id}/ack',headers=csrf_headers(client)).status_code==403
    client.cookies.clear()
    op=make_operator(client,ADMIN,login='alerts-admin'); assert login(client,op['login'],op['password']).status_code==200
    assert client.post(f'/api/v1/alerts/{alert_id}/ack').status_code==403
    assert client.post(f'/api/v1/alerts/{alert_id}/ack',headers=csrf_headers(client)).status_code==200
    assert client.post(f'/api/v1/alerts/{alert_id}/ack',headers=csrf_headers(client)).status_code==200
    assert next(a for a in view(client,aid)['alerts'] if a['id']==alert_id)['acknowledged_at']
    port=os.environ.get('MACIP_TEST_SMTP_PORT'); web=os.environ.get('MACIP_TEST_MAILPIT_URL')
    if not port or not web: pytest.skip('Mailpit descartável não configurado')
    def count():
        with urlopen(web+'/api/v1/messages') as response: return json.load(response)['total']
    before=count()
    with Session(engine()) as db: deliver(db,int(port)); deliver(db,int(port))
    assert count()==before+3
    assert all(a['delivery_state']=='sent' for a in view(client,aid)['alerts'])

def test_dst_unknown_timezone_and_future_event(client):
    from macip_backup_api.monitoring import next_occurrence
    assert next_occurrence(dict(kind='daily',timezone='America/New_York',hour=2,minute=30),datetime(2026,3,8,5,tzinfo=UTC))==datetime(2026,3,9,6,30,tzinfo=UTC)
    aid,auth,repo=installation(client)
    assert post(client,'executions',result(repo,finished_at='2099-01-01T00:00:00Z'),auth).status_code==422
    assert post(client,'schedule',{**settings(),'schedule':{**settings()['schedule'],'timezone':'bad-zone'}},auth).status_code==422


def test_smtp_offline_retry_preserves_occurrence(client):
    import socket
    aid,auth,repo=installation(client)
    assert post(client,'executions',result(repo,status='failed',result_code='restic_failed',snapshot_id=None),auth).status_code==200
    tick()
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0))
        port=sock.getsockname()[1]
        # Porta reservada sem listen: recusa real, nenhum destinatário.
        with Session(engine()) as db:
            deliver(db,port)
    assert view(client,aid)['alerts'][0]['delivery_state']=='pending'
    tick()
    assert len(view(client,aid)['alerts'])==1


def test_public_evaluator_helper(client,tmp_path):
    """Entrada pública documentada, configuração sintética privada e sem tokens de A/B."""
    import subprocess
    from pathlib import Path
    from conftest import TEST_URL
    aid,auth,repo=installation(client)
    assert post(client,'executions',result(repo,status='failed',result_code='restic_failed',snapshot_id=None),auth).status_code==200
    root=tmp_path/'laboratorio com espacos'
    root.mkdir(mode=0o700)
    (root/'private').mkdir(mode=0o700)
    (root/'private/config.json').write_text(json.dumps({'database':TEST_URL}))
    (root/'private/config.json').chmod(0o600)
    script=Path(__file__).resolve().parents[2]/'tools/windows-lab-server.py'
    import socket,sys
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0))
        out=subprocess.run([sys.executable,str(script),'alerts',str(root),'--smtp-port',str(sock.getsockname()[1])],capture_output=True,timeout=30)
    assert out.returncode==0,out.stderr.decode()
    assert 'backup_failed' in [a['kind'] for a in view(client,aid)['alerts']]
    assert auth['Authorization'].encode() not in out.stdout+out.stderr


def test_copy_late_without_current_B_binding(client):
    aid,auth,repo=installation(client)
    assert post(client,'schedule',settings(),auth).status_code==200
    assert post(client,'executions',result(repo),auth).status_code==200
    anchor(aid); tick()
    assert next(a for a in view(client,aid)['alerts'] if a['kind']=='copy_late')['detail']['binding_id'].startswith('unbound:')
