"""Fluxo HTTPS/PG/workers/SFTP/Restic real. Não envolve o aplicativo Windows."""
from contextlib import ExitStack
import base64
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
import uuid

import pytest

from conftest import requires_db,make_operator
from macip_backup_api.lab_runtime import command,write_once
from macip_backup_api.protector import sftp_args_option
from macip_backup_api.lab_supervisor import control,signal_identity
from macip_backup_api.storage_runtime import LabStorage
from test_lab_services_real import laboratory,wait_for
from test_protected_copy import enabled_protection
from macip_backup_api.provisioner import LabFileVault

pytestmark=requires_db


def test_supervised_capacity_retention_real_sftp_and_protected_restore(client,tmp_path,enabled_protection,monkeypatch):
    if not os.environ.get('MACIP_TEST_PROVISIONING_SFTP_IMAGE'):
        pytest.skip('fixture OpenSSH descartável não configurado')
    monkeypatch.setenv('MACIP_API_LAB_AUTOMATION','1');monkeypatch.setenv('MACIP_API_LAB_STORAGE','1')
    with laboratory(tmp_path,storage=True) as (root,cfg,settings,request):
        def create(name):
            row=request('/api/v1/clients/prepare',dict(id=str(uuid.uuid4()),client_name=name+' '+uuid.uuid4().hex[:8],
                installation_identifier='pc-ficticio',machine_name='Máquina fictícia',suggested_time='10:00'))
            return row['installation']['id'],row['client_id']
        aid,client_id=create('Retenção A');other,_=create('Retenção isolada')
        def ready(a):return request(f'/api/v1/installations/{a}/preparation')['profile_available']
        wait_for(lambda:ready(aid) and ready(other),180)
        base=f'/api/v1/installations/{aid}/storage'
        wait_for(lambda:all(not c['stale'] for c in request(base)['capacity'].values()),90)
        first=request(base)
        assert first['automatic'] is False
        assert first['capacity']['a']['last_good']['scope']=='shared-volume'
        assert first['capacity']['b']['last_good']['scope']=='shared-volume'
        prep=request(f'/api/v1/installations/{aid}/preparation')
        # Credenciais são exclusivamente sintéticas deste laboratório e nunca
        # vão aos resultados públicos ou ao diagnóstico.
        def remote_for(a):
            prep=request(f'/api/v1/installations/{a}/preparation')
            profile=request(f'/api/v1/installations/{a}/profile')
            bundle=json.loads(LabFileVault(root/'private/vault-a').get(f'installation/{a}/bundle'))
            setup=root/'private/primary'/a
            args=sftp_args_option(['-F','/dev/null','-oBatchMode=yes','-oIdentitiesOnly=yes','-oIdentityAgent=none',
                '-oStrictHostKeyChecking=yes','-oGlobalKnownHostsFile=/dev/null',
                '-oUserKnownHostsFile='+json.dumps(str(setup/'known_hosts'),ensure_ascii=False),'-i',str(setup/'initial-client-key')])
            def run(*cmd):
                return command('restic','--no-cache','-r',profile['repositorio'],'-o',args,*cmd,
                    env={'PATH':os.environ['PATH'],'RESTIC_PASSWORD':bundle['restic_password']},timeout=45)
            return run
        remote=remote_for(aid);remote_other=remote_for(other)
        before_other=remote_other('--no-lock','snapshots','--json')
        src=root/'origem com espaços á';src.mkdir(mode=0o700)
        originals={};points=[]
        for n in range(5):
            data=secrets.token_bytes(2048+n);(src/'arquivo.bin').write_bytes(data)
            out=remote('backup','--json',str(src));sid=json.loads(out.splitlines()[-1])['snapshot_id']
            originals[sid]=hashlib.sha256(data).hexdigest();points.append(sid)
        # Só três dos cinco pontos têm cópia. Um ponto pendente antigo deve
        # permanecer mesmo fora de keep_last; o último também é preservado.
        from macip_backup_api.lab_runtime import ProtectionWorker
        bcfg=json.loads((root/'private/services/protection.json').read_text())
        actor=ProtectionWorker(bcfg)
        for point in (points[0],points[1],points[3]):
            actor.executor.execute(actor.api,uuid.UUID(aid),'copy',snapshot=point,
                source_key=root/'b-state/readers'/aid/'id_ed25519',source_password_file=root/'reader-channel'/aid/'restic-password')
        # O limite pode ser alto para provar o aviso, sem encher o disco do host.
        request(base+'/policy',dict(keep_a=1,keep_b=1,low_a=first['capacity']['a']['last_good']['total'],low_b=first['capacity']['b']['last_good']['total']),method='PUT')
        wait_for(lambda:all(c['low'] for c in request(base)['capacity'].values()),90)
        # Espera inventário B atualizado antes da simulação; nenhuma intervenção
        # manual de refresh/copy/prove é necessária para a operação da retenção.
        def receipts():
            path=root/'private/storage'/(aid+'-b.json')
            if not path.exists():return False
            return len(json.loads(path.read_text())['points'])>=4
        wait_for(receipts,90)
        def op(side,action='simulate',simulation=None):
            body=dict(id=str(uuid.uuid4()),side=side,action=action)
            if simulation:body['simulation']=simulation
            response=request(base+'/operations',body)
            # O mesmo pedido retorna o mesmo ID e não cria outra exclusão.
            assert request(base+'/operations',body)['id']==response['id']
            def done():
                row=next(o for o in request(base)['operations'] if o['id']==response['id'])
                return row if row['state'] in ('succeeded','failed') else None
            return wait_for(done,100)
        def approved(side,action='simulate',simulation=None):
            # Medição do outro worker pode manter o lock por poucos segundos.
            # Reproduz a orientação visível: recusa sem remoções, nova simulação
            # explícita e novo pedido. Não trata falha real como sucesso.
            for attempt in range(5):
                result=op(side,action,simulation)
                if result['state']=='succeeded':return result
                if result['error']!='busy':return result
                assert not result['result']['removed']
                time.sleep(2)
                if action=='execute':
                    fresh=op(side)
                    if fresh['state']!='succeeded':return fresh
                    simulation=fresh['id']
            return result
        sim=approved('a');assert sim['state']=='succeeded',sim
        rows=sim['result']['plan']['points']
        assert next(p for p in rows if p['id']==points[-1])['action']=='keep'
        assert next(p for p in rows if p['id']==points[2])['reason']=='copy_unproven'
        assert {p['id'] for p in rows if p['action']=='remove'}==set(points[:2])
        # A tela real solicita simulação; execução continua com confirmação.
        if os.environ.get('MACIP_TEST_STORAGE_BROWSER')=='1':
            from conftest import ADMIN_TOKEN
            operator=make_operator(client,{'Authorization':'Bearer '+ADMIN_TOKEN},login='storage-visual',password=secrets.token_urlsafe(24))
            script=Path(__file__).resolve().parents[2]/'portal/e2e/storage.browser.cjs'
            visual=subprocess.run(['node',str(script),cfg['api'],client_id],
                env={**os.environ,'MACIP_TEST_BROWSER_LOGIN':operator['login'],'MACIP_TEST_BROWSER_PASSWORD':operator['password']},
                capture_output=True,timeout=180)
            assert visual.returncode==0,visual.stderr.decode()
            sim=next(o for o in request(base)['operations'] if o['side']=='a' and o['action']=='simulate')
        from urllib.parse import urlsplit
        profile=request(f'/api/v1/installations/{aid}/profile');uri=urlsplit(profile['repositorio'])
        setup=root/'private/primary'/aid
        session=subprocess.Popen(['ssh','-F','/dev/null','-oBatchMode=yes','-oIdentitiesOnly=yes',
            '-oIdentityAgent=none','-oStrictHostKeyChecking=yes','-oGlobalKnownHostsFile=/dev/null',
            '-oUserKnownHostsFile='+json.dumps(str(setup/'known_hosts'),ensure_ascii=False),'-i',str(setup/'initial-client-key'),
            '-p',str(uri.port),'-s',uri.username+'@'+uri.hostname,'sftp'],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        container=json.loads((setup/'container.json').read_text())['container']
        from macip_backup_api.storage_runtime import ssh_processes
        def live_ssh():
            raw=command('docker','exec',container,'sh','-eu','-c','for p in $(pgrep -x sshd); do cat /proc/"$p"/stat; done').decode()
            return [p for p in ssh_processes(raw) if p['state'] not in ('Z','X')]
        try:
            def session_ready():
                if session.poll() is not None:
                    _,diagnostic=session.communicate()
                    raise AssertionError('sessão SSH sintética não iniciou: '+diagnostic.decode())
                try:return len(live_ssh())>1
                except RuntimeError:return False  # processo de autenticação pode sair durante /proc
            wait_for(session_ready,15)
            rejected=op('a','execute',sim['id'])
            assert rejected['state']=='failed' and rejected['error']=='busy'
            assert not rejected['result']['removed']
        finally:
            session.terminate();session.communicate(timeout=5)
        def session_closed():
            try:return len(live_ssh())==1
            except RuntimeError:return False
        wait_for(session_closed,15)
        execute=approved('a','execute',sim['id'])
        if execute['state']=='failed':
            diagnostic=json.loads((root/'private/storage/last-error-a.json').read_text())
            observed=json.loads((root/'private/storage/ssh-observed.json').read_text())
            raise AssertionError(str((diagnostic,observed)))
        assert set(execute['result']['removed'])==set(points[:2])
        assert remote_other('--no-lock','snapshots','--json')==before_other
        # O ponto pendente de cópia e o último/prova não desaparecem.
        remaining=json.loads(remote('--no-lock','snapshots','--json'))
        assert points[-1] in {p['id'] for p in remaining}
        assert prep['repository_id']==json.loads(remote('--no-lock','cat','config'))['id']
        remote('check','--read-data')
        target=root/'restaurado A'
        remote('restore',points[-1],'--target',str(target),'--verify')
        assert hashlib.sha256((target/str(src).lstrip('/')/'arquivo.bin').read_bytes()).hexdigest()==originals[points[-1]]
        # Atualização normal de A permite a política independente de B.
        wait_for(lambda:points[0] not in {p['id'] for p in json.loads((root/'private/storage'/(aid+'-a.json')).read_text())['points']},90)
        bsim=approved('b');assert bsim['state']=='succeeded',bsim
        bresult=approved('b','execute',bsim['id']);assert bresult['state']=='succeeded',bresult
        # A indisponível. A consulta e restauração abaixo usam SÓ o repositório B.
        setup=json.loads((root/'private/primary'/aid/'container.json').read_text())
        command('docker','stop',setup['container'])
        proof=request('/api/v1/protector/installations/'+aid,headers={'Authorization':'Bearer '+cfg['b_token']})['protection']
        b_repo=Path(settings['volume'])/aid
        password=actor.executor.vault.get(f'installation/{aid}/bundle').decode()
        def local_b(*args):return command('restic','--no-cache','-r',str(b_repo),*args,env={'PATH':os.environ['PATH'],'RESTIC_PASSWORD':password})
        local_b('check','--read-data')
        point_b=proof['last_copy']['snapshot_b'];a_point=proof['last_copy']['snapshot_a']
        target_b=root/'restaurado B';local_b('restore',point_b,'--target',str(target_b),'--verify')
        assert hashlib.sha256((target_b/str(src).lstrip('/')/'arquivo.bin').read_bytes()).hexdigest()==originals[a_point]
        # Falha real de acesso a A mantém a medição anterior, mas marca stale.
        wait_for(lambda:request(base)['capacity']['a']['stale'],90)
        after=request(base)['capacity']['a'];assert after['last_good'] and after['error']=='unavailable'
        # Volume B ausente: medição falha, sem recriar nem apagar o volume.
        marker=Path(settings['volume'])/'.macip-volume';moved=marker.with_name('.marker-retido-pela-prova')
        marker.rename(moved)
        try:
            wait_for(lambda:request(base)['capacity']['b']['stale'],90)
            assert request(base)['capacity']['b']['last_good']
            assert op('b')['state']=='failed'
        finally:moved.rename(marker)
