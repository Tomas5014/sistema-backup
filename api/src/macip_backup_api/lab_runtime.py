"""Adaptadores explícitos do laboratório. Nenhum acesso a SSH administrativo do host."""
from __future__ import annotations

import base64
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import tempfile
import threading
import time
import uuid

from .provisioner import ApiTransport, LabFileVault, LabPrimaryAdapter, ProvisioningFailure, reconcile_operation
from .protector import LabProtector, ProtectionError, _directory, _private_file, _manifest_digest, _hash_tree, sftp_args_option


def write_once(path: Path, value: bytes):
    """Não publica bytes parciais; conteúdo diferente exige inspeção."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _directory(path.parent)
    if path.exists() or path.is_symlink():
        if _private_file(path) != value:
            raise RuntimeError('estado privado já existe com conteúdo divergente')
        return
    temp = path.with_name('.' + path.name + '-' + secrets.token_hex(8))
    try:
        fd = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(value); stream.flush(); os.fsync(stream.fileno())
        try:
            os.link(temp, path)
        except FileExistsError:
            if _private_file(path) != value:
                raise RuntimeError('publicação concorrente divergente') from None
        parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(parent)
        finally: os.close(parent)
    finally:
        temp.unlink(missing_ok=True)


def json_once(path, value):
    write_once(path, json.dumps(value, sort_keys=True, ensure_ascii=False).encode())


class LabCommandError(RuntimeError):
    def __init__(self, tool, code):
        super().__init__('comando de laboratório falhou')
        self.tool=Path(tool).name
        self.code=code


def command(*args, env=None, timeout=120):
    # Sem stderr em respostas/logs. Filhos pertencem ao grupo supervisionado.
    registry = None
    with subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          stdin=subprocess.DEVNULL, start_new_session=True, env=env) as process:
        if os.environ.get('MACIP_LAB_PROCESS_DIRECTORY'):
            from .lab_supervisor import identity
            directory=Path(os.environ['MACIP_LAB_PROCESS_DIRECTORY']);_directory(directory)
            registry=directory/str(process.pid)
            token=identity(process.pid)
            if token:write_once(registry,token.encode())
        try:
            output,_=process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:os.killpg(process.pid,signal.SIGCONT);os.killpg(process.pid,signal.SIGKILL)
            except ProcessLookupError:pass
            process.communicate(timeout=5)
            raise RuntimeError('comando de laboratório excedeu o prazo') from None
        finally:
            if registry:registry.unlink(missing_ok=True)
        if process.returncode:raise LabCommandError(args[0],process.returncode)
        return output



def reader_key(directory: Path):
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    _directory(directory)
    key = directory / 'id_ed25519'
    if not key.exists():
        if key.with_suffix('.pub').exists():
            raise RuntimeError('chave privada original de B ausente')
        with tempfile.TemporaryDirectory(dir=directory) as temp:
            candidate = Path(temp) / 'key'
            command('ssh-keygen','-q','-t','ed25519','-N','','-f',str(candidate))
            write_once(key, candidate.read_bytes())
    _private_file(key)
    # Reconstitui somente a parte pública a partir da mesma chave, sem trocar identidade.
    public = ' '.join(command('ssh-keygen', '-y', '-f', str(key)).decode().split()[:2])
    pub_file=key.with_suffix('.pub')
    if pub_file.exists():
        if ' '.join(_private_file(pub_file).decode().split()[:2]) != public:
            raise RuntimeError('parte pública original de B divergente')
    else:write_once(pub_file, public.encode())
    return key, public


class PrimaryWorker:
    def __init__(self, cfg):
        self.cfg = cfg
        self.root = Path(cfg['root'])
        self.accounts = self.root / 'a'
        _directory(self.accounts)
        self.vault = LabFileVault(self.root / 'private/vault-a')
        self.api = ApiTransport(cfg['api'], cfg['token'], 'auto-primary', allow_lab_http=cfg.get('allow_http', False),ca_file=cfg.get('ca'))

    def container(self, name, *args):
        label = command('docker','inspect','-f','{{index .Config.Labels "macip.lab.root"}}',name).decode().strip()
        if label != str(self.root):
            raise RuntimeError('contêiner não pertence à área')
        return command('docker',*args)

    def resources(self, operation):
        if os.getuid() == 0:
            raise RuntimeError('fixture exige executor sem privilégios root no host')
        account = operation['account_name']
        if not re.fullmatch(r'macip[a-f0-9]{20}', account):
            raise RuntimeError('conta incompatível')
        self.stage='host-key'
        setup = self.root / 'private/primary' / operation['installation_id']
        setup.mkdir(mode=0o700, parents=True, exist_ok=True)
        key, _ = reader_key(setup)  # Host key privado de A; não é chave Windows ou de B.
        container = self.cfg['prefix'] + '-' + account
        runtime = setup / 'container.json'
        json_once(runtime, {'container': container, 'account': account, 'installation_id': operation['installation_id']})
        exists = subprocess.run(['docker','inspect',container],capture_output=True,timeout=30).returncode == 0
        repository = self.accounts / account / 'repository'
        # Porta e identidade são persistidas pelo contêiner antes de reconciliar o repositório.
        if not exists:
            if (setup / 'ready.json').exists():
                raise RuntimeError('contêiner original de A ausente; não reconstruir silenciosamente')
            command('docker','run','--pull=never','-d','--name',container,
                '--label','macip.lab.root='+str(self.root),
                '--label','macip.windows-lab='+str(self.root),
                '--label','macip.activation-accounts='+str(self.accounts),
                '-p', self.cfg['ip']+'::22', '--entrypoint','/bin/sh',self.cfg['image'],
                '-c','exec sleep infinity')
        self.container(container,'inspect',container)
        inspected = json.loads(command('docker','inspect',container))[0]
        if not inspected['State']['Running']:
            command('docker','start',container)
        port = int(command('docker','port',container,'22/tcp').decode().strip().rsplit(':',1)[1])
        host_public = ' '.join(command('ssh-keygen','-y','-f',str(key)).decode().split()[:2])
        hosts = f'[{self.cfg["ip"]}]:{port} ' + host_public
        primary = LabPrimaryAdapter(self.accounts, host=self.cfg['ip'], port=port, known_hosts=hosts)
        self.stage='repository'
        result = reconcile_operation(operation, self.vault, primary)
        bundle = json.loads(self.vault.get(f'installation/{operation["installation_id"]}/bundle'))
        marker = setup / 'ready.json'
        self.stage='sshd'
        if not marker.exists():
            # Nenhum dado Windows pode existir ainda: a operação A não foi confirmada.
            command('docker','cp',str(key),container+':/hostkey')
            command('docker','exec',container,'mkdir','-p','/jail','/run/sshd')
            # O repositório passa a ser o original dentro do contêiner e persiste nos
            # reinícios do WORKER. Sua ausência posterior é recusada, não reinicializada.
            has_repo = subprocess.run(['docker','exec',container,'test','-f','/jail/repository/config'],capture_output=True,timeout=30).returncode == 0
            if not has_repo:
                command('docker','cp',str(repository),container+':/jail/repository')
            auth = ('restrict,command="internal-sftp" '+bundle['public_key']+'\n'+
                    'restrict,command="internal-sftp -R" '+operation['reader_public_key']+'\n')
            authorized = setup / 'authorized_keys'
            write_once(authorized, auth.encode())
            command('docker','cp',str(authorized),container+':/authorized_keys')
            # Somente contas dentro deste contêiner descartável, nunca useradd no host.
            command('docker','exec',container,'/bin/sh','-eu','-c',r'''
                if ! id "$1" >/dev/null 2>&1; then useradd --create-home --shell /usr/sbin/nologin "$1"; passwd --delete "$1"; fi
                mkdir -p "/home/$1/.ssh" "/jail/home/$1"
                cp /authorized_keys "/home/$1/.ssh/authorized_keys"
                chown -R "$1:$1" "/home/$1/.ssh" /jail/repository
                chmod 700 "/home/$1/.ssh"
                chmod 600 "/home/$1/.ssh/authorized_keys" /hostkey
            ''','setup',account)
            # A raiz local é registro de identidade. A política efetiva está no contêiner.
            ready_sshd = subprocess.run(['docker','exec',container,'pgrep','-x','sshd'],capture_output=True,timeout=30).returncode == 0
            if not ready_sshd:
                command('docker','exec','-d',container,'/usr/sbin/sshd','-D','-e','-f','/dev/null','-h','/hostkey',
                    '-oUsePAM=no','-oPasswordAuthentication=no','-oKbdInteractiveAuthentication=no','-oPermitRootLogin=no',
                    '-oDisableForwarding=yes','-oAuthorizedKeysFile=.ssh/authorized_keys','-oAllowUsers='+account,
                    '-oChrootDirectory=/jail','-oSubsystem=sftp internal-sftp')
            json_once(marker, {'repository_id': result['repository_id']})
        json_once(self.accounts / account / 'sftp-runtime.json',
                  {'container':container,'account':account,'installation_id':operation['installation_id']})
        # Confere o repositório REAL, não apenas o registro local de A.
        key_path = setup / 'initial-client-key'
        write_once(key_path, base64.b64decode(bundle['private_key']))
        host_file = setup / 'known_hosts'; write_once(host_file, (hosts+'\n').encode())
        def remote(*args, password=None):
            opts=sftp_args_option(['-F','/dev/null','-oBatchMode=yes','-oStrictHostKeyChecking=yes','-oIdentitiesOnly=yes',
                 '-oIdentityAgent=none','-oGlobalKnownHostsFile=/dev/null','-oConnectTimeout=5',
                 '-oUserKnownHostsFile='+json.dumps(str(host_file),ensure_ascii=False),'-i',str(key_path)])
            return command('restic','--no-cache','-r',result['profile']['repositorio'],'-o',opts,*args,
                           env={'PATH':os.environ['PATH'],'RESTIC_PASSWORD':password or bundle['restic_password']},timeout=180)
        self.stage='sftp-identity'
        if json.loads(remote('cat','config'))['id'] != result['repository_id']:
            raise ProvisioningFailure('repository_mismatch')
        self.stage='reader-password'
        handoff = self.root / 'reader-channel' / operation['installation_id']
        handoff.mkdir(mode=0o700,parents=True,exist_ok=True)
        read_password = handoff / 'restic-password'
        if not read_password.exists():
            if (handoff / 'reader-ready.json').exists():
                raise ProvisioningFailure('secret_missing')
            write_once(read_password, secrets.token_urlsafe(48).encode())
        secret = _private_file(read_password).decode()
        try: check = json.loads(remote('cat','config',password=secret))['id']
        except RuntimeError:
            remote('key','add','--new-password-file',str(read_password))
            check = json.loads(remote('cat','config',password=secret))['id']
        if check != result['repository_id']: raise ProvisioningFailure('repository_mismatch')
        json_once(handoff/'reader-ready.json', {'repository_id':check})
        self.stage='seed'
        # Apenas locks reconhecidos como obsoletos pelo Restic; nunca --remove-all.
        remote('unlock')
        seed = self.seed(operation, result, remote, setup, handoff)
        result['lab_seed'] = seed
        return result

    def seed(self, operation, result, remote, setup, handoff):
        source = setup / 'seed-source'
        source.mkdir(mode=0o700,exist_ok=True)
        data = ('Somente dados fictícios MACIP '+operation['installation_id']+'\n').encode()
        write_once(source/'ficticio.txt',data)
        expected = {str(source/'ficticio.txt').lstrip('/'):hashlib.sha256(data).hexdigest()}
        # Referência publicada ANTES de qualquer backup; não deriva da restauração.
        json_once(handoff/'seed-manifest.json',expected)
        digest = _manifest_digest(expected)
        tag = 'macip-lab-seed:'+operation['installation_id']
        points = json.loads(remote('snapshots','--json','--tag',tag))
        if not points:
            output = remote('backup','--json','--tag',tag,str(source))
            # Restic remoto lê a origem no processo local (este executor A).
            points = json.loads(remote('snapshots','--json','--tag',tag))
        if len(points) != 1:
            raise RuntimeError('ponto de ensaio ausente ou ambíguo')
        point = points[0]['id']
        actual = remote('dump',point,str(source/'ficticio.txt'))
        if hashlib.sha256(actual).hexdigest() != expected[str(source/'ficticio.txt').lstrip('/')]:
            raise RuntimeError('referência do ensaio diverge do ponto')
        metadata = dict(repository_id=result['repository_id'],snapshot_a=point,manifest_sha256=digest)
        json_once(handoff/'seed.json',metadata)
        return metadata

    def run_once(self):
        fd=os.open(self.accounts, os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try:
            try: fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError: return
            operation=self.api.claim()
            if not operation: return
            from .lab_supervisor import atomic_json
            active=self.root/'private/services'/'active-primary.json'
            atomic_json(active,{'id':operation['id'],'claim_id':operation['claim_id']})
            try:
                result=self.resources(operation)
                body=dict(claim_id=operation['claim_id'],state='succeeded',result=result)
            except ProvisioningFailure as error:
                body=dict(claim_id=operation['claim_id'],state='failed',error_code=error.code)
            except (RuntimeError,OSError,ValueError,KeyError,subprocess.SubprocessError) as error:
                atomic_json(self.root/'private/services/last-primary-error.json',
                    {'stage':getattr(self,'stage','unknown'),'category':type(error).__name__,
                     'tool':getattr(error,'tool',None),'code':getattr(error,'code',None)})
                body=dict(claim_id=operation['claim_id'],state='failed',error_code='repository_error')
            self.api.result(operation['id'],body)
            active.unlink(missing_ok=True)
        finally: os.close(fd)


class ProtectionWorker:
    def __init__(self,cfg):
        self.cfg=cfg; self.root=Path(cfg['root'])
        self.api=ApiTransport(cfg['api'],cfg['token'],'auto-protection',allow_lab_http=cfg.get('allow_http',False),ca_file=cfg.get('ca'))
        self.executor=LabProtector(Path(cfg['mount']),Path(cfg['volume']),uuid.UUID(cfg['volume_id']),
                                   self.root/'b-state',reserve_bytes=cfg.get('reserve_bytes',64*1024**2))

    def prepare_readers(self):
        errors=0
        for row in self.api.request('/api/v1/protector/automation/installations'):
            try:
                key,pub=reader_key(self.root/'b-state'/'readers'/row['installation_id'])
                if row['reader_public_key'] and row['reader_public_key'] != pub:
                    raise RuntimeError('chave de B divergente')
                if not row['reader_public_key']:
                    self.api.request(f'/api/v1/protector/automation/installations/{row["installation_id"]}/reader',{'public_key':pub})
            except (RuntimeError,OSError,ValueError):errors+=1
        return errors

    def run_once(self):
        errors=self.prepare_readers()
        operation=self.api.request('/api/v1/protector/operations/claim',{'executor_id':'auto-protection'})['operation']
        if not operation:
            if errors:raise RuntimeError('uma instalação exige inspeção da credencial B')
            return
        from .lab_supervisor import atomic_json
        active=self.root/'private/services'/'active-protection.json'
        atomic_json(active,{'id':operation['id'],'claim_id':operation['claim_id']})
        installation=uuid.UUID(operation['installation_id'])
        work=operation['work']; key=self.root/'b-state'/'readers'/str(installation)/'id_ed25519'
        handoff=self.root/'reader-channel'/str(installation)
        credentials=dict(source_key=key,source_password_file=handoff/'restic-password')
        stage='context'
        try:
            context=self.api.request(f'/api/v1/protector/installations/{installation}')
            if context['source']!=work['source']: raise ProtectionError('identidade mudou')
            expected=json.loads(_private_file(handoff/'seed-manifest.json'))
            if _manifest_digest(expected)!=work['seed']['manifest_sha256']:
                raise ProtectionError('referência inválida')
            stage='prepare'
            self.executor.execute(self.api,installation,'prepare',**credentials)
            for snapshot in work['snapshots']:
                stage='copy'
                self.executor.execute(self.api,installation,'copy',snapshot=snapshot,**credentials)
            if operation['kind']=='protect_installation':
                stage='prove'
                self.executor.execute(self.api,installation,'prove',manifest=handoff/'seed-manifest.json')
            body=dict(claim_id=operation['claim_id'],state='succeeded')
        except (ProtectionError,RuntimeError,OSError,ValueError,KeyError,subprocess.SubprocessError) as error:
            atomic_json(self.root/'private/services/last-protection-error.json',
                        dict(stage=stage,category=type(error).__name__,operation=operation['id'],reason=getattr(error,'code',None),command=getattr(error,'operation',None),exit_code=getattr(error,'returncode',None),transport_error=getattr(error,'transport_error',None)))
            body=dict(claim_id=operation['claim_id'],state='failed',error_code='protection_failed')
        self.api.request(f'/api/v1/protector/operations/{operation["id"]}/result',body)
        active.unlink(missing_ok=True)
