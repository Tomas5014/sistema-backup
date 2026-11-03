"""Empacotador real em Git descartável, somente dados sintéticos."""
import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class CandidatePackagingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='macip package regression ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        for directory in ('src', 'api', 'portal', 'deploy', 'docs', 'tests', 'examples'):
            (self.repo / directory).mkdir()
        for filename in ('pyproject.toml', 'README.md', 'registro-trabalho.md', '.dockerignore',
                         'api/pyproject.toml', 'portal/package.json', 'deploy/CANDIDATE',
                         'deploy/install.sh', 'deploy/package-candidate.py',
                         'deploy/central/.env.example'):
            target = self.repo / filename
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / filename, target)
        shutil.copytree(ROOT / 'src/macip_backup', self.repo / 'src/macip_backup',
                        ignore=shutil.ignore_patterns('__pycache__'))
        for directory in ('tests', 'examples', 'docs'):
            (self.repo / directory / 'README.md').write_text('synthetic fixture\n')
        self.git('init', '-q')

    def git(self, *args):
        return subprocess.run(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                               *args], cwd=self.repo, check=True, capture_output=True, text=True)

    def commit(self):
        self.git('add', '.')
        self.git('commit', '-qm', 'synthetic')
        return self.git('rev-parse', 'HEAD').stdout.strip()

    def package(self, sha, name):
        output = self.root / name
        result = subprocess.run(['python3', str(self.repo / 'deploy/package-candidate.py'),
                                 sha, str(output)], capture_output=True, text=True, timeout=60)
        return result, output

    def test_operational_tracked_files_are_rejected_without_publication(self):
        cases = {
            'portal/.env.production': 'SYNTHETIC_SECRET=fixture-only\n',
            'docs/id_ed25519': '-----BEGIN OPENSSH PRIVATE KEY-----\nsynthetic\n',
            'deploy/agent.db-wal': 'synthetic database fragment',
            'deploy/agent.sqlite3-shm': 'synthetic',
            'deploy/agent.db-journal': 'synthetic',
            'api/runtime.env.local': 'SYNTHETIC=fixture',
            'docs/unknown': '-----BEGIN RSA PRIVATE KEY-----\nsynthetic\n',
            'docs/.env.example': 'SYNTHETIC=not-an-approved-template',
            'deploy/database-wal': 'synthetic SQLite auxiliary',
            'docs/database-without-extension': 'SQLite format 3\x00synthetic',
        }
        for index, (name, content) in enumerate(cases.items()):
            with self.subTest(name=name):
                path = self.repo / name
                path.write_text(content)
                sha = self.commit()
                result, output = self.package(sha, f'rejected-{index}')
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((output / 'SHA256SUMS').exists())
                self.assertFalse(list(output.glob('*.tar.gz')))
                self.assertNotIn(content, result.stdout)
                path.unlink()

    def test_models_mentions_and_dirty_worktree_normal_package_install(self):
        (self.repo / 'docs/mentions.md').write_text(
            'Documentation mentions -----BEGIN OPENSSH PRIVATE KEY----- only.\n')
        (self.repo / 'src/example.py').write_text(
            'header = "-----BEGIN OPENSSH PRIVATE KEY-----"\n')
        (self.repo / 'docs/code-block.md').write_text(
            'Example header, not a key:\n```text\n-----BEGIN OPENSSH PRIVATE KEY-----\n```\n')
        sha = self.commit()
        (self.repo / 'portal/.env.production').write_text('SYNTHETIC=untracked')
        result, output = self.package(sha, 'normal')
        self.assertEqual(result.returncode, 0, result.stderr)
        manifest = json.loads((output / 'MANIFEST.json').read_text())
        self.assertEqual(manifest['commit'], sha)
        for line in (output / 'SHA256SUMS').read_text().splitlines():
            digest, name = line.split('  ')
            self.assertEqual(hashlib.sha256((output / name).read_bytes()).hexdigest(), digest)
        extracted = self.root / 'extracted with spaces'
        with tarfile.open(next(output.glob('*.tar.gz'))) as archive:
            names = archive.getnames()
            self.assertFalse(any(name.endswith('.env.production') for name in names))
            for name in ('docs/mentions.md', 'src/example.py', 'deploy/central/.env.example'):
                self.assertTrue(any(item.endswith('/' + name) for item in names))
            archive.extractall(extracted, filter='data')
        bundle = next(extracted.iterdir())
        env = {'PATH': os.environ['PATH'], 'HOME': str(self.root / 'home')}
        (self.root / 'home').mkdir()
        for key in ('HOME', 'BIN', 'CONFIG', 'STATE', 'CACHE'):
            env['MACIP_' + key] = str(self.root / 'installed with spaces' / key)
        installed = subprocess.run(['bash', str(bundle / 'source/deploy/install.sh'),
                                    str(next((bundle / 'wheels').glob('*.whl'))), '--commit', sha],
                                   env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(installed.returncode, 0, installed.stderr)
        for argument in ('--help', '--version'):
            subprocess.run([str(Path(env['MACIP_BIN']) / 'macip-backup'), argument],
                           env=env, cwd=self.root, check=True, capture_output=True)

    def test_build_failure_publishes_no_checksums(self):
        (self.repo / 'pyproject.toml').write_text('not valid toml\n')
        result, output = self.package(self.commit(), 'broken')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(output.exists())

    def test_symlink_is_rejected(self):
        (self.repo / 'docs/link').symlink_to('/tmp/not-a-package-input')
        result, output = self.package(self.commit(), 'link')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(output.exists())
