"""Reconciliação A e recuperação B offline, na fila/supervisão existentes."""
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
import time
from pathlib import Path
import uuid
import zipfile

from .lab_runtime import command
from .lab_supervisor import atomic_json
from .protector import ProtectionError, _directory, _private_file, _hash_tree, _manifest_digest
from .storage_runtime import LabStorage, StorageFailure, ssh_processes


def digest_file(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


class AssistedWorker:
    def __init__(self, actor, side):
        self.actor, self.side, self.root, self.api = actor, side, actor.root, actor.api
        self.storage = LabStorage(actor, side)
        self.prefix = '/api/v1/' + ('provisioner' if side == 'a' else 'protector') + '/assisted'
        self.state = self.root / 'private/assisted'
        self.exports = self.root / 'private/recovery-exports'
        for directory in (self.state, self.exports):
            directory.mkdir(mode=0o700, exist_ok=True)
            _directory(directory)

    @contextmanager
    def idle_ssh(self, container):
        # Não pausa o listener: uma queda do worker não pode deixá-lo em SIGSTOP.
        # O arquivo efetivo é trocado atomicamente. Sessões/preativações ainda
        # presentes impedem confirmar revogação, inclusive após a troca.
        def listener():
            output=self.actor.container(container,'exec',container,'sh','-eu','-c',
                'for p in $(pgrep -x sshd); do cat /proc/"$p"/stat; done').decode()
            alive=[p for p in ssh_processes(output) if p['state'] not in ('Z','X')]
            if len(alive)!=1 or alive[0]['state'] not in ('S','R','I'):
                raise StorageFailure('busy')
            return alive[0]['pid']+':'+alive[0]['starttime']
        def wait_listener():
            deadline=time.monotonic()+3
            while True:
                try:return listener()
                except StorageFailure as error:
                    if error.code!='busy' or time.monotonic()>=deadline:raise
                    time.sleep(.1)
        before=wait_listener()
        yield
        if wait_listener()!=before:
            raise StorageFailure('identity_changed')

    def lifecycle(self, operation):
        ctx = operation['work']['context']
        action = operation['work']['action']
        with self.storage.guarded(ctx) as fd:
            self.api.request(self.prefix + '/operations/' + operation['id'] + '/context')
            run, _, container = self.storage.backend(ctx, fd)
            if action != 'close':
                from .provisioning_sftp import repository_url
                current_port=int(command('docker','port',container,'22/tcp').decode().strip().rsplit(':',1)[1])
                current_key=self.actor.container(container,'exec',container,'ssh-keygen','-y','-f','/hostkey').decode().split()[:2]
                if (ctx['repository'] != repository_url(ctx['account'],self.actor.cfg['ip'],current_port)
                        or ctx['known_hosts'].split()[1:] != current_key):
                    raise StorageFailure('identity_changed')
            points = self.storage.inventory(ctx, run)
            # Não se apresenta conta/repositório vazio como continuidade.
            if not points or ctx['seed']['snapshot_a'] not in {p['id'] for p in points}:
                raise StorageFailure('identity_changed')
            with self.idle_ssh(container):
                self.api.request(self.prefix + '/operations/' + operation['id'] + '/context')
                account = ctx['account']
                path = self.root / 'a' / account / '.ssh/authorized_keys'
                _private_file(path)
                live = self.actor.container(container, 'exec', container, 'cat', f'/home/{account}/.ssh/authorized_keys').decode()
                lines = live.splitlines()
                # Substituição mantém exclusivamente a leitura restrita de B.
                # Encerramento não deixa chaves de acesso do cliente/leitor.
                reader = ctx['reader_public_key']
                preserved = [line for line in lines if reader and line.split()[-2:] == reader.split()]
                if action == 'close':
                    preserved = []
                else:
                    if len(preserved) != 1 and action != 'reopen':
                        raise StorageFailure('identity_changed')
                    if not preserved:
                        if not reader:
                            raise StorageFailure('identity_changed')
                        preserved = ['restrict,command="internal-sftp -R" ' + reader]
                    if 'internal-sftp -R' not in preserved[0]:
                        raise StorageFailure('identity_changed')
                expected = ('\n'.join(preserved) + ('\n' if preserved else '')).encode()
                temporary = path.with_name('authorized_keys.assisted-' + uuid.uuid4().hex)
                try:
                    with temporary.open('xb') as out:
                        temporary.chmod(0o600)
                        out.write(expected); out.flush(); os.fsync(out.fileno())
                    os.replace(temporary, path)
                finally:
                    temporary.unlink(missing_ok=True)
                target = f'/home/{account}/.ssh/authorized_keys'
                from .activation_executor import install_authorized_keys
                install_authorized_keys(lambda *args:self.actor.container(container,*args),container,account,path)
                actual = self.actor.container(container, 'exec', container, 'cat', target)
                if actual != expected:
                    raise StorageFailure('unavailable')
                # Confirma identidade/inventário novamente antes de atestar.
                if self.storage.inventory(ctx, run) != points:
                    raise StorageFailure('identity_changed')
        return dict(repository_a=ctx['repository_a'], repository_b=ctx['repository_b'],
                    revoked=True, inventory_sha256=hashlib.sha256(json.dumps(points, sort_keys=True).encode()).hexdigest())

    def recover(self, operation, journal):
        ctx, work = operation['work']['context'], operation['work']
        executor = self.actor.executor
        # Mesmo lock persistente usado por cópia/retenção, sem consultar A.
        with executor.lock(uuid.UUID(ctx['installation_id'])) as fd:
            current = self.api.request(self.prefix + '/operations/' + operation['id'] + '/context')
            if current['context'] != ctx:
                raise StorageFailure('identity_changed')
            executor.check_volume()
            run, _, _ = self.storage.backend(ctx, fd, offline=True)
            self.storage.inventory(ctx, run)
            points = json.loads(run('--no-lock', 'snapshots', '--json'))
            pairs = {p['snapshot_b']: p['snapshot_a'] for p in current['pairs']}
            common = dict(repository_a=ctx['repository_a'], repository_b=ctx['repository_b'])
            if work['action'] == 'catalog':
                return dict(**common, points=[dict(snapshot_a=pairs[p['id']], snapshot_b=p['id'], time=p['time'])
                    for p in points if p['id'] in pairs])
            chosen = work['point']
            if chosen['snapshot_b'] not in {p['id'] for p in points} or pairs.get(chosen['snapshot_b']) != chosen['snapshot_a']:
                raise StorageFailure('reference_invalid')
            size = json.loads(run('stats', chosen['snapshot_b'], '--mode', 'restore-size', '--json'))['total_size']
            if size > 1024 ** 3:
                raise StorageFailure('reference_invalid')  # Limite explícito do transporte ZIP de laboratório.
            if __import__('shutil').disk_usage(self.state).free < size * 2 + 64 * 1024 ** 2:
                raise StorageFailure('unavailable')
            # Referência do snapshot, não da pasta restaurada. Sem nomes na API/log.
            expected = {}
            for line in run('ls', '--json', chosen['snapshot_b']).splitlines():
                node = json.loads(line)
                if node.get('struct_type') != 'node' or node.get('type') == 'dir':
                    continue
                if node.get('type') != 'file':
                    raise StorageFailure('reference_invalid')
                name = node['path'].lstrip('/')
                if not name or any(v in ('', '.', '..') for v in name.split('/')) or '\\' in name or ':' in name:
                    raise StorageFailure('reference_invalid')
                if len(expected) >= 1000 or name in expected:
                    raise StorageFailure('reference_invalid')
                expected[name] = hashlib.sha256(run('dump', chosen['snapshot_b'], node['path'])).hexdigest()
            if not expected:
                raise StorageFailure('reference_invalid')
            target = self.state / ('restore-' + operation['id'])
            if target.exists() or target.is_symlink():
                raise StorageFailure('interrupted')
            atomic_json(journal, dict(operation=operation['id'], work=work, phase='restoring'))
            target.mkdir(mode=0o700)
            run('check', '--read-data')
            run('restore', chosen['snapshot_b'], '--target', str(target), '--verify')
            actual = _hash_tree(target)
            if actual != expected:
                raise StorageFailure('recovery_failed')
            if sum((target/name).stat().st_size for name in actual)>1024**3:
                raise StorageFailure('reference_invalid')
            self.storage.inventory(ctx, run)
            self.api.request(self.prefix + '/operations/' + operation['id'] + '/context')
            metadata = dict(format=1, operation=operation['id'], installation=ctx['installation_id'],
                repository_a=ctx['repository_a'], repository_b=ctx['repository_b'],
                **chosen, files=expected)
            encoded_metadata=json.dumps(metadata,ensure_ascii=False,sort_keys=True)
            if len(encoded_metadata.encode('utf-8'))>2*1024**2:
                raise StorageFailure('reference_invalid')
            archive = self.exports / (operation['id'] + '.zip')
            temporary = archive.with_suffix('.incomplete')
            if archive.exists() or archive.is_symlink() or temporary.exists() or temporary.is_symlink():
                raise StorageFailure('interrupted')
            with temporary.open('xb') as stream:
                temporary.chmod(0o600)
                with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED, allowZip64=True) as output:
                    output.writestr('RECOVERY.json',encoded_metadata)
                    for name in sorted(expected):
                        output.write(target / name, 'files/' + name)
                stream.flush(); os.fsync(stream.fileno())
            os.replace(temporary, archive)
            return dict(**common, snapshot_b=chosen['snapshot_b'], archive_sha256=digest_file(archive),
                        manifest_sha256=_manifest_digest(expected), files=len(expected),
                        expires_at=(datetime.now(UTC) + timedelta(hours=24)).isoformat())

    def tick(self):
        op = self.api.request(self.prefix + '/claim', dict(executor_id='assisted-' + self.side))['operation']
        if not op:
            return
        uuid.UUID(op['id'])
        active = self.root/'private/services'/('active-'+('primary' if self.side=='a' else 'protection')+'.json')
        atomic_json(active,dict(id=op['id'],claim_id=op['claim_id'],assisted=True))
        journal = self.state / (op['id'] + '.json')
        old = json.loads(_private_file(journal)) if journal.exists() else None
        if old and old['work'] != op['work']:
            raise StorageFailure('identity_changed')
        if old and old.get('phase') == 'completed':
            body = old['body']  # Resultado durável; não repete os efeitos.
        else:
            try:
                result = self.lifecycle(op) if self.side == 'a' else self.recover(op, journal)
                body = dict(state='succeeded', result=result)
            except (StorageFailure, ProtectionError, RuntimeError, OSError, ValueError, KeyError, TypeError) as error:
                code=(error.code if isinstance(error,StorageFailure) else
                      'interrupted' if isinstance(error,ProtectionError) and error.code=='timeout' else
                      'busy' if isinstance(error,ProtectionError) and error.code=='busy' else 'unavailable')
                body = dict(state='failed', error_code=code)
            atomic_json(journal, dict(operation=op['id'], work=op['work'], phase='completed' if body['state']=='succeeded' else 'failed', body=body))
        self.api.request(self.prefix + '/operations/' + op['id'] + '/result', dict(claim_id=op['claim_id'], **body))
        active.unlink(missing_ok=True)
