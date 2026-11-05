"""Helpers do piloto: fixtures privadas; integração real Restic separada."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from datetime import datetime, timezone
import re
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / 'tools/pilot_data.py'


class PilotDataTests(unittest.TestCase):
    def test_baseline_binary_day_six_failure_is_reproduced(self):
        old = subprocess.check_output(['git', 'show', '6713dd8:docs/piloto-mvp-linux-sftp.md'],
                                      cwd=ROOT, text=True)
        body = re.search(r"mutar_dia\(\).*?<<'PY'\n(.*?)\nPY", old, re.S).group(1)
        source = self.area / 'origem';source.mkdir()
        binary = source/'grande-a.bin';binary.write_bytes(b'\xff\x00\xfe')
        rng = SimpleNamespace(sample=lambda files, count:[binary], choice=lambda files:binary)
        with patch.object(sys,'argv',['old-proof',str(source),str(self.area/'old-manifest'),'6']), patch('random.Random', return_value=rng):
            with self.assertRaises(UnicodeDecodeError):exec(compile(body,'<old-day6>','exec'),{})
        self.assertEqual(binary.read_bytes(),b'\xff\x00\xfe')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='pilote données ')
        self.addCleanup(self.temp.cleanup)
        self.area = Path(self.temp.name) / 'área com espaços'
        self.area.mkdir(mode=0o700)
        (self.area / 'locks').mkdir()
        self.lock = self.area / 'locks/maintenance.lock'; self.lock.touch()
        self.task = str(uuid.uuid4()); self.dest = 'local-proof'
        (self.area / 'evidence').mkdir()

    def helper(self, *args, ok=True):
        result = subprocess.run([sys.executable, str(HELPER), '--area', str(self.area),
                                 '--lock-file', str(self.lock), *map(str, args)],
                                capture_output=True, text=True, timeout=30)
        if ok:
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)
        self.assertNotEqual(result.returncode, 0)
        return result

    def generate(self):
        self.helper('generate', '--generation', 'g1', '--large-bytes', 257, '--seed', 'test')

    def fake_bind(self, generation, history=False):
        point = hashlib.sha256(generation.encode()).hexdigest()
        result = {'task_id': self.task, 'execution_id': str(uuid.uuid4()), 'status': 'success',
                  'started_at_utc': datetime.now(timezone.utc).isoformat(),
                  'finished_at_utc': datetime.now(timezone.utc).isoformat(),
                  'destinations': [{'destination_id': self.dest, 'status': 'success', 'snapshot_id': point[:8]}]}
        result_file = self.area / 'evidence' / (generation + '-result.json')
        result_file.write_text(json.dumps([dict(result, execution_id=str(uuid.uuid4()), task_id='wrong'), result] if history else result))
        snapshots = self.area / 'evidence' / (generation + '-snapshots.json')
        snapshots.write_text(json.dumps({'snapshots': [{'id': point, 'tags': [f'task:{self.task}'],
                                                      'paths': [str(self.area / 'origem')]}]}))
        extra = ['--execution', result['execution_id']] if history else []
        self.helper('bind', '--generation', generation, '--result', result_file, '--snapshots', snapshots,
                    '--task', self.task, '--destination', self.dest, '--snapshot', point, *extra)
        return point

    def test_history_selects_exact_execution_and_prior_backup_cannot_bind_new_generation(self):
        self.generate(); point = self.fake_bind('g1', history=True)
        self.helper('mutate', '--generation', 'g2', '--day', 2, '--force-binary')
        records = json.loads((self.area/'evidence/g1-result.json').read_text())
        rejected = self.helper('bind', '--generation', 'g2', '--result', self.area/'evidence/g1-result.json',
            '--snapshots', self.area/'evidence/g1-snapshots.json', '--task', self.task,
            '--destination', self.dest, '--snapshot', point, '--execution', records[1]['execution_id'], ok=False)
        self.assertIn('anterior', rejected.stderr)
        self.assertFalse((self.area/'referencias/g2.binding.json').exists())

    def restored(self):
        operation = self.area / 'recuperacoes' / str(uuid.uuid4())
        restored = operation / (self.area / 'origem').relative_to('/')
        restored.parent.mkdir(parents=True)
        shutil.copytree(self.area / 'origem', restored,
                        ignore=shutil.ignore_patterns('*.tmp'))
        return operation, restored

    def compare(self, point, operation, ok=True, generation='g1'):
        return self.helper('compare', '--binding', self.area / f'referencias/{generation}.binding.json',
                           '--operation-root', operation, '--task', self.task,
                           '--destination', self.dest, '--snapshot', point, ok=ok)

    def test_binary_mutation_twelve_days_and_pending_lock_refusals(self):
        self.generate()
        binary = self.area / 'origem/grande-a.bin'
        original = binary.read_bytes()
        self.helper('mutate', '--generation', 'g2', '--day', 2, '--force-binary', ok=False)
        self.assertEqual(binary.read_bytes(), original)
        self.fake_bind('g1')
        with self.lock.open('a') as locked:
            fcntl.flock(locked, fcntl.LOCK_SH | fcntl.LOCK_NB)
            self.helper('mutate', '--generation', 'g2', '--day', 2, '--force-binary', ok=False)
        self.assertEqual(binary.read_bytes(), original)
        for day in range(2, 14):
            before = binary.read_bytes()
            self.helper('mutate', '--generation', f'g{day}', '--day', day, '--force-binary')
            self.assertTrue(binary.read_bytes().startswith(before))
            self.assertGreater(len(binary.read_bytes()), len(before))
            self.fake_bind(f'g{day}')

    def test_comparison_real_layout_and_all_content_failures(self):
        self.generate(); point = self.fake_bind('g1')
        operation, restored = self.restored()
        self.compare(point, operation)
        old = restored / 'grande-a.bin'; data = old.read_bytes()
        old.write_bytes(b'changed'); self.compare(point, operation, ok=False)
        old.unlink(); self.compare(point, operation, ok=False)
        old.write_bytes(data)
        for name in ('extra.txt', 'ignorar.tmp'):
            extra = restored / name; extra.write_bytes(b'extra')
            self.compare(point, operation, ok=False); extra.unlink()
        self.compare('f' * 64, operation, ok=False)
        self.compare(point, Path('/'), ok=False)
        self.compare(point, self.area / 'origem', ok=False)
        old.unlink(); old.symlink_to(self.area / 'origem/grande-a.bin')
        self.compare(point, operation, ok=False)

    def test_references_are_exclusive_and_validate_manifest(self):
        self.generate(); point = self.fake_bind('g1')
        self.helper('capture', '--generation', 'g1', ok=False)
        self.helper('generate', '--generation', 'g1', '--large-bytes', 1, ok=False)
        operation, _ = self.restored()
        manifest = self.area / 'referencias/g1.json'
        initial = manifest.read_bytes()
        for content in ({'files': {}}, {'files': {'../escape': {'bytes': 1, 'sha256': 'a'*64}}}):
            doc = json.loads(initial); doc.update(content); manifest.write_text(json.dumps(doc))
            self.compare(point, operation, ok=False)
        manifest.write_bytes(initial)
        self.fake_bind_duplicate = self.helper('bind', '--generation', 'g1',
            '--result', self.area / 'evidence/g1-result.json', '--snapshots', self.area / 'evidence/g1-snapshots.json',
            '--task', self.task, '--destination', self.dest, '--snapshot', point, ok=False)
        self.compare(point, operation)

    def test_historical_reference_not_current_source(self):
        self.generate(); point = self.fake_bind('g1'); operation, restored = self.restored()
        old_files = {p.relative_to(self.area / 'origem') for p in (self.area / 'origem').rglob('*.txt')}
        self.helper('mutate', '--generation', 'g2', '--day', 6, '--force-binary')
        new_files = {p.relative_to(self.area / 'origem') for p in (self.area / 'origem').rglob('*.txt')}
        removed = old_files - new_files
        self.assertEqual(len(removed), 1)
        self.assertTrue((restored / next(iter(removed))).is_file())
        self.compare(point, operation)

    def test_symlink_escape_and_source_change_before_binding_refused(self):
        self.generate()
        source = self.area / 'origem'
        outside = self.area / 'outside'; outside.write_text('preserve')
        (source / 'escape.txt').symlink_to(outside)
        self.helper('capture', '--generation', 'g2', ok=False)
        self.assertEqual(outside.read_text(), 'preserve')
        (source / 'escape.txt').unlink()
        (source / 'grande-a.bin').write_bytes(b'changed after capture')
        # Create synthetic evidence files with fake_bind, which must now fail.
        with self.assertRaisesRegex(AssertionError, 'origem mudou'): self.fake_bind('g1')
        self.assertFalse((self.area / 'referencias/g1.binding.json').exists())


@unittest.skipUnless(shutil.which('restic'), 'Restic local indisponível; integração não homologada')
class PilotResticRealTests(PilotDataTests):
    # Only real integration; do not inherit duplicate unit methods.
    test_binary_mutation_twelve_days_and_pending_lock_refusals = None
    test_comparison_real_layout_and_all_content_failures = None
    test_references_are_exclusive_and_validate_manifest = None
    test_historical_reference_not_current_source = None
    test_symlink_escape_and_source_change_before_binding_refused = None
    test_baseline_binary_day_six_failure_is_reproduced = None
    test_history_selects_exact_execution_and_prior_backup_cannot_bind_new_generation = None

    def cli(self, *args):
        env = dict(os.environ, PYTHONPATH=str(ROOT/'src'), HOME=str(self.area),
                   MACIP_HOME=str(self.area/'programa'), MACIP_STATE=str(self.area/'estado'),
                   MACIP_LOCK_DIR=str(self.area/'locks'), MACIP_CACHE=str(self.area/'cache'),
                   PILOT_SYNTHETIC_PASSWORD='synthetic-only-no-operational-credential')
        completed = subprocess.run([sys.executable, '-m', 'macip_backup.cli', *map(str,args)],
                                   env=env, capture_output=True, text=True, timeout=90)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)

    def test_integrated_historical_restore_and_automatic_retention_nine_backups(self):
        self.generate()
        config = self.area / 'config.json'
        config.write_text(json.dumps({'id': self.task, 'name': 'Exclusive retention fixture',
            'client': {'id': 'synthetic-client', 'name': 'Synthetic'}, 'device_id': 'synthetic-device',
            'sources': [str(self.area/'origem')], 'exclusions': ['*.tmp'],
            'destinations': [{'id': self.dest, 'name': 'Local temporary only', 'kind': 'local',
                             'repository': str(self.area/'repositorio'),
                             'password_environment': 'PILOT_SYNTHETIC_PASSWORD', 'retention': {'keep_last':7}}]}))
        created = []; inventories = []
        for day in range(1, 10):
            if day > 1:self.helper('mutate', '--generation', f'g{day}', '--day', day, '--force-binary')
            result = self.cli('agent', config, '--store', self.area/'estado/agent.db')
            inventory = self.cli('snapshots', config, self.dest)
            points = [p for p in inventory['snapshots'] if f'task:{self.task}' in (p.get('tags') or [])]
            prefix = result['destinations'][0]['snapshot_id']
            point = next(p['id'] for p in points if p['id'].startswith(prefix))
            created.append(point); inventories.append({p['id'] for p in points})
            self.assertEqual(inventories[-1], set(created[-7:]))
            result_file = self.area/'evidence'/f'g{day}-result.json';result_file.write_text(json.dumps(result))
            snapshot_file = self.area/'evidence'/f'g{day}-snapshots.json';snapshot_file.write_text(json.dumps(inventory))
            self.helper('bind', '--generation', f'g{day}', '--result', result_file, '--snapshots', snapshot_file,
                        '--task', self.task, '--destination', self.dest, '--snapshot', point)
            if day == 2:
                historical = self.area/'recuperacoes'/str(uuid.uuid4())
                self.cli('restore', config, self.dest, created[0], historical)
                self.compare(created[0], historical)
                removed = set(json.loads((self.area/'referencias/g1.json').read_text())['files']) - set(json.loads((self.area/'referencias/g2.json').read_text())['files'])
                self.assertTrue(removed)
                for name in removed:self.assertTrue((historical/(self.area/'origem').relative_to('/')/name).is_file())
        self.assertEqual(set(created[:2]), set(created) - inventories[-1])
        self.cli('check', config, self.dest, '--read-data')
        kept = self.area/'recuperacoes'/str(uuid.uuid4())
        self.cli('restore', config, self.dest, created[2], kept)
        self.compare(created[2], kept, generation='g3')
        # Preserve task+snapshot inventory as test output before temp cleanup.
        print('REAL LOCAL RETENTION:', json.dumps({'task':self.task, 'created':created,
            'removed':created[:2], 'kept':created[2:], 'counts':[len(i) for i in inventories]}))
