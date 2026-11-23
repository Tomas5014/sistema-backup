"""Capacidade e retenção manual via executores existentes; HTTP nunca executa Restic."""
from datetime import UTC, datetime, timedelta
import os
import uuid
from typing import Literal

from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text, func, or_, and_, bindparam
from sqlalchemy.orm import Session

from .auth import require_viewer, csrf_guard
from .db import session
from .models import InstallationPreparation, ProvisioningOperation
from .protection_routes import current_protection, source_fingerprint, require_protector
from .storage import Policy, Digest, RetentionPlan, capacity_public, digest


def gate():
    if os.environ.get('MACIP_API_LAB_STORAGE') != '1':
        raise HTTPException(404,'capacidade e retenção de laboratório desabilitadas')


def settings(db, aid):
    row=db.execute(text('SELECT * FROM lab_storage_settings WHERE agent_id=:id'),dict(id=aid)).mappings().first()
    return dict(row) if row else dict(policy={},capacity_a=None,capacity_b=None)


def assert_isolated(db,prep,binding):
    identities=(prep.repository_id,binding['repository_id'])
    ambiguous=db.scalar(select(InstallationPreparation.agent_id).where(InstallationPreparation.agent_id!=prep.agent_id,
        or_(InstallationPreparation.repository_id.in_(identities),
            InstallationPreparation.protected_copy['repository_id'].as_string().in_(identities))).limit(1))
    if ambiguous:
        raise HTTPException(409,'identidade de repositório compartilhada ou ambígua; operação recusada')


def context(db, prep):
    if prep.lifecycle != "active":
        raise HTTPException(409,"instalação em atendimento ou encerrada; manutenção suspensa")
    binding=current_protection(prep)
    if prep.state!='primary_ready' or not prep.profile or not binding or binding.get('invalid'):
        raise HTTPException(409,'identidades A/B não verificadas; preserve os pontos e conclua a preparação')
    assert_isolated(db,prep,binding)
    return dict(installation_id=str(prep.agent_id),account=prep.account_name,source=source_fingerprint(prep),
        repository_a=prep.repository_id,repository_b=binding['repository_id'],protection=binding,
        seed=prep.lab_seed,policy=Policy.model_validate(settings(db,prep.agent_id)['policy']).model_dump(),
        pairs={r[0]:r[1] for r in db.execute(text('SELECT snapshot_a,snapshot_b FROM protected_points '
            'WHERE agent_id=:id AND binding_id=:binding'),dict(id=prep.agent_id,binding=binding['binding_id']))})


def preparation(db, aid, lock=False):
    query=select(InstallationPreparation).where(InstallationPreparation.agent_id==aid)
    if lock:query=query.with_for_update()
    row=db.scalar(query)
    if not row:raise HTTPException(404,'instalação inexistente')
    return row


def audit(db, aid, actor, kind, detail):
    from sqlalchemy.dialects.postgresql import JSONB
    db.execute(text('INSERT INTO lab_storage_audit(id,agent_id,actor,kind,detail) VALUES(:id,:agent,:actor,:kind,:detail)')
        .bindparams(bindparam('detail',type_=JSONB)),
        dict(id=uuid.uuid4(),agent=aid,actor=actor,kind=kind,detail=detail))


class Strict(BaseModel):
    model_config=ConfigDict(extra='forbid')


class RequestIn(Strict):
    id: uuid.UUID
    side: Literal['a','b']
    action: Literal['simulate','execute']
    simulation: uuid.UUID | None = None


class Claim(Strict):
    executor_id: str=Field(pattern=r'^[a-zA-Z0-9._-]{1,80}$')


class Point(Strict):
    id: Digest
    time: datetime


class Measurement(Strict):
    total: int=Field(strict=True,ge=0)
    used: int=Field(strict=True,ge=0)
    free: int=Field(strict=True,ge=0)
    volume: Digest
    scope: Literal['shared-volume']
    repository_bytes: int | None=Field(default=None,strict=True,ge=0)


class CapacityIn(Strict):
    source: Digest
    repository_id: Digest
    measurement: Measurement | None=None
    error: Literal['unavailable','identity_changed','busy'] | None=None


class Result(Strict):
    claim_id: uuid.UUID
    state: Literal['succeeded','failed']
    error: Literal['unavailable','identity_changed','busy','inventory_changed','reference_stale','interrupted'] | None=None
    plan: RetentionPlan | None=None
    removed: list[Digest]=Field(default_factory=list,max_length=1000)


def public_op(op):
    return dict(id=str(op.id),side=op.kind[-1],action=op.work['action'],state=op.state,
        requester=op.requester_ref,created_at=op.created_at.isoformat(),
        finished_at=op.finished_at.isoformat() if op.finished_at else None,
        error=op.error_code,result=(op.work or {}).get('result'))


def register_storage(app, identity, primary):
    read=[Depends(gate),Depends(require_viewer)]
    write=[Depends(gate),Depends(csrf_guard)]
    prefix='/api/v1/installations/{aid}/storage'

    @app.get(prefix,dependencies=read)
    def summary(aid: uuid.UUID,db: Session=Depends(session)):
        prep=preparation(db,aid)
        values=settings(db,aid);policy=Policy.model_validate(values['policy']).model_dump()
        from .lab_automation_routes import health
        stale={r['role']:r['stale'] for r in health(db)}
        capacities={side:capacity_public(values['capacity_'+side],threshold=policy['low_'+side],
                    worker_stale=stale['primary' if side=='a' else 'protection']) for side in ('a','b')}
        for side in ('a','b'):
            binding=current_protection(prep)
            expected=prep.repository_id if side=='a' else (binding or {}).get('repository_id')
            good=capacities[side]['last_good']
            if good and (good.get('source')!=(source_fingerprint(prep) if prep.profile else None) or good.get('repository_id')!=expected):
                capacities[side].update(stale=True,low=False)
        operations=db.scalars(select(ProvisioningOperation).where(ProvisioningOperation.agent_id==aid,
            ProvisioningOperation.kind.in_(('storage_a','storage_b'))).order_by(ProvisioningOperation.created_at.desc()).limit(20))
        events=db.execute(text('SELECT actor,kind,detail,created_at FROM lab_storage_audit WHERE agent_id=:id '
                              'ORDER BY created_at DESC LIMIT 20'),dict(id=aid)).mappings().all()
        return dict(policy=policy,automatic=False,capacity=capacities,operations=[public_op(o) for o in operations],audit=[dict(r) for r in events])

    @app.put(prefix+'/policy',dependencies=write)
    def policy(aid: uuid.UUID,payload: Policy,who=Depends(identity),db: Session=Depends(session)):
        preparation(db,aid,True)
        if db.scalar(select(ProvisioningOperation.id).where(ProvisioningOperation.agent_id==aid,ProvisioningOperation.state.in_(('pending','running')))):
            raise HTTPException(409,'operação em andamento; política preservada')
        from sqlalchemy.dialects.postgresql import JSONB
        values=payload.model_dump()
        db.execute(text('INSERT INTO lab_storage_settings(agent_id,policy) VALUES(:id,:policy) '
            'ON CONFLICT(agent_id) DO UPDATE SET policy=excluded.policy').bindparams(
                bindparam('policy',type_=JSONB)),dict(id=aid,policy=values))
        audit(db,aid,who.ref,'policy',values);db.commit()
        return values

    @app.post(prefix+'/operations',status_code=202,dependencies=write)
    def request(aid: uuid.UUID,payload: RequestIn,who=Depends(identity),db: Session=Depends(session)):
        prep=preparation(db,aid,True);ctx=context(db,prep)
        old=db.get(ProvisioningOperation,payload.id)
        request=payload.model_dump(mode='json',exclude={'id'})
        if old:
            if old.agent_id!=aid or old.kind!='storage_'+payload.side or (old.work or {}).get('request')!=request:
                raise HTTPException(409,'identificador de solicitação já usado')
            return public_op(old)
        if db.scalar(select(ProvisioningOperation.id).where(ProvisioningOperation.agent_id==aid,
                       ProvisioningOperation.state.in_(('pending','running')))):
            raise HTTPException(409,'operação da instalação em andamento; aguarde sem forçar locks')
        previous=None
        if payload.action=='execute':
            previous=db.get(ProvisioningOperation,payload.simulation)
            if (not previous or previous.agent_id!=aid or previous.kind!='storage_'+payload.side
                or previous.state!='succeeded' or previous.work['action']!='simulate'
                or previous.work['context']!=ctx or not previous.finished_at
                or datetime.now(UTC)-previous.finished_at>timedelta(minutes=10)):
                raise HTTPException(409,'simulação ausente, antiga ou identidade/política alterada; simule novamente')
        work=dict(action=payload.action,source=ctx['source'],context=ctx,request=request,
                  expected=(previous.work['result']['plan'] if previous else None))
        op=ProvisioningOperation(id=payload.id,client_id=prep.client_id,agent_id=aid,kind='storage_'+payload.side,
            state='pending',step='queued',attempt=1,requester_kind=who.kind,requester_ref=who.ref,work=work)
        db.add(op);audit(db,aid,who.ref,payload.action,dict(operation=str(op.id),**request));db.commit()
        return public_op(op)

    for side,auth,role in (('a',primary,'provisioner'),('b',require_protector,'protector')):
        register_executor(app,side,auth,role)


def register_executor(app,side,auth,role):
    deps=[Depends(gate),Depends(auth)]
    prefix='/api/v1/'+role+'/storage'

    @app.get(prefix+'/installations',dependencies=deps)
    def installations(db: Session=Depends(session)):
        result=[]
        for prep in db.scalars(select(InstallationPreparation).where(InstallationPreparation.state=='primary_ready')):
            try:result.append(context(db,prep))
            except HTTPException:continue
        return result

    @app.get(prefix+'/installations/{aid}',dependencies=deps)
    def current(aid: uuid.UUID,db: Session=Depends(session)):
        return context(db,preparation(db,aid))

    @app.post(prefix+'/installations/{aid}/capacity',dependencies=deps)
    def capacity(aid: uuid.UUID,payload: CapacityIn,db: Session=Depends(session)):
        ctx=context(db,preparation(db,aid,True))
        if payload.source!=ctx['source'] or payload.repository_id!=ctx['repository_'+side]:
            raise HTTPException(409,'identidade da medição mudou')
        if (payload.error is None)==(payload.measurement is None):
            raise HTTPException(422,'medição ou erro obrigatório')
        if payload.measurement and (payload.measurement.free>payload.measurement.total or payload.measurement.used>payload.measurement.total):
            raise HTTPException(422,'medição incoerente')
        old=settings(db,aid)['capacity_'+side] or {}
        now=datetime.now(UTC).isoformat()
        value=dict(last_good=old.get('last_good'),last_attempt=now,error=payload.error)
        if payload.measurement:
            value['last_good']=dict(**payload.measurement.model_dump(),measured_at=now,
                                 source=ctx['source'],repository_id=payload.repository_id)
        from sqlalchemy import bindparam
        from sqlalchemy.dialects.postgresql import JSONB
        db.execute(text('INSERT INTO lab_storage_settings(agent_id,capacity_'+side+') VALUES(:id,:value) '
             'ON CONFLICT(agent_id) DO UPDATE SET capacity_'+side+'=excluded.capacity_'+side)
             .bindparams(bindparam('value',type_=JSONB)),dict(id=aid,value=value));db.commit()
        return {'accepted':True}

    @app.post(prefix+'/claim',dependencies=deps)
    def claim(payload: Claim,db: Session=Depends(session)):
        now=db.scalar(select(func.clock_timestamp()))
        op=db.scalar(select(ProvisioningOperation).where(ProvisioningOperation.kind=='storage_'+side,
            or_(ProvisioningOperation.state=='pending',and_(ProvisioningOperation.state=='running',
                ProvisioningOperation.claim_expires_at<=now))).order_by(ProvisioningOperation.created_at)
            .with_for_update(skip_locked=True).limit(1))
        if not op:return {'operation':None}
        op.state='running';op.claim_id=uuid.uuid4();op.claimed_by=payload.executor_id
        op.claim_expires_at=now+timedelta(minutes=30);op.started_at=op.started_at or now
        db.commit()
        return {'operation':dict(id=str(op.id),installation_id=str(op.agent_id),claim_id=str(op.claim_id),side=side,work=op.work)}

    @app.post(prefix+'/operations/{oid}/result',dependencies=deps)
    def result(oid: uuid.UUID,payload: Result,db: Session=Depends(session)):
        op=db.scalar(select(ProvisioningOperation).where(ProvisioningOperation.id==oid).with_for_update())
        if not op or op.kind!='storage_'+side:raise HTTPException(404,'operação deste executor inexistente')
        body=payload.model_dump(mode='json');fingerprint=digest(body)
        if op.state in ('succeeded','failed'):
            if op.payload_hash!=fingerprint:raise HTTPException(409,'resultado terminal divergente')
            return {'accepted':True}
        if op.claim_id!=payload.claim_id or op.claim_expires_at<=datetime.now(UTC):
            raise HTTPException(409,'reserva substituída ou vencida')
        if (payload.state=='failed')!=bool(payload.error):raise HTTPException(422,'erro incoerente')
        if payload.plan:
            plan=body['plan']
            if (plan.get('repository_id')!=op.work['context']['repository_'+side]
                or plan.get('source')!=op.work['source'] or plan.get('side')!=side
                or digest({k:v for k,v in plan.items() if k!='fingerprint'})!=plan.get('fingerprint')):
                raise HTTPException(422,'plano de retenção incompatível')
        if payload.state=='succeeded' and not payload.plan:raise HTTPException(422,'plano obrigatório')
        allowed={p['id'] for p in (op.work.get('expected') or {}).get('points',[]) if p['action']=='remove'}
        if not set(payload.removed).issubset(allowed):raise HTTPException(422,'remoção não autorizada')
        if payload.state=='succeeded' and op.work['action']=='execute' and set(payload.removed)!=allowed:
            raise HTTPException(422,'remoções ainda não confirmadas')
        if op.work['action']=='execute' and body.get('plan') and body['plan']!=op.work['expected']:
            raise HTTPException(422,'plano aprovado divergente')
        op.work={**op.work,'result':body};op.state=payload.state;op.error_code=payload.error
        op.payload_hash=fingerprint;op.finished_at=datetime.now(UTC);op.step='finished'
        audit(db,op.agent_id,op.claimed_by,'result',dict(operation=str(op.id),**body));db.commit()
        return {'accepted':True}
