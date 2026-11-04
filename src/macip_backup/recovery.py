"""Fila local de recuperação: poll curto separado do worker Restic longo."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import uuid

from .agent import acquire_task_lock, cache_directory, install_cancel_handlers, restore_signal_handlers, task_lock_directory
from .central import load_central
from .cli import _destination_by_id, _safe_restore_target
from .credentials import CredentialError, credential_from_environment_or_os
from .distribution import _request
from .models import ConfigurationError, TaskSpec
from .orchestrator import password_from_environment
from .restic import ResticAdapter, ResticCancelledError, ResticError, ResticTimeoutError


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
    if db.execute('PRAGMA user_version').fetchone()[0] not in (0, 1):
        db.close()
        raise ConfigurationError('esquema da fila de recuperação não suportado')
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('PRAGMA synchronous=FULL')
    db.execute('''CREATE TABLE IF NOT EXISTS recovery_queue (
      bond TEXT NOT NULL, id TEXT NOT NULL, claim TEXT NOT NULL,
      payload TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
      event TEXT, sent INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(bond,id))''')
    db.execute('PRAGMA user_version=1')
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
        status, body = _request(spec, 'GET', '/api/v1/agent/recovery', token)
        if status != 200 or not isinstance(body, dict) or not isinstance(body.get('operations'), list):
            summary['retry'] += 1
            return summary
        for operation in body['operations']:
            _validate(operation, spec)
            cursor = db.execute('INSERT OR IGNORE INTO recovery_queue(bond,id,claim,payload) VALUES(?,?,?,?)',
                                (bond, operation['id'], str(uuid.uuid4()), json.dumps(operation)))
            summary['received'] += cursor.rowcount
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
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1 or limit > 100:
        raise ConfigurationError('limite do worker deve ser 1..100')
    spec = load_central(central)
    locks = _locks(spec, 'worker')
    db = None
    summary = {'completed': 0, 'deferred': 0}
    try:
        db = _database(queue)
        bond = _bond(spec)
        # Nunca retomar efeito que pode ter ocorrido antes de queda/reboot.
        for row in db.execute("SELECT * FROM recovery_queue WHERE bond=? AND state='running'", (bond,)).fetchall():
            _finish(db, row, 'interrupted', error='abandoned')
        token = credential_from_environment_or_os(spec.token_environment)
        rows = db.execute("SELECT * FROM recovery_queue WHERE bond=? AND state='pending' ORDER BY rowid LIMIT ?", (bond, limit)).fetchall()
        for row in rows:
            operation = json.loads(row['payload'])
            task = _validate(operation, spec)
            task_locks = acquire_task_lock(task.id)
            adapter = ResticAdapter(timeouts=task.timeouts, cache_directory=cache_directory())
            handlers = {}
            target = None
            try:
                status, ack = _request(spec, 'POST', f"/api/v1/agent/recovery/{row['id']}/claim", token, {'claim_id': row['claim']})
                if status != 200 or not isinstance(ack, dict) or ack.get('id') != row['id'] or ack.get('claim_id') != row['claim'] or ack.get('state') != 'running':
                    summary['deferred'] += 1
                    continue
                db.execute("UPDATE recovery_queue SET state='running' WHERE bond=? AND id=?", (bond, row['id']))
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
