"""Migração atômica da fila de recuperação: SQLite real, falhas injetadas."""
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
import uuid
from unittest.mock import patch

from macip_backup import recovery
from macip_backup.models import ConfigurationError


def _env(d):
    return {'MACIP_HOME': str(d / 'h'), 'MACIP_STATE': str(d / 's'),
            'MACIP_LOCK_DIR': str(d / 'l'), 'MACIP_CACHE': str(d / 'c')}


def _build_v1(db, bond, n=3):
    db.execute('CREATE TABLE recovery_queue (bond TEXT, id TEXT, claim TEXT, '
               'payload TEXT, state TEXT DEFAULT "pending", event TEXT, '
               'sent INTEGER DEFAULT 0, PRIMARY KEY(bond,id))')
    db.execute('PRAGMA user_version=1')
    for _ in range(n):
        db.execute('INSERT INTO recovery_queue(bond,id,claim,payload,state,event,sent) '
                   'VALUES(?,?,?,?,?,?,?)',
                   (bond, str(uuid.uuid4()), str(uuid.uuid4()), '{}', 'pending',
                    json.dumps({'state': 'succeeded'}), 0))
    db.commit()


def _build_v2(db, bond, n=3):
    db.execute('CREATE TABLE recovery_queue (bond TEXT, id TEXT, claim TEXT, '
               'payload TEXT, state TEXT DEFAULT "pending", event TEXT, '
               'sent INTEGER DEFAULT 0, deferred_until INTEGER DEFAULT 0, '
               'PRIMARY KEY(bond,id))')
    db.execute('PRAGMA user_version=2')
    for _ in range(n):
        db.execute('INSERT INTO recovery_queue(bond,id,claim,payload,state,event,sent,deferred_until) '
                   'VALUES(?,?,?,?,?,?,?,?)',
                   (bond, str(uuid.uuid4()), str(uuid.uuid4()), '{}', 'pending',
                    json.dumps({'state': 'succeeded'}), 0, 0))
    db.commit()


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='recovery migration ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bond = 'b' * 64
        self.env = patch.dict(os.environ, _env(self.root))
        self.env.start()
        self.addCleanup(self.env.stop)

    def _open_raw(self, path):
        return sqlite3.connect(path)

    def _count_rows(self, path):
        with sqlite3.connect(path) as db:
            return db.execute('SELECT COUNT(*) FROM recovery_queue').fetchone()[0]

    def _version(self, path):
        with sqlite3.connect(path) as db:
            return db.execute('PRAGMA user_version').fetchone()[0]

    def _columns(self, path):
        with sqlite3.connect(path) as db:
            return {row[1] for row in db.execute('PRAGMA table_info(recovery_queue)').fetchall()}

    def test_migrate_v1_populated(self):
        q = self.root / 'q.db'
        with self._open_raw(q) as db:
            _build_v1(db, self.bond)
        with patch.object(recovery, '_database', side_effect=recovery._database):
            db = recovery._database(q)
        self.assertEqual(self._version(q), 3)
        self.assertIn('deferred_until', self._columns(q))
        self.assertIn('evaluated_at', self._columns(q))
        self.assertEqual(self._count_rows(q), 3)
        db.close()

    def test_migrate_v2_populated(self):
        q = self.root / 'q.db'
        with self._open_raw(q) as db:
            _build_v2(db, self.bond)
        db = recovery._database(q)
        self.assertEqual(self._version(q), 3)
        self.assertIn('evaluated_at', self._columns(q))
        self.assertEqual(self._count_rows(q), 3)
        db.close()

    def test_open_v3_no_changes(self):
        q = self.root / 'q.db'
        db = recovery._database(q)
        db.close()
        cols_before = self._columns(q)
        version_before = self._version(q)
        db = recovery._database(q)
        db.close()
        self.assertEqual(self._version(q), version_before)
        self.assertEqual(self._columns(q), cols_before)

    def _connect_with_failure(self, path, fail_pattern):
        """Wrap sqlite3.connect so the returned connection's execute raises
        on SQL matching fail_pattern. Patches the instance, not the class."""
        original_connect = sqlite3.connect

        def connect_factory(*a, **kw):
            conn = original_connect(*a, **kw)
            original_execute = conn.execute

            def failing_execute(sql, *args, **kwargs):
                if fail_pattern in sql:
                    raise sqlite3.OperationalError('injected failure')
                return original_execute(sql, *args, **kwargs)

            conn.execute = failing_execute
            return conn

        return patch('sqlite3.connect', side_effect=connect_factory)

    def test_failure_after_first_alter_rolls_back(self):
        q = self.root / 'q.db'
        with self._open_raw(q) as db:
            _build_v1(db, self.bond)
        with self._connect_with_failure(q, 'discovery_cursor'):
            try:
                recovery._database(q)
                failed = False
            except Exception:
                failed = True
        self.assertTrue(failed)
        self.assertEqual(self._version(q), 1)
        self.assertEqual(self._count_rows(q), 3)

    def test_failure_before_version_update_rolls_back(self):
        q = self.root / 'q.db'
        with self._open_raw(q) as db:
            _build_v1(db, self.bond)
        with self._connect_with_failure(q, 'user_version=3'):
            try:
                recovery._database(q)
                failed = False
            except Exception:
                failed = True
        self.assertTrue(failed)
        self.assertEqual(self._version(q), 1)
        self.assertEqual(self._count_rows(q), 3)

    def test_retry_after_failure_completes(self):
        q = self.root / 'q.db'
        with self._open_raw(q) as db:
            _build_v1(db, self.bond)
        with self._connect_with_failure(q, 'discovery_cursor'):
            try:
                recovery._database(q)
            except Exception:
                pass
        db = recovery._database(q)
        self.assertEqual(self._version(q), 3)
        self.assertEqual(self._count_rows(q), 3)
        db.close()

    def test_future_schema_rejected(self):
        q = self.root / 'q.db'
        with self._open_raw(q) as db:
            db.execute('CREATE TABLE recovery_queue (bond TEXT, id TEXT, PRIMARY KEY(bond,id))')
            db.execute('PRAGMA user_version=99')
            db.commit()
        with self.assertRaises(ConfigurationError):
            recovery._database(q)

    def test_partially_migrated_v2_handled(self):
        """Banco parcialmente migrado pela implementação anterior:
        evaluated_at existe mas user_version ainda 2 (falha antes do
        PRAGMA). A nova implementação deve concluir sem apagar dados."""
        q = self.root / 'q.db'
        with self._open_raw(q) as db:
            _build_v2(db, self.bond)
            # Simula falha anterior: adiciona evaluated_at mas não atualiza versão
            db.execute('ALTER TABLE recovery_queue ADD COLUMN evaluated_at INTEGER NOT NULL DEFAULT 0')
            db.commit()
        self.assertEqual(self._version(q), 2)
        db = recovery._database(q)
        self.assertEqual(self._version(q), 3)
        self.assertEqual(self._count_rows(q), 3)
        db.close()


class ConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='recovery migration concurrent ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bond = 'b' * 64
        self.env = patch.dict(os.environ, _env(self.root))
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_poll_and_worker_open_simultaneously(self):
        """Poll e worker abrem o mesmo banco antigo simultaneamente: a
        migração deve ser serializada (um espera, outro migra), sem
        corrupção nem perda de dados."""
        q = self.root / 'q.db'
        with sqlite3.connect(q) as db:
            _build_v1(db, self.bond)
        results = []
        errors = []

        def open_db():
            try:
                db = recovery._database(q)
                results.append(db.execute('SELECT COUNT(*) FROM recovery_queue').fetchone()[0])
                db.close()
            except Exception as e:
                errors.append(e)

        import threading
        t1 = threading.Thread(target=open_db)
        t2 = threading.Thread(target=open_db)
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)
        self.assertEqual(errors, [])
        self.assertEqual(results, [3, 3])
        with sqlite3.connect(q) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 3)


if __name__ == '__main__':
    unittest.main()
