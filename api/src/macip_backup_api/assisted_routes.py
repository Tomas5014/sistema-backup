"""Operações administrativas fixas. Restic/SSH continuam nos executores A/B."""
from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
from typing import Literal
import uuid

from fastapi import Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.orm import Session

from .auth import csrf_guard, require_viewer
from .db import session
from .models import InstallationPreparation, ProvisioningOperation
from .protection_routes import require_protector
from .protector import _directory
from .provisioner import LabPrimaryAdapter
from .storage_routes import audit, assert_isolated

KINDS = ('lifecycle_a', 'recovery_b')
ERRORS = Literal['busy', 'unavailable', 'identity_changed', 'reference_invalid', 'recovery_failed', 'interrupted']


def enabled():
    if os.environ.get('MACIP_API_LAB_ASSISTED') != '1':
        raise HTTPException(404, 'operação assistida de laboratório desabilitada')


def installation(db, aid):
    row = db.scalar(select(InstallationPreparation).where(
        InstallationPreparation.agent_id == aid).with_for_update())
    if not row:
        raise HTTPException(404, 'instalação inexistente')
    return row


def assert_device(db, aid, credential_id, *, allow_unregistered=False):
    """Revalida sob o mesmo lock usado por substituição/encerramento."""
    prep = db.scalar(select(InstallationPreparation).where(
        InstallationPreparation.agent_id == aid).with_for_update())
    if prep is None and allow_unregistered:
        return None
    credential = db.execute(text('SELECT revoked_at,device_epoch FROM agent_credentials WHERE id=:id'),
                            dict(id=credential_id)).mappings().first()
    if (not prep or not credential or credential['revoked_at']
            or credential['device_epoch'] != prep.device_epoch or prep.lifecycle != 'active'):
        raise HTTPException(403, 'dispositivo revogado ou instalação em atendimento')
    return prep


def context(db,prep):
    # Preserva o vínculo B mesmo quando uma troca de chave exige nova prova.
    # Recuperação não depende de A estar online, nem de sua autorização SSH.
    binding = prep.protected_copy
    if not prep.profile or not binding or not prep.repository_id or not prep.lab_seed:
        raise HTTPException(409, 'repositórios ainda não identificados; atendimento necessário')
    assert_isolated(db,prep,binding)
    return dict(installation_id=str(prep.agent_id), epoch=prep.device_epoch,
                account=prep.account_name, repository=prep.profile['repositorio'], known_hosts=prep.profile['known_hosts'], repository_a=prep.repository_id,
                repository_b=binding['repository_id'], protection=binding,
                seed=prep.lab_seed, reader_public_key=prep.reader_public_key)


def public_op(row):
    return dict(id=str(row.id), kind=row.kind, action=row.work['action'],
                state=row.state, step=row.step, attempt=row.attempt,
                device_epoch=row.work['context']['epoch'], error_code=row.error_code,
                result=row.result, created_at=row.created_at, started_at=row.started_at, finished_at=row.finished_at)


def ensure_idle(db, aid):
    if db.scalar(select(ProvisioningOperation.id).where(ProvisioningOperation.agent_id == aid,
                 ProvisioningOperation.state.in_(('pending', 'running'))).limit(1)):
        raise HTTPException(409, 'há trabalho em andamento; aguarde a conclusão e tente novamente')


def enqueue(db, prep, request_id, action, key, who, **extra):
    ensure_idle(db, prep.agent_id)
    row = ProvisioningOperation(id=request_id, client_id=prep.client_id, agent_id=prep.agent_id,
        kind='lifecycle_a' if action in ('replace', 'close', 'reopen') else 'recovery_b',
        attempt=1, state='pending', step='queued', requester_kind=who.kind,
        requester_ref=who.ref, work_key=key,
        work=dict(action=action, context=context(db,prep), **extra))
    db.add(row)
    audit(db, prep.agent_id, who.ref, 'assisted_requested',
          dict(operation=str(row.id), action=action, device_epoch=prep.device_epoch))
    db.flush()
    return row


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Action(Strict):
    id: uuid.UUID
    action: Literal['replace', 'close', 'reopen', 'catalog', 'restore']
    expected_epoch: int = Field(gt=0, strict=True)
    confirmed: Literal[True]
    catalog_id: uuid.UUID | None = None
    snapshot_b: str | None = Field(default=None, pattern=r'^[a-f0-9]{64}$')

    @field_validator('confirmed', mode='before')
    @classmethod
    def explicit_confirmation(cls, value):
        if value is not True:
            raise HTTPException(422, 'confirmação explícita obrigatória')
        return value


class Claim(Strict):
    executor_id: str = Field(pattern=r'^[a-zA-Z0-9][a-zA-Z0-9._-]{0,79}$')


class Point(Strict):
    snapshot_a: str = Field(pattern=r'^[a-f0-9]{64}$')
    snapshot_b: str = Field(pattern=r'^[a-f0-9]{64}$')
    time: datetime


class Evidence(Strict):
    repository_a: str = Field(pattern=r'^[a-f0-9]{64}$')
    repository_b: str = Field(pattern=r'^[a-f0-9]{64}$')
    revoked: bool = Field(default=False, strict=True)
    inventory_sha256: str | None = Field(default=None, pattern=r'^[a-f0-9]{64}$')
    points: list[Point] | None = Field(default=None, max_length=1000)
    archive_sha256: str | None = Field(default=None, pattern=r'^[a-f0-9]{64}$')
    manifest_sha256: str | None = Field(default=None, pattern=r'^[a-f0-9]{64}$')
    files: int | None = Field(default=None, gt=0, strict=True)
    expires_at: datetime | None = None
    snapshot_b: str | None = Field(default=None, pattern=r'^[a-f0-9]{64}$')


class Result(Strict):
    claim_id: uuid.UUID
    state: Literal['succeeded', 'failed']
    error_code: ERRORS | None = None
    result: Evidence | None = None


def register_assisted(app, admin_identity, provisioner):
    read = [Depends(enabled), Depends(require_viewer)]
    write = [Depends(enabled), Depends(csrf_guard)]
    prefix = '/api/v1/installations/{aid}/assisted'

    @app.get(prefix, dependencies=read)
    def overview(aid: uuid.UUID, db: Session = Depends(session)):
        prep = installation(db, aid)
        rows = db.scalars(select(ProvisioningOperation).where(
            ProvisioningOperation.agent_id == aid, ProvisioningOperation.kind.in_(KINDS))
            .order_by(ProvisioningOperation.created_at.desc(), ProvisioningOperation.id.desc()).limit(50))
        return dict(lifecycle=prep.lifecycle, device_epoch=prep.device_epoch,
                    access_state=prep.access_state, current_device=prep.current_device,
                    operations=[public_op(row) for row in rows])

    @app.post(prefix, status_code=202, dependencies=write)
    def request(aid: uuid.UUID, payload: Action, who=Depends(admin_identity), db: Session = Depends(session)):
        prep = installation(db, aid)
        key = (payload.action + ':' + str(payload.expected_epoch) if payload.action in ('replace', 'close', 'reopen')
               else 'restore:' + str(payload.catalog_id) + ':' + str(payload.snapshot_b)
               if payload.action == 'restore' else 'catalog:' + str(payload.id))
        old = db.get(ProvisioningOperation, payload.id)
        if old and (old.agent_id != aid or old.kind not in KINDS or old.work_key != key):
            raise HTTPException(409, 'identificador já usado por outro atendimento')
        old = old or db.scalar(select(ProvisioningOperation).where(
            ProvisioningOperation.agent_id == aid, ProvisioningOperation.kind.in_(KINDS),
            ProvisioningOperation.work_key == key))
        if old:
            return public_op(old)
        if prep.device_epoch != payload.expected_epoch:
            raise HTTPException(409, 'computador mudou; atualize a tela antes de confirmar')
        ensure_idle(db, aid)
        chosen = None
        if payload.action == 'restore':
            catalog = db.get(ProvisioningOperation, payload.catalog_id) if payload.catalog_id else None
            if (not catalog or catalog.agent_id != aid or catalog.kind != 'recovery_b'
                    or catalog.work['action'] != 'catalog' or catalog.state != 'succeeded'
                    or catalog.work['context'] != context(db,prep)):
                raise HTTPException(409, 'consulte novamente os pontos desta instalação em B')
            chosen = next((p for p in catalog.result['points'] if p['snapshot_b'] == payload.snapshot_b), None)
            if not chosen:
                raise HTTPException(409, 'ponto não pertence ao catálogo confirmado')
        if payload.action in ('replace', 'close', 'reopen'):
            if payload.action == 'replace' and (prep.lifecycle not in ('active','awaiting_device') or (prep.lifecycle=='awaiting_device' and prep.current_device is None)):
                raise HTTPException(409, 'conclua o atendimento atual antes de substituir novamente')
            if payload.action == 'close' and prep.lifecycle not in ('active', 'awaiting_device'):
                raise HTTPException(409, 'encerramento já solicitado ou instalação em atendimento')
            if payload.action == 'reopen' and prep.lifecycle != 'closed':
                raise HTTPException(409, 'somente uma instalação encerrada pode ser reaberta')
            # Primeiro valida a existência das identidades; nunca altera o banco
            # e depois descobre que não sabe qual repositório deve preservar.
            context(db,prep)
            prep.device_epoch += 1
            prep.current_device = None
            prep.lifecycle = dict(replace='replacing', close='closing', reopen='reopening')[payload.action]
            prep.access_state = 'requested'
            db.execute(text('UPDATE agent_credentials SET revoked_at=coalesce(revoked_at,now()) WHERE agent_id=:id'), dict(id=aid))
            db.execute(text("UPDATE activation_codes SET revoked_at=coalesce(revoked_at,now()),state='revoked' WHERE agent_id=:id"), dict(id=aid))
            db.execute(text('UPDATE windows_monitors SET paused=true WHERE agent_id=:id'), dict(id=aid))
        row = enqueue(db, prep, payload.id, payload.action, key, who, **({'point': chosen} if chosen else {}))
        db.commit()
        return public_op(row)

    @app.post(prefix + '/operations/{oid}/retry', dependencies=write)
    def retry(aid: uuid.UUID, oid: uuid.UUID, who=Depends(admin_identity), db: Session = Depends(session)):
        prep = installation(db, aid)
        row = db.get(ProvisioningOperation, oid)
        if not row or row.agent_id != aid or row.kind not in KINDS:
            raise HTTPException(404, 'atendimento inexistente')
        if row.state in ('pending', 'running', 'succeeded'):
            return public_op(row)
        if row.work['action'] == 'restore':
            raise HTTPException(409, 'pasta parcial preservada; faça nova consulta e solicite outra recuperação')
        if row.work['context'] != context(db,prep):
            raise HTTPException(409, 'identidade mudou; atendimento anterior preservado')
        ensure_idle(db, aid)
        audit(db, aid, who.ref, 'assisted_retry', dict(operation=str(oid), previous_error=row.error_code, attempt=row.attempt,
               started_at=row.started_at.isoformat() if row.started_at else None,
               finished_at=row.finished_at.isoformat() if row.finished_at else None))
        row.state, row.step, row.error_code, row.payload_hash = 'pending', 'queued', None, None
        row.attempt += 1
        row.result = None
        row.started_at = row.finished_at = row.claim_id = row.claim_expires_at = row.claimed_by = None
        if row.kind == 'lifecycle_a':
            prep.access_state = 'requested'
        db.commit()
        return public_op(row)

    @app.post(prefix + '/operations/{oid}/revoke-export', dependencies=write)
    def revoke_export(aid: uuid.UUID, oid: uuid.UUID, who=Depends(admin_identity), db: Session = Depends(session)):
        row = db.get(ProvisioningOperation, oid)
        if not row or row.agent_id != aid or row.kind != 'recovery_b' or row.work['action'] != 'restore' or row.state != 'succeeded':
            raise HTTPException(404, 'recuperação concluída inexistente')
        row.result = {**row.result, 'export_revoked': True}
        audit(db, aid, who.ref, 'export_revoked', dict(operation=str(oid)))
        db.commit()
        return {'revoked': True}

    @app.get(prefix + '/operations/{oid}/download', dependencies=[Depends(enabled), Depends(admin_identity)])
    def download(aid: uuid.UUID, oid: uuid.UUID, db: Session = Depends(session)):
        row = db.get(ProvisioningOperation, oid)
        if (not row or row.agent_id != aid or row.kind != 'recovery_b' or row.work['action'] != 'restore'
                or row.state != 'succeeded' or not row.result or row.result.get('export_revoked')
                or datetime.fromisoformat(row.result['expires_at']) <= datetime.now(UTC)):
            raise HTTPException(409, 'arquivos não disponíveis; consulte a recuperação e seu prazo')
        root = Path(os.environ.get('MACIP_LAB_RECOVERY_EXPORT_ROOT', ''))
        try:
            if not root.is_absolute():
                raise ValueError
            _directory(root)
            archive = root / (str(oid) + '.zip')
            if archive.resolve()!=archive:
                raise ValueError
            LabPrimaryAdapter._validate_private_file(archive)
            with archive.open("rb") as stream:
                if hashlib.file_digest(stream,"sha256").hexdigest() != row.result["archive_sha256"]:
                    raise ValueError
        except (ValueError, OSError, RuntimeError):
            raise HTTPException(409, 'exportação indisponível; arquivos de B preservados') from None
        return FileResponse(archive, media_type='application/zip', filename='recuperacao-' + str(oid) + '.zip',
                            headers={'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})

    for side, dependency, kinds in (('provisioner', provisioner, ('lifecycle_a',)),
                                    ('protector', require_protector, ('recovery_b',))):
        register_executor(app, side, dependency, kinds)


def register_executor(app, side, auth, kinds):
    prefix = '/api/v1/' + side + '/assisted'
    dependencies = [Depends(enabled), Depends(auth)]

    @app.post(prefix + '/claim', dependencies=dependencies)
    def claim(payload: Claim, db: Session = Depends(session)):
        row = db.scalar(select(ProvisioningOperation).where(ProvisioningOperation.kind.in_(kinds),
            or_(ProvisioningOperation.state == 'pending', and_(ProvisioningOperation.state == 'running',
                ProvisioningOperation.claim_expires_at <= func.clock_timestamp())))
            .order_by(ProvisioningOperation.created_at, ProvisioningOperation.id).with_for_update(skip_locked=True).limit(1))
        if not row:
            return {'operation': None}
        row.state, row.step, row.claim_id = 'running', 'verifying', uuid.uuid4()
        row.started_at = row.started_at or datetime.now(UTC)
        row.claim_expires_at = datetime.now(UTC) + timedelta(minutes=30)
        row.claimed_by = payload.executor_id
        db.commit()
        return {'operation': dict(id=str(row.id), kind=row.kind, claim_id=str(row.claim_id), work=row.work)}

    @app.get(prefix + '/operations/{oid}/context', dependencies=dependencies)
    def executor_context(oid: uuid.UUID, db: Session = Depends(session)):
        row = db.get(ProvisioningOperation, oid)
        if not row or row.kind not in kinds:
            raise HTTPException(404, 'operação deste executor inexistente')
        prep = installation(db, row.agent_id)
        if row.state != 'running' or row.work['context'] != context(db,prep):
            raise HTTPException(409, 'operação deixou de ser vigente')
        pairs = [dict(snapshot_a=r[0], snapshot_b=r[1]) for r in db.execute(text(
            'SELECT DISTINCT snapshot_a,snapshot_b FROM protected_points WHERE agent_id=:id'), dict(id=prep.agent_id))]
        return dict(context=row.work['context'], pairs=pairs)

    class Release(Strict):
        claim_id: uuid.UUID

    @app.post(prefix + '/operations/{oid}/release', dependencies=dependencies)
    def release(oid: uuid.UUID, payload: Release, db: Session = Depends(session)):
        row = db.scalar(select(ProvisioningOperation).where(ProvisioningOperation.id == oid).with_for_update())
        if not row or row.kind not in kinds:
            raise HTTPException(404, 'operação deste executor inexistente')
        if row.state in ('succeeded','failed'):
            return {'accepted': True}
        if row.claim_id != payload.claim_id:
            raise HTTPException(409, 'reserva substituída')
        row.claim_expires_at = datetime.now(UTC)
        db.commit()
        return {'accepted': True}

    @app.post(prefix + '/operations/{oid}/result', dependencies=dependencies)
    def finish(oid: uuid.UUID, payload: Result, db: Session = Depends(session)):
        initial = db.get(ProvisioningOperation, oid)
        if not initial or initial.kind not in kinds:
            raise HTTPException(404, 'operação deste executor inexistente')
        prep = installation(db, initial.agent_id)
        row = db.scalar(select(ProvisioningOperation).where(ProvisioningOperation.id == oid).with_for_update())
        values = payload.model_dump(mode='json')
        digest = hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()
        if row.state in ('succeeded', 'failed'):
            if digest == row.payload_hash:
                return {'accepted': True}
            raise HTTPException(409, 'resultado terminal divergente')
        if (row.state != 'running' or row.claim_id != payload.claim_id
                or row.claim_expires_at <= datetime.now(UTC) or row.work['context'] != context(db,prep)):
            raise HTTPException(409, 'reserva vencida ou identidade alterada')
        if (payload.state == 'succeeded') != bool(payload.result) or (payload.state == 'failed') != bool(payload.error_code):
            raise HTTPException(422, 'resultado incoerente')
        if payload.result:
            result = payload.result
            if result.repository_a != prep.repository_id or result.repository_b != row.work['context']['repository_b']:
                raise HTTPException(409, 'evidência pertence a outro repositório')
            if row.kind == 'lifecycle_a' and (not result.revoked or not result.inventory_sha256):
                raise HTTPException(409, 'executor ainda não confirmou o acesso e inventário')
            if row.work['action'] == 'catalog':
                pairs = {(r[0], r[1]) for r in db.execute(text(
                    'SELECT snapshot_a,snapshot_b FROM protected_points WHERE agent_id=:id'), dict(id=prep.agent_id))}
                if (result.points is None or any((p.snapshot_a,p.snapshot_b) not in pairs or p.time.tzinfo is None for p in result.points)
                        or len({p.snapshot_b for p in result.points}) != len(result.points)):
                    raise HTTPException(409, 'catálogo não confirmado para esta instalação')
            if row.work['action'] == 'restore' and (not result.archive_sha256 or not result.manifest_sha256
                    or not result.files or result.snapshot_b != row.work['point']['snapshot_b']
                    or not result.expires_at or result.expires_at.tzinfo is None
                    or not row.started_at < result.expires_at <= row.started_at + timedelta(hours=25)):
                raise HTTPException(409, 'recuperação incompleta; não publicar arquivos')
        row.state, row.error_code, row.payload_hash = payload.state, payload.error_code, digest
        row.result = values['result']
        row.finished_at, row.step = datetime.now(UTC), 'verified' if payload.state == 'succeeded' else 'failed'
        if row.kind == 'lifecycle_a':
            prep.access_state = 'confirmed' if payload.state == 'succeeded' else 'failed'
            if payload.state == 'succeeded':
                prep.lifecycle = 'closed' if row.work['action'] == 'close' else 'awaiting_device'
                if row.work['action'] == 'reopen':
                    prep.protected_copy = {**prep.protected_copy, 'binding_id': str(uuid.uuid4()), 'proof': None, 'invalid': True}
                    prep.protection_state = 'pending'
        audit(db, prep.agent_id, side, 'assisted_result', dict(operation=str(row.id), state=row.state, error=row.error_code))
        db.commit()
        return {'accepted': True}
