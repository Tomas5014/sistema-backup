"""Fila local de recuperação: poll curto separado do worker Restic longo."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import time
import uuid

from .agent import acquire_task_lock, cache_directory, install_cancel_handlers, restore_signal_handlers, task_lock_directory
from .central import load_central
from .cli import _destination_by_id, _safe_restore_target
from .credentials import CredentialError, credential_from_environment_or_os
from .distribution import _request
from .models import ConfigurationError, TaskSpec
from .orchestrator import password_from_environment
from .restic import ResticAdapter, ResticCancelledError, ResticError, ResticTimeoutError


# Adiamento temporário de operação inelegível (tarefa ocupada, falha
# transitória de comunicação, resposta ambígua de claim). Persistido em
# deferred_until para sobreviver a reinícios e impedir que a mesma operação
# monopolize rodadas seguidas.
DEFERRED_SECONDS = 30
# Marcador local de bloqueio definitivo (conflito de claim, contrato
# inválido, falha de autorização): valor alto enough para nunca ser selecionado
# novamente, mas finito e diagnosticável no banco local.
_BLOCKED = 1 << 62
# Orçamento de descoberta: requisições GET por rodada de poll. Cada requisição
# traz até DISCOVERY_PAGE_SIZE operações; o cursor persiste entre rodadas.
DISCOVERY_PAGE_SIZE = 100
DISCOVERY_BUDGET = 5


def _now():
    return int(time.time())


def _bond(spec):
    return hashlib.sha256(f'{spec.url}\0{spec.agent_id}'.encode()).hexdigest()


def _locks(spec, role):
    directory = task_lock_directory()
    directory.mkdir(parents=True, exist_ok=True)
    files = []
    try:
        for name, mode in [('maintenance.lock', fcntl.LOCK_SH),
                           (f'recovery-{role}-{spec.agent_id}.lock', fcntl.LOCK_EX)]:
            stream = (directory / name).open('a')
            files.append(stream)
            fcntl.flock(stream, mode | fcntl.LOCK_NB)
        return files
    except (OSError, BlockingIOError):
        for stream in files:
            stream.close()
        raise ResticError('manutenção ou outra operação de recuperação em andamento') from None


def _database(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=10)
    os.chmod(path, 0o600)
    db.row_factory = sqlite3.Row
    if db.execute('PRAGMA user_version').fetchone()[0] not in (0, 1, 2, 3):
        db.close()
        raise ConfigurationError('esquema da fila de recuperação não suportado')
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('PRAGMA synchronous=FULL')
    db.execute('''CREATE TABLE IF NOT EXISTS recovery_queue (
      bond TEXT NOT NULL, id TEXT NOT NULL, claim TEXT NOT NULL,
      payload TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
      event TEXT, sent INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(bond,id))''')
    columns = {row[1] for row in db.execute('PRAGMA table_info(recovery_queue)').fetchall()}
    # v1->v2: adiamento persistente por operação.
    if 'deferred_until' not in columns:
        db.execute('ALTER TABLE recovery_queue ADD COLUMN deferred_until INTEGER NOT NULL DEFAULT 0')
    # v2->v3: seleção justa. evaluated_at registra quando a operação foi
    # avaliada pela última vez; ORDER BY evaluated_at, rowid garante que
    # operações nunca avaliadas (0) ou avaliadas há mais tempo sejam
    # selecionadas primeiro, independentemente do intervalo entre rodadas.
    if 'evaluated_at' not in columns:
        db.execute('ALTER TABLE recovery_queue ADD COLUMN evaluated_at INTEGER NOT NULL DEFAULT 0')
    db.execute('''CREATE TABLE IF NOT EXISTS discovery_cursor (
      bond TEXT NOT NULL PRIMARY KEY, cursor TEXT)''')
    db.execute('PRAGMA user_version=3')
    db.commit()
    return db


def _validate_payload(operation, spec):
    if 'target' in operation or 'restore_root' in operation:
        raise ConfigurationError('caminho remoto de recuperação não é permitido')
    if (str(uuid.UUID(operation['id'])) != operation['id']
            or operation['agent_id'] != spec.agent_id
            or operation.get('contract_version') != 1
            or operation['kind'] not in ('snapshots', 'restore')):
        raise ConfigurationError('operação de recuperação inválida')
    config = operation['config']
    digest = hashlib.sha256(json.dumps(config, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    if digest != operation['config_hash']:
        raise ConfigurationError('hash da configuração de recuperação inválido')
    task = TaskSpec.from_dict(config)
    if task.id != operation['task_id'] or task.device_id != spec.agent_id:
        raise ConfigurationError('identidade da recuperação inválida')
    task.validate_recovery_for_execution()
    _destination_by_id(task, operation['destination_id'])
    return task


def _validate(operation, spec):
    try:
        return _validate_payload(operation, spec)
    except (KeyError, TypeError, ValueError):
        raise ConfigurationError('contrato de operação inválido') from None


def _get_cursor(db, bond):
    row = db.execute('SELECT cursor FROM discovery_cursor WHERE bond=?', (bond,)).fetchone()
    return row[0] if row else None


def _set_cursor(db, bond, cursor):
    db.execute('INSERT OR REPLACE INTO discovery_cursor(bond,cursor) VALUES(?,?)', (bond, cursor))


def poll(central, queue):
    spec = load_central(central)
    locks = _locks(spec, 'poll')
    db = None
    summary = {'received': 0, 'sent': 0, 'blocked': 0, 'retry': 0}
    try:
        db = _database(queue)
        bond = _bond(spec)
        token = credential_from_environment_or_os(spec.token_environment)
        # Resultados são duráveis e enviados independentemente do worker.
        for row in db.execute('SELECT * FROM recovery_queue WHERE bond=? AND event IS NOT NULL AND sent=0', (bond,)).fetchall():
            status, ack = _request(spec, 'POST', f"/api/v1/agent/recovery/{row['id']}/result", token, json.loads(row['event']))
            event = json.loads(row['event'])
            if status == 200 and isinstance(ack, dict) and ack.get('id') == row['id'] and ack.get('state') == event['state']:
                db.execute('UPDATE recovery_queue SET sent=1 WHERE bond=? AND id=?', (bond, row['id']))
                summary['sent'] += 1
            elif status in (400, 403, 404, 409, 422):
                db.execute('UPDATE recovery_queue SET sent=-1 WHERE bond=? AND id=?', (bond, row['id']))
                summary['blocked'] += 1
            else:
                summary['retry'] += 1
            db.commit()
        # Descoberta paginada: avança além das primeiras 100 operações com
        # cursor persistente e orçamento por rodada. Retorna ao início ao
        # concluir uma passagem (next_cursor=None). Deduplicação local
        # preserva operações já conhecidas.
        cursor = _get_cursor(db, bond)
        for _ in range(DISCOVERY_BUDGET):
            path = f'/api/v1/agent/recovery?limit={DISCOVERY_PAGE_SIZE}'
            if cursor:
                path += f'&after={cursor}'
            status, body = _request(spec, 'GET', path, token)
            if status != 200 or not isinstance(body, dict) or not isinstance(body.get('operations'), list):
                summary['retry'] += 1
                break
            for operation in body['operations']:
                _validate(operation, spec)
                inserted = db.execute('INSERT OR IGNORE INTO recovery_queue(bond,id,claim,payload) VALUES(?,?,?,?)',
                                      (bond, operation['id'], str(uuid.uuid4()), json.dumps(operation)))
                summary['received'] += inserted.rowcount
            db.commit()
            next_cursor = body.get('next_cursor')
            if not next_cursor:
                cursor = None
                break
            cursor = next_cursor
        _set_cursor(db, bond, cursor)
        db.commit()
        return summary
    finally:
        if db is not None:
            db.close()
        for stream in locks:
            stream.close()


def _finish(db, row, state, result=None, error=None):
    event = {'claim_id': row['claim'], 'state': state, 'result': result, 'error': error}
    db.execute('UPDATE recovery_queue SET state=?,event=?,sent=0 WHERE bond=? AND id=?',
               (state, json.dumps(event), row['bond'], row['id']))
    db.commit()


def _defer(db, row, until):
    """Adiamento persistente: a operação permanece pending mas é excluída da
    seleção enquanto ``deferred_until > now``. ``until=0`` reabilita.
    ``evaluated_at`` é atualizado para seleção justa: operações avaliadas
    há mais tempo (ou nunca) são selecionadas primeiro na próxima rodada."""
    db.execute('UPDATE recovery_queue SET deferred_until=?,evaluated_at=? WHERE bond=? AND id=?',
               (until, _now(), row['bond'], row['id']))
    db.commit()


def _catalog(adapter, task, destination, password):
    snapshots = adapter.snapshots(destination, password, timeout_seconds=task.timeouts.snapshots_seconds)
    points = []
    for snapshot in snapshots:
        tags = snapshot.get('tags') or []
        if f'task:{task.id}' not in tags:
            continue
        if not re.fullmatch(r'[0-9a-f]{64}', snapshot.get('id', '')):
            raise ResticError('catálogo inválido')
        point = {'id': snapshot['id'], 'tags': [f'task:{task.id}']}
        for field in ('time', 'hostname'):
            if field in snapshot:
                point[field] = snapshot[field]
        points.append(point)
    if len(points) > 1000:
        raise ResticError('catálogo excede limite de 1000 pontos')
    return points


def _root(policy):
    document = json.loads(Path(policy).read_text())
    if not isinstance(document, dict) or set(document) != {'restore_root'} or not isinstance(document['restore_root'], str):
        raise ConfigurationError('política local de recuperação inválida')
    root = Path(document['restore_root'])
    if not root.is_absolute() or root == Path('/') or root.resolve() != root:
        raise ConfigurationError('raiz de recuperação inválida ou com links')
    info = root.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ConfigurationError('raiz de recuperação exige dono atual e modo privado')
    return root


def work(central, queue, policy, limit=1):
    # ``limit`` = número máximo de operações selecionadas por rodada para
    # avaliação (lock de tarefa + claim). Operações adiadas (tarefa ocupada,
    # falha transitória) são excluídas da seleção por deferred_until, então
    # um item inelegível nunca monopoliza todas as rodadas — inclusive com
    # limit=1, a próxima rodada seleciona outra operação elegível.
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1 or limit > 100:
        raise ConfigurationError('limite do worker deve ser 1..100')
    spec = load_central(central)
    locks = _locks(spec, 'worker')
    db = None
    summary = {'completed': 0, 'deferred': 0, 'blocked': 0, 'retry': 0}
    try:
        db = _database(queue)
        bond = _bond(spec)
        # Nunca retomar efeito que pode ter ocorrido antes de queda/reboot.
        for row in db.execute("SELECT * FROM recovery_queue WHERE bond=? AND state='running'", (bond,)).fetchall():
            _finish(db, row, 'interrupted', error='abandoned')
        token = credential_from_environment_or_os(spec.token_environment)
        now = _now()
        rows = db.execute(
            "SELECT * FROM recovery_queue WHERE bond=? AND state='pending' AND deferred_until<=? "
            "ORDER BY evaluated_at, rowid LIMIT ?", (bond, now, limit)).fetchall()
        for row in rows:
            operation = json.loads(row['payload'])
            # Contrato inválido é bloqueio definitivo local: não aborta o
            # lote e não tenta claim (a API recusaria resultado de operação
            # não iniciada). Permanece diagnosticável no banco local.
            try:
                task = _validate(operation, spec)
            except ConfigurationError:
                _defer(db, row, _BLOCKED)
                summary['blocked'] += 1
                continue
            # Tarefa ocupada (lock EX de outra execução) adia sem abortar o
            # lote: outra operação de tarefa elegível prossegue na mesma rodada.
            try:
                task_locks = acquire_task_lock(task.id)
            except ResticError:
                _defer(db, row, now + DEFERRED_SECONDS)
                summary['deferred'] += 1
                continue
            adapter = ResticAdapter(timeouts=task.timeouts, cache_directory=cache_directory())
            handlers = {}
            target = None
            try:
                status, ack = _request(spec, 'POST', f"/api/v1/agent/recovery/{row['id']}/claim", token, {'claim_id': row['claim']})
                # 409 = conflito confirmado de claim de outro executor: não
                # tomar o claim, não repetir restauração. Bloqueio local
                # permanente. O claim_id local é preservado para diagnóstico.
                if status == 409:
                    _defer(db, row, _BLOCKED)
                    summary['blocked'] += 1
                    continue
                # Resposta ambígua/incompatível NUNCA autoriza efeito Restic.
                # Uma resposta ambígua (200 com ack malformado) não comprova
                # conflito: a API pode ter registrado o claim corretamente
                # antes de a resposta ser perdida ou alterada no transporte.
                # Adia e reenvia o MESMO claim_id (idempotente na API).
                # Transitória (0/429/5xx) também adia.
                # Definitiva sem ambiguidade (401/403/404/400/422) bloqueia.
                if (status != 200 or not isinstance(ack, dict)
                        or ack.get('id') != row['id'] or ack.get('claim_id') != row['claim']
                        or ack.get('state') != 'running'):
                    if status == 0 or status == 429 or 500 <= status < 600 or status == 200:
                        _defer(db, row, now + DEFERRED_SECONDS)
                        summary['retry'] += 1
                    else:
                        _defer(db, row, _BLOCKED)
                        summary['blocked'] += 1
                    continue
                db.execute("UPDATE recovery_queue SET state='running',deferred_until=0 WHERE bond=? AND id=?", (bond, row['id']))
                db.commit()
                handlers = install_cancel_handlers(adapter)
                destination = _destination_by_id(task, operation['destination_id'])
                destination.validate_for_execution()
                password = password_from_environment(destination)
                points = _catalog(adapter, task, destination, password)
                if operation['kind'] == 'snapshots':
                    result = {'snapshots': points}
                else:
                    if operation.get('snapshot_id') not in {p['id'] for p in points}:
                        _finish(db, row, 'failed', error='snapshot_missing')
                        summary['completed'] += 1
                        continue
                    root = _root(policy)
                    target = _safe_restore_target(task, destination, root / row['id'])
                    for other in task.destinations:
                        _safe_restore_target(task, other, target)
                    # Claim local de pasta: nunca reutilizar um diretório, inclusive parcial.
                    target.mkdir(mode=0o700, exist_ok=False)
                    if target.is_symlink() or target.resolve() != target or root.resolve() != root:
                        raise ConfigurationError('pasta de recuperação alterada')
                    adapter.restore(destination, password, operation['snapshot_id'], target,
                                    timeout_seconds=task.timeouts.restore_seconds)
                    result = {'target': str(target)}
                if adapter.cancelled:
                    raise ResticCancelledError('interrupted')
                _finish(db, row, 'succeeded', result=result)
            except CredentialError:
                _finish(db, row, 'failed', error='credential_missing')
            except ResticCancelledError:
                _finish(db, row, 'interrupted', {'target': str(target)} if target else None, 'interrupted')
            except ResticTimeoutError:
                _finish(db, row, 'failed', {'target': str(target)} if target else None, 'timeout')
            except (ConfigurationError, OSError, ValueError):
                _finish(db, row, 'failed', {'target': str(target)} if target else None, 'invalid_target')
            except ResticError:
                _finish(db, row, 'failed', {'target': str(target)} if target else None, 'repository_error')
            finally:
                if handlers:
                    restore_signal_handlers(handlers)
                for stream in task_locks:
                    stream.close()
            summary['completed'] += 1
            if adapter.cancelled:
                break
        return summary
    finally:
        if db is not None:
            db.close()
        for stream in locks:
            stream.close()
