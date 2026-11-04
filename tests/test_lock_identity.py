import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from macip_backup.agent import acquire_task_lock, task_lock_directory
from macip_backup.restic import ResticError


class RegisteredLockTests(unittest.TestCase):
    def test_registered_absent_override_divergence_invalid_and_development(self):
        with tempfile.TemporaryDirectory(prefix='registered lock ') as directory:
            root = Path(directory)
            home, state, registered = root / 'program', root / 'state', root / 'registered locks'
            home.mkdir()
            env = {'HOME': str(root), 'MACIP_HOME': str(home), 'MACIP_STATE': str(state)}
            with patch.dict(os.environ, env, clear=True):
                self.assertEqual(task_lock_directory(), state / 'locks')
                self.assertFalse((home / 'install.json').exists())
                manifest = home / 'install.json'
                manifest.write_text(json.dumps({'lock_directory': str(registered)}))
                self.assertEqual(task_lock_directory(), registered)
                with patch.dict(os.environ, {'MACIP_LOCK_DIR': str(root / 'other')}):
                    with self.assertRaises(ResticError):
                        acquire_task_lock('proof')
                    self.assertFalse((root / 'other').exists())
                for invalid in ('[]', '{bad secret synthetic', '{"lock_directory": null}'):
                    manifest.write_text(invalid)
                    with self.assertRaises(ResticError) as error:
                        acquire_task_lock('proof')
                    self.assertNotIn('secret', str(error.exception))
                    self.assertFalse(registered.exists())
