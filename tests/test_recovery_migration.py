"""Migração atômica da fila de recuperação: SQLite real, falhas injetadas.

A injeção usa ``sqlite3.connect(factory=...)`` com uma subclasse de
``sqlite3.Connection``: atribuir ``conn.execute = ...`` na instância falha
com ``AttributeError`` neste ambiente (atributo read-only), o que faria os
testes passarem sem atingir o ponto pretendido. Cada teste de falha exige
o marcador sintético ``INJECTED:<marker>`` na exceção e confirma que o ponto
de injeção foi registrado em ``hits``.
"""
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


def _tables(db):
    return {row[0] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}


def _columns(db):
    return {row[1] for row in db.execute('PRAGMA table_info(recovery_queue)').fetchall()}


def _snapshot(path):
    """Esquema, versão e dados completos via conexão independente."""
    with sqlite3.connect(path) as db:
        rows = db.execute('SELECT bond,id,claim,payload,state,event,sent '
                          'FROM recovery_queue').fetchall()
        return {
            'version': db.execute('PRAGMA user_version').fetchone()[0],
            'tables': _tables(db),
            'columns': _columns(db),
            'rows': sorted(rows),
        }


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

    def _connect_with_failure(self, marker):
        """sqlite3.connect com factory de subclasse: o execute da conexão
        injetada levanta ``INJECTED:<marker>`` quando o SQL contém o marker;
        ``marker='COMMIT'`` injeta no commit. Devolve (patch, hits)."""
        hits = []
        original = sqlite3.connect

        class _Conn(sqlite3.Connection):
            def execute(self, sql, *args, **kwargs):
                if marker != 'COMMIT' and marker in sql:
                    hits.append(sql)
                    raise sqlite3.OperationalError(f'INJECTED:{marker}')
                return super().execute(sql, *args, **kwargs)

            def commit(self):
                if marker == 'COMMIT':
                    hits.append('commit')
                    raise sqlite3.OperationalError('INJECTED:COMMIT')
                return super().commit()

        def connect_factory(path, *args, **kwargs):
            return original(path, *args, factory=_Conn, **kwargs)

        return patch('sqlite3.connect', side_effect=connect_factory), hits

    def _assert_injected(self, marker):
        """Roda _database sob o patch e exige exatamente a falha injetada."""
        q = self.root / 'q.db'
        with self._open_raw(q) as db:
            _build_v1(db, self.bond)
        before = _snapshot(q)
        connect_patch, hits = self._connect_with_failure(marker)
        with connect_patch:
            with self.assertRaises(sqlite3.OperationalError) as caught:
                recovery._database(q)
        # Ponto de injeção realmente atingido e exceção sintética específica —
        # qualquer falha prematura da própria configuração é rejeitada.
        self.assertEqual(str(caught.exception), f'INJECTED:{marker}')
        self.assertTrue(hits, 'o ponto de injeção nunca foi atingido')
        return before, _snapshot(q)

    def test_migrate_v1_populated(self):
        q = self.root / 'q.db'
        with self._open_raw(q) as db:
            _build_v1(db, self.bond)
        db = recovery._database(q)
        db.close()
        after = _snapshot(q)
        self.assertEqual(after['version'], 3)
        self.assertIn('deferred_until', after['columns'])
        self.assertIn('evaluated_at', after['columns'])
        self.assertIn('discovery_cursor', after['tables'])
        self.assertEqual(len(after['rows']), 3)

    def test_migrate_v2_populated(self):
        q = self.root / 'q.db'
        with self._open_raw(q) as db:
            _build_v2(db, self.bond)
        db = recovery._database(q)
        db.close()
        after = _snapshot(q)
        self.assertEqual(after['version'], 3)
        self.assertIn('evaluated_at', after['columns'])
        self.assertEqual(len(after['rows']), 3)

    def test_open_v3_no_changes(self):
        q = self.root / 'q.db'
        db = recovery._database(q)
        db.close()
        before = _snapshot(q)
        db = recovery._database(q)
        db.close()
        self.assertEqual(_snapshot(q), before)

    def test_failure_after_schema_alter_rolls_back(self):
        """Falha após ALTER realmente executado (discovery_cursor): rollback
        desfaz esquema, versão e preserva todos os dados anteriores."""
        before, after = self._assert_injected('discovery_cursor')
        self.assertEqual(after, before)

    def test_failure_before_version_update_rolls_back(self):
        """Falha em PRAGMA user_version=3: versão anterior e esquema antigo
        preservados, dados intactos."""
        before, after = self._assert_injected('user_version=3')
        self.assertEqual(after, before)

    def test_failure_before_commit_rolls_back(self):
        """Falha no commit, após PRAGMA user_version=3: a atualização de
        versão também é desfeita (PRAGMA user_version é transacional)."""
        before, after = self._assert_injected('COMMIT')
        self.assertEqual(after, before)

    def test_retry_after_failure_completes(self):
        q = self.root / 'q.db'
        with self._open_raw(q) as db:
            _build_v1(db, self.bond)
        connect_patch, hits = self._connect_with_failure('discovery_cursor')
        with connect_patch:
            with self.assertRaises(sqlite3.OperationalError):
                recovery._database(q)
        db = recovery._database(q)
        db.close()
        after = _snapshot(q)
        self.assertEqual(after['version'], 3)
        self.assertEqual(len(after['rows']), 3)

    def test_future_schema_rejected(self):
        q = self.root / 'q.db'
        with self._open_raw(q) as db:
            db.execute('CREATE TABLE recovery_queue (bond TEXT, id TEXT, PRIMARY KEY(bond,id))')
            db.execute('PRAGMA user_version=99')
            db.commit()
        with self.assertRaises(ConfigurationError):
            recovery._database(q)

    def test_future_version_on_second_read_preserves_original_error(self):
        """Versão futura detectada na releitura após o lock: ConfigurationError
        original propagado (nunca ProgrammingError por rollback sobre conexão
        fechada), versão futura preservada, nenhuma migração indevida e a
        conexão de inicialização encerrada."""
        q = self.root / 'q.db'
        with self._open_raw(q) as db:
            _build_v1(db, self.bond)
        before = _snapshot(q)
        created = []
        original = sqlite3.connect

        class _Conn(sqlite3.Connection):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self._reads = 0
                self.closed = False

            def execute(self, sql, *args, **kwargs):
                if sql.strip() == 'PRAGMA user_version':
                    self._reads += 1
                    if self._reads == 2:
                        return super().execute('SELECT 99')
                return super().execute(sql, *args, **kwargs)

            def close(self):
                self.closed = True
                super().close()

        def connect_factory(path, *args, **kwargs):
            conn = original(path, *args, factory=_Conn, **kwargs)
            created.append(conn)
            return conn

        with patch('sqlite3.connect', side_effect=connect_factory):
            with self.assertRaises(ConfigurationError) as caught:
                recovery._database(q)
        self.assertNotIsInstance(caught.exception, sqlite3.ProgrammingError)
        self.assertIn('não suportado', str(caught.exception))
        # Conexão de inicialização encerrada, arquivo intacto.
        self.assertEqual(len(created), 1)
        self.assertTrue(created[0].closed)
        self.assertEqual(_snapshot(q), before)

    def test_partially_migrated_v2_handled(self):
        """Banco parcialmente migrado pela implementação anterior:
        evaluated_at existe mas user_version ainda 2 (falha antes do
        PRAGMA). A nova implementação deve concluir sem apagar dados."""
        q = self.root / 'q.db'
        with self._open_raw(q) as db:
            _build_v2(db, self.bond)
            db.execute('ALTER TABLE recovery_queue ADD COLUMN evaluated_at INTEGER NOT NULL DEFAULT 0')
            db.commit()
        with sqlite3.connect(q) as check:
            self.assertEqual(check.execute('PRAGMA user_version').fetchone()[0], 2)
        db = recovery._database(q)
        db.close()
        after = _snapshot(q)
        self.assertEqual(after['version'], 3)
        self.assertEqual(len(after['rows']), 3)


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
        import threading
        q = self.root / 'q.db'
        with sqlite3.connect(q) as db:
            _build_v1(db, self.bond)
        results, errors = [], []

        def open_db():
            try:
                db = recovery._database(q)
                results.append(db.execute('SELECT COUNT(*) FROM recovery_queue').fetchone()[0])
                db.close()
            except Exception as e:
                errors.append(e)

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
