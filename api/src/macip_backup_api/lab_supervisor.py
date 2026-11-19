"""Supervisor POSIX por área descartável. Sem serviços globais e sem shell livre."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

from .lab_runtime import PrimaryWorker, ProtectionWorker, json_once
from .protector import _directory, _private_file
from .provisioner import ApiTransport

ROLES=('central','activation','primary','protection','alerts')


def identity(pid):
    try:
        fields=Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()
        return None if fields[0]=='Z' else f'{pid}:{fields[19]}'
    except (FileNotFoundError,ProcessLookupError):return None


def signal_identity(value, sig, *, group=False):
    if value and identity(int(value.split(':')[0]))==value:
        try:
            (os.killpg if group else os.kill)(int(value.split(':')[0]),sig)
        except ProcessLookupError:pass


def atomic_json(path,value):
    # O registro operacional pode mudar; arquivos de configuração continuam imutáveis.
    from .lab_runtime import write_once
    temporary=path.with_name(path.name+'.new-'+str(os.getpid()))
    write_once(temporary,json.dumps(value,sort_keys=True).encode())
    os.replace(temporary,path)


def base_environment():
    env={key:value for key,value in os.environ.items() if not key.startswith('MACIP_')}
    env['PYTHONPATH']=str(Path(__file__).resolve().parents[1])
    return env


def child(root,role):
    cfg=json.loads(_private_file(root/'private/services'/f'{role}.json'))
    env=base_environment()
    if cfg.get('ca'):env['SSL_CERT_FILE']=cfg['ca']
    if role in ('central','activation'):
        helper=Path(__file__).resolve().parents[3]/'tools/windows-lab-server.py'
        args=[sys.executable,str(helper),'activation-api' if role=='central' else 'activation-a',str(root),
              '--certificate',cfg['certificate'],'--key',cfg['key'],'--ca',cfg['ca']]
        if role=='activation':args+=['--delivery-port',str(cfg['port'])]
    else:
        args=[sys.executable,'-m','macip_backup_api.lab_supervisor','worker',str(root),'--role',role]
        # Registro de filhos Restic com grupos próprios para encerramento exclusivo.
        process_dir=root/'private/services'/('children-'+role)
        process_dir.mkdir(mode=0o700,exist_ok=True)
        env['MACIP_LAB_PROCESS_DIRECTORY']=str(process_dir)
    log=root/'private/services'/f'{role}.log'
    descriptor=os.open(log,os.O_WRONLY|os.O_CREAT|os.O_APPEND|os.O_NOFOLLOW,0o600)
    try:
        return subprocess.Popen(args,env=env,stdin=subprocess.DEVNULL,stdout=descriptor,stderr=descriptor,
                                start_new_session=True,cwd=root)
    finally:os.close(descriptor)


def stop_child(root,role,process,token):
    registry=root/'private/services'/('children-'+role)
    descendants=[]
    if registry.exists():
        for file in registry.iterdir():
            if file.name.isdigit():
                try: descendants.append(_private_file(file).decode())
                except OSError:pass
    for value in descendants:
        signal_identity(value,signal.SIGCONT,group=True)
        signal_identity(value,signal.SIGTERM,group=True)
    signal_identity(token,signal.SIGCONT,group=True)
    signal_identity(token,signal.SIGTERM,group=True)
    try:process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        signal_identity(token,signal.SIGKILL,group=True)
        process.wait(timeout=3)
    # Um worker pode iniciar outro comando durante sua parada graciosa.
    # Depois de o pai sair, nenhum registro novo pode ser produzido por ele.
    if registry.exists():
        for file in registry.iterdir():
            if file.name.isdigit():
                try:
                    value=_private_file(file).decode()
                    if value not in descendants:descendants.append(value)
                except OSError:pass
    for value in descendants:
        signal_identity(value,signal.SIGCONT,group=True)
        signal_identity(value,signal.SIGKILL,group=True)
    deadline=time.monotonic()+2
    while any(identity(int(value.split(':')[0]))==value for value in descendants) and time.monotonic()<deadline:
        time.sleep(.05)
    if any(identity(int(value.split(':')[0]))==value for value in descendants):
        raise RuntimeError('filho original ainda ativo; reserva não liberada')
    if role in ('primary','protection'):
        active=root/'private/services'/('active-'+role+'.json')
        if active.exists():
            reservation=json.loads(_private_file(active))
            cfg=json.loads(_private_file(root/'private/services'/f'{role}.json'))
            transport=ApiTransport(cfg['api'],cfg['token'],'supervisor-release',
                                  allow_lab_http=cfg.get('allow_http',False),ca_file=cfg.get('ca'))
            prefix='provisioner' if role=='primary' else 'protector'
            try:
                transport.request(f'/api/v1/{prefix}/operations/{reservation["id"]}/release',{'claim_id':reservation['claim_id']})
                active.unlink(missing_ok=True)
            except RuntimeError:pass # a reserva durável ainda expira normalmente se a API estiver fora



def serve(root):
    state=root/'private/services'; _directory(state)
    fd=os.open(state/'supervisor.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd);return
    stop=threading.Event()
    signal.signal(signal.SIGTERM,lambda *_:stop.set())
    signal.signal(signal.SIGINT,lambda *_:stop.set())
    own=identity(os.getpid())
    if own is None:raise RuntimeError('identidade POSIX não verificável')
    children={}; restarts={role:0 for role in ROLES}; retries={role:0 for role in ROLES}
    record={'supervisor':own,'children':{},'restarts':restarts}
    atomic_json(state/'processes.json',record)
    try:
        while not stop.is_set():
            for role in ROLES:
                proc,token=children.get(role,(None,None))
                if proc and proc.poll() is None:continue
                if proc:
                    stop_child(root,role,proc,token)
                    restarts[role]+=1
                    children.pop(role,None)
                    retries[role]=time.monotonic()+min(30,2*restarts[role])
                if time.monotonic()<retries[role]:continue
                proc=child(root,role)
                token=identity(proc.pid)
                children[role]=(proc,token)
            record['children']={role:token for role,(_,token) in children.items()}
            record['updated_at']=time.time()
            atomic_json(state/'processes.json',record)
            stop.wait(0.5)
    finally:
        for role in ('primary','protection','alerts','activation','central'):
            if role in children:
                proc,token=children[role];stop_child(root,role,proc,token)
        atomic_json(state/'processes.json',{'supervisor':None,'children':{},'restarts':restarts})
        os.close(fd)


def worker(root,role):
    cfg=json.loads(_private_file(root/'private/services'/f'{role}.json'))
    stop=threading.Event(); state={'value':'ready'}
    signal.signal(signal.SIGTERM,lambda *_:stop.set())
    if role=='alerts':
        os.environ.update(MACIP_API_DATABASE_URL=cfg['database'],MACIP_API_LAB_ALERTS='1')
        from .db import engine,migrate_now
        from .central_alerts import evaluate,deliver
        from .lab_automation_routes import heartbeat
        from sqlalchemy.orm import Session
        from datetime import UTC,datetime
        migrate_now()
        def once():
            with Session(engine()) as db:
                evaluate(db,datetime.now(UTC));deliver(db,cfg['smtp_port'])
        def pulse():
            with Session(engine()) as db:
                heartbeat(db,'alerts',state['value']);db.commit()
    else:
        actor=PrimaryWorker(cfg) if role=='primary' else ProtectionWorker(cfg)
        if cfg.get('storage'):
            from .storage_runtime import LabStorage
            storage=LabStorage(actor,'a' if role=='primary' else 'b')
            def once():
                actor.run_once()
                storage.tick()
        else:
            once=actor.run_once
        transport=ApiTransport(cfg['api'],cfg['token'],'heartbeat-'+role,allow_lab_http=cfg.get('allow_http',False),ca_file=cfg.get('ca'))
        prefix='/api/v1/provisioner' if role=='primary' else '/api/v1/protector'
        def pulse():transport.request(prefix+'/automation/heartbeat',{'state':state['value']})
    def keep_alive():
        while not stop.is_set():
            try:pulse()
            except Exception:pass  # não imprime corpo HTTP, token nem credencial
            stop.wait(5)
    thread=threading.Thread(target=keep_alive,daemon=True);thread.start()
    try:
        while not stop.is_set():
            state['value']='busy'
            try:once();state['value']='ready'
            except Exception:state['value']='retry'
            stop.wait(cfg.get('interval_seconds',2))
    finally:
        stop.set();thread.join(timeout=1)


def control(root,action,role=None):
    state=root/'private/services'; _directory(state)
    record_path=state/'processes.json'
    record=json.loads(_private_file(record_path)) if record_path.exists() else {}
    token=record.get('supervisor')
    running=bool(token and identity(int(token.split(':')[0]))==token)
    if action=='status':
        return {'running':running,**record}
    if action=='start':
        if running:return {'running':True,'already_running':True}
        if any(value and identity(int(value.split(':')[0]))==value for value in record.get('children',{}).values()):
            raise RuntimeError('workers próprios ainda ativos sem supervisor; execute services-stop antes de retomar')
        fd=os.open(state/'supervisor.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd);raise RuntimeError('supervisor em transição; tente novamente') from None
        os.close(fd)
        log=os.open(state/'supervisor.log',os.O_CREAT|os.O_APPEND|os.O_WRONLY|os.O_NOFOLLOW,0o600)
        try:
            proc=subprocess.Popen([sys.executable,'-m','macip_backup_api.lab_supervisor','serve',str(root)],
                   env=base_environment(),stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True,cwd=root)
        finally:os.close(log)
        for _ in range(100):
            if proc.poll() is not None:raise RuntimeError('supervisor não iniciou; confira log privado')
            if record_path.exists():
                current=json.loads(_private_file(record_path))
                if current.get('supervisor')==identity(proc.pid):return {'running':True,'supervisor':current['supervisor']}
            time.sleep(.05)
        raise RuntimeError('prazo de início do supervisor excedido')
    if not running:
        if action=='stop':
            class RecordedProcess:
                def __init__(self,value):self.value=value
                def wait(self,timeout):
                    deadline=time.monotonic()+timeout
                    while identity(int(self.value.split(':')[0]))==self.value:
                        if time.monotonic()>=deadline:raise subprocess.TimeoutExpired('worker próprio',timeout)
                        time.sleep(.05)
            for child_role in ('primary','protection','alerts','activation','central'):
                value=record.get('children',{}).get(child_role)
                if value:stop_child(root,child_role,RecordedProcess(value),value)
            atomic_json(record_path,{'supervisor':None,'children':{},'restarts':record.get('restarts',{})})
        return {'running':False}
    if action=='restart':
        if role not in ROLES:raise RuntimeError('papel de worker inválido')
        child_token=record.get('children',{}).get(role)
        if child_token:
            # O supervisor encerra também filhos registrados antes de repor o worker.
            signal_identity(child_token,signal.SIGKILL)
        return {'restart_requested':role}
    if action=='stop':
        signal_identity(token,signal.SIGTERM)
        for _ in range(500):
            if identity(int(token.split(':')[0]))!=token:return {'running':False}
            time.sleep(.05)
        raise RuntimeError('parada inconclusiva; preserve a área e inspecione os processos próprios')
    raise RuntimeError('operação de serviço inválida')


def main():
    p=argparse.ArgumentParser();p.add_argument('action',choices=('serve','worker','start','status','stop','restart'))
    p.add_argument('area',type=Path);p.add_argument('--role',choices=ROLES)
    args=p.parse_args();_directory(args.area)
    if args.action=='serve':serve(args.area)
    elif args.action=='worker':worker(args.area,args.role)
    else:print(json.dumps(control(args.area,args.action,args.role)))


if __name__=='__main__':main()
