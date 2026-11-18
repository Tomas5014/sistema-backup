"""Registros do executor B para o laboratório de cópia protegida."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from . import config
from .db import session
from .models import InstallationPreparation

_bearer = HTTPBearer(auto_error=False)
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class BindingIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    previous_binding: uuid.UUID | None
    source: Digest
    volume_id: uuid.UUID
    destination: Digest
    repository_id: Digest


class BindingRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    binding_id: uuid.UUID


class PointIn(BindingRef):
    snapshot_a: Digest
    snapshot_b: Digest


class ProofIn(PointIn):
    method: Literal["restic-offline-sha256-v1"]
    files: int = Field(gt=0, strict=True)
    manifest_sha256: Digest
    restored_sha256: Digest


def require_protector(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> None:
    if not config.lab_protection_enabled():
        raise HTTPException(404, "proteção de laboratório desabilitada")
    token = os.environ.get("MACIP_API_PROTECTOR_TOKEN", "")
    others = (os.environ.get("MACIP_API_PROVISIONER_TOKEN"), os.environ.get("MACIP_API_ADMIN_TOKEN"))
    if len(token) < 32 or token in others:
        raise HTTPException(503, "credencial de B não configurada")
    if credentials is None:
        raise HTTPException(401, "credencial de B ausente")
    if not hmac.compare_digest(credentials.credentials.encode(), token.encode()):
        raise HTTPException(403, "credencial de B inválida")


def source_fingerprint(row: InstallationPreparation) -> str:
    source = [str(row.agent_id), row.repository_id, row.profile["repositorio"],
              row.profile["known_hosts"], row.public_key_fingerprint]
    return hashlib.sha256(json.dumps(source, separators=(",", ":")).encode()).hexdigest()


def current_protection(row: InstallationPreparation) -> dict | None:
    if row.state == "primary_ready" and row.profile and row.protected_copy:
        if row.protected_copy["source"] == source_fingerprint(row):
            return row.protected_copy
    return None


def public_protection(row: InstallationPreparation) -> tuple[str, dict | None, bool]:
    binding = current_protection(row)
    if binding is None:
        return "pending", None, False
    if binding.get("invalid"):
        return "pending", binding.get("last_copy"), False
    proof = binding.get("proof")
    state = "proven" if proof else "prepared"
    return state, binding.get("last_copy"), bool(proof and config.lab_protection_enabled())


def register_protection(app, get_preparation, profile_is_current):
    prefix = "/api/v1/protector/installations/{agent_id}"
    auth = [Depends(require_protector)]

    def get(db, agent_id, *, lock=False):
        row = get_preparation(db, agent_id, lock=lock)
        if row.state != "primary_ready" or not profile_is_current(row):
            raise HTTPException(409, "primário ainda não verificado")
        return row

    @app.get(prefix, dependencies=auth)
    def context(agent_id: uuid.UUID, db: Session = Depends(session)):
        row = get(db, agent_id)
        return {
            "source": source_fingerprint(row),
            "repository": row.profile["repositorio"],
            "known_hosts": row.profile["known_hosts"],
            "repository_id": row.repository_id,
            "windows_key_fingerprint": row.public_key_fingerprint,
            "binding_id": (row.protected_copy or {}).get("binding_id"),
            "protection": current_protection(row),
        }

    @app.post(prefix + "/binding", dependencies=auth)
    def bind(agent_id: uuid.UUID, payload: BindingIn, db: Session = Depends(session)):
        row = get(db, agent_id, lock=True)
        values = payload.model_dump(mode="json", exclude={"previous_binding"})
        if values["source"] != source_fingerprint(row) or payload.repository_id == row.repository_id:
            raise HTTPException(409, "identidade dos repositórios incompatível")
        old = row.protected_copy or {}
        if not old.get("invalid") and all(old.get(key) == value for key, value in values.items()):
            return old
        if old.get("binding_id") != (str(payload.previous_binding) if payload.previous_binding else None):
            raise HTTPException(409, "vínculo de B mudou")
        row.protected_copy = {**values, "binding_id": str(uuid.uuid4())}
        row.protection_state = "prepared"
        db.commit()
        return row.protected_copy

    @app.post(prefix + "/invalidate", dependencies=auth)
    def invalidate(agent_id: uuid.UUID, payload: BindingRef, db: Session = Depends(session)):
        row = get(db, agent_id, lock=True)
        binding = current_protection(row)
        if not binding or binding["binding_id"] != str(payload.binding_id):
            raise HTTPException(409, "vínculo de B mudou")
        # O inventário confirmado continua disponível como evidência.
        row.protected_copy = {
            **binding, "binding_id": str(uuid.uuid4()), "proof": None, "invalid": True,
        }
        row.protection_state = "pending"
        db.commit()
        return row.protected_copy

    def point(db, agent_id, payload):
        row = get(db, agent_id, lock=True)
        binding = current_protection(row)
        if not binding or binding.get("invalid") or binding["binding_id"] != str(payload.binding_id):
            raise HTTPException(409, "vínculo de B mudou")
        pair = payload.model_dump(mode="json", include={"snapshot_a", "snapshot_b"})
        return row, binding, pair

    @app.post(prefix + "/copy", dependencies=auth)
    def copy(agent_id: uuid.UUID, payload: PointIn, db: Session = Depends(session)):
        row, binding, pair = point(db, agent_id, payload)
        old = binding.get("last_copy")
        if old and all(old[key] == value for key, value in pair.items()):
            return old
        record = {**pair, "confirmed_at": datetime.now(UTC).isoformat()}
        row.protected_copy = {**binding, "last_copy": record}
        db.commit()
        return record

    @app.post(prefix + "/proof", dependencies=auth)
    def prove(agent_id: uuid.UUID, payload: ProofIn, db: Session = Depends(session)):
        row, binding, pair = point(db, agent_id, payload)
        last = binding.get("last_copy")
        if (
            not last or any(last[key] != value for key, value in pair.items())
            or payload.manifest_sha256 != payload.restored_sha256
        ):
            raise HTTPException(409, "evidência de restauração não corresponde à cópia confirmada")
        values = payload.model_dump(mode="json", exclude={"binding_id"})
        old = binding.get("proof")
        if old and all(old[key] == value for key, value in values.items()):
            return old
        proof = {**values, "verified_at": datetime.now(UTC).isoformat()}
        row.protected_copy = {**binding, "proof": proof}
        row.protection_state = "proven"
        db.commit()
        return proof
