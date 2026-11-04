"""Fila real SQLite; API e motor simulados para falhas controladas."""
import fcntl
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
import uuid
from unittest.mock import patch

from macip_backup import recovery
from macip_backup.restic import ResticCancelledError, ResticError


class Engine:
    cancelled = False
    cancel_cleanup_error = None

    def __init__(self):
        self.points = []
        self.restores = 0
        self.interrupt = False

    def snapshots(self, *args, **kwargs):
        return self.points

    def initialize(self, *args, **kwargs):
        raise AssertionError('recuperação jamais inicializa repositório')

    def restore(self, destination, password, snapshot, target, **kwargs):
        self.restores += 1
        (target / 'restored.txt').write_text('partial' if self.interrupt else 'restored')
        if self.interrupt:
            raise ResticCancelledError('synthetic interruption')


class RecoveryWorkerTests(unittest.TestCase):
    def test_poll_proceeds_while_restore_is_running(self):
        operation = self.operation()
        self.enqueue(operation)
        started, release = threading.Event(), threading.Event()
        errors = []
        original = self.adapter.restore
        def blocked(*args, **kwargs):
            started.set()
            if not release.wait(5):
                raise AssertionError('test restore not released')
            return original(*args, **kwargs)
        def worker():
            try:
                self.work()
            except BaseException as error:
                errors.append(error)
        with patch.object(self.adapter, 'restore', side_effect=blocked), patch.object(
            recovery, 'install_cancel_handlers', return_value={}
        ):
            thread = threading.Thread(target=worker)
            thread.start()
            try:
                self.assertTrue(started.wait(3))
                self.assertEqual(recovery.poll(self.central, self.queue)['received'], 0)
                self.assertTrue(thread.is_alive())
            finally:
                release.set()
                thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='recovery worker with spaces ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.agent = str(uuid.uuid4())
        self.task = str(uuid.uuid4())
        self.central = self.root / 'central.json'
        self.central.write_text(json.dumps({'url': 'http://127.0.0.1:65534', 'agent_id': self.agent,
                                             'token_environment': 'RECOVERY_TOKEN'}))
        self.queue = self.root / 'recovery queue.db'
        self.restore_root = self.root / 'authorized root'
        self.restore_root.mkdir(mode=0o700)
        self.policy = self.root / 'local policy.json'
        self.policy.write_text(json.dumps({'restore_root': str(self.restore_root)}))
        self.config = {
            'id': self.task, 'name': 'Synthetic', 'client': {'id': 'c', 'name': 'C'},
            'device_id': self.agent, 'sources': [str(self.root / 'original')],
            'exclusions': ['*.tmp'], 'destinations': [{'id': 'local', 'name': 'Local',
                'kind': 'local', 'repository': str(self.root / 'repository'),
                'password_environment': 'RECOVERY_PASSWORD'}],
        }
        self.remote = []
        self.events = []
        self.drop_result = False
        self.claim_refuse = {}
        self.adapter = Engine()
        self.adapter.points = [{'id': 'a' * 64, 'tags': [f'task:{self.task}'],
                                'time': '2026-11-04T12:00:00Z'}]
        environment = patch.dict(os.environ, {'MACIP_HOME': str(self.root / 'program'),
            'MACIP_STATE': str(self.root / 'state'), 'MACIP_LOCK_DIR': str(self.root / 'locks'),
            'MACIP_CACHE': str(self.root / 'cache'), 'RECOVERY_TOKEN': 'synthetic-token',
            'RECOVERY_PASSWORD': 'synthetic-password'})
        environment.start()
        self.addCleanup(environment.stop)
        request = patch.object(recovery, '_request', side_effect=self.request)
        request.start()
        self.addCleanup(request.stop)
        engine = patch.object(recovery, 'ResticAdapter', return_value=self.adapter)
        engine.start()
        self.addCleanup(engine.stop)

    def operation(self, kind='restore'):
        config = json.loads(json.dumps(self.config))
        operation = {'id': str(uuid.uuid4()), 'agent_id': self.agent, 'task_id': self.task,
                     'contract_version': 1, 'kind': kind, 'destination_id': 'local',
                     'revision': 1, 'config': config,
                     'config_hash': hashlib.sha256(json.dumps(config, sort_keys=True, separators=(',', ':')).encode()).hexdigest()}
        if kind == 'restore':
            operation.update(snapshot_id='a' * 64, query_id=str(uuid.uuid4()))
        return operation

    def request(self, spec, method, path, token, payload=None):
        self.assertEqual(token, 'synthetic-token')
        if method == 'GET':
            return 200, {'operations': self.remote}
        operation_id = path.split('/')[-2]
        if path.endswith('/claim'):
            if operation_id in self.claim_refuse:
                return self.claim_refuse[operation_id]
            return 200, {'id': operation_id, 'claim_id': payload['claim_id'], 'state': 'running'}
        self.events.append((operation_id, payload))
        if self.drop_result:
            self.drop_result = False
            return 0, None
        return 200, {'id': operation_id, 'state': payload['state']}

    def enqueue(self, operation):
        self.remote = [operation]
        self.assertEqual(recovery.poll(self.central, self.queue)['received'], 1)

    def enqueue_many(self, operations):
        self.remote = list(operations)
        self.assertEqual(recovery.poll(self.central, self.queue)['received'], len(operations))

    def config_for(self, task_id):
        config = json.loads(json.dumps(self.config))
        config['id'] = task_id
        return config

    def operation_for(self, task_id, kind='restore'):
        config = self.config_for(task_id)
        operation = {'id': str(uuid.uuid4()), 'agent_id': self.agent, 'task_id': task_id,
                     'contract_version': 1, 'kind': kind, 'destination_id': 'local',
                     'revision': 1, 'config': config,
                     'config_hash': hashlib.sha256(json.dumps(config, sort_keys=True, separators=(',', ':')).encode()).hexdigest()}
        if kind == 'restore':
            operation.update(snapshot_id='a' * 64, query_id=str(uuid.uuid4()))
        return operation

    def hold_task_lock(self, task_id):
        path = Path(os.environ['MACIP_LOCK_DIR']) / f'{task_id}.lock'
        stream = path.open('a')
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.addCleanup(stream.close)
        return stream

    def points_for(self, *task_ids):
        self.adapter.points = [{'id': 'a' * 64, 'tags': [f'task:{tid}'],
                                'time': '2026-11-04T12:00:00Z'} for tid in task_ids]

    def state_of(self, operation):
        with closing(sqlite3.connect(self.queue)) as db:
            row = db.execute('SELECT state,deferred_until FROM recovery_queue WHERE id=?',
                             (operation['id'],)).fetchone()
        return row

    def work_limit(self, limit):
        return recovery.work(self.central, self.queue, self.policy, limit=limit)

    def event(self, operation):
        with closing(sqlite3.connect(self.queue)) as db:
            row = db.execute('SELECT state,event FROM recovery_queue WHERE id=?', (operation['id'],)).fetchone()
        return row[0], json.loads(row[1]) if row[1] else None

    def work(self):
        return recovery.work(self.central, self.queue, self.policy)

    def test_lost_completion_response_replays_event_without_second_restore(self):
        operation = self.operation()
        self.enqueue(operation)
        self.work()
        self.assertEqual(self.adapter.restores, 1)
        self.drop_result = True
        self.assertEqual(recovery.poll(self.central, self.queue)['retry'], 1)
        self.assertEqual(recovery.poll(self.central, self.queue)['sent'], 1)
        self.work()
        self.assertEqual(self.adapter.restores, 1)
        self.assertEqual(self.events[0], self.events[1])
        self.assertEqual(self.event(operation)[0], 'succeeded')
        self.assertNotIn('synthetic-password', json.dumps(self.events))

    def test_catalog_filters_other_tasks_and_never_initializes(self):
        operation = self.operation('snapshots')
        self.adapter.points.append({'id': 'b' * 64, 'tags': ['task:another']})
        self.enqueue(operation)
        self.work()
        state, event = self.event(operation)
        self.assertEqual(state, 'succeeded')
        self.assertEqual([point['id'] for point in event['result']['snapshots']], ['a' * 64])
        self.assertEqual(self.adapter.restores, 0)

    def test_removed_snapshot_fails_before_target_creation(self):
        operation = self.operation()
        self.enqueue(operation)
        self.adapter.points = []
        self.work()
        self.assertEqual(self.event(operation)[1]['error'], 'snapshot_missing')
        self.assertEqual(self.adapter.restores, 0)
        self.assertFalse((self.restore_root / operation['id']).exists())

    def test_invalid_or_linked_authorized_root_fails_without_restoring(self):
        operation = self.operation()
        linked = self.root / 'linked root'
        linked.symlink_to(self.restore_root)
        for bad_root in (linked, self.restore_root / '..', Path('/')):
            with self.subTest(root=str(bad_root)):
                self.queue = self.root / f'queue-{uuid.uuid4()}.db'
                self.policy.write_text(json.dumps({'restore_root': str(bad_root)}))
                self.enqueue(operation)
                self.work()
                self.assertEqual(self.event(operation)[1]['error'], 'invalid_target')
        self.assertEqual(self.adapter.restores, 0)

    def test_overlaps_and_existing_directory_are_refused(self):
        for source in ('source', 'repository', 'existing'):
            with self.subTest(overlap=source):
                self.queue = self.root / f'queue-{uuid.uuid4()}.db'
                operation = self.operation()
                target = self.restore_root / operation['id']
                if source == 'source':
                    operation['config']['sources'] = [str(target / 'removed old source')]
                elif source == 'repository':
                    operation['config']['destinations'][0]['repository'] = str(target / 'repository')
                else:
                    target.mkdir()
                    (target / 'sentinel').write_text('preserve')
                operation['config_hash'] = hashlib.sha256(json.dumps(operation['config'], sort_keys=True, separators=(',', ':')).encode()).hexdigest()
                self.enqueue(operation)
                self.work()
                self.assertEqual(self.event(operation)[1]['error'], 'invalid_target')
                if source == 'existing':
                    self.assertEqual((target / 'sentinel').read_text(), 'preserve')
        self.assertEqual(self.adapter.restores, 0)

    def test_exclusive_maintenance_prevents_effects_and_preserves_queue(self):
        operation = self.operation()
        self.enqueue(operation)
        lock_path = Path(os.environ['MACIP_LOCK_DIR']) / 'maintenance.lock'
        with lock_path.open('a') as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(ResticError):
                self.work()
        self.assertEqual(self.event(operation), ('pending', None))
        self.assertEqual(self.adapter.restores, 0)
        self.assertFalse((self.restore_root / operation['id']).exists())

    def test_interruption_keeps_partial_folder_and_never_reuses_it(self):
        operation = self.operation()
        self.enqueue(operation)
        self.adapter.interrupt = True
        self.work()
        state, event = self.event(operation)
        self.assertEqual(state, 'interrupted')
        self.assertEqual(event['error'], 'interrupted')
        target = Path(event['result']['target'])
        self.assertEqual((target / 'restored.txt').read_text(), 'partial')
        self.adapter.interrupt = False
        self.work()
        self.assertEqual(self.adapter.restores, 1)

    def test_running_record_after_restart_is_abandoned_without_effect(self):
        operation = self.operation()
        self.enqueue(operation)
        target = self.restore_root / operation['id']
        target.mkdir()
        (target / 'partial').write_text('preserve')
        with closing(sqlite3.connect(self.queue)) as db:
            db.execute("UPDATE recovery_queue SET state='running' WHERE id=?", (operation['id'],))
            db.commit()
        self.work()
        self.assertEqual(self.event(operation)[0], 'interrupted')
        self.assertEqual(self.event(operation)[1]['error'], 'abandoned')
        self.assertEqual(self.adapter.restores, 0)
        self.assertEqual((target / 'partial').read_text(), 'preserve')

    def test_busy_task_defers_without_blocking_eligible_operation(self):
        task_a, task_b = str(uuid.uuid4()), str(uuid.uuid4())
        op_a = self.operation_for(task_a)
        op_b = self.operation_for(task_b)
        self.points_for(task_a, task_b)
        self.enqueue_many([op_a, op_b])
        self.hold_task_lock(task_a)
        summary = self.work_limit(100)
        self.assertEqual(summary['completed'], 1)
        self.assertEqual(summary['deferred'], 1)
        self.assertEqual(self.event(op_b)[0], 'succeeded')
        self.assertEqual(self.state_of(op_a)[0], 'pending')
        self.assertEqual(self.adapter.restores, 1)

    def test_busy_task_with_limit_one_progresses_across_successive_rounds(self):
        task_a, task_b = str(uuid.uuid4()), str(uuid.uuid4())
        op_a = self.operation_for(task_a)
        op_b = self.operation_for(task_b)
        self.points_for(task_a, task_b)
        self.enqueue_many([op_a, op_b])
        self.hold_task_lock(task_a)
        first = self.work_limit(1)
        self.assertEqual(first, {'completed': 0, 'deferred': 1, 'blocked': 0, 'retry': 0})
        self.assertEqual(self.state_of(op_a)[0], 'pending')
        second = self.work_limit(1)
        self.assertEqual(second['completed'], 1)
        self.assertEqual(self.event(op_b)[0], 'succeeded')
        self.assertEqual(self.state_of(op_a)[0], 'pending')

    def test_claim_conflict_does_not_monopolize_first_position(self):
        task_a, task_b = str(uuid.uuid4()), str(uuid.uuid4())
        op_a = self.operation_for(task_a)
        op_b = self.operation_for(task_b)
        self.points_for(task_a, task_b)
        self.enqueue_many([op_a, op_b])
        self.claim_refuse[op_a['id']] = (409, None)
        with closing(sqlite3.connect(self.queue)) as db:
            original_claim = db.execute('SELECT claim FROM recovery_queue WHERE id=?',
                                        (op_a['id'],)).fetchone()[0]
        first = self.work_limit(1)
        self.assertEqual(first['blocked'], 1)
        self.assertEqual(first['completed'], 0)
        with closing(sqlite3.connect(self.queue)) as db:
            row = db.execute('SELECT claim,state FROM recovery_queue WHERE id=?', (op_a['id'],)).fetchone()
        self.assertEqual(row[0], original_claim)
        self.assertEqual(row[1], 'pending')
        second = self.work_limit(1)
        self.assertEqual(second['completed'], 1)
        self.assertEqual(self.event(op_b)[0], 'succeeded')

    def test_restart_between_rounds_preserves_deferral(self):
        task_a, task_b = str(uuid.uuid4()), str(uuid.uuid4())
        op_a = self.operation_for(task_a)
        op_b = self.operation_for(task_b)
        self.points_for(task_a, task_b)
        self.enqueue_many([op_a, op_b])
        self.hold_task_lock(task_a)
        first = self.work_limit(1)
        self.assertEqual(first['deferred'], 1)
        with closing(sqlite3.connect(self.queue)) as db:
            deferred_until = db.execute('SELECT deferred_until FROM recovery_queue WHERE id=?',
                                        (op_a['id'],)).fetchone()[0]
        self.assertGreater(deferred_until, 0)
        second = self.work_limit(1)
        self.assertEqual(second['completed'], 1)
        self.assertEqual(self.event(op_b)[0], 'succeeded')
        self.assertEqual(self.state_of(op_a)[0], 'pending')

    def test_busy_task_runs_again_after_deferral_expires_and_lock_released(self):
        task_a = str(uuid.uuid4())
        op_a = self.operation_for(task_a)
        self.points_for(task_a)
        self.enqueue(op_a)
        holder = self.hold_task_lock(task_a)
        with patch.object(recovery, 'DEFERRED_SECONDS', 10), patch.object(recovery, '_now', return_value=1000):
            self.assertEqual(self.work_limit(1)['deferred'], 1)
        holder.close()
        with patch.object(recovery, '_now', return_value=1011):
            summary = self.work_limit(1)
        self.assertEqual(summary['completed'], 1)
        self.assertEqual(self.event(op_a)[0], 'succeeded')
        self.assertEqual(self.adapter.restores, 1)

    def test_transient_communication_failure_stays_recoverable(self):
        op = self.operation()
        self.enqueue(op)
        self.claim_refuse[op['id']] = (0, None)
        with patch.object(recovery, 'DEFERRED_SECONDS', 10), patch.object(recovery, '_now', return_value=1000):
            summary = self.work_limit(1)
        self.assertEqual(summary['retry'], 1)
        self.assertEqual(summary['completed'], 0)
        self.assertEqual(self.adapter.restores, 0)
        self.assertEqual(self.state_of(op)[0], 'pending')
        del self.claim_refuse[op['id']]
        with patch.object(recovery, '_now', return_value=1011):
            summary = self.work_limit(1)
        self.assertEqual(summary['completed'], 1)
        self.assertEqual(self.event(op)[0], 'succeeded')
        self.assertEqual(self.adapter.restores, 1)

    def test_ambiguous_claim_response_starts_no_restic_effect(self):
        op = self.operation()
        self.enqueue(op)
        self.claim_refuse[op['id']] = (200, {'id': op['id'], 'claim_id': 'not-the-claim', 'state': 'running'})
        summary = self.work_limit(1)
        self.assertEqual(summary['blocked'], 1)
        self.assertEqual(summary['completed'], 0)
        self.assertEqual(self.adapter.restores, 0)
        self.assertEqual(self.state_of(op)[0], 'pending')

    def test_no_eligible_operations_terminates_quickly(self):
        task_a = str(uuid.uuid4())
        op_a = self.operation_for(task_a)
        self.enqueue(op_a)
        self.hold_task_lock(task_a)
        with patch.object(recovery, 'DEFERRED_SECONDS', 10), patch.object(recovery, '_now', return_value=1000):
            self.assertEqual(self.work_limit(1)['deferred'], 1)
            summary = self.work_limit(1)
        self.assertEqual(summary, {'completed': 0, 'deferred': 0, 'blocked': 0, 'retry': 0})
