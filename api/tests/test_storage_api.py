"""PostgreSQL e autenticação reais; inventários e resultados do executor simulados."""
from datetime import UTC, datetime
import uuid

import pytest
from sqlalchemy import text

from conftest import ADMIN_TOKEN, requires_db, make_operator, login, csrf_headers
from macip_backup_api.db import engine
from macip_backup_api.storage import plan
from test_provisioning import prepare,request_body,claim,finish,success_for,PROVISIONER
from test_protected_copy import enabled_protection,InProcessProtector,AUTH

pytestmark=requires_db
ADMIN={'Authorization':f'Bearer {ADMIN_TOKEN}'}


@pytest.fixture
def storage(client,monkeypatch,enabled_protection):
    monkeypatch.setenv('MACIP_API_LAB_STORAGE','1')
    first=prepare(client,request_body());op=claim(client)
    assert finish(client,op,success_for(op)).status_code==200
    aid=first['installation']['id'];api=InProcessProtector(client)
    ctx=api.request('/api/v1/protector/installations/'+aid)
    api.request('/api/v1/protector/installations/'+aid+'/binding',dict(previous_binding=None,
        source=ctx['source'],volume_id=str(uuid.uuid4()),destination='d'*64,repository_id='b'*64))
    return aid


def root(aid):return f'/api/v1/installations/{aid}/storage'


def request(client,aid,action='simulate',side='a',**kw):
    return client.post(root(aid)+'/operations',headers=ADMIN,json=dict(id=str(uuid.uuid4()),action=action,side=side,**kw))


def take(client,role='provisioner',auth=PROVISIONER):
    r=client.post(f'/api/v1/{role}/storage/claim',headers=auth,json={'executor_id':'unit-lab'})
    assert r.status_code==200,r.text
    return r.json()['operation']


def finish_storage(client,op,body):
    return client.post('/api/v1/provisioner/storage/operations/'+op['id']+'/result',headers=PROVISIONER,
                       json=dict(claim_id=op['claim_id'],**body))


def test_policy_absent_invalid_values_and_disabled_gate(client,storage,monkeypatch):
    path=root(storage)
    data=client.get(path,headers=ADMIN).json()
    assert data['policy']['keep_a'] is None and data['policy']['keep_b'] is None
    assert data['automatic'] is False
    for bad in (True,0,-1,1.5,'7',10001):
        assert client.put(path+'/policy',headers=ADMIN,json={'keep_a':bad}).status_code==422
    assert client.put(path+'/policy',headers=ADMIN,json={'low_a':0}).status_code==422
    assert client.put(path+'/policy',headers=ADMIN,json={'pins_a':['../outro']}).status_code==422
    assert client.put(path+'/policy',headers=ADMIN,json={'automatic':True}).status_code==422
    assert client.put(path+'/policy',headers=ADMIN,json={'keep_a':None,'keep_b':3}).status_code==200
    monkeypatch.delenv('MACIP_API_LAB_STORAGE')
    assert client.get(path,headers=ADMIN).status_code==404
    assert request(client,storage).status_code==404


def test_queue_scopes_duplicate_expired_claim_and_late_result(client,storage):
    r=request(client,storage);assert r.status_code==202,r.text
    assert request(client,storage,side='b').status_code==409
    assert take(client,'protector',AUTH) is None
    first=take(client);assert first['id']==r.json()['id']
    assert client.post('/api/v1/provisioner/storage/claim',headers=AUTH,json={'executor_id':'other'}).status_code==403
    assert client.put(root(storage)+'/policy',headers=ADMIN,json={'keep_a':2}).status_code==409
    body=dict(state='succeeded',error=None,plan=plan(first['work']['context'],'a',[],[]),removed=[])
    with engine().begin() as db:db.execute(text("UPDATE provisioning_operations SET claim_expires_at=now()-interval '1 second' WHERE id=:id"),dict(id=first['id']))
    second=take(client);assert second['claim_id']!=first['claim_id']
    assert finish_storage(client,first,body).status_code==409
    assert finish_storage(client,second,body).status_code==200
    assert finish_storage(client,second,body).status_code==200
    data=client.get(root(storage),headers=ADMIN).json()
    assert data['operations'][0]['result']['removed']==[]
    assert len([a for a in data['audit'] if a['kind']=='result'])==1
    assert request(client,storage,'execute',simulation=first['id']).status_code==202


def test_execute_requires_current_simulation_policy_and_identity(client,storage):
    assert request(client,storage,'execute',simulation=str(uuid.uuid4())).status_code==409
    assert request(client,storage).status_code==202
    op=take(client)
    body=dict(state='succeeded',error=None,plan=plan(op['work']['context'],'a',[],[]),removed=[])
    assert finish_storage(client,op,body).status_code==200
    assert request(client,storage,'execute',side='b',simulation=op['id']).status_code==409
    assert client.put(root(storage)+'/policy',headers=ADMIN,json={'keep_a':1}).status_code==200
    assert request(client,storage,'execute',simulation=op['id']).status_code==409
    assert request(client,storage).status_code==202
    op2=take(client)
    body['plan']=plan(op2['work']['context'],'a',[],[])
    assert finish_storage(client,op2,body).status_code==200
    with engine().begin() as db:db.execute(text("UPDATE provisioning_operations SET finished_at=now()-interval '11 minutes' WHERE id=:id"),dict(id=op2['id']))
    assert request(client,storage,'execute',simulation=op2['id']).status_code==409


def test_capacity_failure_preserves_previous_and_stale_health(client,storage):
    context=client.get('/api/v1/provisioner/storage/installations/'+storage,headers=PROVISIONER).json()
    path='/api/v1/provisioner/storage/installations/'+storage+'/capacity'
    payload=dict(source=context['source'],repository_id=context['repository_a'],measurement=dict(
        total=1000,used=990,free=10,volume='f'*64,scope='shared-volume',repository_bytes=5))
    assert client.put(root(storage)+'/policy',headers=ADMIN,json={'low_a':20}).status_code==200
    assert client.post(path,headers=PROVISIONER,json=payload).status_code==200
    # Até com medição recente, heartbeat inexistente não pode ser anunciado atual.
    data=client.get(root(storage),headers=ADMIN).json()['capacity']['a'];assert data['stale']
    with engine().begin() as db:db.execute(text("INSERT INTO lab_worker_heartbeats(role,state) VALUES('primary','ready')"))
    data=client.get(root(storage),headers=ADMIN).json()['capacity']['a'];assert data['low'] and not data['stale']
    assert client.post(path,headers=PROVISIONER,json=dict(source=context['source'],repository_id=context['repository_a'],error='unavailable')).status_code==200
    after=client.get(root(storage),headers=ADMIN).json()['capacity']['a']
    assert after['stale'] and not after['low'] and after['last_good']==data['last_good']
    assert client.post(path,headers=PROVISIONER,json={**payload,'repository_id':'0'*64}).status_code==409
    assert 'RESTIC_PASSWORD' not in str(client.get(root(storage),headers=ADMIN).json())


def test_permissions_viewer_read_only_admin_csrf_and_audit(client,storage,monkeypatch):
    monkeypatch.setenv('MACIP_API_SESSION_INSECURE_COOKIE','1')
    for role in ('viewer','admin'):
        client.cookies.clear()
        op=make_operator(client,ADMIN,login='storage-'+role,role=role)
        assert login(client,op['login'],op['password']).status_code==200
        assert client.get(root(storage)).status_code==200
        body=dict(id=str(uuid.uuid4()),side='a',action='simulate')
        assert client.post(root(storage)+'/operations',json=body).status_code==403
        r=client.post(root(storage)+'/operations',json=body,headers=csrf_headers(client))
        assert r.status_code==(403 if role=='viewer' else 202)
        assert client.put(root(storage)+'/policy',json={'keep_a':1},headers=csrf_headers(client)).status_code==(403 if role=='viewer' else 409)
    data=client.get(root(storage)).json()
    assert data['audit'][0]['actor']=='storage-admin'


def test_migration_13_to_14_preserves_rows_and_repeats(client,monkeypatch):
    from macip_backup_api import migrations
    name='retention_test_'+uuid.uuid4().hex
    versions=[migration for migration in migrations.MIGRATIONS if migration[0]<=14]
    with engine().begin() as db:
        db.execute(text('CREATE SCHEMA '+name))
        db.execute(text('SET LOCAL search_path TO '+name))
        monkeypatch.setattr(migrations,'MIGRATIONS',versions[:-1])
        assert migrations.run_migrations(db)==list(range(1,14))
        cid=uuid.uuid4()
        db.execute(text('INSERT INTO clients(id,name) VALUES(:id,:name)'),dict(id=cid,name='Fictício migração'))
        before=list(db.execute(text('SELECT id,name,created_at FROM clients')))
        monkeypatch.setattr(migrations,'MIGRATIONS',versions)
        assert migrations.run_migrations(db)==[14]
        assert migrations.run_migrations(db)==[]
        assert list(db.execute(text('SELECT id,name,created_at FROM clients')))==before
        db.execute(text('SET LOCAL search_path TO public'))
        db.execute(text('DROP SCHEMA '+name+' CASCADE'))


def test_ambiguous_repository_shared_with_other_installation_refuses(client,storage):
    # A fixture genérica devolve o mesmo repository_id sintético; um clone
    # assim nunca deve autorizar retenção por uma instalação arbitrária.
    second=prepare(client,request_body());op=claim(client)
    assert second['installation']['id']!=storage
    assert finish(client,op,success_for(op)).status_code==200
    response=request(client,storage)
    assert response.status_code==409 and 'ambígua' in response.json()['detail']
    assert client.get('/api/v1/provisioner/storage/installations',headers=PROVISIONER).json()==[]
