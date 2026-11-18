"""Contrato de ativação v1. Segredos de repositório são entregues apenas por A."""

import base64
from datetime import timedelta
import hashlib
import hmac
import os
import re
import secrets
import uuid
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .auth import csrf_guard
from .db import session
from .models import InstallationPreparation
from .protection_routes import public_protection, source_fingerprint


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def fingerprint(public_key):
    return "SHA256:" + base64.b64encode(hashlib.sha256(
        base64.b64decode(public_key.split()[1])
    ).digest()).decode().rstrip("=")


def lab_enabled():
    if os.environ.get("MACIP_API_LAB_ACTIVATION") != "1":
        raise HTTPException(404, "ativação de laboratório desabilitada")


def delivery_url():
    value = os.environ.get("MACIP_API_ACTIVATION_DELIVERY_URL", "")
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise HTTPException(503, "endpoint HTTPS de A não configurado")
    return value


class IssueIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    installation_id: uuid.UUID


class RedeemIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    activation_code: str = Field(min_length=43, max_length=64)
    activation_id: uuid.UUID
    device_id: uuid.UUID
    resume_secret: str = Field(min_length=43, max_length=128)
    public_key: str

    @field_validator("public_key")
    @classmethod
    def ed25519(cls, value):
        try:
            kind, encoded = value.split()
            data = base64.b64decode(encoded, validate=True)
            if kind != "ssh-ed25519" or len(data) != 51 or data[:19] != b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20":
                raise ValueError
        except (ValueError, TypeError):
            raise HTTPException(422, "chave pública Ed25519 inválida") from None
        return kind + " " + encoded


class AuthorizeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    activation_id: uuid.UUID
    resume_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


def register_activation(app, admin_identity, provisioner):
    gate = [Depends(lab_enabled)]

    def rate_redeem(request: Request, db: Session = Depends(session)):
        if request.url.scheme != "https":
            raise HTTPException(400, "resgate exige HTTPS")
        remote = digest(request.client.host)
        db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:remote))"), {"remote": remote})
        count = db.scalar(text(
            "SELECT count(*) FROM activation_attempts WHERE remote_hash=:remote "
            "AND attempted_at > now() - interval '1 minute'"
        ), {"remote": remote})
        if count >= 20:
            raise HTTPException(429, "limite de tentativas; aguarde um minuto")
        db.execute(text("INSERT INTO activation_attempts(remote_hash) VALUES(:remote)"), {"remote": remote})
        db.commit()

    def lock_installation(db, agent):
        row = db.query(InstallationPreparation).filter_by(agent_id=agent).with_for_update().one_or_none()
        if row is None:
            raise HTTPException(404, "instalação inexistente")
        return row

    def get_code(db, code_id):
        row = db.execute(text("SELECT * FROM activation_codes WHERE id=:id FOR UPDATE"), {"id": code_id}).mappings().one_or_none()
        if row is None:
            raise HTTPException(404, "ativação inexistente")
        return row

    @app.post("/api/v1/activation-codes", dependencies=[*gate, Depends(csrf_guard)], status_code=201)
    def issue(payload: IssueIn, response: Response, identity=Depends(admin_identity), db: Session = Depends(session)):
        row = lock_installation(db, payload.installation_id)
        if not public_protection(row)[2]:
            raise HTTPException(409, "prova vigente de B pendente")
        delivery_url()
        public = os.environ.get("MACIP_API_ACTIVATION_PUBLIC_URL", "").rstrip("/")
        if urlsplit(public).scheme != "https" or not urlsplit(public).hostname or "#" in public or "?" in public or "@" in public:
            raise HTTPException(503, "URL pública HTTPS da central não configurada")
        now = db.scalar(text("SELECT clock_timestamp()"))
        code = secrets.token_urlsafe(32)
        code_id = uuid.uuid4()
        db.execute(text(
            "INSERT INTO activation_codes(id,agent_id,code_hash,requester_kind,requester_ref,expires_at,issued_source) "
            "VALUES(:id,:agent,:hash,:kind,:ref,:expires,:source)"
        ), dict(id=code_id, agent=row.agent_id, hash=digest(code), kind=identity.kind,
                ref=identity.ref, expires=now + timedelta(minutes=10), source=source_fingerprint(row)))
        db.commit()
        response.headers["Cache-Control"] = "no-store"
        return {"id": str(code_id), "activation_code": public + "#macip-activation=" + code,
                "expires_at": (now + timedelta(minutes=10)).isoformat()}

    @app.post("/api/v1/activation-codes/{code_id}/revoke", dependencies=[*gate, Depends(csrf_guard)])
    def revoke(code_id: uuid.UUID, identity=Depends(admin_identity), db: Session = Depends(session)):
        row = get_code(db, code_id)
        db.execute(text("UPDATE activation_codes SET revoked_at=now(),state='revoked' WHERE id=:id"), {"id": row["id"]})
        db.commit()
        return {"state": "revoked"}

    @app.post("/api/v1/activations/redeem", dependencies=[*gate, Depends(rate_redeem)])
    def redeem(payload: RedeemIn, request: Request, response: Response, db: Session = Depends(session)):
        row = db.execute(text("SELECT * FROM activation_codes WHERE code_hash=:hash FOR UPDATE"),
                         {"hash": digest(payload.activation_code)}).mappings().one_or_none()
        if row is None or row["revoked_at"]:
            raise HTTPException(403, "código inválido ou revogado")
        now = db.scalar(text("SELECT clock_timestamp()"))
        if row["used_at"]:
            same = (row["activation_id"] == payload.activation_id and row["device_id"] == payload.device_id
                    and row["public_key"] == payload.public_key
                    and hmac.compare_digest(row["resume_hash"], digest(payload.resume_secret)))
            if not same:
                raise HTTPException(409, "código já usado por outra ativação")
            if row["used_at"] + timedelta(hours=24) <= now:
                raise HTTPException(410, "retomada expirada")
        else:
            if row["expires_at"] <= now:
                raise HTTPException(410, "código vencido")
            installation = lock_installation(db, row["agent_id"])
            if row["issued_source"] != source_fingerprint(installation) or not public_protection(installation)[2]:
                raise HTTPException(409, "prova vigente de B pendente")
            try:
                db.execute(text(
                    "UPDATE activation_codes SET used_at=:now,activation_id=:aid,device_id=:device,"
                    "resume_hash=:resume,public_key=:key,state='pending' WHERE id=:id"
                ), dict(now=now, aid=payload.activation_id, device=payload.device_id,
                        resume=digest(payload.resume_secret), key=payload.public_key, id=row["id"]))
                db.commit()
            except IntegrityError:
                db.rollback()
                raise HTTPException(409, "identidade já vinculada a outra ativação") from None
        response.headers["Cache-Control"] = "no-store"
        return {"contract_version": 1, "activation_id": str(payload.activation_id),
                "delivery_url": delivery_url(), "state": row["state"] if row["used_at"] else "pending"}

    @app.post("/api/v1/activation-executor/authorize", dependencies=[*gate, Depends(provisioner)])
    def authorize(payload: AuthorizeIn, db: Session = Depends(session)):
        row = db.execute(text("SELECT * FROM activation_codes WHERE activation_id=:id FOR UPDATE"),
                         {"id": payload.activation_id}).mappings().one_or_none()
        now = db.scalar(text("SELECT clock_timestamp()"))
        if (not row or row["revoked_at"] or not hmac.compare_digest(row["resume_hash"], payload.resume_hash)
                or row["used_at"] + timedelta(hours=24) <= now):
            raise HTTPException(403, "ativação recusada")
        installation = lock_installation(db, row["agent_id"])
        expected = row["registered_source"] or row["issued_source"]
        if source_fingerprint(installation) != expected:
            raise HTTPException(409, "identidade da instalação mudou")
        if row["registered_source"]:
            ready = public_protection(installation)[2]
        else:
            if not public_protection(installation)[2]:
                raise HTTPException(409, "prova de B pendente antes do registro")
            ready = False
        return {"installation_id": str(row["agent_id"]), "public_key": row["public_key"],
                "account_name": installation.account_name,
                "profile": installation.profile, "registered": bool(row["registered_source"]), "ready": ready}

    @app.post("/api/v1/activation-executor/registered", dependencies=[*gate, Depends(provisioner)])
    def registered(payload: AuthorizeIn, db: Session = Depends(session)):
        # Revalida autorização e vínculo antes de invalidar a prova pela nova chave.
        context = authorize(payload, db)
        if context["registered"]:
            return {"state": "awaiting_protection"}
        row = db.execute(text("SELECT * FROM activation_codes WHERE activation_id=:id"),
                         {"id": payload.activation_id}).mappings().one()
        installation = lock_installation(db, row["agent_id"])
        installation.public_key_fingerprint = fingerprint(row["public_key"])
        installation.protection_state = "pending"
        db.execute(text("UPDATE activation_codes SET registered_source=:source,state='awaiting_protection' WHERE id=:id"),
                   {"source": source_fingerprint(installation), "id": row["id"]})
        db.commit()
        return {"state": "awaiting_protection"}

    @app.post("/api/v1/activation-executor/delivered", dependencies=[*gate, Depends(provisioner)])
    def delivered(payload: AuthorizeIn, db: Session = Depends(session)):
        context = authorize(payload, db)
        if not context["ready"]:
            raise HTTPException(409, "nova prova de B pendente")
        db.execute(text("UPDATE activation_codes SET state='delivered',delivered_at=coalesce(delivered_at,now()) WHERE activation_id=:id"),
                   {"id": payload.activation_id})
        db.commit()
        return context
