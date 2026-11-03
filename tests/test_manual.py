import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from macip_backup.manual import load_environment
from macip_backup.models import ConfigurationError
from macip_backup.credentials import credential_from_environment_or_os, CredentialError

ROOT = Path(__file__).resolve().parents[1]


class ManualEnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='manual credentials ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.file = self.root / 'agent.env'

    def write(self, content):
        self.file.write_text(content)
        self.file.chmod(0o600)

    def test_file_only_is_absent_until_explicit_loading_and_preserves_literal_bytes(self):
        value = 'synthetic space $dollar #hash ;semi `tick` =equal á'
        self.write('CREDENTIAL=' + value + '\nSSL_CERT_FILE=/tmp/CA with spaces.pem\n')
        with patch.dict(os.environ, {}, clear=True), patch(
            'macip_backup.credentials._keyctl_lookup', return_value=None
        ):
            with self.assertRaises(CredentialError):
                credential_from_environment_or_os('CREDENTIAL')
            os.environ.update(load_environment(self.file))
            self.assertEqual(credential_from_environment_or_os('CREDENTIAL'), value)
            self.assertEqual(os.environ['SSL_CERT_FILE'], '/tmp/CA with spaces.pem')

    def test_unsupported_input_is_rejected_without_values(self):
        for content in ('C= synthetic', 'C=synthetic ', 'C="synthetic"',
                        "C='synthetic'", 'C=synthetic\\n', 'C=synthetic\ncontinued',
                        'export C=synthetic', 'C=synthetic\r\n', 'C=synthetic\x00',
                        'C=synthetic\nC=duplicate', 'MACIP_LOCK_DIR=/tmp/synthetic'):
            with self.subTest(content=repr(content)):
                self.write(content)
                with self.assertRaises(ConfigurationError) as error:
                    load_environment(self.file)
                self.assertNotIn('synthetic', str(error.exception))

    def test_permissions_are_required(self):
        self.write('C=synthetic\n')
        for mode in (0o644, 0o700):
            self.file.chmod(mode)
            with self.assertRaises(ConfigurationError):
                load_environment(self.file)

    def test_public_manual_entrypoint_clean_environment_and_child_tls(self):
        value = 'synthetic space $dollar #hash ;semi `tick` =equal'
        ca = str(self.root / 'CA with spaces.pem')
        self.write(f'CREDENTIAL={value}\nSSL_CERT_FILE={ca}\n')
        expected = self.root / 'expected.json'
        expected.write_text(json.dumps({'password': value, 'ca': ca}))
        binary = self.root / 'restic'
        binary.write_text('#!/usr/bin/env python3\n'
                          'import json, os\n'
                          'expected=json.load(open(os.environ["EXPECTED_FILE"]))\n'
                          'assert os.environ["RESTIC_PASSWORD"] == expected["password"]\n'
                          'assert os.environ["SSL_CERT_FILE"] == expected["ca"]\n'
                          'print("[]")\n')
        binary.chmod(0o755)
        config = self.root / 'task.json'
        config.write_text(json.dumps({
            'id': 'manual-test', 'name': 'Manual', 'client': {'id': 'c', 'name': 'C'},
            'device_id': 'd', 'sources': [str(self.root / 'old-source')],
            'destinations': [{'id': 'local', 'name': 'Local', 'kind': 'local',
                              'repository': str(self.root / 'repo'),
                              'password_environment': 'CREDENTIAL'}],
        }))
        env = {'PATH': str(self.root) + os.pathsep + os.environ['PATH'],
               'PYTHONPATH': str(ROOT / 'src'), 'HOME': str(self.root),
               'EXPECTED_FILE': str(expected)}
        result = subprocess.run(['python3', '-m', 'macip_backup.manual', '--env-file',
                                 str(self.file), '--', 'snapshots', str(config), 'local'],
                                env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['snapshots'], [])
        self.assertNotIn(value, result.stdout + result.stderr)
