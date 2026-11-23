"""Atualização administrada do fixture. Nunca recebe comandos de navegador."""
from contextlib import contextmanager,ExitStack
from datetime import UTC,datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tarfile
import time
import uuid
from urllib.request import urlopen
from urllib.parse import urlsplit

from sqlalchemy import create_engine,text
from .lab_supervisor import atomic_json,control,identity,ROLES
from .protector import _directory,_private_file
from .storage_runtime import exclusive,StorageFailure

ORIGIN='Tomas5014/sistema-backup'


class UpdateError(RuntimeError):pass


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def safe_id(value):
    if not re.fullmatch('[a-f0-9]{64}',value):raise UpdateError('identificador local de artefato inválido')
    return value


def verify_source(source,manifest):
    actual={p.relative_to(source).as_posix() for p in source.rglob('*') if p.is_file()}
    if any(p.is_symlink() for p in source.rglob('*')):raise UpdateError('link em versão instalada')
    if actual!=set(manifest['files']):raise UpdateError('lista de arquivos divergente')
    for name,expected in manifest['files'].items():
        p=source/name
        if p.resolve()!=p or not p.is_file() or sha(p)!=expected:raise UpdateError('integridade de arquivo divergente')


def selected(root,rid):
    release=root/'updates/releases'/safe_id(rid)
    manifest=json.loads(_private_file(release/'MANIFEST.json'))
    source=release/'source';verify_source(source,manifest)
    return release,manifest


def stage(root,package,expected,commit):
    safe_id(expected)
    if not re.fullmatch('[a-f0-9]{40}',commit) or sha(package)!=expected:raise UpdateError('pacote/commit não corresponde à aprovação local')
    directory=root/'updates';directory.mkdir(mode=0o700,exist_ok=True)
    releases=directory/'releases';releases.mkdir(mode=0o700,exist_ok=True)
    target=releases/expected
    if target.exists():
        _,m=selected(root,expected)
        if m['commit']!=commit:raise UpdateError('commit aprovado divergente')
        return expected
    temporary=releases/('.stage-'+uuid.uuid4().hex);temporary.mkdir(mode=0o700)
    try:
        with tarfile.open(package) as tar:
            members=tar.getmembers()
            for member in members:
                p=Path(member.name)
                if (member.issym() or member.islnk() or p.is_absolute() or '..' in p.parts
                    or not (member.isfile() or member.isdir()) or p.parts[0] not in ('source','MANIFEST.json')):
                    raise UpdateError('entrada de pacote insegura')
            names=[m.name for m in members]
            if len(set(names))!=len(names):raise UpdateError('entrada duplicada')
            tar.extractall(temporary,filter='data')
        m=json.loads((temporary/'MANIFEST.json').read_text())
        if (m['format']!=1 or m['origin']!=ORIGIN or m['commit']!=commit or m['activation_contract']!=1
            or m['results_contract']!=1 or m['controller_protocol']!=1
            or type(m['postgres_min']) is not int or type(m['postgres_max']) is not int
            or not 1<=m['postgres_min']<=m['postgres_max']):raise UpdateError('origem/contrato/versão não autorizado')
        if not re.fullmatch('[a-f0-9]{40}',m['windows']):raise UpdateError('referência Windows inválida')
        verify_source(temporary/'source',m)
        for p in (temporary/'source').rglob('*'):
            p.chmod(0o555 if p.is_dir() else 0o444)
        (temporary/'MANIFEST.json').chmod(0o600)
        shutil.copyfile(package,temporary/'package.tar.gz');(temporary/'package.tar.gz').chmod(0o600)
        temporary.rename(target)
        return expected
    finally:
        if temporary.exists():
            for p in temporary.rglob('*'):
                if p.is_dir():p.chmod(0o700)
            shutil.rmtree(temporary)


class LaboratoryUpdate:
    def __init__(self,root):
        self.root=root;_directory(root)
        self.directory=root/'updates';self.directory.mkdir(mode=0o700,exist_ok=True);_directory(self.directory)
        self.cfg=json.loads(_private_file(root/'private/config.json'))
        url=urlsplit(self.cfg['api'])
        if url.scheme!='https' or url.username or url.password or url.hostname!=self.cfg['ip']:
            raise UpdateError('atualização exige o HTTPS local registrado')
        with socket.socket() as probe:probe.bind((self.cfg['ip'],0))
        self.engine=create_engine(self.cfg['database'])
        url=self.engine.url
        if url.host!='127.0.0.1' or url.username!='postgres' or not (url.database=='macip_windows_test' or re.fullmatch('macip_verify_[a-f0-9]{32}',url.database or '')):
            raise UpdateError('banco fora do layout descartável autorizado')
        self.tx=self.read('transaction.json')

    def read(self,name):
        p=self.directory/name
        return json.loads(_private_file(p)) if p.exists() else {}

    def event(self,state,step,**values):
        self.tx={**self.tx,**values,'state':state,'step':step,'updated_at':datetime.now(UTC).isoformat(),'executor':identity(os.getpid())}
        atomic_json(self.directory/'transaction.json',self.tx)

    def gate(self,phase):
        fd=os.open(self.directory/'writers.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        deadline=time.monotonic()+30
        try:
            while True:
                try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB);break
                except BlockingIOError:
                    if time.monotonic()>=deadline:raise UpdateError('escrita HTTP ainda ativa; barreira recusada') from None
                    time.sleep(.05)
            atomic_json(self.directory/'gate.json',dict(phase=phase))
        finally:os.close(fd)

    def docker(self,*args,input=None):
        if os.environ.get('DOCKER_HOST') not in ('unix:///var/run/docker.sock','unix:///run/docker.sock'):
            raise UpdateError('selecione explicitamente o socket Docker local do laboratório')
        result=subprocess.run(['docker',*args],input=input,capture_output=True,timeout=120)
        if result.returncode:raise UpdateError('operação PostgreSQL/Docker recusada')
        return result.stdout

    def validate_database(self):
        info=json.loads(self.docker('inspect',self.cfg['pg']))[0]
        ports=info['NetworkSettings']['Ports'].get('5432/tcp') or []
        if (info['Config']['Labels'].get('macip.windows-lab')!=str(self.root) or not info['State']['Running']
            or not any(p['HostIp']=='127.0.0.1' and int(p['HostPort'])==self.engine.url.port for p in ports)):
            raise UpdateError('banco/container/porta não correspondem à área descartável')

    def schema(self):
        self.validate_database()
        with self.engine.connect() as db:return db.scalar(text('SELECT max(version) FROM schema_migrations'))

    def pending(self):
        self.validate_database()
        with self.engine.connect() as db:
            return db.scalar(text("SELECT (SELECT count(*) FROM provisioning_operations WHERE state IN ('pending','running'))+"
                "(SELECT count(*) FROM recovery_operations WHERE state IN ('pending','running'))"))

    def preflight(self,rid,rollback=False):
        release,m=selected(self.root,rid)
        current=self.schema()
        if current>m['postgres_max'] or (rollback and current<m['postgres_min']):
            raise UpdateError('esquema incompatível: recuse volta simples; use correção à frente ou restore controlado')
        info=json.loads(self.docker('inspect',self.cfg['pg']))[0]
        if info['Config']['Labels'].get('macip.windows-lab')!=str(self.root) or not info['State']['Running']:
            raise UpdateError('PostgreSQL não é o contêiner ativo desta área')
        services=control(self.root,'status')
        if not services['running'] or not all(services.get('children',{}).get(r) and identity(int(services['children'][r].split(':')[0]))==services['children'][r] for r in ROLES):
            raise UpdateError('serviços incompletos; confira/recupere antes da atualização')
        if list((self.root/'b-state').glob('*.maintenance-a.json')):raise UpdateError('manutenção A abandonada; preserve e inspecione')
        size=sum(p.stat().st_size for name in ('private','b-state','reader-channel','a') for p in (self.root/name).rglob('*') if p.is_file())
        with self.engine.connect() as db:db_size=db.scalar(text('SELECT pg_database_size(current_database())'))
        if shutil.disk_usage(self.root).free<2*(size+db_size)+64*1024**2:raise UpdateError('espaço insuficiente para backup e recuperação')
        return dict(current_schema=current,target_schema=m['postgres_max'],commit=m['commit'],pending=self.pending())

    @contextmanager
    def quiet(self,wait=30):
        self.gate('drain');self.event('running','drain',error=None)
        deadline=time.monotonic()+wait
        while self.pending():
            if time.monotonic()>=deadline:
                self.gate('open');raise UpdateError('fila não ficou ociosa; termine os trabalhos e repita')
            time.sleep(.2)
        stack=ExitStack()
        while True:
            try:
                stack.enter_context(exclusive(self.root/'a',directory=True))
                for account in sorted((self.root/'a').iterdir()):
                    if account.is_dir():stack.enter_context(exclusive(account,directory=True))
                for p in sorted((self.root/'b-state').glob('*.lock')):stack.enter_context(exclusive(p))
                break
            except StorageFailure:
                stack.close();stack=ExitStack()
                if time.monotonic()>=deadline:raise UpdateError('lock de execução ainda ocupado; aguarde e repita') from None
                time.sleep(.1)
        with stack:
            self.gate('freeze')
            # Também protege a primeira atualização de uma versão ainda sem
            # guardião HTTP. Aguarda transações existentes, nunca mata o banco.
            with self.engine.connect() as db:
                db.execute(text("SET lock_timeout='2s'"))
                tables=db.scalars(text("SELECT tablename FROM pg_tables WHERE schemaname='public'")).all()
                quote=db.dialect.identifier_preparer.quote
                db.execute(text('LOCK TABLE '+','.join('public.'+quote(t) for t in tables)+' IN ACCESS EXCLUSIVE MODE'))
                if db.scalar(text("SELECT (SELECT count(*) FROM provisioning_operations WHERE state IN ('pending','running'))+(SELECT count(*) FROM recovery_operations WHERE state IN ('pending','running'))")):
                    raise UpdateError('novo trabalho detectado; atualização recusada')
                control(self.root,'stop')
                db.rollback()
            if self.pending():raise UpdateError('estado ainda pendente; preserve a área para recuperação')
            yield

    def dbname(self):
        value=self.engine.url.database
        if not re.fullmatch('[a-zA-Z][a-zA-Z0-9_]{0,62}',value):raise UpdateError('nome de banco não suportado')
        return value

    def fingerprint(self,url=None,reference=None):
        self.validate_database()
        engine=create_engine(url) if url else self.engine
        try:
            with engine.connect() as db:
                db.execute(text("SET TIME ZONE 'UTC'"))
                tables=db.scalars(text("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename")).all()
                result={}
                for t in tables:
                    quote=db.dialect.identifier_preparer.quote
                    columns=(reference.get(t,{}).get('columns') if reference else None) or list(db.scalars(text(
                        "SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name=:table ORDER BY ordinal_position"),dict(table=t)))
                    selection=','.join(quote(c) for c in columns)
                    values=sorted(db.scalars(text('SELECT row_to_json(t)::text FROM (SELECT '+selection+' FROM public.'+quote(t)+') AS t')).all())
                    result[t]=dict(count=len(values),sha256=hashlib.sha256(json.dumps(values).encode()).hexdigest(),columns=columns)
                return result
        finally:
            if url:engine.dispose()

    def restored_database(self,dump):
        name='macip_verify_'+uuid.uuid4().hex
        self.docker('exec',self.cfg['pg'],'createdb','-U','postgres',name)
        try:self.docker('exec','-i',self.cfg['pg'],'pg_restore','-U','postgres','--no-owner','--exit-on-error','--single-transaction','-d',name,input=dump)
        except Exception:
            self.docker('exec',self.cfg['pg'],'dropdb','-U','postgres',name);raise
        return name,self.engine.url.set(database=name).render_as_string(hide_password=False)

    def backup(self):
        bid=str(uuid.uuid4());directory=self.directory/'backups'/bid;directory.mkdir(mode=0o700,parents=True)
        self.event('running','backup',backup=bid)
        before=self.fingerprint()
        dump=self.docker('exec',self.cfg['pg'],'pg_dump','-U','postgres','-Fc',self.dbname())
        (directory/'database.dump').write_bytes(dump);(directory/'database.dump').chmod(0o600)
        verify,url=self.restored_database(dump)
        try:
            if self.fingerprint(url)!=before:raise UpdateError('dump restaurado diverge do banco original')
        finally:self.docker('exec',self.cfg['pg'],'dropdb','-U','postgres',verify)
        archive=directory/'state.tar'
        with tarfile.open(archive,'w') as tar:
            for name in ('private','b-state','reader-channel','a'):
                path=self.root/name
                if not path.exists():continue
                for p in sorted(path.rglob('*')):
                    if p.is_symlink() or p.resolve()!=p:raise UpdateError('link no estado; backup recusado')
                    if p.is_file() and not (p.suffix=='.log' or p.name.endswith('.lock') or p.name=='processes.json' or p.name.startswith('active-') or any(x.startswith('children-') for x in p.parts)):
                        tar.add(p,arcname=p.relative_to(self.root).as_posix(),recursive=False)
        archive.chmod(0o600)
        manifest=dict(format=1,root=str(self.root),active=self.read('active.json'),schema=self.schema(),fingerprint=before,
                      dump_sha256=sha(directory/'database.dump'),state_sha256=sha(archive))
        atomic_json(directory/'MANIFEST.json',manifest)
        return bid

    def migrate(self,source):
        code="import os;from sqlalchemy import create_engine;from macip_backup_api.migrations import migrate;migrate(create_engine(os.environ['MACIP_API_DATABASE_URL']))"
        result=subprocess.run([sys.executable,'-c',code],env={**os.environ,'PYTHONPATH':str(source/'api/src'),
            'PYTHONDONTWRITEBYTECODE':'1','MACIP_API_DATABASE_URL':self.cfg['database']},capture_output=True,timeout=120)
        if result.returncode:raise UpdateError('migração falhou; transação e backup preservados')

    def health(self):
        tls=json.loads(_private_file(self.root/'private/services/central.json'))
        deadline=time.monotonic()+30
        while time.monotonic()<deadline:
            try:
                with urlopen(self.cfg['api']+'/healthz',context=ssl.create_default_context(cafile=tls['ca']),timeout=2) as response:
                    if response.status==200:return
            except (OSError,ValueError):pass
            time.sleep(.2)
        raise UpdateError('serviço não confirmou saúde no prazo')

    def start(self,active,health_only=False):
        atomic_json(self.directory/'active.json',active)
        atomic_json(self.directory/'paused.json',dict(roles=['primary','protection','alerts','activation'] if health_only else []))
        control(self.root,'start');self.health()

    def resume_workers(self):
        since=datetime.now(UTC)
        self.gate('drain');atomic_json(self.directory/'paused.json',dict(roles=[]))
        deadline=time.monotonic()+30
        while time.monotonic()<deadline:
            services=control(self.root,'status')
            with self.engine.connect() as db:
                fresh=db.scalar(text("SELECT count(*) FROM lab_worker_heartbeats WHERE last_seen_at>=:since"),dict(since=since))
            if fresh==3 and all(services.get('children',{}).get(r) and identity(int(services['children'][r].split(':')[0]))==services['children'][r] for r in ROLES):
                self.gate('open');return
            time.sleep(.2)
        raise UpdateError('workers não confirmaram retomada; estado preservado para recover')

    def apply(self,rid,*,rollback=False,wait=30):
        with exclusive(self.directory/'update.lock'):
            self.preflight(rid,rollback)
            release,m=selected(self.root,rid);old=self.read('active.json')
            if self.tx.get('state')=='running':raise UpdateError('tentativa anterior incompleta; use recover')
            self.tx={'transaction':str(uuid.uuid4()),'actor_uid':os.getuid()}
            self.event('running','preflight',target=rid,previous=old,error=None)
            try:
                with self.quiet(wait):
                    self.backup()
                    self.event('running','migration')
                    self.migrate(release/'source')
                    if self.schema()!=m['postgres_max']:raise UpdateError('esquema final diverge do manifesto')
                    active=dict(id=rid,source=str(release/'source'),commit=m['commit'],schema=m['postgres_max'],windows=m['windows'])
                    self.event('running','health')
                    self.start(active,True)
                    # Dados históricos devem permanecer: apenas schema_migrations
                    # pode crescer durante uma migração expansiva.
                    manifest=json.loads(_private_file(self.directory/'backups'/self.tx['backup']/'MANIFEST.json'))
                    current=self.fingerprint(reference=manifest['fingerprint'])
                    if any(current.get(t)!=v for t,v in manifest['fingerprint'].items() if t!='schema_migrations'):
                        raise UpdateError('conferência de preservação dos dados falhou')
                    self.event('running','workers',active=active,error=None)
                    self.resume_workers()
                    self.event('succeeded','verified',active=active,error=None)
                    return dict(state='succeeded',commit=m['commit'],backup=self.tx['backup'])
            except Exception as error:
                self.event('failed',self.tx.get('step','preflight'),error=str(error) if isinstance(error,UpdateError) else type(error).__name__)
                raise UpdateError('atualização recusada/falhou; use status e recover; backup/arquivos preservados') from None

    def recover(self):
        with exclusive(self.directory/'update.lock'):
            old=self.tx.get('previous')
            if not old:raise UpdateError('sem versão anterior registrada; mantenha a área parada e instale um artefato aprovado')
            release,m=selected(self.root,old['id'])
            if not m['postgres_min']<=self.schema()<=m['postgres_max']:
                raise UpdateError('esquema incompatível com a versão anterior; correção à frente ou restore explícito')
            if self.tx.get('step')=='restore':raise UpdateError('restore incompleto: preserve backup e diretórios rescue para recuperação assistida')
            with self.quiet(30):self.start(old,True)
            self.resume_workers()
            self.event('recovered','verified',error=None)
            return {'state':'recovered','commit':old['commit']}

    def backup_manifest(self,bid):
        if not re.fullmatch('[a-f0-9-]{36}',bid or ''):raise UpdateError('backup local inválido')
        directory=self.directory/'backups'/bid
        m=json.loads(_private_file(directory/'MANIFEST.json'))
        if m['root']!=str(self.root) or sha(directory/'database.dump')!=m['dump_sha256'] or sha(directory/'state.tar')!=m['state_sha256']:
            raise UpdateError('identidade/integridade do backup divergente')
        return directory,m

    def restore_preview(self,bid):
        _,m=self.backup_manifest(bid);now=self.fingerprint()
        changed=[t for t,v in now.items() if m['fingerprint'].get(t)!=v]
        return dict(backup=bid,changed_tables=changed,
            warning='escritas posteriores não estarão na central restaurada; banco e arquivos atuais serão preservados separadamente',
            repository_warning='restore SQL não recupera snapshots removidos; manutenção ficará bloqueada até reconciliar')

    def restore(self,bid,wait=30):
        with exclusive(self.directory/'update.lock'):
            directory,m=self.backup_manifest(bid)
            previous=self.read('active.json');target=m['active'] or previous
            if not target:raise UpdateError('nenhum código aprovado para recuperação')
            release,version=selected(self.root,target['id'])
            if not version.get('reconciliation',False):
                controller=self.read('controller.json');release,version=selected(self.root,controller['id'])
                target=dict(id=controller['id'],source=str(release/'source'),commit=version['commit'],schema=version['postgres_max'],windows=version['windows'])
            if not version['postgres_min']<=m['schema']<=version['postgres_max']:
                raise UpdateError('backup incompatível com o código de recuperação')
            self.tx={'transaction':str(uuid.uuid4()),'actor_uid':os.getuid()}
            self.event('running','restore-preflight',previous=previous,restore_source=bid,error=None)
            with self.quiet(wait):
                rescue_backup=self.backup()
                self.event('running','restore',restore_source=bid,rescue_backup=rescue_backup)
                name,url=self.restored_database((directory/'database.dump').read_bytes())
                restored=self.fingerprint(url)
                if set(restored)!=set(m['fingerprint']) or any(
                    any(restored[t].get(k)!=v for k,v in saved.items()) for t,saved in m['fingerprint'].items()):
                    raise UpdateError('restore não corresponde ao backup')
                staging=self.directory/('restore-'+uuid.uuid4().hex);staging.mkdir(mode=0o700)
                with tarfile.open(directory/'state.tar') as tar:
                    for member in tar.getmembers():
                        p=Path(member.name)
                        if not member.isfile() or p.is_absolute() or '..' in p.parts or p.parts[0] not in ('private','a','b-state','reader-channel'):
                            raise UpdateError('entrada de estado insegura')
                    tar.extractall(staging,filter='data')
                for p in staging.rglob('*'):p.chmod(0o700 if p.is_dir() else 0o600)
                rescue=self.directory/'rescue'/uuid.uuid4().hex;rescue.mkdir(mode=0o700,parents=True)
                for entry in ('private','a','b-state','reader-channel'):
                    old=self.root/entry;new=staging/entry
                    if old.exists():old.rename(rescue/entry)
                    if not new.exists():new.mkdir(mode=0o700)
                    new.rename(self.root/entry)
                staging.rmdir()
                self.cfg=json.loads(_private_file(self.root/'private/config.json'));self.cfg['database']=url
                atomic_json(self.root/'private/config.json',self.cfg)
                alerts=json.loads(_private_file(self.root/'private/services/alerts.json'));alerts['database']=url
                atomic_json(self.root/'private/services/alerts.json',alerts)
                self.engine.dispose();self.engine=create_engine(url)
                self.start(target,True);self.gate('reconcile')
                self.event('restored','inventory-required',rescue_database=self.dbname(),rescue_state=str(rescue),
                    previous_database_retained=True,error=None)
                return dict(state='restored',maintenance='blocked',rescue_backup=rescue_backup,
                            note='banco anterior preservado; execute reconcile antes de manutenção')

    def reconcile(self):
        with exclusive(self.directory/'update.lock'):
            if self.read('gate.json').get('phase')!='reconcile':raise UpdateError('não há restore aguardando inventário')
            if self.tx.get('state')=='reconciling':
                from .models import InstallationPreparation
                from .protection_routes import public_protection
                from sqlalchemy.orm import Session
                with Session(self.engine) as db:
                    rows=db.query(InstallationPreparation).filter_by(state='primary_ready',lifecycle='active').all()
                    if self.pending() or not all(public_protection(p)[0]=='proven' for p in rows):
                        return dict(state='reconciling',maintenance='blocked')
                self.gate('open');self.event('recovered','reconciled',error=None)
                return dict(state='recovered',maintenance='open')
            from .lab_runtime import PrimaryWorker,ProtectionWorker
            from .storage_runtime import LabStorage
            from .storage_routes import context,audit
            from .models import InstallationPreparation
            from .lab_automation_routes import enqueue
            from sqlalchemy.orm import Session
            a=LabStorage(PrimaryWorker(json.loads(_private_file(self.root/'private/services/primary.json'))),'a')
            b=LabStorage(ProtectionWorker(json.loads(_private_file(self.root/'private/services/protection.json'))),'b')
            os.environ['MACIP_API_LAB_AUTOMATION']='1'
            absent_summary=[]
            with Session(self.engine) as db:
                for prep in db.query(InstallationPreparation).filter_by(state='primary_ready',lifecycle='active').all():
                    ctx=context(db,prep)
                    with a.guarded(ctx) as fd:
                        ar,_,_=a.backend(ctx,fd);br,_,_=b.backend(ctx,fd)
                        points_a={p['id'] for p in a.inventory(ctx,ar)};points_b={p['id'] for p in b.inventory(ctx,br)}
                    if not prep.lab_seed or prep.lab_seed['snapshot_a'] not in points_a:
                        raise UpdateError('ponto de ensaio ausente em A; snapshots não são recuperados pelo dump')
                    known={r[0] for r in db.execute(text("SELECT payload->>'snapshot_id' FROM windows_results WHERE agent_id=:id AND payload->>'repository_id'=:repo"),dict(id=prep.agent_id,repo=prep.repository_id)) if r[0]}
                    absent=sorted(known-points_a)
                    if absent:absent_summary.append(dict(installation=str(prep.agent_id),repository_id=prep.repository_id,snapshots=absent))
                    audit(db,prep.agent_id,'local-update','reconciled_absent',dict(repository_id=prep.repository_id,snapshots=absent))
                    binding=ctx['protection'];prep.protected_copy={**binding,'binding_id':str(uuid.uuid4()),'proof':None,'invalid':True}
                    prep.protection_state='pending';db.flush();enqueue(db,prep,force=True)
                db.commit()
            atomic_json(self.directory/'paused.json',dict(roles=[]))
            self.event('reconciling','new-protection-proof',absent_snapshots=absent_summary,inventory_warning=sum(len(v['snapshots']) for v in absent_summary),error=None)
            return dict(state='reconciling',maintenance='blocked',note='aguarde novas provas B e repita reconcile')
