"""Retenção explícita do fixture A/B, nunca sobre caminhos recebidos pelo painel."""
from contextlib import contextmanager, ExitStack
from datetime import UTC, datetime
import fcntl
import json
import os
from pathlib import Path
import shutil
import time
import sys
import uuid

from .lab_runtime import command
from .lab_supervisor import atomic_json
from .protector import _directory, _private_file, _run, ProtectionError
from .provisioner import LabFileVault
from .storage import digest, plan


class StorageFailure(RuntimeError):
    def __init__(self, code):
        self.code=code
        super().__init__(code)


@contextmanager
def exclusive(path, *, directory=False):
    fd=os.open(path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW) if directory else os.open(
        path,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
    try:
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise StorageFailure('busy') from None
        yield fd
    finally:os.close(fd)


class LabStorage:
    def __init__(self, actor, side):
        self.actor=actor;self.root=actor.root;self.side=side;self.api=actor.api
        self.prefix='/api/v1/'+('provisioner' if side=='a' else 'protector')+'/storage'
        self.state=self.root/'private/storage'
        self.state.mkdir(mode=0o700,exist_ok=True);_directory(self.state)
        self.last_measure=0

    @contextmanager
    def guarded(self, ctx):
        aid=ctx['installation_id'];uuid.UUID(aid)
        # Mesmo lock herdado pelos filhos Restic do executor B. Inclui A, pois
        # A só pode esquecer após conferir a existência da cópia em B.
        with ExitStack() as stack:
            fd=stack.enter_context(exclusive(self.root/'b-state'/f'{aid}.lock'))
            marker=self.root/'b-state'/f'{aid}.maintenance-a.json'
            if marker.exists() or marker.is_symlink():
                raise StorageFailure('interrupted')
            if self.side=='a':
                stack.enter_context(exclusive(self.root/'a',directory=True))
                stack.enter_context(exclusive(self.root/'a'/ctx['account'],directory=True))
            yield fd

    def backend(self, ctx, fd, *, offline=False):
        aid=ctx['installation_id']
        if self.side=='a':
            runtime=json.loads(_private_file(self.root/'a'/ctx['account']/'sftp-runtime.json'))
            if runtime['installation_id']!=aid or runtime['account']!=ctx['account']:
                raise StorageFailure('identity_changed')
            container=runtime['container']
            info=json.loads(command('docker','inspect',container))[0]
            if (info['Config']['Labels'].get('macip.lab.root')!=str(self.root)
                or not info['State']['Running'] or info['State'].get('Paused')):
                raise StorageFailure('unavailable')
            bundle=LabFileVault(self.root/'private/vault-a').get(f'installation/{aid}/bundle')
            if bundle is None:raise StorageFailure('unavailable')
            secret=json.loads(bundle)['restic_password']
            # O Restic Linux aprovado do host, sem instalar pacotes no host/serviço.
            binary=shutil.which('restic')
            if not binary:raise StorageFailure('unavailable')
            command('docker','cp',binary,container+':/macip-storage-restic')
            command('docker','exec',container,'chmod','755','/macip-storage-restic')
            def run(*args):
                return _run(['docker','exec','--env','RESTIC_PASSWORD',container,'timeout',
                    '--signal=TERM','--kill-after=3s','100s','/macip-storage-restic','--no-cache',
                    '-r','/jail/repository',*args],
                    {'PATH':os.environ['PATH'],'RESTIC_PASSWORD':secret},operation='armazenamento A',lock_fd=fd)
            def capacity():
                raw=command('docker','exec',container,'df','-Pk','/jail/repository').decode().splitlines()[-1].split()
                total,used,free=(int(raw[i])*1024 for i in (1,2,3))
                size=int(command('docker','exec',container,'du','-sk','/jail/repository').split()[0])*1024
                # A medição é do filesystem Docker compartilhado, não de quota por cliente.
                server=command('docker','info','--format','{{.ID}}').decode().strip()
                return dict(total=total,used=used,free=free,scope='shared-volume',
                            volume=digest(['docker',server,raw[0]]),repository_bytes=size)
            return run,capacity,container
        executor=self.actor.executor
        executor.check_volume_identity()
        binding=ctx['protection'];repo=executor.volume/aid
        if (str(executor.volume_id)!=binding['volume_id'] or digest_path(repo)!=binding['destination']):
            raise StorageFailure('identity_changed')
        _directory(repo)
        password=executor.vault.get(f'installation/{aid}/bundle')
        if password is None:raise StorageFailure('unavailable')
        def run(*args):
            executor.check_volume_identity()
            command=[executor.restic,'--no-cache','-r',str(repo),*args]
            environment={'PATH':os.environ['PATH'],'RESTIC_PASSWORD':password.decode()}
            if offline:
                command=[sys.executable,'-m','macip_backup_api.protection_offline',*command]
                environment['PYTHONPATH']=str(Path(__file__).resolve().parents[1])
            return _run(command,environment,operation='armazenamento B',lock_fd=fd)
        def capacity():
            executor.check_volume_identity()
            usage=shutil.disk_usage(executor.volume)
            size=int(command('du','-sk',str(repo)).split()[0])*1024
            return dict(total=usage.total,used=usage.used,free=usage.free,scope='shared-volume',
                        volume=digest(['local',os.stat(executor.mount).st_dev]),repository_bytes=size)
        return run,capacity,None

    def inventory(self, ctx, run):
        if json.loads(run('--no-lock','cat','config'))['id']!=ctx['repository_'+self.side]:
            raise StorageFailure('identity_changed')
        points=json.loads(run('--no-lock','snapshots','--json'))
        if not isinstance(points,list) or len(points)>1000:raise StorageFailure('unavailable')
        from .storage_routes import Point
        return [Point.model_validate(dict(id=p['id'],time=p['time'])).model_dump(mode='json') for p in points]

    def receipt_path(self, ctx, side):
        return self.state/(ctx['installation_id']+'-'+side+'.json')

    def receipt(self, ctx):
        side='b' if self.side=='a' else 'a'
        try:value=json.loads(_private_file(self.receipt_path(ctx,side)))
        except (OSError,ValueError):raise StorageFailure('reference_stale') from None
        age=(datetime.now(UTC)-datetime.fromisoformat(value['at'])).total_seconds()
        if (age<0 or age>90 or value['source']!=ctx['source'] or value['protection']!=ctx['protection']['binding_id']
            or value['repository_id']!=ctx['repository_'+side] or not value.get('valid')):
            raise StorageFailure('reference_stale')
        if self.side=='a':
            # A recebe só a atestação e metadados de volume do fixture, nunca a
            # senha de escrita B. Confere disponibilidade física novamente.
            registered=json.loads(_private_file(self.root/'private/services/installation.json'))
            volume=Path(registered['volume']);mount=volume.parent
            binding=ctx['protection'];repo=volume/ctx['installation_id']
            if (not mount.is_mount() or volume.resolve()!=volume or not volume.is_relative_to(Path('/dev/shm'))
                or registered['volume_id']!=binding['volume_id'] or volume.stat().st_dev!=mount.stat().st_dev
                or _private_file(volume/'.macip-volume').decode()!=binding['volume_id']
                or digest_path(repo)!=binding['destination']):
                raise StorageFailure('reference_stale')
            _directory(repo)
            import hashlib
            if hashlib.sha256(_private_file(repo/'config')).hexdigest()!=value.get('config_sha256'):
                raise StorageFailure('reference_stale')
        return [p['id'] for p in value['points']]

    def save_receipt(self, ctx, points, valid=True):
        config_hash=None
        if self.side=='b' and valid:
            import hashlib
            repo=self.actor.executor.volume/ctx['installation_id']
            config_hash=hashlib.sha256(_private_file(repo/'config')).hexdigest()
        atomic_json(self.receipt_path(ctx,self.side),dict(at=datetime.now(UTC).isoformat(),valid=valid,
            source=ctx['source'],protection=ctx['protection']['binding_id'],repository_id=ctx['repository_'+self.side],
            points=points,config_sha256=config_hash))

    @contextmanager
    def fence(self, container, ctx=None, operation=None):
        if container is None:yield;return
        # Fixture possui só um listener. Sessão SFTP existente implica recusa,
        # nunca encerramento de backup/restore do técnico. Listener parado não
        # aceita sessões novas durante recálculo e forget locais no contêiner.
        def pids():
            output=command('docker','exec',container,'sh','-eu','-c',
                'for p in $(pgrep -x sshd); do cat /proc/"$p"/stat; done').decode()
            observed=ssh_processes(output)
            atomic_json(self.state/'ssh-observed.json',observed)
            return [p['pid'] for p in observed if p['state'] not in ('Z','X')]
        ids=pids()
        if len(ids)!=1:raise StorageFailure('busy')
        pid=ids[0]
        state=next(v['state'] for v in json.loads(_private_file(self.state/'ssh-observed.json')) if v['pid']==pid)
        if state not in ('S','R','I'):raise StorageFailure('busy')
        before=command('docker','exec',container,'cat',f'/proc/{pid}/stat').decode().rsplit(')',1)[1].split()[19]
        command('docker','exec',container,'kill','-STOP',pid)
        marker=None
        try:
            deadline=time.monotonic()+2
            while True:
                fields=command('docker','exec',container,'cat',f'/proc/{pid}/stat').decode().rsplit(')',1)[1].split()
                if fields[19]!=before:raise StorageFailure('identity_changed')
                if fields[0]=='T':break
                if time.monotonic()>=deadline:raise StorageFailure('busy')
                time.sleep(.02)
            if pids()!=ids:raise StorageFailure('busy')
            if ctx is not None:
                marker=self.root/'b-state'/(ctx['installation_id']+'.maintenance-a.json')
                from .lab_runtime import json_once
                json_once(marker,dict(operation=operation,container=container,pid=pid,starttime=before))
            yield
        finally:
            after=command('docker','exec',container,'cat',f'/proc/{pid}/stat').decode().rsplit(')',1)[1].split()[19]
            if after!=before:raise StorageFailure('identity_changed')
            command('docker','exec',container,'kill','-CONT',pid)
            if marker:marker.unlink()

    def perform(self, operation):
        ctx=operation['work']['context'];expected=operation['work']['expected']
        removed=[];calculated=None
        journal=self.state/(operation['id']+'.json')
        stage='guard'
        try:
            with self.guarded(ctx) as fd:
                stage='context'
                if self.api.request(self.prefix+'/installations/'+ctx['installation_id'])!=ctx:
                    raise StorageFailure('identity_changed')
                stage='backend'
                run,capacity,container=self.backend(ctx,fd)
                stage='fence'
                with self.fence(container,ctx,operation['id']) if operation['work']['action']=='execute' else ExitStack():
                    stage='inventory'
                    points=self.inventory(ctx,run)
                    stage='peer'
                    peer=self.receipt(ctx)
                    calculated=plan(ctx,self.side,points,peer)
                    if operation['work']['action']=='simulate':
                        self.save_receipt(ctx,points)
                        return dict(state='succeeded',error=None,plan=calculated,removed=[])
                    candidates={p['id'] for p in expected['points'] if p['action']=='remove'}
                    keep={p['id'] for p in expected['points'] if p['action']=='keep'}
                    original=keep|candidates
                    current={p['id'] for p in points}
                    prior=json.loads(_private_file(journal)) if journal.exists() else None
                    if prior:
                        if prior['expected']!=expected or prior['operation']!=operation['id']:
                            raise StorageFailure('identity_changed')
                        # Uma chamada forget pode ter excluído parte antes de falhar.
                        # Só aceita a ausência dos candidatos aprovados, nunca de keep.
                        if not keep.issubset(current) or not current.issubset(original):
                            raise StorageFailure('inventory_changed')
                        removed=sorted(original-current)
                        revised={p['id']:p['action'] for p in calculated['points']}
                        if any(revised.get(p)!='remove' for p in candidates&current):
                            raise StorageFailure('inventory_changed')
                    elif calculated['fingerprint']!=expected['fingerprint']:
                        raise StorageFailure('inventory_changed')
                    atomic_json(journal,dict(operation=operation['id'],expected=expected,removed=removed))
                    remaining=sorted((candidates & current))
                    if remaining:
                        stage='forget'
                        if self.api.request(self.prefix+'/installations/'+ctx['installation_id'])!=ctx:
                            raise StorageFailure('identity_changed')
                        # Lock exclusivo nativo do Restic; sem --no-lock, unlock,
                        # --prune ou IDs abreviados. Falha preserva a parte restante.
                        try:run('forget',*remaining)
                        finally:
                            after=self.inventory(ctx,run);ids={p['id'] for p in after}
                            removed=sorted(candidates-ids)
                            atomic_json(journal,dict(operation=operation['id'],expected=expected,removed=removed))
                            self.save_receipt(ctx,after)
                            if not keep.issubset(ids):raise StorageFailure('inventory_changed')
                            if ids & candidates:raise StorageFailure('unavailable')
                    return dict(state='succeeded',error=None,plan=expected,removed=removed)
        except (StorageFailure,ProtectionError,RuntimeError,OSError,ValueError,KeyError,TypeError) as error:
            atomic_json(self.state/('last-error-'+self.side+'.json'),dict(operation=operation['id'],stage=stage,category=type(error).__name__,code=error.code if isinstance(error,StorageFailure) else 'unavailable'))
            return dict(state='failed',error=error.code if isinstance(error,StorageFailure) else 'unavailable',
                        plan=expected or calculated,removed=removed)

    def tick(self):
        # Medir é independente do backup: erros são registrados, não propagados
        # ao orquestrador nem convertidos em resultado de backup.
        if time.monotonic()-self.last_measure>30:
            self.last_measure=time.monotonic()
            for ctx in self.api.request(self.prefix+'/installations'):
                payload=dict(source=ctx['source'],repository_id=ctx['repository_'+self.side])
                try:
                    with self.guarded(ctx) as fd:
                        run,sample,_=self.backend(ctx,fd)
                        points=self.inventory(ctx,run)
                        payload['measurement']=sample()
                        self.save_receipt(ctx,points)
                except (StorageFailure,ProtectionError,RuntimeError,OSError,ValueError,KeyError,TypeError) as error:
                    payload['error']=error.code if isinstance(error,StorageFailure) and error.code in ('busy','identity_changed') else 'unavailable'
                    if payload['error']=='busy':
                        self.last_measure=min(self.last_measure,time.monotonic()-28)
                    else:self.save_receipt(ctx,[],False)
                self.api.request(self.prefix+'/installations/'+ctx['installation_id']+'/capacity',payload)
        op=self.api.request(self.prefix+'/claim',dict(executor_id='storage-'+self.side))['operation']
        if op:
            result=self.perform(op)
            self.api.request(self.prefix+'/operations/'+op['id']+'/result',dict(claim_id=op['claim_id'],**result))


def digest_path(path):
    import hashlib
    return hashlib.sha256(os.fsencode(path)).hexdigest()


def ssh_processes(output):
    """Z/X já saíram; T/t ainda podem escrever após SIGCONT e contam como vivos."""
    values=[]
    for line in output.splitlines():
        try:
            head,tail=line.rsplit(')',1)
            pid,comm=head.split(' (',1)
        except ValueError:raise StorageFailure('unavailable') from None
        fields=tail.split()
        if not pid.isdecimal() or comm!='sshd' or len(fields)<20:
            raise StorageFailure('unavailable')
        values.append(dict(pid=pid,state=fields[0],starttime=fields[19]))
    return values
