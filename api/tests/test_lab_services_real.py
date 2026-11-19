"""Supervisor, HTTPS, PostgreSQL, SFTP, Restic e B reais em área descartável."""
import base64
from contextlib import contextmanager,ExitStack
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import socket
import ssl
import subprocess
import time
import tempfile
import sys
import uuid
from urllib.request import Request,urlopen

import pytest
from sqlalchemy import text

from conftest import requires_db,TEST_URL,ADMIN_TOKEN,make_operator
from macip_backup_api.db import engine
from macip_backup_api.lab_runtime import json_once,command
from macip_backup_api.lab_services import configure_services
from macip_backup_api.lab_supervisor import control,identity,signal_identity
from test_protected_copy import enabled_protection,TOKEN

pytestmark=requires_db
ADMIN={'Authorization':f'Bearer {ADMIN_TOKEN}'}


def wait_for(fn,seconds=120):
    until=time.monotonic()+seconds
    last=None
    while time.monotonic()<until:
        try:
            value=fn()
            if value:return value
        except (OSError,ValueError,KeyError) as error:last=type(error).__name__
        time.sleep(.2)
    raise AssertionError('prazo da prova excedido; última categoria='+str(last))


def port():
    with socket.socket() as s:s.bind(('127.0.0.1',0));return s.getsockname()[1]


@contextmanager
def laboratory(tmp_path):
    image=os.environ.get('MACIP_TEST_PROVISIONING_SFTP_IMAGE')
    if not image:pytest.skip('imagem OpenSSH descartável não configurada')
    root=tmp_path/'laboratorio automático';root.mkdir(mode=0o700)
    for name in ('a','b-state','private','evidence'):(root/name).mkdir(mode=0o700)
    cert=root/'private/cert.pem';key=root/'private/tls-key.pem'
    command('openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1','-subj','/CN=localhost',
            '-addext','subjectAltName=IP:127.0.0.1,DNS:localhost','-keyout',str(key),'-out',str(cert))
    key.chmod(0o600)
    api_port,delivery=port(),port()
    cfg=dict(api=f'https://127.0.0.1:{api_port}',activation_public=f'https://127.0.0.1:{api_port}',
             activation_delivery=f'https://127.0.0.1:{delivery}/v1/activate',api_port=api_port,ip='127.0.0.1',
             database=TEST_URL,admin=ADMIN_TOKEN,a_token='test-provisioner-token-1234567890abcdef',b_token=TOKEN,
             sftp_image=image,sshd='unused-'+uuid.uuid4().hex,pg='external-test-pg')
    json_once(root/'private/config.json',cfg)
    settings=None
    try:
        settings=configure_services(root,cfg,certificate=str(cert),key=str(key),ca=str(cert),delivery_port=delivery,mailpit_image='axllent/mailpit:v1.29.7')
        helper=Path(__file__).resolve().parents[2]/'tools/windows-lab-server.py'
        started=json.loads(command(sys.executable,str(helper),'services-start',str(root)))
        assert started['running']
        assert control(root,'start')['already_running']
        context=ssl.create_default_context(cafile=str(cert))
        def request(path,body=None,headers=None):
            with urlopen(Request(cfg['api']+path,data=json.dumps(body).encode() if body is not None else None,
                headers={'Content-Type':'application/json',**(headers or ADMIN)},method='POST' if body is not None else 'GET'),context=context,timeout=15) as response:
                return json.load(response)
        wait_for(lambda:request('/healthz'))
        wait_for(lambda:all(not w['stale'] for w in request('/api/v1/automation/health')))
        yield root,cfg,settings,request
    finally:
        if (root/'private/services').exists():
            control(root,'stop')
            assert not control(root,'status')['running']
        # Somente os recursos marcados com esta raiz exata.
        listing=subprocess.run(['docker','ps','-aq','--filter','label=macip.lab.root='+str(root)],capture_output=True,check=True,text=True).stdout.split()
        for container in listing:command('docker','rm','-f',container)
        if settings:
            name=settings['mailpit']
            label=command('docker','inspect','-f','{{index .Config.Labels "macip.windows-lab"}}',name).decode().strip()
            assert label==str(root)
            command('docker','rm','-f',name)
            volume=Path(settings['volume'])
            assert (volume/'.macip-volume').read_text()==settings['volume_id']
            shutil.rmtree(volume)


def test_supervised_auto_prepare_restart_activation_and_pending_points(client,tmp_path,enabled_protection,monkeypatch):
    if (os.environ.get('MACIP_TEST_AUTOMATION_BROWSER')!='1' or
        not os.environ.get('MACIP_TEST_POWERSHELL') or not os.environ.get('MACIP_TEST_WINDOWS_WORKTREE')):
        pytest.skip('prova completa exige Chrome e consumidor PowerShell/worktree explicitamente configurados')
    monkeypatch.setenv('MACIP_API_LAB_AUTOMATION','1')
    monkeypatch.setenv('MACIP_API_LAB_ALERTS','1')
    with laboratory(tmp_path) as (root,cfg,settings,request), ExitStack() as resources:
        def prepare(name):
            body=dict(id=str(uuid.uuid4()),client_name=name,installation_identifier='pc-lab',machine_name='PC Fictício',suggested_time='10:00')
            first=request('/api/v1/clients/prepare',body)
            assert request('/api/v1/clients/prepare',body)['installation']['id']==first['installation']['id']
            return first['installation']['id']
        visual=None
        if os.environ.get('MACIP_TEST_AUTOMATION_BROWSER')=='1':
            op=make_operator(client,ADMIN,login='auto-'+uuid.uuid4().hex[:8],name='Operador fictício',password=secrets.token_urlsafe(24))
            result_file=root/'private/browser-result.json'
            script=Path(__file__).resolve().parents[2]/'portal/e2e/lab-automation.browser.cjs'
            command('node',str(script),cfg['api'],'Cliente fictício A '+uuid.uuid4().hex[:8],str(result_file),
                    env={**os.environ,'MACIP_TEST_BROWSER_LOGIN':op['login'],'MACIP_TEST_BROWSER_PASSWORD':op['password']},timeout=210)
            visual=json.loads(result_file.read_text())
            a=visual['installation_id']
        else:a=prepare('Cliente fictício A '+uuid.uuid4().hex[:8])
        b=prepare('Cliente fictício B '+uuid.uuid4().hex[:8])
        # Reserva de B real em andamento; reinício substitui somente o worker deste laboratório.
        def running_b():
            with engine().connect() as db:
                if db.scalar(text("SELECT count(*) FROM provisioning_operations WHERE kind IN ('prepare_primary','retry_primary') AND state='failed'")):
                    raise AssertionError('preparação A falhou; confira diagnóstico privado sanitizado')
                return db.execute(text("SELECT id FROM provisioning_operations WHERE kind='protect_installation' AND state='running' LIMIT 1")).first()
        wait_for(running_b)
        before=control(root,'status')['children']['protection']
        control(root,'restart','protection')
        wait_for(lambda:control(root,'status').get('children',{}).get('protection') not in (None,before))
        def ready(aid):return request(f'/api/v1/installations/{aid}/preparation')['profile_available']
        wait_for(lambda:ready(a) and ready(b),seconds=180)
        assert control(root,'status')['restarts']['protection']>=1
        first=request(f'/api/v1/installations/{a}/preparation')
        repo=first['repository_id']
        code=visual['code'] if visual else request('/api/v1/activation-codes',{'installation_id':a})
        pwsh=os.environ.get('MACIP_TEST_POWERSHELL')
        windows=os.environ.get('MACIP_TEST_WINDOWS_WORKTREE')
        harness=None;winbase=None
        if pwsh and windows:
            winbase=Path(resources.enter_context(tempfile.TemporaryDirectory(prefix='macip-auto-windows-')))
            # A GUI no Linux inicia consultas pelo pwsh; o shim chama Restic real.
            (winbase/'restic-real.ps1').write_text('& /usr/bin/restic @args\nexit $LASTEXITCODE')
            code_file=winbase/'code.json';code_file.write_text(json.dumps(code));code_file.chmod(0o600)
            harness=winbase/'consumer.ps1'
            harness.write_text(r'''
param($Windows,$Base,$CodeFile,$Action,$Diagnostic)
$ErrorActionPreference='Stop'
$script:phase='load'
trap {
 $diagnostic=@{stage=$script:phase;category=$_.Exception.GetType().Name;line=$_.InvocationInfo.ScriptLineNumber;file=[IO.Path]::GetFileName($_.InvocationInfo.ScriptName)}
 $diagnostic | ConvertTo-Json -Compress | Set-Content -LiteralPath $Diagnostic -Encoding UTF8
 exit 1
}
. (Join-Path $Windows 'agent/windows/gui/MacipBackup-Gui.ps1') -BaseDir $Base -AgentePath (Join-Path $Windows 'agent/windows/Backup-MacipCliente.ps1')
function Protect-ResticPassword([securestring]$Senha,[string]$Destino) {
 $ptr=[Runtime.InteropServices.Marshal]::SecureStringToBSTR($Senha)
 try{$v=[Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr)}finally{[Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr)}
 [IO.File]::WriteAllText($Destino,[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($v)))
}
function Read-ResticPassword([string]$Caminho){ConvertTo-SecureString ([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String([IO.File]::ReadAllText($Caminho)))) -AsPlainText -Force}
function Get-SshExe {'/usr/bin/ssh'}
$script:ResticExeNome='restic-real.ps1'
$directory=Join-Path $Base 'activation-code'
if($Action -in @('start','activate')){
 $code=(Get-Content -LiteralPath $CodeFile -Raw|ConvertFrom-Json).activation_code
 $script:phase='activation'
 $result=Invoke-CodeActivation -Code $code -Directory $directory -WaitSeconds $(if($Action -eq 'start'){0}else{90}) -PollSeconds 1
 Write-MonitorJson (Join-Path $Base 'activation-state.json') @{state=$result.State}
 if($result.State -eq 'ready'){
  $source=Join-Path $Base 'origem ação';New-Item -ItemType Directory $source|Out-Null
  [IO.File]::WriteAllText((Join-Path $source 'ignorar.tmp'),'excluido')
  $script:phase='build'
  $built=Build-Configuracao $result.Profile $result.Key $result.Password $source @('*.tmp') '10:00'
  Write-MonitorJson (Join-Path $Base 'built.json') $built
  $script:phase='schedule'
  Set-MonitorSchedule $built.Config '10:00' $false
  $script:phase='sync'
  [void](Sync-MonitorQueue $built.Config)
 }
}elseif($Action -eq 'backup'){
 $built=Get-Content (Join-Path $Base 'built.json') -Raw|ConvertFrom-Json
 $result=Invoke-AgenteBackup -Config $built.Config -DiretorioEstado $built.Estado
 Write-MonitorJson (Join-Path $Base 'last-backup.json') $result
 if($result.status -ne 'sucesso'){throw 'backup real nao concluiu'}
}elseif($Action -eq 'sync'){
 $built=Get-Content (Join-Path $Base 'built.json') -Raw|ConvertFrom-Json
 [void](Sync-MonitorQueue $built.Config)
}
''')
            def consumer(action):
                return command(pwsh,'-NoProfile','-File',str(harness),windows,str(winbase),str(code_file),action,str(root/'private/windows-diagnostic.json'),
                    env={**os.environ,'SSL_CERT_FILE':str(root/'private/cert.pem'),'NO_PROXY':'127.0.0.1,localhost'},timeout=180)
            consumer('start')
            assert json.loads((winbase/'activation-state.json').read_text())['state']=='pending'
            original_hash=hashlib.sha256((winbase/'activation-code/id_ed25519').read_bytes()).hexdigest()
            consumer('activate')
            assert json.loads((winbase/'activation-state.json').read_text())['state']=='ready'
            assert hashlib.sha256((winbase/'activation-code/id_ed25519').read_bytes()).hexdigest()==original_hash
            attempt=json.loads((winbase/'activation-code/attempt.json').read_text())
            resume=base64.b64decode((winbase/'activation-code/resume.dpapi').read_text()).decode()
            activation={**attempt,'resume_secret':resume}
            key_path=winbase/'activation-code/id_ed25519'
        else:
            key_path=root/'private/test-windows-key'
            command('ssh-keygen','-q','-t','ed25519','-N','','-f',str(key_path))
            public=' '.join(key_path.with_suffix('.pub').read_text().split()[:2])
            activation=dict(activation_code=code['activation_code'].split('#macip-activation=')[1],activation_id=str(uuid.uuid4()),device_id=str(uuid.uuid4()),resume_secret=secrets.token_urlsafe(32),public_key=public)
            request('/api/v1/activations/redeem',activation)
        def deliver():
            with urlopen(Request(cfg['activation_delivery'],data=json.dumps({'activation_id':activation['activation_id'],'resume_secret':activation['resume_secret']}).encode(),headers={'Content-Type':'application/json'}),context=ssl.create_default_context(cafile=str(root/'private/cert.pem')),timeout=20) as response:return json.load(response)
        # Nenhum comando refresh/copy/prove por instalação.
        bundle=wait_for(lambda:(value if (value:=deliver())['state']=='ready' else None),seconds=180)
        assert bundle['profile']['repositorio_id']==repo
        assert request(f'/api/v1/installations/{a}/preparation')['profile_available']
        enrollment=request('/api/v1/activations/monitoring',{'activation_id':activation['activation_id'],'resume_secret':activation['resume_secret']})
        auth={'Authorization':'Bearer '+enrollment['token']}
        # Prazo curto explícito somente na configuração deste ensaio fictício.
        with engine().connect() as db:
            previous=db.scalar(text('SELECT schedule_id FROM windows_monitors WHERE agent_id=:id'),{'id':a})
        request('/api/v1/windows-results/schedule',dict(previous_schedule_id=str(previous) if previous else None,
            schedule_id=str(uuid.uuid4()),schedule=dict(kind='daily',timezone='UTC',hour=10,minute=0,tolerance_seconds=60,copy_seconds=60),paused=False),auth)
        # Suspende somente o worker B próprio: acumula três pontos REAIS, não só o último.
        child=control(root,'status')['children']['protection'];signal_identity(child,signal.SIGSTOP)
        source=(winbase/'origem ação') if winbase else root/'private/windows-source'
        source.mkdir(mode=0o700,exist_ok=True)
        hosts=root/'private/test-hosts';hosts.write_text(bundle['profile']['known_hosts']+'\n');hosts.chmod(0o600)
        from macip_backup_api.protector import sftp_args_option
        options=sftp_args_option(['-F','/dev/null','-oBatchMode=yes','-oStrictHostKeyChecking=yes','-oIdentitiesOnly=yes',
              '-oIdentityAgent=none','-oGlobalKnownHostsFile=/dev/null','-oUserKnownHostsFile='+json.dumps(str(hosts),ensure_ascii=False),'-i',str(key_path)])
        def restic(*args):return command('restic','--no-cache','-r',bundle['profile']['repositorio'],'-o',options,*args,env={'PATH':os.environ['PATH'],'RESTIC_PASSWORD':bundle['restic_password']})
        points=[]
        from datetime import UTC,datetime
        for n in range(3):
            (source/'fictício.txt').write_text('geração '+str(n))
            if harness:
                consumer('backup')
                snapshot=json.loads((winbase/'last-backup.json').read_text(encoding='utf-8-sig'))['snapshot']
            else:
                output=restic('backup','--json',str(source))
                snapshot=next(json.loads(line)['snapshot_id'] for line in output.splitlines() if json.loads(line)['message_type']=='summary')
                now=datetime.now(UTC).isoformat()
                result=dict(execution_id=str(uuid.uuid4()),trigger='manual',started_at=now,finished_at=now,status='success',result_code='ok',repository_id=repo,snapshot_id=snapshot)
                request('/api/v1/windows-results/executions',result,auth)
                request('/api/v1/windows-results/executions',result,auth)
            points.append(snapshot)
        with engine().connect() as db:
            count=db.scalar(text('SELECT count(*) FROM windows_results WHERE agent_id=:id'),{'id':a})
            assert count==3
        # Relógio REAL: B parado, três snapshots confirmados vencem o prazo de 60 s.
        def copy_alerts():
            alerts=request(f'/api/v1/installations/{a}/alerts')['alerts']
            return [event for event in alerts if event['kind']=='copy_late' and not event['resolved_at']]
        alerts=wait_for(lambda:(value if len(value:=copy_alerts())==3 else None),seconds=90)
        assert next(w for w in request('/api/v1/automation/health') if w['role']=='protection')['stale']
        if visual:
            command('node',str(Path(__file__).resolve().parents[2]/'portal/e2e/lab-alerts.browser.cjs'),cfg['api'],visual['client_id'],
                env={**os.environ,'MACIP_TEST_BROWSER_LOGIN':op['login'],'MACIP_TEST_BROWSER_PASSWORD':op['password']},timeout=60)
        else:
            for alert in alerts:request(f'/api/v1/alerts/{alert["id"]}/ack',{},ADMIN)
        def delivered():
            with urlopen('http://127.0.0.1:'+str(settings['mailpit_web_port'])+'/api/v1/messages') as response:
                return json.load(response)['total']>=3
        wait_for(delivered,seconds=15)
        signal_identity(child,signal.SIGCONT)
        def all_copied():
            with engine().connect() as db:
                return set(points).issubset({r[0] for r in db.execute(text('SELECT snapshot_a FROM protected_points WHERE agent_id=:id'),{'id':a})})
        wait_for(all_copied,seconds=150)
        other=request(f'/api/v1/installations/{b}/preparation')
        assert other['repository_id']!=repo
        with engine().connect() as db:
            assert not set(points)&{r[0] for r in db.execute(text('SELECT snapshot_a FROM protected_points WHERE agent_id=:id'),{'id':b})}
        assert restic('cat','config')
        wait_for(lambda:not copy_alerts(),seconds=30)
        if harness:
            # Central parada em SIGSTOP, somente neste laboratório. SFTP segue disponível.
            server=control(root,'status')['children']['central']
            signal_identity(server,signal.SIGSTOP)
            try:
                (source/'fictício.txt').write_text('backup durante indisponibilidade')
                consumer('backup')
                assert json.loads((winbase/'last-backup.json').read_text(encoding='utf-8-sig'))['status']=='sucesso'
                monitor=json.loads((winbase/'config-1/monitoring.json').read_text())
                queue=Path(monitor['queue'])
                pending=list(queue.glob('event-*.json'))
                assert len(pending)==1
                execution_id=json.loads(pending[0].read_text())['execution_id']
            finally:signal_identity(server,signal.SIGCONT)
            consumer('sync') # simula somente o disparo da tarefa COM, não executa backup
            assert not list(queue.glob('event-*.json'))
            history=request('/api/v1/executions?agent_id='+a)
            assert len(history)==4 and sum(row['execution_id']==execution_id for row in history)==1
            points.append(json.loads((winbase/'last-backup.json').read_text(encoding='utf-8-sig'))['snapshot'])
            wait_for(all_copied,seconds=90)
