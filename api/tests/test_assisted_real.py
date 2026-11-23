"""Fluxo real A/SFTP, B/Restic offline, TLS e supervisão. Windows não nativo."""
from contextlib import contextmanager
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import ssl
import subprocess
import sys
import time
from urllib.request import Request, urlopen
from urllib.error import HTTPError
import uuid
import zipfile

import pytest
from sqlalchemy import text

from conftest import requires_db, make_operator
from macip_backup_api.db import engine
from macip_backup_api.lab_runtime import command
from macip_backup_api.lab_supervisor import control
from macip_backup_api.protector import sftp_args_option
from test_lab_services_real import laboratory, wait_for, ADMIN
from test_protected_copy import enabled_protection

pytestmark = requires_db
REPO=Path(__file__).resolve().parents[2]


def test_real_replacement_offline_b_recovery_and_closure(client,tmp_path,enabled_protection):
    if os.environ.get('MACIP_TEST_ASSISTED_REAL')!='1':
        pytest.skip('prova assistida real não habilitada explicitamente')
    root=None;source=None
    try:
        with laboratory(tmp_path,storage=True) as (root,cfg,settings,request):
            def prep(aid):return request(f'/api/v1/installations/{aid}/preparation')
            def state(aid):return request(f'/api/v1/installations/{aid}/assisted')
            def idle(aid):
                with engine().connect() as db:return db.scalar(text("SELECT count(*) FROM provisioning_operations WHERE agent_id=:id AND state IN ('pending','running')"),dict(id=aid))==0
            identities=[]
            for n in range(2):
                created=request('/api/v1/clients/prepare',dict(id=str(uuid.uuid4()),client_name='Fictício assistido '+str(n)+' '+uuid.uuid4().hex[:8],
                    installation_identifier='pc-unico',machine_name='Windows fictício',suggested_time='11:00'))
                aid=created['installation']['id'];wait_for(lambda:prep(aid)['profile_available'],180)
                identities.append((aid,created['client_id'],prep(aid)['repository_id']))
            a,cid,repository_a=identities[0];b,_,repository_other=identities[1]
            assert repository_a!=repository_other
            tls=ssl.create_default_context(cafile=str(root/'private/cert.pem'))
            def raw(path,body=None):
                with urlopen(Request(path,data=json.dumps(body).encode() if body is not None else None,
                    headers={'Content-Type':'application/json',**ADMIN}),context=tls,timeout=30) as response:return response.read()
            def activate(aid,label):
                code=request('/api/v1/activation-codes',{'installation_id':aid})
                key=root/'private'/('device-'+label)
                command('ssh-keygen','-q','-t','ed25519','-N','','-f',str(key))
                public=' '.join(key.with_suffix('.pub').read_text().split()[:2])
                payload=dict(activation_code=code['activation_code'].split('#macip-activation=')[1],activation_id=str(uuid.uuid4()),
                    device_id=str(uuid.uuid4()),resume_secret=secrets.token_urlsafe(32),public_key=public)
                request('/api/v1/activations/redeem',payload)
                def delivered():
                    value=json.loads(raw(cfg['activation_delivery'],{k:payload[k] for k in ('activation_id','resume_secret')}))
                    if value['state']!='ready':
                        with engine().connect() as db:
                            failures=db.scalar(text("SELECT count(*) FROM provisioning_operations WHERE agent_id=:id AND kind='protect_installation' AND state='failed'"),dict(id=aid))
                        diagnostic=root/'private/services/last-protection-error.json'
                        if failures>=2 and diagnostic.exists():
                            info=json.loads(diagnostic.read_text())
                            raise AssertionError('Prova B pendente: '+json.dumps({k:info.get(k) for k in ('stage','category','reason','command','exit_code')}))
                    return value if value['state']=='ready' else None
                bundle=wait_for(delivered,180)
                token=request('/api/v1/activations/monitoring',{k:payload[k] for k in ('activation_id','resume_secret')})
                hosts=root/'private'/('hosts-'+label);hosts.write_text(bundle['profile']['known_hosts']+'\n');hosts.chmod(0o600)
                options=sftp_args_option(['-F','/dev/null','-oBatchMode=yes','-oStrictHostKeyChecking=yes','-oIdentitiesOnly=yes',
                    '-oIdentityAgent=none','-oGlobalKnownHostsFile=/dev/null','-oConnectTimeout=3',
                    '-oUserKnownHostsFile='+json.dumps(str(hosts),ensure_ascii=False),'-i',str(key)])
                def restic(*args):return command('restic','--no-cache','-r',bundle['profile']['repositorio'],'-o',options,*args,
                    env={'PATH':os.environ['PATH'],'RESTIC_PASSWORD':bundle['restic_password']},timeout=120)
                return restic,{'Authorization':'Bearer '+token['token']},payload
            restic,old_auth,old_activation=activate(a,'antigo')
            source=root/'private/origem com espaços ação';source.mkdir(mode=0o700)
            (source/'ação.txt').write_bytes('referência anterior\n'.encode())
            (source/'binário.bin').write_bytes(bytes(range(256))*1024)
            (source/'ignorar.tmp').write_bytes(b'excluido')
            expected={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in source.iterdir() if p.suffix!='.tmp'}
            def backup(run,auth):
                data=run('backup','--json','--exclude','*.tmp',str(source))
                snapshot=next(json.loads(line)['snapshot_id'] for line in data.splitlines() if json.loads(line).get('message_type')=='summary')
                now=datetime.now(UTC).isoformat()
                body=dict(execution_id=str(uuid.uuid4()),trigger='manual',started_at=now,finished_at=now,status='success',result_code='ok',repository_id=repository_a,snapshot_id=snapshot)
                request('/api/v1/windows-results/executions',body,auth)
                def copied():
                    with engine().connect() as db:return db.scalar(text('SELECT snapshot_b FROM protected_points WHERE agent_id=:id AND snapshot_a=:point ORDER BY confirmed_at DESC LIMIT 1'),dict(id=a,point=snapshot))
                pair=wait_for(copied,120);wait_for(lambda:idle(a))
                return snapshot,pair,body
            point_a,point_b,old_event=backup(restic,old_auth)
            before={p['id'] for p in json.loads(restic('snapshots','--json'))}
            current=prep(a);account='macip'+hashlib.sha256((cid+':pc-unico').encode()).hexdigest()[:20]
            runtime=json.loads((root/'a'/account/'sftp-runtime.json').read_text());container=runtime['container']
            authorized=command('docker','exec',container,'cat',f'/home/{account}/.ssh/authorized_keys').decode().splitlines()
            reader=next(line for line in authorized if 'internal-sftp -R' in line)
            operator=make_operator(client,ADMIN,login='assistido-'+uuid.uuid4().hex[:8],password=secrets.token_urlsafe(24))
            def visual(action,snapshot=None):
                out=root/'private'/('browser-'+action+'.json')
                command('node',str(REPO/'portal/e2e/assisted.browser.cjs'),cfg['api'],cid,action,str(out),snapshot or '',
                    env={**os.environ,'MACIP_TEST_BROWSER_LOGIN':operator['login'],'MACIP_TEST_BROWSER_PASSWORD':operator['password']},timeout=180)
                return json.loads(out.read_text()),Path(str(out)+'.zip')
            def action(aid,kind,epoch,**extra):
                return request(f'/api/v1/installations/{aid}/assisted',dict(id=str(uuid.uuid4()),action=kind,expected_epoch=epoch,confirmed=True,**extra))
            def finished(aid,oid):
                def check():
                    row=next(o for o in state(aid)['operations'] if o['id']==oid)
                    return row if row['state'] in ('succeeded','failed') else None
                return wait_for(check,120)
            if os.environ.get('MACIP_TEST_ASSISTED_BROWSER')=='1':
                replaced,_=visual('replace')
            else:replaced=action(a,'replace',1)
            assert finished(a,replaced['id'])['state']=='succeeded'
            assert action(a,'replace',1)['id']==replaced['id']
            assert state(a)['access_state']=='confirmed' and state(a)['lifecycle']=='awaiting_device'
            after=command('docker','exec',container,'cat',f'/home/{account}/.ssh/authorized_keys').decode().splitlines()
            assert after==[reader]
            with pytest.raises(RuntimeError):restic('cat','config')
            with pytest.raises(HTTPError) as denied:request('/api/v1/windows-results/executions',old_event,old_auth)
            assert denied.value.code==403
            new_restic,new_auth,_=activate(a,'novo')
            new_restic_profile=request(f'/api/v1/installations/{a}/profile')['repositorio']
            assert json.loads(new_restic('cat','config'))['id']==repository_a
            assert {p['id'] for p in json.loads(new_restic('snapshots','--json'))}==before
            (source/'ação.txt').write_bytes(b'geracao nova')
            (source/'binário.bin').unlink()
            new_point_a,new_point_b,new_event=backup(new_restic,new_auth)
            assert point_a!=new_point_a
            assert before.issubset({p['id'] for p in json.loads(new_restic('snapshots','--json'))})
            assert prep(b)['repository_id']==repository_other and prep(b)['profile_available']
            protected=Path(settings['volume'])/a
            config_before=hashlib.sha256((protected/'config').read_bytes()).hexdigest()
            from macip_backup_api.provisioner import LabFileVault
            b_password=LabFileVault(root/'b-state/vault').get(f'installation/{a}/bundle').decode()
            def b_inventory():
                return {p['id'] for p in json.loads(command(sys.executable,'-m','macip_backup_api.protection_offline',
                    shutil.which('restic'),'--no-cache','--no-lock','-r',str(protected),'snapshots','--json',
                    env={'PATH':os.environ['PATH'],'RESTIC_PASSWORD':b_password,'PYTHONPATH':str(REPO/'api/src')}))}
            b_points_before=b_inventory()
            # Só A da primeira instalação é desligado; B não tem acesso à origem Windows.
            command('docker','stop','-t','1',container)
            source.chmod(0o000)
            assert not os.access(source,os.R_OK|os.X_OK)
            assert not json.loads(command('docker','inspect',container))[0]['State']['Running']
            if os.environ.get('MACIP_TEST_ASSISTED_BROWSER')=='1':
                recovered,archive=visual('recover',point_b)
                restore=recovered['restore']
            else:
                catalog=action(a,'catalog',2);assert finished(a,catalog['id'])['state']=='succeeded'
                restore=action(a,'restore',2,catalog_id=catalog['id'],snapshot_b=point_b)
                assert finished(a,restore['id'])['state']=='succeeded'
                archive=root/'private/recuperado.zip'
                archive.write_bytes(raw(cfg['api']+f'/api/v1/installations/{a}/assisted/operations/{restore["id"]}/download'))
            terminal=finished(a,restore['id']);assert terminal['state']=='succeeded'
            assert terminal['result']['archive_sha256']==hashlib.sha256(archive.read_bytes()).hexdigest()
            with zipfile.ZipFile(archive) as data:
                receipt=json.loads(data.read('RECOVERY.json'))
                assert receipt['snapshot_a']==point_a and receipt['snapshot_b']==point_b
                actual={Path(name).name:hashlib.sha256(data.read('files/'+name)).hexdigest() for name in receipt['files']}
                assert actual==expected and 'ignorar.tmp' not in actual
            pwsh=os.environ.get('MACIP_TEST_POWERSHELL');windows=os.environ.get('MACIP_TEST_WINDOWS_WORKTREE')
            if pwsh and windows:
                parent=root/'private/windows destino ação';parent.mkdir(mode=0o700)
                harness=root/'private/importar.ps1'
                harness.write_text("param($Module,$Archive,$Parent)\n$ErrorActionPreference='Stop'\n. $Module\nImport-AssistedRecovery -Archive $Archive -Parent $Parent|ConvertTo-Json -Compress\n")
                imported=json.loads(command(pwsh,'-NoProfile','-File',str(harness),str(Path(windows)/'agent/windows/gui/Assisted-Recovery.ps1'),str(archive),str(parent)))
                assert imported['State']=='verified'
                recovered_files=Path(imported['Destination'])/'arquivos'
                assert {p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in recovered_files.rglob('*') if p.is_file()}==expected
            # Nova consulta permite uma operação distinta; volume ausente não publica sucesso parcial.
            wait_for(lambda:idle(a));catalog=action(a,'catalog',2);assert finished(a,catalog['id'])['state']=='succeeded'
            marker=Path(settings['volume'])/'.macip-volume';saved=marker.read_bytes();marker.unlink()
            try:
                failed=action(a,'restore',2,catalog_id=catalog['id'],snapshot_b=point_b)
                assert finished(a,failed['id'])['state']=='failed'
                assert not (root/'private/recovery-exports'/(failed['id']+'.zip')).exists()
            finally:marker.write_bytes(saved);marker.chmod(0o600)
            assert hashlib.sha256((protected/'config').read_bytes()).hexdigest()==config_before
            assert b_inventory()==b_points_before
            wait_for(lambda:idle(a))
            closed,_=visual('close') if os.environ.get('MACIP_TEST_ASSISTED_BROWSER')=='1' else (action(a,'close',2),None)
            assert finished(a,closed['id'])['state']=='failed'
            assert state(a)['lifecycle']=='closing' and state(a)['access_state']=='failed'
            assert (Path(settings['volume'])/a/'config').exists()
            assert prep(b)['repository_id']==repository_other
            # Retorno da infraestrutura original, sem recriar repositório ou identidade.
            command('docker','start',container)
            command('docker','exec','-d',container,'/usr/sbin/sshd','-D','-e','-f','/dev/null','-h','/hostkey',
                '-oUsePAM=no','-oPasswordAuthentication=no','-oKbdInteractiveAuthentication=no','-oPermitRootLogin=no',
                '-oDisableForwarding=yes','-oAuthorizedKeysFile=.ssh/authorized_keys','-oAllowUsers='+account,
                '-oChrootDirectory=/jail','-oSubsystem=sftp internal-sftp')
            retried=request(f'/api/v1/installations/{a}/assisted/operations/{closed["id"]}/retry',{})
            terminal=finished(a,retried['id'])
            assert terminal['state']=='succeeded',terminal['error_code']
            assert state(a)['lifecycle']=='closed'
            assert command('docker','exec',container,'cat',f'/home/{account}/.ssh/authorized_keys')==b''
            with pytest.raises(HTTPError) as denied:request('/api/v1/windows-results/executions',new_event,new_auth)
            assert denied.value.code==403
            assert (Path(settings['volume'])/a/'config').exists()
            # Reabertura confere o repositório existente e exige prova/ativação novas.
            reopened=action(a,'reopen',3)
            reopened_result=finished(a,reopened['id'])
            current_port=int(command('docker','port',container,'22/tcp').decode().strip().rsplit(':',1)[1])
            # Docker pode realocar porta efêmera após stop/start. Isso NÃO é
            # continuidade autorizada: o executor deve recusar a reabertura.
            from macip_backup_api.provisioning_sftp import repository_url
            if repository_url(account,cfg['ip'],current_port) != new_restic_profile:
                assert reopened_result['state']=='failed' and reopened_result['error_code']=='identity_changed'
                assert not prep(a)['activation_available']
            else:
                assert reopened_result['state']=='succeeded'
                wait_for(lambda:prep(a)['activation_available'],180)
            assert prep(a)['repository_id']==repository_a
            # Segunda instalação sem mudança de endereço: fechar/reabrir deve
            # preservar seus próprios pontos e exigir nova ativação/prova.
            wait_for(lambda:idle(b))
            other_close=action(b,'close',1);assert finished(b,other_close['id'])['state']=='succeeded'
            other_open=action(b,'reopen',2);assert finished(b,other_open['id'])['state']=='succeeded'
            wait_for(lambda:prep(b)['activation_available'],180)
            assert state(b)['lifecycle']=='awaiting_device' and not prep(b)['profile_available']
            assert prep(b)['repository_id']==repository_other
            with engine().connect() as db:
                assert db.scalar(text('SELECT count(*) FROM windows_results WHERE agent_id=:id'),dict(id=a))==2
    finally:
        if source and source.exists():source.chmod(0o700)
        if root and root.exists():shutil.rmtree(root)
