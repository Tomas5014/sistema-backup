"""Encadeamento de laboratório na fila de provisionamento já existente."""
from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
import uuid
from typing import Literal

from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text, func, or_, and_
from sqlalchemy.orm import Session

from .auth import require_viewer
from .db import session
from .models import Agent, InstallationPreparation, ProvisioningOperation
from .activation_routes import RedeemIn
from .protection_routes import require_protector, source_fingerprint, public_protection, current_protection

KINDS = ('protect_installation', 'copy_points')
LEASE = timedelta(minutes=30)


def enabled():
    return os.environ.get('MACIP_API_LAB_AUTOMATION') == '1'


def gate():
    if not enabled():
        raise HTTPException(404, 'automação de laboratório desabilitada')


def heartbeat(db, role, state):
    db.execute(text('INSERT INTO lab_worker_heartbeats(role,state) VALUES(:role,:state) '
                    'ON CONFLICT(role) DO UPDATE SET last_seen_at=now(),state=excluded.state'), dict(role=role, state=state))


def health(db):
    now = datetime.now(UTC)
    rows = {r['role']: r for r in db.execute(text('SELECT * FROM lab_worker_heartbeats')).mappings()}
    return [dict(role=role, last_seen_at=rows[role]['last_seen_at'].isoformat() if role in rows else None,
                 stale=role not in rows or now - rows[role]['last_seen_at'] > timedelta(seconds=60),
                 state=rows[role]['state'] if role in rows else 'unknown')
            for role in ('primary', 'protection', 'alerts')]


def summary(db, preparation):
    latest = db.scalar(select(ProvisioningOperation).where(ProvisioningOperation.agent_id == preparation.agent_id,
             ProvisioningOperation.kind.in_(KINDS)).order_by(ProvisioningOperation.created_at.desc()).limit(1))
    failed = db.scalar(select(ProvisioningOperation).where(ProvisioningOperation.agent_id == preparation.agent_id,
             ProvisioningOperation.kind.in_(KINDS), ProvisioningOperation.error_code.is_not(None))
             .order_by(ProvisioningOperation.finished_at.desc()).limit(1))
    def brief(row):
        return dict(id=str(row.id), kind=row.kind, state=row.state, step=row.step, error_code=row.error_code,
                    started_at=row.started_at.isoformat() if row.started_at else None,
                    finished_at=row.finished_at.isoformat() if row.finished_at else None) if row else None
    return {'workers': health(db), 'last_attempt': brief(latest), 'last_error': brief(failed)}


def enqueue(db, preparation, *, force=False):
    """Chamada dentro da transação dona do resultado/identidade. Não executa Restic."""
    if not enabled() or preparation.state != 'primary_ready' or not preparation.lab_seed or preparation.lifecycle not in ('active','awaiting_device'):
        return
    db.flush()
    db.refresh(preparation, with_for_update=True)
    source = source_fingerprint(preparation)
    active = db.scalar(select(ProvisioningOperation).where(ProvisioningOperation.agent_id == preparation.agent_id,
                       ProvisioningOperation.state.in_(('pending', 'running'))))
    if active:
        if active.kind not in KINDS or (active.work or {}).get('source') == source:
            return
        active.state, active.error_code, active.step = 'failed', 'identity_changed', 'superseded'
        active.finished_at = datetime.now(UTC)
        db.flush()
    binding = current_protection(preparation)
    if not public_protection(preparation)[2]:
        kind, snapshots = 'protect_installation', [preparation.lab_seed['snapshot_a']]
        material = [source, (binding or {}).get('binding_id')]
    else:
        copied = {r[0] for r in db.execute(text('SELECT snapshot_a FROM protected_points WHERE agent_id=:id AND binding_id=:binding'),
                  dict(id=preparation.agent_id, binding=binding['binding_id']))}
        reconciled=db.execute(text("SELECT detail->'snapshots' FROM lab_storage_audit WHERE agent_id=:id AND kind='reconciled_absent' AND detail->>'repository_id'=:repo ORDER BY created_at DESC LIMIT 1"),dict(id=preparation.agent_id,repo=preparation.repository_id)).first()
        absent=set(reconciled[0]) if reconciled else set()
        snapshots = sorted({r[0] for r in db.execute(text("SELECT payload->>'snapshot_id' FROM windows_results WHERE agent_id=:id "
             "AND payload->>'repository_id'=:repo AND payload->>'snapshot_id' IS NOT NULL"),
             dict(id=preparation.agent_id, repo=preparation.repository_id))} - copied - absent)
        if not snapshots:
            return
        kind = 'copy_points'
        material = [source, binding['binding_id'], snapshots]
    key = kind + ':' + hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()
    latest = db.scalar(select(ProvisioningOperation).where(ProvisioningOperation.agent_id == preparation.agent_id,
                       ProvisioningOperation.work_key == key).order_by(ProvisioningOperation.created_at.desc()).limit(1))
    if latest and latest.state == 'succeeded':
        # Uma perda posterior da prova exige identidade/vínculo novo, nunca sucesso inventado.
        return
    if latest and latest.finished_at and not force:
        if datetime.now(UTC) - latest.finished_at < timedelta(seconds=min(300, 15 * latest.attempt)):
            return
    db.add(ProvisioningOperation(id=uuid.uuid4(), client_id=preparation.client_id, agent_id=preparation.agent_id,
           kind=kind, attempt=(latest.attempt + 1 if latest else 1), state='pending', step='queued',
           requester_kind='automation', requester_ref='lab-v1', work_key=key,
           work={'source': source, 'snapshots': snapshots, 'seed': preparation.lab_seed}))
    db.flush()


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Pulse(Strict):
    state: Literal['ready', 'busy', 'retry']


class ReaderKey(Strict):
    public_key: str


class Claim(Strict):
    executor_id: str = Field(pattern=r'^[a-zA-Z0-9][a-zA-Z0-9._-]{0,79}$')


class Result(Strict):
    claim_id: uuid.UUID
    state: Literal['succeeded', 'failed']
    error_code: Literal['unavailable', 'identity_changed', 'protection_failed', 'reference_invalid', 'interrupted'] | None = None


def register_automation(app, provisioner):
    a = [Depends(gate), Depends(provisioner)]
    b = [Depends(gate), Depends(require_protector)]

    @app.post('/api/v1/provisioner/automation/heartbeat', dependencies=a)
    def pulse_a(payload: Pulse, db: Session = Depends(session)):
        heartbeat(db, 'primary', payload.state); db.commit()
        return {'accepted': True}

    @app.post('/api/v1/protector/automation/heartbeat', dependencies=b)
    def pulse_b(payload: Pulse, db: Session = Depends(session)):
        heartbeat(db, 'protection', payload.state); db.commit()
        return {'accepted': True}

    @app.get('/api/v1/automation/health', dependencies=[Depends(gate), Depends(require_viewer)])
    def workers(db: Session = Depends(session)):
        return health(db)

    @app.get('/api/v1/protector/automation/installations', dependencies=b)
    def installations(db: Session = Depends(session)):
        return [{'installation_id': str(row.agent_id), 'reader_public_key': row.reader_public_key}
                for row in db.scalars(select(InstallationPreparation).order_by(InstallationPreparation.created_at))]

    @app.post('/api/v1/protector/automation/installations/{installation}/reader', dependencies=b)
    def reader(installation: uuid.UUID, payload: ReaderKey, db: Session = Depends(session)):
        # Mesmo validador Ed25519 do contrato de ativação.
        key = RedeemIn.ed25519(payload.public_key)
        row = db.scalar(select(InstallationPreparation).where(InstallationPreparation.agent_id == installation).with_for_update())
        if row is None:
            raise HTTPException(404, 'instalação inexistente')
        if row.reader_public_key and row.reader_public_key != key:
            raise HTTPException(409, 'chave de leitura de B já registrada; inspeção necessária')
        row.reader_public_key = key
        db.commit()
        return {'accepted': True}

    @app.post('/api/v1/protector/operations/claim', dependencies=b)
    def claim(payload: Claim, db: Session = Depends(session)):
        # Varredura reconcilia intenções cuja transmissão foi interrompida.
        for prep in db.scalars(select(InstallationPreparation).where(InstallationPreparation.state == 'primary_ready')
                               .order_by(InstallationPreparation.agent_id).with_for_update()):
            enqueue(db, prep)
        operation = db.scalar(select(ProvisioningOperation).where(ProvisioningOperation.kind.in_(KINDS), or_(
           ProvisioningOperation.state == 'pending', and_(ProvisioningOperation.state == 'running',
           ProvisioningOperation.claim_expires_at <= func.clock_timestamp())))
           .order_by(ProvisioningOperation.created_at, ProvisioningOperation.id).with_for_update(skip_locked=True).limit(1))
        if not operation:
            db.commit()
            return {'operation': None}
        now = db.scalar(select(func.clock_timestamp()))
        operation.state, operation.claim_id, operation.claimed_by = 'running', uuid.uuid4(), payload.executor_id
        operation.claim_expires_at = now + LEASE
        operation.started_at = operation.started_at or now
        operation.step = 'verifying' if operation.kind == 'protect_installation' else 'copying'
        db.commit()
        return {'operation': dict(id=str(operation.id), installation_id=str(operation.agent_id), kind=operation.kind,
                claim_id=str(operation.claim_id), work=operation.work)}

    class Release(Strict):
        claim_id: uuid.UUID

    def release_claim(operation_id, payload, db, kinds):
        row=db.scalar(select(ProvisioningOperation).where(ProvisioningOperation.id==operation_id).with_for_update())
        if not row or row.kind not in kinds:
            raise HTTPException(404, 'operação deste executor inexistente')
        if row.state in ('succeeded','failed'):
            return {'accepted': True}
        if row.claim_id != payload.claim_id:
            raise HTTPException(409, 'reserva já substituída')
        row.claim_expires_at=datetime.now(UTC)
        db.commit()
        return {'accepted': True}

    @app.post('/api/v1/protector/operations/{operation_id}/release', dependencies=b)
    def release_b(operation_id: uuid.UUID, payload: Release, db: Session = Depends(session)):
        return release_claim(operation_id,payload,db,KINDS)

    @app.post('/api/v1/provisioner/operations/{operation_id}/release', dependencies=a)
    def release_a(operation_id: uuid.UUID, payload: Release, db: Session = Depends(session)):
        return release_claim(operation_id,payload,db,('prepare_primary','retry_primary'))

    @app.post('/api/v1/protector/operations/{operation_id}/result', dependencies=b)
    def finish(operation_id: uuid.UUID, payload: Result, db: Session = Depends(session)):
        existing = db.get(ProvisioningOperation, operation_id)
        if not existing or existing.kind not in KINDS:
            raise HTTPException(404, 'operação de B inexistente')
        prep = db.scalar(select(InstallationPreparation).where(InstallationPreparation.agent_id == existing.agent_id).with_for_update())
        operation = db.scalar(select(ProvisioningOperation).where(ProvisioningOperation.id == operation_id).with_for_update())
        digest = hashlib.sha256(json.dumps(payload.model_dump(mode='json'), sort_keys=True).encode()).hexdigest()
        if operation.state in ('succeeded', 'failed'):
            if operation.payload_hash == digest:
                return {'accepted': True}
            raise HTTPException(409, 'resultado terminal divergente ou reserva substituída')
        now = db.scalar(select(func.clock_timestamp()))
        if operation.claim_id != payload.claim_id or operation.claim_expires_at <= now:
            raise HTTPException(409, 'reserva de B inválida ou vencida')
        if (payload.state == 'failed') != bool(payload.error_code):
            raise HTTPException(422, 'resultado exige categoria de erro coerente')
        if operation.work['source'] != source_fingerprint(prep):
            raise HTTPException(409, 'identidade mudou durante operação')
        if payload.state == 'succeeded':
            binding = current_protection(prep)
            if not binding or (operation.kind == 'protect_installation' and not public_protection(prep)[2]):
                raise HTTPException(409, 'prova vigente ainda ausente')
            copied = {r[0] for r in db.execute(text('SELECT snapshot_a FROM protected_points WHERE agent_id=:id AND binding_id=:binding'),
                      dict(id=prep.agent_id, binding=binding['binding_id']))}
            if not set(operation.work['snapshots']).issubset(copied):
                raise HTTPException(409, 'pontos ainda não confirmados em B')
        operation.state, operation.error_code, operation.payload_hash = payload.state, payload.error_code, digest
        operation.finished_at, operation.step = now, ('verified' if payload.state == 'succeeded' else 'failed')
        db.flush()
        if payload.state == 'succeeded':
            enqueue(db, prep)
        db.commit()
        return {'accepted': True}
