"""Fila real PostgreSQL; resultados dos executores simulados nos testes unitários."""
import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
import secrets
import uuid

import pytest
from sqlalchemy import text, select
from sqlalchemy.orm import Session

from conftest import requires_db, ADMIN_TOKEN
from macip_backup_api.db import engine
from macip_backup_api.models import InstallationPreparation, ProvisioningOperation
from macip_backup_api.lab_automation_routes import enqueue
from test_provisioning import prepare,request_body,claim,success_for,finish,PROVISIONER
from test_protected_copy import enabled_protection,AUTH,InProcessProtector

pytestmark=requires_db
ADMIN={'Authorization':f'Bearer {ADMIN_TOKEN}'}

@pytest.fixture
def automation(monkeypatch,enabled_protection):
    monkeypatch.setenv('MACIP_API_LAB_AUTOMATION','1')


def reader(client,installation):
    key='ssh-ed25519 '+base64.b64encode(b'\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20'+secrets.token_bytes(32)).decode()
    response=client.post(f'/api/v1/protector/automation/installations/{installation}/reader',json={'public_key':key},headers=AUTH)
    assert response.status_code==200,response.text
    return key


def initial(client):
    prep=prepare(client,request_body());aid=prep['installation']['id']
    reader(client,aid);operation=claim(client)
    result=success_for(operation)
    result['result']['lab_seed']={'repository_id':result['result']['repository_id'],'snapshot_a':'b'*64,'manifest_sha256':'c'*64}
    assert finish(client,operation,result).status_code==200
    return aid


def bclaim(client):
    response=client.post('/api/v1/protector/operations/claim',json={'executor_id':'lab-b'},headers=AUTH)
    assert response.status_code==200,response.text
    return response.json()['operation']


def bfinish(client,operation,**updates):
    body={'claim_id':operation['claim_id'],'state':'failed','error_code':'protection_failed',**updates}
    return client.post(f'/api/v1/protector/operations/{operation["id"]}/result',json=body,headers=AUTH)


def test_auto_chain_scopes_no_fake_success_and_reader_identity(client,automation):
    prep=prepare(client,request_body());aid=prep['installation']['id']
    assert claim(client) is None  # A espera somente a chave pública de leitura de B
    key=reader(client,aid)
    assert client.post(f'/api/v1/protector/automation/installations/{aid}/reader',json={'public_key':key},headers=AUTH).status_code==200
    other=key[:-2]+'AA'
    assert client.post(f'/api/v1/protector/automation/installations/{aid}/reader',json={'public_key':other},headers=PROVISIONER).status_code==403
    op=claim(client); assert op['reader_public_key']==key
    payload=success_for(op)
    assert finish(client,op,payload).status_code==422
    payload['result']['lab_seed']={'repository_id':'a'*64,'snapshot_a':'b'*64,'manifest_sha256':'c'*64}
    assert finish(client,op,payload).status_code==200
    # A jamais recebe trabalhos de B na fila existente.
    assert claim(client) is None
    operation=bclaim(client)
    assert operation['kind']=='protect_installation' and operation['installation_id']==aid
    assert client.post('/api/v1/protector/operations/claim',json={'executor_id':'a'},headers=PROVISIONER).status_code==403
    assert bfinish(client,operation,state='succeeded',error_code=None).status_code==409
    assert client.get(f'/api/v1/installations/{aid}/profile',headers=ADMIN).status_code==409
    assert bfinish(client,operation).status_code==200
    assert bfinish(client,operation).status_code==200
    public=client.get(f'/api/v1/installations/{aid}/preparation',headers=ADMIN).json()
    assert public['automation']['last_error']['error_code']=='protection_failed'
    assert len([w for w in public['automation']['workers'] if w['stale']])==3


def test_claim_expiry_fences_old_result_and_does_not_block_other_installation(client,automation):
    a=initial(client);b=initial(client)
    first=bclaim(client)
    second=bclaim(client)
    assert {first['installation_id'],second['installation_id']}=={a,b}
    assert bfinish(client,second).status_code==200
    with engine().begin() as db:
        db.execute(text("UPDATE provisioning_operations SET claim_expires_at=now()-interval '1 second' WHERE id=:id"),{'id':first['id']})
    resumed=bclaim(client)
    assert resumed['id']==first['id'] and resumed['claim_id']!=first['claim_id']
    assert client.post(f'/api/v1/protector/operations/{resumed["id"]}/release',json={'claim_id':first['claim_id']},headers=AUTH).status_code==409
    assert bfinish(client,first).status_code==409
    assert bfinish(client,resumed).status_code==200


def test_atomic_enqueue_many_points_and_stale_health(client,automation):
    aid=initial(client)
    operation=bclaim(client)
    assert bfinish(client,operation).status_code==200
    # Uma falha permanece no histórico; retomada cria nova tentativa com backoff.
    with engine().begin() as db:
        db.execute(text("UPDATE provisioning_operations SET finished_at=now()-interval '1 hour' WHERE id=:id"),{'id':operation['id']})
    again=bclaim(client)
    assert again['id']!=operation['id']
    assert bfinish(client,again,state='succeeded',error_code=None).status_code==409
    pulse=client.post('/api/v1/protector/automation/heartbeat',json={'state':'busy'},headers=AUTH)
    assert pulse.status_code==200
    health=client.get('/api/v1/automation/health',headers=ADMIN).json()
    assert not next(w for w in health if w['role']=='protection')['stale']
    with engine().begin() as db:
        db.execute(text("UPDATE lab_worker_heartbeats SET last_seen_at=now()-interval '2 minutes'"))
    assert all(w['stale'] for w in client.get('/api/v1/automation/health',headers=ADMIN).json())


def test_new_source_supersedes_old_claim(client,automation):
    aid=initial(client);operation=bclaim(client)
    with Session(engine()) as db:
        row=db.get(InstallationPreparation,uuid.UUID(aid))
        row.public_key_fingerprint='SHA256:changed'
        enqueue(db,row);db.commit()
    assert bfinish(client,operation).status_code==409
    next_op=bclaim(client)
    assert next_op['id']!=operation['id']


def fake_proof(client, aid):
    api=InProcessProtector(client);prefix=f'/api/v1/protector/installations/{aid}'
    context=api.request(prefix)
    binding=api.request(prefix+'/binding',dict(previous_binding=context['binding_id'],source=context['source'],volume_id=str(uuid.uuid4()),destination='d'*64,repository_id='e'*64))
    pair=dict(binding_id=binding['binding_id'],snapshot_a='b'*64,snapshot_b='f'*64)
    api.request(prefix+'/copy',pair)
    api.request(prefix+'/proof',{**pair,'method':'restic-offline-sha256-v1','files':1,'manifest_sha256':'c'*64,'restored_sha256':'c'*64})
    return binding


def test_all_pending_points_in_same_queue_idempotent_and_last_success_preserved(client,automation,monkeypatch):
    from macip_backup_api.auth import token_hash
    from macip_backup_api.models import AgentCredential
    monkeypatch.setenv('MACIP_API_LAB_ALERTS','1')
    aid=initial(client);initial_op=bclaim(client)
    binding=fake_proof(client,aid)
    assert bfinish(client,initial_op,state='succeeded',error_code=None).status_code==200
    token=secrets.token_urlsafe(32)
    with Session(engine()) as db:
        db.add(AgentCredential(id=uuid.uuid4(),agent_id=uuid.UUID(aid),token_hash=token_hash(token),scope='windows-results'));db.commit()
    auth={'Authorization':'Bearer '+token}
    for n in range(3):
        body=dict(execution_id=str(uuid.uuid4()),trigger='manual',started_at='2026-01-01T01:00:00Z',finished_at='2026-01-01T01:01:00Z',status='success',result_code='ok',repository_id='a'*64,snapshot_id=str(n)*64)
        for _ in range(2):
            assert client.post('https://testserver/api/v1/windows-results/executions',json=body,headers=auth).status_code==200
    with ThreadPoolExecutor(2) as pool:claims=list(pool.map(lambda _:bclaim(client),range(2)))
    assert len([c for c in claims if c])==1
    first=next(c for c in claims if c)
    assert first['work']['snapshots']==['0'*64]
    api=InProcessProtector(client)
    api.request(f'/api/v1/protector/installations/{aid}/copy',dict(binding_id=binding['binding_id'],snapshot_a='0'*64,snapshot_b='1'*64))
    assert bfinish(client,first,state='succeeded',error_code=None).status_code==200
    assert bfinish(client,first,state='succeeded',error_code=None).status_code==200
    next_op=bclaim(client)
    assert next_op['work']['snapshots']==['1'*64,'2'*64]
    previous=client.get(f'/api/v1/installations/{aid}/preparation',headers=ADMIN).json()['last_protected_copy']
    assert bfinish(client,next_op).status_code==200
    assert client.get(f'/api/v1/installations/{aid}/preparation',headers=ADMIN).json()['last_protected_copy']==previous
    assert client.post(f'/api/v1/provisioner/operations/{next_op["id"]}/release',json={'claim_id':next_op['claim_id']},headers=PROVISIONER).status_code==404


def test_legacy_primary_terminal_hash_survives_additive_seed_field(client,monkeypatch):
    import hashlib,json
    from macip_backup_api.provisioning_routes import ResultIn
    monkeypatch.delenv('MACIP_API_LAB_AUTOMATION',raising=False)
    prepared=prepare(client,request_body());operation=claim(client);payload=success_for(operation)
    assert finish(client,operation,payload).status_code==200
    legacy=ResultIn(**payload).model_dump(mode='json')
    legacy['result'].pop('lab_seed',None)
    digest=hashlib.sha256(json.dumps(legacy,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    with engine().begin() as db:db.execute(text('UPDATE provisioning_operations SET payload_hash=:hash WHERE id=:id'),dict(hash=digest,id=operation['id']))
    assert finish(client,operation,payload).status_code==200
