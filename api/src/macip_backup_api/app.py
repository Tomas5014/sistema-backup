"""API central MAC IP — primeira etapa: cadastro + resultados.

Códigos de resposta:
  201 recurso/execução criada     200 duplicata idêntica (idempotente)
  409 mesmo execution_id, conteúdo diferente    401 credencial ausente
  403 credencial inválida/revogada ou identidade cruzada
  413 corpo acima do limite                     422 validação de schema
"""

from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from datetime import UTC, datetime

import math
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import config
from .auth import (
    csrf_guard,
    csrf_pair_guard,
    new_agent_token,
    require_admin,
    require_agent,
    require_operator,
    require_viewer,
    token_hash,
)
from .db import migrate_now, session
from .models import (
    Agent,
    AgentCredential,
    AttemptResultRow,
    Client,
    DestinationResultRow,
    Execution,
    Operator,
    OperatorSession,
)
from .operators import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    LoginRateLimited,
    audit,
    check_login_rate,
    clear_attempts,
    create_session,
    hash_password,
    normalize_login,
    record_failed_attempt,
    revoke_sessions,
)
from . import operators as ops
from .schemas import (
    AgentIn,
    AgentOut,
    ClientIn,
    ClientOut,
    CredentialIn,
    CredentialInfo,
    CredentialOut,
    ExecutionDetail,
    ExecutionIn,
    ExecutionSummary,
    LoginIn,
    OperatorIn,
    OperatorOut,
    OperatorUpdate,
    PasswordResetIn,
)


_SENSITIVE_FIELDS = {
    "password", "password_hash", "token", "session", "cookie",
    "authorization", "secret", "senha",
}


def _mask_sensitive(error: dict) -> dict:
    """Campos sensíveis nunca voltam no `input` de um 422."""
    loc = {str(part).lower() for part in error.get("loc", ())}
    if loc & _SENSITIVE_FIELDS and "input" in error:
        error["input"] = "***"
    return error


def _json_safe(value: object) -> object:
    """Erros de validação podem carregar o valor ofensor (NaN/inf) —
    sanitiza recursivamente para a resposta sair como JSON válido."""
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _canonical_hash(payload: ExecutionIn) -> str:
    blob = json.dumps(
        payload.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(blob.encode()).hexdigest()


def _out_agent(agent: Agent) -> AgentOut:
    return AgentOut(
        id=agent.id,
        client_id=agent.client_id,
        name=agent.name,
        created_at=agent.created_at,
        last_seen_at=agent.last_seen_at,
    )


def _out_execution(row: Execution) -> ExecutionSummary:
    return ExecutionSummary(
        id=row.id,
        agent_id=row.agent_id,
        client_id=row.agent.client_id,
        execution_id=row.execution_id,
        task_id=row.task_id,
        status=row.status,
        started_at_utc=row.started_at_utc,
        finished_at_utc=row.finished_at_utc,
        received_at=row.received_at,
    )


def create_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI):
        migrate_now()  # advisory lock + migrações no start
        yield

    app = FastAPI(title="MAC IP Backup API", version="0.1.0", lifespan=lifespan)

    @app.exception_handler(RequestValidationError)
    async def validation_handler(_: Request, exc: RequestValidationError):
        # jsonable_encoder serializa ctx (ValueError etc.) e _json_safe
        # limpa floats não finitos carregados do valor ofensor
        return JSONResponse(
            {"detail": [
                _mask_sensitive(error)
                for error in _json_safe(jsonable_encoder(exc.errors()))
            ]},
            status_code=422,
        )

    @app.middleware("http")
    async def limit_body(request: Request, call_next):
        """Limite pelos bytes REALMENTE recebidos — cobre chunked e
        ausência de Content-Length; interrompe a leitura no limite."""

        limit = config.max_body_bytes()
        length = request.headers.get("content-length")
        if length is not None:
            try:
                declared = int(length)
            except ValueError:
                return JSONResponse(
                    {"detail": "Content-Length inválido"}, status_code=400
                )
            if declared < 0:
                return JSONResponse(
                    {"detail": "Content-Length inválido"}, status_code=400
                )
            if declared > limit:
                return JSONResponse(
                    {"detail": "corpo acima do limite"}, status_code=413
                )
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > limit:
                return JSONResponse(
                    {"detail": "corpo acima do limite"}, status_code=413
                )
        request._body = bytes(body)  # corpo já consumido — repassa ao handler
        return await call_next(request)

    # ---- health ------------------------------------------------------
    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz(db: Session = Depends(session)) -> dict:
        db.execute(select(func.now()))
        return {"database": "ok"}

    # ---- administrativo ----------------------------------------------
    @app.post("/api/v1/clients", status_code=201, dependencies=[Depends(require_admin), Depends(csrf_guard)])
    def create_client(payload: ClientIn, db: Session = Depends(session)) -> ClientOut:
        client = Client(id=uuid.uuid4(), name=payload.name)
        db.add(client)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise HTTPException(409, "cliente já existe")
        return ClientOut(id=client.id, name=client.name, created_at=client.created_at)

    @app.get("/api/v1/clients", dependencies=[Depends(require_viewer)])
    def list_clients(
        limit: int = Query(50, ge=1, le=500),
        offset: int = Query(0, ge=0),
        db: Session = Depends(session),
    ) -> list[ClientOut]:
        rows = db.execute(
            select(Client).order_by(Client.name).limit(limit).offset(offset)
        ).scalars()
        return [
            ClientOut(id=c.id, name=c.name, created_at=c.created_at) for c in rows
        ]

    @app.post(
        "/api/v1/clients/{client_id}/agents",
        status_code=201,
        dependencies=[Depends(require_admin), Depends(csrf_guard)],
    )
    def create_agent(
        client_id: uuid.UUID, payload: AgentIn, db: Session = Depends(session)
    ) -> AgentOut:
        if db.get(Client, client_id) is None:
            raise HTTPException(404, "cliente inexistente")
        agent = Agent(id=uuid.uuid4(), client_id=client_id, name=payload.name)
        db.add(agent)
        db.commit()
        return _out_agent(agent)

    @app.get("/api/v1/clients/{client_id}/agents", dependencies=[Depends(require_viewer)])
    def list_client_agents(
        client_id: uuid.UUID,
        limit: int = Query(50, ge=1, le=500),
        offset: int = Query(0, ge=0),
        db: Session = Depends(session),
    ) -> list[AgentOut]:
        rows = db.execute(
            select(Agent)
            .where(Agent.client_id == client_id)
            .order_by(Agent.name)
            .limit(limit)
            .offset(offset)
        ).scalars()
        return [_out_agent(a) for a in rows]

    @app.get("/api/v1/agents", dependencies=[Depends(require_viewer)])
    def list_agents(
        client_id: uuid.UUID | None = None,
        limit: int = Query(50, ge=1, le=500),
        offset: int = Query(0, ge=0),
        db: Session = Depends(session),
    ) -> list[AgentOut]:
        query = select(Agent).order_by(Agent.name)
        if client_id is not None:
            query = query.where(Agent.client_id == client_id)
        rows = db.execute(query.limit(limit).offset(offset)).scalars()
        return [_out_agent(a) for a in rows]

    @app.post(
        "/api/v1/agents/{agent_id}/credentials",
        status_code=201,
        dependencies=[Depends(require_admin), Depends(csrf_guard)],
    )
    def provision_credential(
        agent_id: uuid.UUID, payload: CredentialIn, db: Session = Depends(session)
    ) -> CredentialOut:
        if db.get(Agent, agent_id) is None:
            raise HTTPException(404, "agente inexistente")
        token = new_agent_token()
        credential = AgentCredential(
            id=uuid.uuid4(),
            agent_id=agent_id,
            token_hash=token_hash(token),
            label=payload.label,
        )
        db.add(credential)
        db.commit()
        return CredentialOut(
            credential_id=credential.id, agent_id=agent_id, token=token
        )

    @app.get("/api/v1/agents/{agent_id}/credentials", dependencies=[Depends(require_viewer)])
    def list_credentials(
        agent_id: uuid.UUID, db: Session = Depends(session)
    ) -> list[CredentialInfo]:
        rows = db.execute(
            select(AgentCredential).where(AgentCredential.agent_id == agent_id)
        ).scalars()
        return [
            CredentialInfo(
                id=c.id,
                agent_id=c.agent_id,
                label=c.label,
                created_at=c.created_at,
                revoked_at=c.revoked_at,
            )
            for c in rows
        ]

    @app.post(
        "/api/v1/credentials/{credential_id}/revoke",
        dependencies=[Depends(require_admin), Depends(csrf_guard)],
    )
    def revoke_credential(
        credential_id: uuid.UUID, db: Session = Depends(session)
    ) -> dict:
        credential = db.get(AgentCredential, credential_id)
        if credential is None:
            raise HTTPException(404, "credencial inexistente")
        if credential.revoked_at is None:
            credential.revoked_at = datetime.now(UTC)
            db.commit()
        return {"id": str(credential_id), "revoked": True}

    @app.get("/api/v1/executions", dependencies=[Depends(require_viewer)])
    def list_executions(
        client_id: uuid.UUID | None = None,
        agent_id: uuid.UUID | None = None,
        task_id: str | None = None,
        status: str | None = Query(
            None, pattern="^(success|partial|failed|interrupted)$"
        ),
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = Query(50, ge=1, le=500),
        offset: int = Query(0, ge=0),
        db: Session = Depends(session),
    ) -> list[ExecutionSummary]:
        for value in (since, until):
            if value is not None and value.tzinfo is None:
                raise HTTPException(422, "filtro temporal exige timezone")
        if since is not None and until is not None and since > until:
            raise HTTPException(422, "intervalo inválido: since > until")
        query = (
            select(Execution)
            .join(Agent, Execution.agent_id == Agent.id)
            .order_by(Execution.started_at_utc.desc())
        )
        if client_id is not None:
            query = query.where(Agent.client_id == client_id)
        if agent_id is not None:
            query = query.where(Execution.agent_id == agent_id)
        if task_id is not None:
            query = query.where(Execution.task_id == task_id)
        if status is not None:
            query = query.where(Execution.status == status)
        if since is not None:
            query = query.where(Execution.started_at_utc >= since)
        if until is not None:
            query = query.where(Execution.started_at_utc <= until)
        return [_out_execution(e) for e in db.execute(query.limit(limit).offset(offset)).scalars()]

    @app.get("/api/v1/executions/{execution_row_id}", dependencies=[Depends(require_viewer)])
    def get_execution(
        execution_row_id: uuid.UUID, db: Session = Depends(session)
    ) -> ExecutionDetail:
        row = db.get(Execution, execution_row_id)
        if row is None:
            raise HTTPException(404, "execução inexistente")
        destinations = db.execute(
            select(DestinationResultRow)
            .where(DestinationResultRow.execution_row_id == row.id)
            .order_by(DestinationResultRow.id)
        ).scalars()
        detail = _out_execution(row).model_dump()
        detail["destinations"] = [
            {
                "destination_id": d.destination_id,
                "status": d.status,
                "started_at_utc": d.started_at_utc,
                "finished_at_utc": d.finished_at_utc,
                "duration_seconds": d.duration_seconds,
                "snapshot_id": d.snapshot_id,
                "snapshots_available": d.snapshots_available,
                "files_processed": d.files_processed,
                "bytes_processed": d.bytes_processed,
                "error": d.error,
                "exit_code": d.exit_code,
                "failed_operation": d.failed_operation,
                "timeout_seconds": d.timeout_seconds,
                "attempts": [
                    {
                        "attempt": a.attempt,
                        "status": a.status,
                        "started_at_utc": a.started_at_utc,
                        "finished_at_utc": a.finished_at_utc,
                        "duration_seconds": a.duration_seconds,
                        "retryable": a.retryable,
                        "snapshot_id": a.snapshot_id,
                        "snapshots_available": a.snapshots_available,
                        "files_processed": a.files_processed,
                        "bytes_processed": a.bytes_processed,
                        "error": a.error,
                        "exit_code": a.exit_code,
                        "failed_operation": a.failed_operation,
                        "timeout_seconds": a.timeout_seconds,
                    }
                    for a in db.execute(
                        select(AttemptResultRow)
                        .where(AttemptResultRow.destination_row_id == d.id)
                        .order_by(AttemptResultRow.attempt)
                    ).scalars()
                ],
            }
            for d in destinations
        ]
        return ExecutionDetail(**detail)

    # ---- operadores: sessão ------------------------------------------
    def _set_session_cookie(response: Response, token: str) -> None:
        response.set_cookie(
            SESSION_COOKIE,
            token,
            max_age=config.session_ttl_seconds(),
            httponly=True,
            samesite="lax",
            secure=config.session_cookie_secure(),
            path="/",
        )

    def _set_csrf_cookie(response: Response) -> str:
        value = secrets.token_urlsafe(24)
        response.set_cookie(
            CSRF_COOKIE,
            value,
            httponly=False,  # o JS lê e repete em X-CSRF-Token
            samesite="lax",
            secure=config.session_cookie_secure(),
            path="/",
        )
        return value

    def _out_operator(row: Operator) -> OperatorOut:
        return OperatorOut(
            id=row.id,
            login=row.login,
            name=row.name,
            role=row.role,
            active=row.active,
            created_at=row.created_at,
            last_login_at=row.last_login_at,
        )

    @app.get("/api/v1/auth/csrf", status_code=204)
    def csrf_token(response: Response) -> None:
        """Emite o cookie CSRF pré-sessão — o login exige o par."""
        _set_csrf_cookie(response)

    @app.post("/api/v1/auth/login", dependencies=[Depends(csrf_pair_guard)])
    def login(
        payload: LoginIn,
        request: Request,
        response: Response,
        db: Session = Depends(session),
    ) -> OperatorOut:
        login_name = normalize_login(payload.login)
        ip = request.client.host if request.client else "desconhecido"
        try:
            check_login_rate(db, login_name, ip)
        except LoginRateLimited:
            audit.warning("login rate-limit login=%s ip=%s", login_name, ip)
            raise HTTPException(429, "muitas tentativas — aguarde")

        operator = db.execute(
            select(Operator).where(Operator.login == login_name)
        ).scalar_one_or_none()
        # resposta genérica — não revela existência nem desativação;
        # _verify_or_burn_time equaliza o tempo de resposta
        valid = ops._verify_or_burn_time(
            operator.password_hash if operator else None, payload.password
        )
        if not valid or operator is None or not operator.active:
            record_failed_attempt(db, login_name, ip)
            db.commit()
            audit.warning("login falhou login=%s ip=%s", login_name, ip)
            raise HTTPException(401, "credenciais inválidas")

        clear_attempts(db, login_name, ip)
        operator.last_login_at = datetime.now(UTC)
        _, token = create_session(db, operator)
        db.commit()
        _set_session_cookie(response, token)
        _set_csrf_cookie(response)  # rotaciona a cada login
        audit.info("login ok operator=%s ip=%s", operator.login, ip)
        return _out_operator(operator)

    @app.post(
        "/api/v1/auth/logout",
        status_code=204,
        dependencies=[Depends(csrf_guard)],
    )
    def logout(
        request: Request,
        response: Response,
        db: Session = Depends(session),
    ) -> None:
        token = request.cookies.get(SESSION_COOKIE)
        if token:
            session_row = db.execute(
                select(OperatorSession).where(
                    OperatorSession.token_hash == token_hash(token)
                )
            ).scalar_one_or_none()
            if session_row is not None and session_row.revoked_at is None:
                session_row.revoked_at = datetime.now(UTC)
                audit.info("logout operator=%s", session_row.operator_id)
                db.commit()
        response.delete_cookie(SESSION_COOKIE, path="/")
        response.delete_cookie(CSRF_COOKIE, path="/")

    @app.get("/api/v1/auth/me")
    def me(operator: Operator = Depends(require_operator)) -> OperatorOut:
        return _out_operator(operator)

    # ---- operadores: administração -------------------------------------
    @app.get("/api/v1/operators", dependencies=[Depends(require_admin)])
    def list_operators(
        limit: int = Query(50, ge=1, le=500),
        offset: int = Query(0, ge=0),
        db: Session = Depends(session),
    ) -> list[OperatorOut]:
        rows = db.execute(
            select(Operator).order_by(Operator.login).limit(limit).offset(offset)
        ).scalars()
        return [_out_operator(row) for row in rows]

    @app.post(
        "/api/v1/operators",
        status_code=201,
        dependencies=[Depends(require_admin), Depends(csrf_guard)],
    )
    def create_operator(
        payload: OperatorIn, db: Session = Depends(session)
    ) -> OperatorOut:
        operator = Operator(
            id=uuid.uuid4(),
            login=normalize_login(payload.login),
            name=payload.name,
            password_hash=hash_password(payload.password),
            role=payload.role,
            active=True,
        )
        db.add(operator)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise HTTPException(409, "login já existe")
        audit.info("operator criado login=%s role=%s", operator.login, operator.role)
        return _out_operator(operator)

    @app.patch(
        "/api/v1/operators/{operator_id}",
        dependencies=[Depends(require_admin), Depends(csrf_guard)],
    )
    def update_operator(
        operator_id: uuid.UUID,
        payload: OperatorUpdate,
        db: Session = Depends(session),
    ) -> OperatorOut:
        operator = db.get(Operator, operator_id)
        if operator is None:
            raise HTTPException(404, "operador inexistente")
        changes: list[str] = []
        if payload.name is not None and payload.name != operator.name:
            operator.name = payload.name
            changes.append("name")
        if payload.role is not None and payload.role != operator.role:
            operator.role = payload.role
            changes.append("role")
        if payload.active is not None and payload.active != operator.active:
            operator.active = payload.active
            changes.append("active")
            if not payload.active:
                revoke_sessions(db, operator.id)  # desativou → sessões morrem
        db.commit()
        if changes:
            audit.info(
                "operator alterado login=%s campos=%s",
                operator.login,
                ",".join(changes),
            )
        return _out_operator(operator)

    @app.post(
        "/api/v1/operators/{operator_id}/reset-password",
        status_code=204,
        dependencies=[Depends(require_admin), Depends(csrf_guard)],
    )
    def reset_password(
        operator_id: uuid.UUID,
        payload: PasswordResetIn,
        db: Session = Depends(session),
    ) -> None:
        operator = db.get(Operator, operator_id)
        if operator is None:
            raise HTTPException(404, "operador inexistente")
        operator.password_hash = hash_password(payload.password)
        revoke_sessions(db, operator.id)  # senha nova → sessões antigas morrem
        db.commit()
        audit.info("operator senha redefinida login=%s", operator.login)

    # ---- agente autenticado ------------------------------------------
    @app.post("/api/v1/agent/heartbeat", status_code=204)
    def heartbeat(agent: Agent = Depends(require_agent)) -> None:
        return None  # require_agent já atualizou last_seen_at

    @app.post("/api/v1/agent/executions")
    def submit_execution(
        payload: ExecutionIn,
        agent: Agent = Depends(require_agent),
        db: Session = Depends(session),
    ) -> dict:
        # identidade vem da credencial — corpo só pode confirmá-la
        if payload.agent_id is not None and str(payload.agent_id) != str(agent.id):
            raise HTTPException(403, "execution atribuída a outro agente")
        if payload.client_id is not None and str(payload.client_id) != str(agent.client_id):
            raise HTTPException(403, "execution atribuída a outro cliente")

        payload_hash = _canonical_hash(payload)
        row = db.execute(
            pg_insert(Execution)
            .values(
                id=uuid.uuid4(),
                agent_id=agent.id,
                execution_id=payload.execution_id,
                task_id=payload.task_id,
                status=payload.status,
                started_at_utc=payload.started_at_utc,
                finished_at_utc=payload.finished_at_utc,
                payload_hash=payload_hash,
            )
            .on_conflict_do_nothing(constraint="uq_execution_agent_exec")
            .returning(Execution.id)
        ).scalar_one_or_none()

        if row is None:
            existing = db.execute(
                select(Execution).where(
                    Execution.agent_id == agent.id,
                    Execution.execution_id == payload.execution_id,
                )
            ).scalar_one()
            if existing.payload_hash == payload_hash:
                return {"status": "duplicate", "execution_id": payload.execution_id}
            raise HTTPException(
                409, "execution_id já recebido com conteúdo diferente"
            )

        execution_row_id = row
        for destination in payload.destinations:
            dest_row = DestinationResultRow(
                execution_row_id=execution_row_id,
                destination_id=destination.destination_id,
                status=destination.status,
                started_at_utc=destination.started_at_utc,
                finished_at_utc=destination.finished_at_utc,
                duration_seconds=destination.duration_seconds,
                snapshot_id=destination.snapshot_id,
                snapshots_available=destination.snapshots_available,
                files_processed=destination.files_processed,
                bytes_processed=destination.bytes_processed,
                error=destination.error,
                exit_code=destination.exit_code,
                failed_operation=destination.failed_operation,
                timeout_seconds=destination.timeout_seconds,
            )
            db.add(dest_row)
            db.flush()
            for attempt in destination.attempts:
                db.add(
                    AttemptResultRow(
                        destination_row_id=dest_row.id,
                        attempt=attempt.attempt,
                        status=attempt.status,
                        started_at_utc=attempt.started_at_utc,
                        finished_at_utc=attempt.finished_at_utc,
                        duration_seconds=attempt.duration_seconds,
                        retryable=attempt.retryable,
                        snapshot_id=attempt.snapshot_id,
                        snapshots_available=attempt.snapshots_available,
                        files_processed=attempt.files_processed,
                        bytes_processed=attempt.bytes_processed,
                        error=attempt.error,
                        exit_code=attempt.exit_code,
                        failed_operation=attempt.failed_operation,
                        timeout_seconds=attempt.timeout_seconds,
                    )
                )
        db.commit()
        return {
            "status": "created",
            "id": str(execution_row_id),
            "execution_id": payload.execution_id,
        }

    return app


app = create_app()


def run() -> None:
    import uvicorn

    uvicorn.run("macip_backup_api.app:app", host="127.0.0.1", port=8080)
