import os
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class MaintenanceBackupTests(unittest.TestCase):
    def test_default_backup_and_relative_custom_directory_rejection(self):
        with tempfile.TemporaryDirectory(prefix='maintenance default ') as directory:
            root = Path(directory)
            state = root / 'state with spaces'
            state.mkdir()
            with sqlite3.connect(state / 'agent.db') as database:
                database.execute('CREATE TABLE proof (id INTEGER)')
            env = {'HOME': str(root), 'MACIP_STATE': str(state), 'PATH': os.environ['PATH']}
            target = root / 'backup with spaces.db'
            script = ROOT / 'deploy/backup-agent-db.sh'
            good = subprocess.run(['bash', str(script), str(target)], env=env,
                                  capture_output=True, timeout=10)
            self.assertEqual(good.returncode, 0, good.stderr)
            self.assertTrue((state / 'locks/maintenance.lock').is_file())
            bad_env = dict(env, MACIP_LOCK_DIR='relative/locks')
            bad = subprocess.run(['bash', str(script), str(root / 'bad.db')], env=bad_env,
                                 capture_output=True, timeout=10)
            self.assertNotEqual(bad.returncode, 0)
            executor = subprocess.run(['python3', '-c',
                'from macip_backup.agent import acquire_task_lock; acquire_task_lock("test")'],
                env=dict(bad_env, PYTHONPATH=str(ROOT / 'src')),
                capture_output=True, timeout=10)
            self.assertNotEqual(executor.returncode, 0)
            self.assertFalse((root / 'bad.db').exists())
