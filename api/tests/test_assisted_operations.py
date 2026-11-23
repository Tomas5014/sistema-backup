"""API/concorrência PostgreSQL reais; resultados de executor controlados."""
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from conftest import requires_db, ADMIN_TOKEN
from test_lab_automation import automation, initial, bclaim, bfinish, fake_proof
from test_protected_copy import enabled_protection, AUTH
from test_provisioning import PROVISIONER
from macip_backup_api.db import engine
from macip_backup_api.auth import token_hash
from macip_backup_api.models import InstallationPreparation, AgentCredential, ProvisioningOperation

pytestmark = requires_db
ADMIN = {'Authorization': 'Bearer ' + ADMIN_TOKEN}


@pytest.fixture
def ready(client, automation, monkeypatch):
    monkeypatch.setenv('MACIP_API_LAB_ASSISTED', '1')
    monkeypatch.setenv('MACIP_API_LAB_ACTIVATION', '1')
    monkeypatch.setenv('MACIP_API_LAB_ALERTS', '1')
    monkeypatch.setenv('MACIP_API_ACTIVATION_PUBLIC_URL', 'https://testserver')
    monkeypatch.setenv('MACIP_API_ACTIVATION_DELIVERY_URL', 'https://testserver/v1/activate')
    count = 0
    def create():
        nonlocal count
        aid = initial(client)
        op = bclaim(client); fake_proof(client, aid)
        assert bfinish(client, op, state='succeeded', error_code=None).status_code == 200
        if count:
            from macip_backup_api.protection_routes import source_fingerprint
            with Session(engine()) as db:
                row=db.get(InstallationPreparation,uuid.UUID(aid));rid=uuid.uuid4().hex*2
                row.repository_id=rid;row.profile={**row.profile,'repositorio_id':rid}
                row.lab_seed={**row.lab_seed,'repository_id':rid}
                row.protected_copy={**row.protected_copy,'repository_id':uuid.uuid4().hex*2,'source':source_fingerprint(row)}
                db.commit()
        count+=1
        return aid
    return create


def request(client, aid, action, epoch=1, **extra):
    body = dict(id=str(uuid.uuid4()), action=action, expected_epoch=epoch, confirmed=True, **extra)
    return client.post(f'/api/v1/installations/{aid}/assisted', json=body, headers=ADMIN)


def claim(client, side='provisioner'):
    response = client.post('/api/v1/' + side + '/assisted/claim', json={'executor_id': 'test-worker'},
                           headers=PROVISIONER if side=='provisioner' else AUTH)
    assert response.status_code == 200, response.text
    return response.json()['operation']


def result(client, op, side='provisioner', **updates):
    ctx = op['work']['context']
    body = dict(claim_id=op['claim_id'], state='succeeded', result=dict(
        repository_a=ctx['repository_a'], repository_b=ctx['repository_b'], revoked=True,
        inventory_sha256='1'*64), **updates)
    return client.post('/api/v1/'+side+'/assisted/operations/'+op['id']+'/result',json=body,
                       headers=PROVISIONER if side=='provisioner' else AUTH)


def status(client, aid):
    return client.get(f'/api/v1/installations/{aid}/assisted',headers=ADMIN).json()


def token(aid):
    raw = uuid.uuid4().hex + uuid.uuid4().hex
    with Session(engine()) as db:
        epoch = db.get(InstallationPreparation, uuid.UUID(aid)).device_epoch
        db.add(AgentCredential(id=uuid.uuid4(), agent_id=uuid.UUID(aid), token_hash=token_hash(raw),
                              scope='windows-results', device_epoch=epoch)); db.commit()
    return {'Authorization': 'Bearer '+raw}


def event(snapshot='b'*64):
    now = datetime.now(UTC).isoformat()
    return dict(execution_id=str(uuid.uuid4()), trigger='manual', started_at=now, finished_at=now,
                status='success', result_code='ok', repository_id='a'*64, snapshot_id=snapshot)


def test_replacement_is_idempotent_revokes_access_and_preserves_points(client, ready):
    aid = ready(); old = token(aid)
    initial_event = event()
    assert client.post('https://testserver/api/v1/windows-results/executions',json=initial_event,headers=old).status_code==200
    code=client.post('/api/v1/activation-codes',json={'installation_id':aid},headers=ADMIN).json()
    before=client.get(f'/api/v1/installations/{aid}/preparation',headers=ADMIN).json()
    response = request(client, aid, 'replace'); assert response.status_code==202,response.text
    opid=response.json()['id']
    assert status(client,aid)['access_state']=='requested'
    assert request(client,aid,'replace').json()['id']==opid
    assert client.post('https://testserver/api/v1/windows-results/executions',json=event(),headers=old).status_code==403
    assert client.post('/api/v1/activation-codes',json={'installation_id':aid},headers=ADMIN).status_code==409
    op=claim(client)
    assert result(client,op).status_code==200
    assert result(client,op).status_code==200
    assert status(client,aid)['access_state']=='confirmed'
    assert status(client,aid)['lifecycle']=='awaiting_device'
    after=client.get(f'/api/v1/installations/{aid}/preparation',headers=ADMIN).json()
    assert after['repository_id']==before['repository_id']
    assert after['last_protected_copy']==before['last_protected_copy']
    assert not after['profile_available'] and after['activation_available']
    with engine().connect() as db:
        assert db.scalar(text('SELECT count(*) FROM windows_results WHERE agent_id=:id'),dict(id=aid))==1
        assert db.scalar(text('SELECT revoked_at IS NOT NULL FROM activation_codes WHERE id=:id'),dict(id=code['id']))
        assert db.scalar(text('SELECT count(*) FROM protected_points WHERE agent_id=:id'),dict(id=aid))==1
    assert client.post('/api/v1/activation-codes',json={'installation_id':aid},headers=ADMIN).status_code==201


def test_old_results_do_not_replace_new_device_or_schedule(client,ready):
    aid=ready();old=token(aid)
    assert client.post('https://testserver/api/v1/windows-results/executions',json=event(),headers=old).status_code==200
    request(client,aid,'replace');op=claim(client);assert result(client,op).status_code==200
    # Ativação/prova são exercitadas pelo contrato existente e no ensaio real.
    with Session(engine()) as db:
        prep=db.get(InstallationPreparation,uuid.UUID(aid));prep.lifecycle='active';db.commit()
    new=token(aid);new_event=event()
    assert client.post('https://testserver/api/v1/windows-results/executions',json=new_event,headers=new).status_code==200
    assert client.post('https://testserver/api/v1/windows-results/executions',json=event(),headers=old).status_code==403
    overview=client.get(f'/api/v1/installations/{aid}/alerts',headers=ADMIN).json()
    assert overview['last_backup']['execution_id']==new_event['execution_id']
    with engine().connect() as db:
        assert db.scalars(text('SELECT DISTINCT device_epoch FROM windows_results WHERE agent_id=:id ORDER BY device_epoch'),dict(id=aid)).all()==[1,2]
    schedule=dict(previous_schedule_id=None,schedule_id=str(uuid.uuid4()),paused=True,
        schedule=dict(kind='daily',hour=11,minute=0,timezone='America/Sao_Paulo',tolerance_seconds=600,copy_seconds=3600))
    assert client.post('https://testserver/api/v1/windows-results/schedule',json=schedule,headers=new).status_code==200
    assert client.post('https://testserver/api/v1/windows-results/schedule',json=schedule,headers=old).status_code==403


def test_failure_retry_lease_and_two_clients_isolated(client,ready):
    a=ready(); b=ready();b_token=token(b)
    assert request(client,a,'close').status_code==202
    op=claim(client)
    body=dict(claim_id=op['claim_id'],state='failed',error_code='unavailable')
    assert client.post('/api/v1/provisioner/assisted/operations/'+op['id']+'/result',json=body,headers=PROVISIONER).status_code==200
    assert status(client,a)['access_state']=='failed' and status(client,a)['lifecycle']=='closing'
    with Session(engine()) as db:b_repo=db.get(InstallationPreparation,uuid.UUID(b)).repository_id
    assert client.post('https://testserver/api/v1/windows-results/executions',json={**event(),'repository_id':b_repo},headers=b_token).status_code==200
    retry=client.post(f'/api/v1/installations/{a}/assisted/operations/{op["id"]}/retry',headers=ADMIN)
    assert retry.status_code==200
    again=claim(client);assert again['id']==op['id'] and again['claim_id']!=op['claim_id']
    assert result(client,op).status_code==409
    assert result(client,again).status_code==200
    assert status(client,a)['lifecycle']=='closed' and status(client,b)['lifecycle']=='active'
    assert request(client,a,'reopen',epoch=2).status_code==202


def test_concurrent_clicks_and_foreign_result_refused(client,ready):
    aid=ready()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:request(client,aid,'replace'),range(2)))
    assert {r.status_code for r in results}=={202}
    assert len({r.json()['id'] for r in results})==1
    op=claim(client)
    assert client.post('/api/v1/protector/assisted/operations/'+op['id']+'/result',headers=AUTH,
                       json=dict(claim_id=op['claim_id'],state='failed',error_code='unavailable')).status_code==404


def test_explicit_confirmation_viewer_and_csrf(client,ready):
    from conftest import make_operator, login, csrf_headers
    from fastapi.testclient import TestClient
    aid=ready()
    payload=dict(id=str(uuid.uuid4()),action='close',expected_epoch=1,confirmed=False)
    assert client.post(f'/api/v1/installations/{aid}/assisted',json=payload,headers=ADMIN).status_code==422
    payload['confirmed']=True
    make_operator(client,ADMIN,role='viewer')
    with TestClient(client.app,base_url='https://testserver') as portal:
        assert login(portal,'operador','senha-super-secreta-1').status_code==200
        assert portal.get(f'/api/v1/installations/{aid}/assisted').status_code==200
        assert portal.post(f'/api/v1/installations/{aid}/assisted',json=payload,headers=csrf_headers(portal)).status_code==403
    make_operator(client,ADMIN,login='administrador',role='admin')
    with TestClient(client.app,base_url='https://testserver') as portal:
        assert login(portal,'administrador','senha-super-secreta-1').status_code==200
        assert portal.post(f'/api/v1/installations/{aid}/assisted',json=payload).status_code==403
        assert portal.post(f'/api/v1/installations/{aid}/assisted',json=payload,headers=csrf_headers(portal)).status_code==202


def test_catalog_and_restore_bound_to_installation_snapshot_no_fake_success(client,ready):
    aid=ready();op_request=request(client,aid,'catalog');assert op_request.status_code==202
    op=claim(client,'protector');ctx=op['work']['context']
    evidence=dict(repository_a=ctx['repository_a'],repository_b=ctx['repository_b'],
                  points=[dict(snapshot_a='b'*64,snapshot_b='f'*64,time=datetime.now(UTC).isoformat())])
    body=dict(claim_id=op['claim_id'],state='succeeded',result=evidence)
    assert client.post('/api/v1/protector/assisted/operations/'+op['id']+'/result',json=body,headers=AUTH).status_code==200
    restored=request(client,aid,'restore',catalog_id=op['id'],snapshot_b='f'*64)
    assert restored.status_code==202,restored.text
    duplicate=request(client,aid,'restore',catalog_id=op['id'],snapshot_b='f'*64)
    assert duplicate.json()['id']==restored.json()['id']
    restore=claim(client,'protector')
    assert result(client,restore,'protector').status_code==409
    fail=dict(claim_id=restore['claim_id'],state='failed',error_code='interrupted')
    assert client.post('/api/v1/protector/assisted/operations/'+restore['id']+'/result',json=fail,headers=AUTH).status_code==200
    assert client.get(f'/api/v1/installations/{aid}/assisted/operations/{restore["id"]}/download',headers=ADMIN).status_code==409
    assert client.post(f'/api/v1/installations/{aid}/assisted/operations/{restore["id"]}/retry',headers=ADMIN).status_code==409


def test_snapshot_other_client_and_stale_catalog_are_refused(client,ready):
    a=ready();b=ready()
    first=request(client,a,'catalog');op=claim(client,'protector')
    # Outro par, não confirmado para A, nunca vira catálogo autorizado.
    evidence=dict(repository_a='a'*64,repository_b='e'*64,points=[dict(snapshot_a='2'*64,snapshot_b='3'*64,time=datetime.now(UTC).isoformat())])
    response=client.post('/api/v1/protector/assisted/operations/'+op['id']+'/result',json=dict(claim_id=op['claim_id'],state='succeeded',result=evidence),headers=AUTH)
    assert response.status_code==409
    fail=dict(claim_id=op['claim_id'],state='failed',error_code='reference_invalid')
    assert client.post('/api/v1/protector/assisted/operations/'+op['id']+'/result',json=fail,headers=AUTH).status_code==200
    assert request(client,b,'restore',catalog_id=first.json()['id'],snapshot_b='f'*64).status_code==409


def test_no_secret_leak_expired_and_revoked_download(client,ready,tmp_path,monkeypatch):
    from datetime import timedelta
    import hashlib
    import json
    aid=ready();req=request(client,aid,'catalog');op=claim(client,'protector')
    ctx=op['work']['context']
    body=dict(claim_id=op['claim_id'],state='succeeded',result=dict(repository_a=ctx['repository_a'],repository_b=ctx['repository_b'],points=[dict(snapshot_a='b'*64,snapshot_b='f'*64,time=datetime.now(UTC).isoformat())]))
    assert client.post('/api/v1/protector/assisted/operations/'+op['id']+'/result',json=body,headers=AUTH).status_code==200
    restore=request(client,aid,'restore',catalog_id=op['id'],snapshot_b='f'*64);work=claim(client,'protector')
    exports=tmp_path/'exports';exports.mkdir(mode=0o700);monkeypatch.setenv('MACIP_LAB_RECOVERY_EXPORT_ROOT',str(exports))
    archive=exports/(work['id']+'.zip');archive.write_bytes(b'synthetic zip bytes');archive.chmod(0o600)
    completed=dict(claim_id=work['claim_id'],state='succeeded',result=dict(repository_a=ctx['repository_a'],repository_b=ctx['repository_b'],snapshot_b='f'*64,
        files=2,archive_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),manifest_sha256='2'*64,expires_at=(datetime.now(UTC)+timedelta(hours=24)).isoformat()))
    assert client.post('/api/v1/protector/assisted/operations/'+work['id']+'/result',json=completed,headers=AUTH).status_code==200
    download=f'/api/v1/installations/{aid}/assisted/operations/{work["id"]}/download'
    assert client.get(download,headers=ADMIN).content==archive.read_bytes()
    from macip_backup_api import assisted_routes
    class Future(datetime):
        @classmethod
        def now(cls,tz=None):return datetime.now(tz)+timedelta(days=2)
    with monkeypatch.context() as clock:
        clock.setattr(assisted_routes,'datetime',Future)
        assert client.get(download,headers=ADMIN).status_code==409
    assert client.post(download.replace('/download','/revoke-export'),headers=ADMIN).status_code==200
    assert client.get(download,headers=ADMIN).status_code==409
    # Reenvio idempotente não apaga a revogação posterior do arquivo.
    assert client.post('/api/v1/protector/assisted/operations/'+work['id']+'/result',json=completed,headers=AUTH).status_code==200
    assert client.get(download,headers=ADMIN).status_code==409
    assert all(word not in json.dumps(status(client,aid)).lower() for word in ('restic_password','private_key','resume_secret','activation_code'))


def test_migration_14_to_15_preserves_identity_tokens_and_rows(client,monkeypatch):
    from macip_backup_api import migrations
    schema='assisted_migration_test_'+uuid.uuid4().hex
    versions=migrations.MIGRATIONS
    with engine().begin() as db:
        db.execute(text('CREATE SCHEMA '+schema));db.execute(text('SET LOCAL search_path TO '+schema))
        monkeypatch.setattr(migrations,'MIGRATIONS',[m for m in versions if m[0]<=14])
        assert migrations.run_migrations(db)==list(range(1,15))
        cid,aid,oid,device,credential=[uuid.uuid4() for _ in range(5)]
        db.execute(text('INSERT INTO clients(id,name) VALUES(:id,:name)'),dict(id=cid,name='Fictício preservado'))
        db.execute(text('INSERT INTO agents(id,client_id,name) VALUES(:id,:client,:name)'),dict(id=aid,client=cid,name='Computador anterior'))
        db.execute(text("INSERT INTO provisioning_operations(id,client_id,agent_id,kind,attempt,state,step,requester_kind,requester_ref) VALUES(:id,:client,:agent,'prepare_primary',1,'succeeded','verified','admin','teste')"),dict(id=oid,client=cid,agent=aid))
        db.execute(text("INSERT INTO installation_preparations(agent_id,client_id,current_operation_id,state,step,repository_id) VALUES(:agent,:client,:op,'primary_ready','verified',:repo)"),dict(agent=aid,client=cid,op=oid,repo='a'*64))
        db.execute(text('INSERT INTO agent_credentials(id,agent_id,token_hash,scope) VALUES(:id,:agent,:hash,:scope)'),dict(id=credential,agent=aid,hash='b'*64,scope='windows-results'))
        db.execute(text("INSERT INTO activation_codes(id,agent_id,code_hash,requester_kind,requester_ref,expires_at,issued_source,state,device_id,delivered_at) VALUES(:id,:agent,:hash,'admin','teste',now()+interval '1 hour',:source,'delivered',:device,now())"),dict(id=uuid.uuid4(),agent=aid,hash='c'*64,source='d'*64,device=device))
        before=db.execute(text('SELECT agent_id,client_id,current_operation_id,state,step,repository_id FROM installation_preparations')).one()
        monkeypatch.setattr(migrations,'MIGRATIONS',versions)
        assert migrations.run_migrations(db)==[15]
        assert migrations.run_migrations(db)==[]
        assert db.execute(text('SELECT agent_id,client_id,current_operation_id,state,step,repository_id FROM installation_preparations')).one()==before
        assert db.execute(text('SELECT lifecycle,device_epoch,current_device,access_state FROM installation_preparations')).one()==('active',1,device,'none')
        assert db.execute(text('SELECT token_hash,revoked_at,device_epoch FROM agent_credentials')).one()==('b'*64,None,1)
        monkeypatch.setattr(migrations,'MIGRATIONS',[m for m in versions if m[0]<=14])
        with pytest.raises(migrations.MigrationError):migrations.run_migrations(db)
        assert db.scalar(text('SELECT max(version) FROM schema_migrations'))==15
        db.execute(text('SET LOCAL search_path TO public'));db.execute(text('DROP SCHEMA '+schema+' CASCADE'))


def test_update_fingerprint_preserves_old_columns_during_expansion(client):
    from macip_backup_api.lab_updates import LaboratoryUpdate
    update=object.__new__(LaboratoryUpdate);update.engine=engine();update.validate_database=lambda:None
    table='assisted_update_test_'+uuid.uuid4().hex
    try:
        with engine().begin() as db:
            db.execute(text('CREATE TABLE '+table+' (id integer, value text)'))
            db.execute(text('INSERT INTO '+table+" VALUES(1,'preservar')"))
        before=update.fingerprint()
        with engine().begin() as db:db.execute(text('ALTER TABLE '+table+' ADD COLUMN generation integer DEFAULT 1'))
        assert update.fingerprint()[table]['sha256']!=before[table]['sha256']
        assert update.fingerprint(reference=before)[table]==before[table]
        with engine().begin() as db:db.execute(text('UPDATE '+table+" SET value='alterado'"))
        assert update.fingerprint(reference=before)[table]['sha256']!=before[table]['sha256']
    finally:
        with engine().begin() as db:db.execute(text('DROP TABLE '+table))


def test_shared_repository_identity_refuses_before_revocation(client,ready):
    a=ready();b=ready()
    with Session(engine()) as db:
        original=db.get(InstallationPreparation,uuid.UUID(a))
        other=db.get(InstallationPreparation,uuid.UUID(b))
        other.repository_id=original.repository_id;db.commit()
    response=request(client,a,'close')
    assert response.status_code==409
    assert status(client,a)['lifecycle']=='active' and status(client,a)['access_state']=='none'
