"""Guardião estável do laboratório; não pertence ao código que está sendo trocado."""
import fcntl
import json
import os
from pathlib import Path
import sys

from fastapi import Depends
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

DRAIN_ALLOWED=('/api/v1/provisioner/','/api/v1/protector/','/api/v1/agent/',
               '/api/v1/windows-results/','/api/v1/auth/')


def public_state(root):
    directory=root/'updates'
    def read(name):
        p=directory/name
        return json.loads(p.read_text()) if p.exists() else {}
    active=read('active.json');transaction=read('transaction.json');gate=read('gate.json')
    state=transaction.get('state','idle')
    if state=='running' and transaction.get('executor'):
        token=transaction['executor'];pid=token.split(':')[0]
        try:
            fields=Path('/proc',pid,'stat').read_text().rsplit(')',1)[1].split()
            current=None if fields[0]=='Z' else pid+':'+fields[19]
        except (FileNotFoundError,ProcessLookupError):current=None
        if current!=token:state='needs-recovery'
    return dict(commit=active.get('commit'),schema=active.get('schema'),windows=active.get('windows'),
        phase=gate.get('phase','open'),state=state,step=transaction.get('step'),
        error=transaction.get('error'),updated_at=transaction.get('updated_at'),
        needs_reconciliation=gate.get('phase')=='reconcile',inventory_warning=transaction.get('inventory_warning',0))


def attach(app,root):
    from macip_backup_api.auth import require_viewer
    directory=root/'updates';directory.mkdir(mode=0o700,exist_ok=True)
    class Gate(BaseHTTPMiddleware):
        async def dispatch(self,request,call_next):
            if request.method in ('GET','HEAD','OPTIONS'):
                if public_state(root)['phase']!='open' and request.url.path.endswith('/profile'):
                    return JSONResponse({'detail':'perfil bloqueado até concluir manutenção e prova B'},status_code=503)
                return await call_next(request)
            fd=os.open(directory/'writers.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
            try:
                try:fcntl.flock(fd,fcntl.LOCK_SH|fcntl.LOCK_NB)
                except BlockingIOError:return JSONResponse({'detail':'atualização: escrita temporariamente suspensa'},status_code=503)
                state=public_state(root);phase=state['phase'];path=request.url.path
                allowed=phase=='open'
                if phase in ('drain','reconcile'):
                    allowed=path.startswith(DRAIN_ALLOWED)
                if not allowed:return JSONResponse({'detail':'laboratório em manutenção; resultado local deve permanecer na fila'},status_code=503)
                return await call_next(request)
            finally:os.close(fd)
    app.add_middleware(Gate)
    @app.get('/api/v1/lab/version',dependencies=[Depends(require_viewer)])
    def version():return public_state(root)


def main():
    import argparse,runpy
    p=argparse.ArgumentParser();p.add_argument('root',type=Path);p.add_argument('source',type=Path)
    args=p.parse_args();root=args.root;source=args.source
    sys.path.insert(0,str(source/'api/src'))
    helper=runpy.run_path(str(source/'tools/windows-lab-server.py'),run_name='lab_helper')
    cfg=json.loads((root/'private/config.json').read_text())
    tls=json.loads((root/'private/services/central.json').read_text())
    os.environ.update(helper['api_env'](cfg),SSL_CERT_FILE=tls['ca'])
    from macip_backup_api.app import create_app
    from fastapi.staticfiles import StaticFiles
    from fastapi.responses import FileResponse
    from fastapi import HTTPException
    import uvicorn
    app=create_app();attach(app,root)
    app.mount('/assets',StaticFiles(directory=source/'portal/dist/assets'))
    @app.get('/{path:path}')
    def frontend(path:str):
        if path.startswith('api/'):raise HTTPException(404,'rota inexistente')
        return FileResponse(source/'portal/dist/index.html')
    uvicorn.run(app,host=cfg['ip'],port=cfg['api_port'],ssl_certfile=tls['certificate'],ssl_keyfile=tls['key'],log_level='warning')


if __name__=='__main__':main()
