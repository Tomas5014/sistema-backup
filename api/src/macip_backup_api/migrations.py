"""Migrações versionadas e transacionais — nunca apagam histórico.

Cada migração é uma lista de DDL executada DENTRO de uma transação;
`schema_migrations` registra versões aplicadas. Um advisory lock de
sessão impede corrida entre instâncias.
"""

from __future__ import annotations

from sqlalchemy import Connection, Engine, text

MIGRATIONS: list[tuple[int, str, str]] = [
    (
        1,
        "init",
        """
        CREATE TABLE IF NOT EXISTS clients (
            id          uuid PRIMARY KEY,
            name        varchar(200) NOT NULL UNIQUE,
            created_at  timestamptz NOT NULL DEFAULT now()
        );
        CREATE TABLE IF NOT EXISTS agents (
            id           uuid PRIMARY KEY,
            client_id    uuid NOT NULL REFERENCES clients(id),
            name         varchar(200) NOT NULL,
            created_at   timestamptz NOT NULL DEFAULT now(),
            last_seen_at timestamptz
        );
        CREATE INDEX IF NOT EXISTS ix_agents_client ON agents(client_id);
        CREATE TABLE IF NOT EXISTS agent_credentials (
            id         uuid PRIMARY KEY,
            agent_id   uuid NOT NULL REFERENCES agents(id),
            token_hash varchar(64) NOT NULL UNIQUE,
            label      varchar(200),
            created_at timestamptz NOT NULL DEFAULT now(),
            revoked_at timestamptz
        );
        CREATE INDEX IF NOT EXISTS ix_credentials_agent ON agent_credentials(agent_id);
        CREATE TABLE IF NOT EXISTS executions (
            id            uuid PRIMARY KEY,
            agent_id      uuid NOT NULL REFERENCES agents(id),
            execution_id  varchar(64) NOT NULL,
            task_id       varchar(200) NOT NULL,
            status        varchar(20) NOT NULL,
            started_at_utc  timestamptz NOT NULL,
            finished_at_utc timestamptz NOT NULL,
            received_at   timestamptz NOT NULL DEFAULT now(),
            payload_hash  varchar(64) NOT NULL,
            CONSTRAINT uq_execution_agent_exec UNIQUE (agent_id, execution_id)
        );
        CREATE INDEX IF NOT EXISTS ix_executions_agent   ON executions(agent_id);
        CREATE INDEX IF NOT EXISTS ix_executions_task    ON executions(task_id);
        CREATE INDEX IF NOT EXISTS ix_executions_status  ON executions(status);
        CREATE INDEX IF NOT EXISTS ix_executions_started ON executions(started_at_utc);
        CREATE TABLE IF NOT EXISTS destination_results (
            id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            execution_row_id   uuid NOT NULL REFERENCES executions(id),
            destination_id     varchar(200) NOT NULL,
            status             varchar(20) NOT NULL,
            started_at_utc     timestamptz NOT NULL,
            finished_at_utc    timestamptz NOT NULL,
            duration_seconds   numeric,
            snapshot_id        varchar(128),
            snapshots_available integer,
            files_processed    bigint,
            bytes_processed    bigint,
            error              text,
            exit_code          integer,
            failed_operation   varchar(200),
            timeout_seconds    integer
        );
        CREATE INDEX IF NOT EXISTS ix_dest_exec ON destination_results(execution_row_id);
        CREATE TABLE IF NOT EXISTS attempt_results (
            id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            destination_row_id bigint NOT NULL REFERENCES destination_results(id),
            attempt            integer NOT NULL,
            status             varchar(20) NOT NULL,
            started_at_utc     timestamptz NOT NULL,
            finished_at_utc    timestamptz NOT NULL,
            duration_seconds   numeric,
            retryable          boolean NOT NULL DEFAULT false,
            snapshot_id        varchar(128),
            snapshots_available integer,
            files_processed    bigint,
            bytes_processed    bigint,
            error              text,
            exit_code          integer,
            failed_operation   varchar(200),
            timeout_seconds    integer
        );
        CREATE INDEX IF NOT EXISTS ix_attempt_dest ON attempt_results(destination_row_id);
        """,
    ),
    (
        2,
        "operators-sessions",
        """
        CREATE TABLE IF NOT EXISTS operators (
            id            uuid PRIMARY KEY,
            login         varchar(64) NOT NULL UNIQUE,
            name          varchar(200) NOT NULL,
            password_hash varchar(256) NOT NULL,
            role          varchar(20) NOT NULL
                          CHECK (role IN ('admin','viewer')),
            active        boolean NOT NULL DEFAULT true,
            created_at    timestamptz NOT NULL DEFAULT now(),
            last_login_at timestamptz
        );
        CREATE TABLE IF NOT EXISTS operator_sessions (
            id          uuid PRIMARY KEY,
            operator_id uuid NOT NULL REFERENCES operators(id),
            token_hash  varchar(64) NOT NULL UNIQUE,
            created_at  timestamptz NOT NULL DEFAULT now(),
            expires_at  timestamptz NOT NULL,
            revoked_at  timestamptz
        );
        CREATE INDEX IF NOT EXISTS ix_operator_sessions_operator
            ON operator_sessions(operator_id);
        CREATE TABLE IF NOT EXISTS login_attempts (
            id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            login        varchar(64) NOT NULL,
            ip           varchar(64) NOT NULL,
            attempted_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX IF NOT EXISTS ix_login_attempts_login
            ON login_attempts(login, attempted_at);
        CREATE INDEX IF NOT EXISTS ix_login_attempts_ip
            ON login_attempts(ip, attempted_at);
        """,
    ),
    (
        3,
        "tasks-revisions",
        """
        CREATE TABLE IF NOT EXISTS tasks (
            id               uuid PRIMARY KEY,
            agent_id         uuid NOT NULL REFERENCES agents(id),
            name             varchar(200) NOT NULL,
            current_revision integer NOT NULL,
            created_at       timestamptz NOT NULL DEFAULT now(),
            archived_at      timestamptz
        );
        CREATE INDEX IF NOT EXISTS ix_tasks_agent ON tasks(agent_id);
        CREATE TABLE IF NOT EXISTS task_revisions (
            id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            task_id          uuid NOT NULL REFERENCES tasks(id),
            revision         integer NOT NULL,
            contract_version integer NOT NULL,
            config           jsonb NOT NULL,
            author_kind      varchar(20) NOT NULL
                             CHECK (author_kind IN ('operator','admin')),
            author_ref       varchar(200) NOT NULL,
            created_at       timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_task_revision UNIQUE (task_id, revision)
        );
        CREATE INDEX IF NOT EXISTS ix_task_revisions_task
            ON task_revisions(task_id);
        """,
    ),
    (
        4,
        "task_revisions: snapshot do nome do cliente",
        # Exportação histórica imutável: client.name é editável e hoje
        # o JSON da revisão usaria o nome ATUAL. Guardamos o nome no
        # momento da revisão. Revisões já existentes recebem o nome
        # vigente na migração — o nome que tinham na criação não foi
        # registrado e nenhum valor é inventado para elas.
        """
        ALTER TABLE task_revisions
            ADD COLUMN IF NOT EXISTS client_name varchar(200);
        UPDATE task_revisions r
            SET client_name = c.name
            FROM tasks t
            JOIN agents a ON a.id = t.agent_id
            JOIN clients c ON c.id = a.client_id
            WHERE r.task_id = t.id;
        """,
    ),
    (
        5,
        "distribuição gerenciada de tarefas",
        # Estado DESEJADO por tarefa + log de confirmações do agente.
        # Tarefas existentes começam sem linha em task_distributions
        # (= não publicadas) — nenhum efeito sobre o que já operava.
        """
        CREATE TABLE IF NOT EXISTS task_distributions (
            task_id uuid PRIMARY KEY REFERENCES tasks(id),
            generation integer NOT NULL DEFAULT 0,
            published_revision integer,
            desired_action varchar(20) NOT NULL DEFAULT 'none',
            author_kind varchar(20),
            author_ref varchar(200),
            updated_at timestamptz NOT NULL DEFAULT now(),
            confirmed_generation integer,
            confirmed_status varchar(20),
            confirmed_revision integer,
            confirmed_detail varchar(1000),
            confirmed_agent_time varchar(100),
            confirmed_at timestamptz
        );
        CREATE TABLE IF NOT EXISTS task_confirmations (
            id bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
            task_id uuid NOT NULL REFERENCES tasks(id),
            generation integer NOT NULL,
            status varchar(20) NOT NULL,
            applied_revision integer,
            detail varchar(1000),
            agent_time varchar(100),
            payload_hash varchar(64) NOT NULL,
            received_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_task_confirmation UNIQUE (task_id, generation)
        );
        CREATE INDEX IF NOT EXISTS ix_task_confirmations_task
            ON task_confirmations(task_id);
        """,
    ),
    (
        6,
        "tentativas de aplicação e último sucesso na distribuição",
        # `attempt` distingue tentativas da MESMA geração (falha ->
        # sucesso) mantendo deduplicação por conteúdo; last_applied_*
        # registra o último sucesso mesmo quando o estado vigente é
        # falha de tentativa posterior.
        """
        ALTER TABLE task_confirmations
            ADD COLUMN IF NOT EXISTS attempt integer NOT NULL DEFAULT 1;
        ALTER TABLE task_confirmations
            DROP CONSTRAINT IF EXISTS uq_task_confirmation;
        ALTER TABLE task_confirmations
            ADD CONSTRAINT uq_task_confirmation
            UNIQUE (task_id, generation, attempt);
        ALTER TABLE task_distributions
            ADD COLUMN IF NOT EXISTS confirmed_attempt integer;
        ALTER TABLE task_distributions
            ADD COLUMN IF NOT EXISTS last_applied_generation integer;
        ALTER TABLE task_distributions
            ADD COLUMN IF NOT EXISTS last_applied_revision integer;
        ALTER TABLE task_distributions
            ADD COLUMN IF NOT EXISTS last_applied_attempt integer;
        ALTER TABLE task_distributions
            ADD COLUMN IF NOT EXISTS last_applied_at timestamptz;
        """,
    ),
]

MIGRATIONS.append((7, 'asynchronous-linux-recovery', '''
CREATE TABLE IF NOT EXISTS recovery_operations (
 id uuid PRIMARY KEY, client_id uuid NOT NULL REFERENCES clients(id),
 agent_id uuid NOT NULL REFERENCES agents(id), task_id uuid NOT NULL REFERENCES tasks(id),
 revision integer NOT NULL, kind text NOT NULL CHECK(kind IN ('snapshots','restore')),
 destination_id text NOT NULL, snapshot_id text, query_id uuid REFERENCES recovery_operations(id),
 config jsonb NOT NULL, config_hash text NOT NULL,
 requester_kind text NOT NULL, requester_ref text NOT NULL,
 claim_id uuid, state text NOT NULL DEFAULT 'pending'
 CHECK(state IN ('pending','running','succeeded','failed','interrupted')),
 result jsonb, error text, created_at timestamptz NOT NULL DEFAULT now(),
 started_at timestamptz, finished_at timestamptz,
 UNIQUE(query_id, snapshot_id),
 FOREIGN KEY(task_id, revision) REFERENCES task_revisions(task_id, revision)
);
CREATE INDEX IF NOT EXISTS ix_recovery_agent_pending ON recovery_operations(agent_id, state);
'''))

MIGRATIONS.append((8, 'primary-installation-provisioning', '''
ALTER TABLE agents
 ADD COLUMN IF NOT EXISTS installation_identifier varchar(80),
 ADD COLUMN IF NOT EXISTS platform varchar(20),
 ADD COLUMN IF NOT EXISTS suggested_time varchar(5);

CREATE UNIQUE INDEX IF NOT EXISTS uq_agents_client_installation
 ON agents(client_id, installation_identifier)
 WHERE installation_identifier IS NOT NULL;

CREATE TABLE IF NOT EXISTS provisioning_operations (
 id uuid PRIMARY KEY,
 client_id uuid NOT NULL REFERENCES clients(id),
 agent_id uuid NOT NULL REFERENCES agents(id),
 kind varchar(30) NOT NULL CHECK(kind IN ('prepare_primary','retry_primary')),
 attempt integer NOT NULL CHECK(attempt > 0),
 state varchar(20) NOT NULL CHECK(state IN ('pending','running','succeeded','failed')),
 step varchar(40) NOT NULL,
 requester_kind varchar(20) NOT NULL,
 requester_ref varchar(200) NOT NULL,
 claim_id uuid,
 claimed_by varchar(100),
 payload_hash varchar(64),
 error_code varchar(50),
 created_at timestamptz NOT NULL DEFAULT now(),
 started_at timestamptz,
 finished_at timestamptz
);
CREATE INDEX IF NOT EXISTS ix_provisioning_operations_agent
 ON provisioning_operations(agent_id, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS uq_provisioning_active_installation
 ON provisioning_operations(agent_id)
 WHERE state IN ('pending','running');

CREATE TABLE IF NOT EXISTS installation_preparations (
 agent_id uuid PRIMARY KEY REFERENCES agents(id),
 client_id uuid NOT NULL REFERENCES clients(id),
 current_operation_id uuid NOT NULL REFERENCES provisioning_operations(id),
 state varchar(30) NOT NULL
   CHECK(state IN ('pending','running','primary_ready','failed')),
 step varchar(40) NOT NULL,
 protection_state varchar(20) NOT NULL DEFAULT 'pending'
   CHECK(protection_state IN ('pending','prepared','proven')),
 account_name varchar(32),
 repository_id varchar(64),
 public_key_fingerprint varchar(128),
 vault_reference varchar(200),
 profile jsonb,
 error_code varchar(50),
 created_at timestamptz NOT NULL DEFAULT now(),
 updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_installation_preparations_client
 ON installation_preparations(client_id);
'''))

MIGRATIONS.append((9, 'primary-provisioning-claim-expiry', '''
ALTER TABLE provisioning_operations
 ADD COLUMN IF NOT EXISTS claim_expires_at timestamptz;
'''))

MIGRATIONS.append((10, 'protected-copy-evidence', '''
ALTER TABLE installation_preparations
 ADD COLUMN IF NOT EXISTS protected_copy jsonb;
'''))

MIGRATIONS.append((11, 'lab-activation-codes', '''
CREATE TABLE IF NOT EXISTS activation_codes (
 id uuid PRIMARY KEY,
 agent_id uuid NOT NULL REFERENCES agents(id),
 code_hash varchar(64) NOT NULL UNIQUE,
 requester_kind varchar(20) NOT NULL, requester_ref varchar(200) NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(), expires_at timestamptz NOT NULL,
 revoked_at timestamptz, used_at timestamptz, delivered_at timestamptz,
 activation_id uuid UNIQUE, device_id uuid, resume_hash varchar(64),
 public_key text, issued_source varchar(64) NOT NULL,
 registered_source varchar(64),
 state varchar(30) NOT NULL DEFAULT 'issued'
);
CREATE TABLE IF NOT EXISTS activation_attempts (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 remote_hash varchar(64) NOT NULL,
 attempted_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_activation_attempts_remote ON activation_attempts(remote_hash, attempted_at);
'''))

MIGRATIONS.append((12, "windows-central-alerts-lab", r'''
ALTER TABLE agent_credentials ADD COLUMN IF NOT EXISTS scope varchar(30) NOT NULL DEFAULT 'agent';
CREATE TABLE IF NOT EXISTS windows_monitors (
 agent_id uuid PRIMARY KEY REFERENCES agents(id), schedule_id uuid NOT NULL,
 schedule jsonb NOT NULL, paused boolean NOT NULL DEFAULT false,
 effective_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS windows_results (
 agent_id uuid NOT NULL REFERENCES agents(id), execution_id uuid NOT NULL,
 started_at timestamptz NOT NULL, finished_at timestamptz NOT NULL,
 received_at timestamptz NOT NULL DEFAULT now(), payload jsonb NOT NULL,
 payload_hash varchar(64) NOT NULL, PRIMARY KEY(agent_id,execution_id)
);
CREATE TABLE IF NOT EXISTS protected_points (
 agent_id uuid NOT NULL REFERENCES agents(id), binding_id uuid NOT NULL,
 snapshot_a varchar(64) NOT NULL, snapshot_b varchar(64) NOT NULL,
 confirmed_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(agent_id,binding_id,snapshot_a)
);
CREATE TABLE IF NOT EXISTS central_alerts (
 id uuid PRIMARY KEY, agent_id uuid NOT NULL REFERENCES agents(id),
 kind varchar(30) NOT NULL, occurrence_key varchar(240) NOT NULL,
 detail jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
 resolved_at timestamptz, acknowledged_at timestamptz, acknowledged_by varchar(200),
 delivery_state varchar(20) NOT NULL DEFAULT 'pending', delivered_at timestamptz,
 UNIQUE(agent_id,kind,occurrence_key)
);
'''))

MIGRATIONS.append((13, "supervised-lab-automation", r'''
ALTER TABLE installation_preparations ADD COLUMN IF NOT EXISTS reader_public_key text;
ALTER TABLE installation_preparations ADD COLUMN IF NOT EXISTS lab_seed jsonb;
ALTER TABLE provisioning_operations ADD COLUMN IF NOT EXISTS work_key varchar(200);
ALTER TABLE provisioning_operations ADD COLUMN IF NOT EXISTS work jsonb;
ALTER TABLE provisioning_operations DROP CONSTRAINT IF EXISTS provisioning_operations_kind_check;
ALTER TABLE provisioning_operations ADD CONSTRAINT provisioning_operations_kind_check
 CHECK(kind IN ('prepare_primary','retry_primary','protect_installation','copy_points'));
CREATE INDEX IF NOT EXISTS ix_provisioning_work ON provisioning_operations(agent_id,work_key,created_at);
CREATE TABLE IF NOT EXISTS lab_worker_heartbeats (
 role varchar(20) PRIMARY KEY CHECK(role IN ('primary','protection','alerts')),
 last_seen_at timestamptz NOT NULL DEFAULT now(), state varchar(20) NOT NULL
 CHECK(state IN ('ready','busy','retry'))
);
'''))

MIGRATIONS.append((14, "lab-retention-capacity", r'''
ALTER TABLE provisioning_operations DROP CONSTRAINT IF EXISTS provisioning_operations_kind_check;
ALTER TABLE provisioning_operations ADD CONSTRAINT provisioning_operations_kind_check
 CHECK(kind IN ('prepare_primary','retry_primary','protect_installation','copy_points','storage_a','storage_b'));
CREATE TABLE IF NOT EXISTS lab_storage_settings (
 agent_id uuid PRIMARY KEY REFERENCES agents(id), policy jsonb NOT NULL DEFAULT '{}',
 capacity_a jsonb, capacity_b jsonb
);
CREATE TABLE IF NOT EXISTS lab_storage_audit (
 id uuid PRIMARY KEY, agent_id uuid NOT NULL REFERENCES agents(id),
 actor varchar(240) NOT NULL, kind varchar(30) NOT NULL, detail jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now()
);
'''))

MIGRATIONS.append((15, "assisted-installation-operations", r'''
ALTER TABLE installation_preparations ADD COLUMN IF NOT EXISTS lifecycle varchar(20) NOT NULL DEFAULT 'active';
ALTER TABLE installation_preparations ADD COLUMN IF NOT EXISTS device_epoch integer NOT NULL DEFAULT 1 CHECK(device_epoch>0);
ALTER TABLE installation_preparations ADD COLUMN IF NOT EXISTS current_device uuid;
ALTER TABLE installation_preparations ADD COLUMN IF NOT EXISTS access_state varchar(20) NOT NULL DEFAULT 'none';
ALTER TABLE installation_preparations DROP CONSTRAINT IF EXISTS installation_lifecycle_check;
ALTER TABLE installation_preparations ADD CONSTRAINT installation_lifecycle_check CHECK(lifecycle IN ('active','replacing','awaiting_device','closing','closed','reopening'));
ALTER TABLE installation_preparations DROP CONSTRAINT IF EXISTS installation_access_check;
ALTER TABLE installation_preparations ADD CONSTRAINT installation_access_check CHECK(access_state IN ('none','requested','confirmed','failed'));
ALTER TABLE agent_credentials ADD COLUMN IF NOT EXISTS device_epoch integer NOT NULL DEFAULT 1;
ALTER TABLE activation_codes ADD COLUMN IF NOT EXISTS device_epoch integer NOT NULL DEFAULT 1;
UPDATE installation_preparations p SET current_device=(SELECT c.device_id FROM activation_codes c
 WHERE c.agent_id=p.agent_id AND c.state='delivered' AND c.revoked_at IS NULL
 ORDER BY c.delivered_at DESC NULLS LAST,c.created_at DESC LIMIT 1) WHERE p.current_device IS NULL;
ALTER TABLE windows_results ADD COLUMN IF NOT EXISTS device_epoch integer NOT NULL DEFAULT 1;
ALTER TABLE windows_monitors ADD COLUMN IF NOT EXISTS device_epoch integer NOT NULL DEFAULT 1;
ALTER TABLE provisioning_operations ADD COLUMN IF NOT EXISTS result jsonb;
ALTER TABLE provisioning_operations DROP CONSTRAINT IF EXISTS provisioning_operations_kind_check;
ALTER TABLE provisioning_operations ADD CONSTRAINT provisioning_operations_kind_check
 CHECK(kind IN ('prepare_primary','retry_primary','protect_installation','copy_points','storage_a','storage_b','lifecycle_a','recovery_b'));
CREATE UNIQUE INDEX IF NOT EXISTS uq_assisted_request ON provisioning_operations(agent_id,work_key)
 WHERE kind IN ('lifecycle_a','recovery_b');
'''))

_ADVISORY_KEY = 727_203_001


class MigrationError(RuntimeError):
    pass


def run_migrations(connection: Connection) -> list[int]:
    """Aplica migrações pendentes; devolve versões aplicadas agora."""
    applied: list[int] = []
    connection.execute(
        text(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version integer PRIMARY KEY, name varchar(200) NOT NULL, "
            "applied_at timestamptz NOT NULL DEFAULT now())"
        )
    )
    done = {
        row[0]
        for row in connection.execute(text("SELECT version FROM schema_migrations"))
    }
    max_supported = MIGRATIONS[-1][0]
    unknown = sorted(v for v in done if v > max_supported or v < 1)
    if unknown:
        raise MigrationError(
            f"schema em versão não suportada por este código: {unknown} "
            f"(suportadas: 1..{max_supported}) — banco preservado"
        )
    for version, name, ddl in MIGRATIONS:
        if version in done:
            continue
        connection.execute(text(ddl))
        connection.execute(
            text("INSERT INTO schema_migrations (version, name) VALUES (:v, :n)"),
            {"v": version, "n": name},
        )
        applied.append(version)
    return applied


def migrate(engine: Engine) -> list[int]:
    """Advisory lock de sessão + transação — seguro contra concorrência."""
    with engine.connect() as connection:
        connection.execute(text("SELECT pg_advisory_lock(:k)"), {"k": _ADVISORY_KEY})
        connection.commit()  # lock é de sessão — persiste fora da txn
        try:
            with connection.begin():
                return run_migrations(connection)
        finally:
            connection.execute(
                text("SELECT pg_advisory_unlock(:k)"), {"k": _ADVISORY_KEY}
            )
            connection.commit()
