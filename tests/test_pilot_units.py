"""Units em HOME descartável; ações systemd simuladas, verify real separado."""
import importlib.util
import hashlib
import json
import os
from pathlib import Path
import shutil
import shlex
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

SCRIPT=Path(__file__).resolve().parents[1]/'tools/pilot_units.py'
spec=importlib.util.spec_from_file_location('pilot_units',SCRIPT)
units=importlib.util.module_from_spec(spec);spec.loader.exec_module(units)


class PilotUnitTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='pilot units ação ')
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.area=self.root/'pilot area';self.area.mkdir(mode=0o700)
        self.home=self.root/'home';self.home.mkdir()
        runtime=self.root/'runtime';runtime.mkdir(mode=0o700)
        self.env={'HOME':str(self.home),'PATH':os.environ['PATH'],'XDG_RUNTIME_DIR':str(runtime)}
        self.environment=patch.dict(os.environ,self.env,clear=True)
        self.environment.start();self.addCleanup(self.environment.stop)
        self.plan={'area':str(self.area),'task_id':str(uuid.uuid4()),'agent_id':str(uuid.uuid4()),
                   'bond':'a'*16,'managed_timer':True,'schedule_results':[]}
        self.plan_file=self.root/'plan.json';self.plan_file.write_text(json.dumps(self.plan))
        self.plan=units.load_plan(self.plan_file)
        self.folder=units.unit_directory();self.folder.mkdir(parents=True)
        for name in ('programa/current/bin','config','estado','cache','locks','recuperacoes'):
            (self.area/name).mkdir(parents=True,exist_ok=True)
        (self.area/'programa/current/bin/python').symlink_to(sys.executable)
        (self.area/'programa/install.json').write_text(json.dumps({'lock_directory':str(self.area/'locks'),
            'commit':'ea74d5d5bf8792b374addbc6addf98345c3327a7','version':'0.2.1'}))
        for name,body in [('central.json',{'agent_id':self.plan['agent_id'],'url':'http://127.0.0.1:1',
                                          'token_environment':'SYNTHETIC_SECRET'}),
                          ('recovery.json',{'restore_root':str(self.area/'recuperacoes')})]:
            (self.area/'config'/name).write_text(json.dumps(body))
        (self.area/'config/agent.env').write_text('SYNTHETIC_SECRET=value\n')
        (self.area/'config/agent.env').chmod(0o600)
        self.plan['bond']=hashlib.sha256(f"http://127.0.0.1:1|{self.plan['agent_id']}|{self.area/'estado/agent.db'}".encode()).hexdigest()[:16]
        self.plan_file.write_text(json.dumps(self.plan))

    def own_managed(self):
        for name,markers in units.identities(self.plan).items():
            (self.folder/name).write_text(''.join(f'# macip-backup: {k}={v}\n' for k,v in markers.items())+'[Unit]\nDescription=synthetic\n')

    def record(self):
        path=self.root/'inventory.json';path.write_text(json.dumps(units.inventory(self.plan)));return path

    def cli(self,*args,env=None):
        return subprocess.run([sys.executable,str(SCRIPT),'--plan',str(self.plan_file),*args],
                              env=env or self.env,capture_output=True,text=True,timeout=20)

    def test_preflight_rejects_inactive_foreign_collision_and_ignores_similar_name(self):
        self.assertEqual(self.cli('preflight').returncode,0)
        (self.folder/'macip-mvp-poll-neighbor.timer').write_text('foreign inactive')
        self.assertEqual(self.cli('preflight').returncode,0)
        (self.folder/'macip-mvp-poll.timer').write_text('foreign inactive')
        self.assertNotEqual(self.cli('preflight').returncode,0)

    def test_literal_complete_markers_prefix_and_ambiguity(self):
        self.own_managed();name=next(iter(units.identities(self.plan)))
        path=self.folder/name;good=path.read_text()
        for bad in (good.replace(str(self.area/'programa'),str(self.area/'programa-outro')),
                    good.replace('# macip-backup: central-agent=', '# incomplete: central-agent='),
                    good+'# macip-backup: install='+str(self.area/'programa')+'\n'):
            path.write_text(bad)
            with self.assertRaises(ValueError):units.inventory(self.plan)
        path.write_text(good)
        self.assertEqual(len(units.inventory(self.plan)['units']),2)

    def test_inventory_stale_missing_and_new_owned_resources_refused(self):
        self.own_managed();record=self.record()
        units.verify(self.plan,record)
        data=json.loads(record.read_text());data['units'].pop(next(iter(data['units'])));record.write_text(json.dumps(data))
        with self.assertRaises(ValueError):units.verify(self.plan,record)
        (self.folder/'unexpected.service').write_text('# macip-backup: install='+str(self.area/'programa')+'\n')
        with self.assertRaises(ValueError):units.inventory(self.plan)

    def test_install_complete_units_no_enable_no_overwrite_and_final_inventory(self):
        self.own_managed()
        with patch.object(units.subprocess,'run') as run:
            units.install(self.plan)
            self.assertEqual(run.call_count,1)
            self.assertEqual(run.call_args.args[0][:3],['systemd-analyze','--user','verify'])
        self.assertEqual(len(units.inventory(self.plan)['units']),6)
        for name in units.recovery_identities(self.plan):
            text=(self.folder/name).read_text()
            self.assertNotIn('...',text)
            if name.endswith('.service'):
                for value in ('KillMode=control-group','TimeoutStartSec=infinity','TimeoutStopSec=45s',
                              'UMask=0077','Environment="MACIP_LOCK_DIR=', 'macip_backup.manual'):
                    self.assertIn(value,text)
                self.assertIn('agent.env',text)
        with self.assertRaises(ValueError):units.install(self.plan)
        record=self.record();units.verify(self.plan,record)
        (self.folder/self.plan['work_name']).with_suffix('.service').write_text('foreign replaced')
        with self.assertRaises(ValueError):units.verify(self.plan,record)

    def stateful_ctl(self, states, trigger=None):
        # systemctl falso com estado por unit. 'trigger' aplica novos estados
        # DURANTE o disable --now: simula timer que dispara na transição.
        fake=self.root/'fakebin';fake.mkdir();log=self.root/'calls';store=self.root/'states'
        store.mkdir()
        for name,state in states.items():(store/name).write_text(state)
        trig=self.root/'trigger-on-disable'
        if trigger:
            trig.mkdir()
            for name,state in trigger.items():(trig/name).write_text(state)
        ctl=fake/'systemctl';jobs=self.root/'jobs';jobs.write_text('')
        ctl.write_text(f'''#!/bin/sh
printf "%s\\n" "$*" >> "{log}"
case "$*" in
  *show*) for name in "$@"; do case "$name" in *.service|*.timer) cat "{store}/$name" ;; esac; done ;;
  *disable*)
    for name in "$@"; do case "$name" in *.service|*.timer) echo inactive > "{store}/$name" ;; esac; done
    if [ -d "{trig}" ]; then for f in "{trig}"/*; do cat "$f" > "{store}/$(basename "$f")"; done; fi ;;
  *stop*)
    for name in "$@"; do
      case "$name" in *.service|*.timer) echo inactive > "{store}/$name"; sed -i "/ $name /d" "{jobs}" ;; esac
    done ;;
  *list-jobs*) cat "{jobs}" ;;
esac
''')
        ctl.chmod(0o755)
        return dict(self.env,PATH=str(fake)+os.pathsep+self.env['PATH'],TEST_LOG=str(log)),log,jobs

    def test_activating_service_refused_and_stop_names_exact_via_public_cli(self):
        self.own_managed()
        for name,content in units.render(self.plan).items():(self.folder/name).write_text(content)
        record=self.record()
        services=[n for n in units.current_expected(self.plan) if n.endswith('.service')]
        timers=[n for n in units.current_expected(self.plan) if n.endswith('.timer')]
        states={n:'inactive' for n in services+timers};states[services[0]]='activating'
        env,log,jobs=self.stateful_ctl(states)
        args=['--settle-seconds','0','--inventory',str(record),'stop']
        refused=self.cli(*args,env=env)
        self.assertEqual(refused.returncode,3,refused.stderr)
        payload=json.loads(refused.stdout)
        self.assertEqual(payload['status'],'incomplete')
        self.assertEqual(payload['timers'],'disabled')
        self.assertIn(services[0],payload['busy'])
        lines=log.read_text().splitlines()
        self.assertTrue(any(' disable ' in line for line in lines))
        self.assertFalse(any(' stop ' in line for line in lines))
        result=self.cli('--settle-seconds','0','--inventory',str(record),'--allow-stop','stop',env=env)
        self.assertEqual(result.returncode,0,result.stderr)
        actions=[line for line in log.read_text().splitlines() if ' stop ' in line or ' disable ' in line]
        expected=set(units.current_expected(self.plan))
        for action in actions:
            self.assertNotIn('*',action)
            names=[word for word in action.split() if word.endswith(('.service','.timer'))]
            self.assertTrue(set(names)<=expected)

    def test_service_started_during_disable_is_not_reported_done(self):
        # Corrida do achado: timer dispara durante disable --now; a lista
        # "running" coletada antes ficaria vazia e o helper diria ok.
        self.own_managed()
        for name,content in units.render(self.plan).items():(self.folder/name).write_text(content)
        record=self.record()
        names=list(units.current_expected(self.plan))
        services=[n for n in names if n.endswith('.service')]
        states={n:'inactive' for n in names}
        env,log,jobs=self.stateful_ctl(states,trigger={services[0]:'active'})
        result=self.cli('--settle-seconds','0','--inventory',str(record),'stop',env=env)
        self.assertEqual(result.returncode,3,result.stderr)
        payload=json.loads(result.stdout)
        self.assertEqual(payload['status'],'incomplete')
        self.assertEqual(payload['timers'],'disabled')
        self.assertIn(services[0],payload['busy'])
        self.assertFalse(any(' stop ' in line for line in log.read_text().splitlines()))
        # com --allow-stop o serviço próprio é parado e o estado final confirmado
        result=self.cli('--settle-seconds','0','--inventory',str(record),'--allow-stop','stop',env=env)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertTrue(any(' stop ' in line and services[0] in line
                            for line in log.read_text().splitlines()))

    def test_pending_job_blocks_ok_and_is_stopped_with_allow_stop(self):
        self.own_managed()
        for name,content in units.render(self.plan).items():(self.folder/name).write_text(content)
        record=self.record()
        names=list(units.current_expected(self.plan))
        services=[n for n in names if n.endswith('.service')]
        states={n:'inactive' for n in names}
        env,log,jobs=self.stateful_ctl(states)
        jobs.write_text(f'7 {services[0]} start waiting\n8 foreign.service start waiting\n')
        result=self.cli('--settle-seconds','0','--inventory',str(record),'stop',env=env)
        self.assertEqual(result.returncode,3)
        self.assertEqual(json.loads(result.stdout)['jobs'],[services[0]])
        result=self.cli('--settle-seconds','0','--inventory',str(record),'--allow-stop','stop',env=env)
        self.assertEqual(result.returncode,0,result.stderr)
        stop=[line for line in log.read_text().splitlines() if ' stop ' in line]
        self.assertTrue(stop and services[0] in stop[0])
        self.assertNotIn('foreign.service',stop[0])

    @unittest.skipUnless(shutil.which('systemd-analyze'),'systemd-analyze ausente')
    def test_rendered_units_real_systemd_analyze_verify(self):
        paths=[]
        for name,content in units.render(self.plan).items():
            path=self.root/name;path.write_text(content);paths.append(str(path))
        result=subprocess.run(['systemd-analyze','--user','verify',*paths],env=self.env,
                              capture_output=True,text=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_generated_command_runs_with_clean_service_environment(self):
        # Simula somente transporte de argv/Environment do texto gerado.
        # O parser de credenciais e CLI são reais, instalados num venv local;
        # não é execução no gerenciador systemd real.
        executable=self.area/'programa/current/bin/python'
        executable.unlink()
        subprocess.run([sys.executable,'-m','venv','--without-pip',str(self.area/'programa/current')],
                       check=True,capture_output=True,timeout=30)
        site=Path(subprocess.check_output([str(executable),'-c',
            'import sysconfig;print(sysconfig.get_paths()["purelib"])'],text=True).strip())
        shutil.copytree(SCRIPT.parents[1]/'src/macip_backup',site/'macip_backup',
                        ignore=shutil.ignore_patterns('__pycache__'))
        (self.area/'config/central.json').write_text(json.dumps({'agent_id':self.plan['agent_id'],
            'url':'http://127.0.0.1:1','token_environment':'SYNTHETIC_SECRET',
            'connect_timeout_seconds':1,'request_timeout_seconds':1}))
        text=units.render(self.plan)[self.plan['poll_name']+'.service']
        environment=dict(self.env)
        self.assertNotIn('SYNTHETIC_SECRET',environment)
        for line in text.splitlines():
            if line.startswith('Environment='):
                key,value=shlex.split(line.partition('=')[2])[0].split('=',1)
                environment[key]=value
        command=shlex.split(next(l.partition('=')[2] for l in text.splitlines() if l.startswith('ExecStart=')))
        result=subprocess.run(command,env=environment,cwd=self.root,capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertNotIn('SYNTHETIC_SECRET=value',result.stdout+result.stderr)
        self.assertEqual(json.loads(result.stdout)['retry'],1)
        self.assertTrue((self.area/'estado/recovery.db').exists())
        self.assertTrue((self.area/'locks/maintenance.lock').exists())

    def test_cli_result_names_exact_identities_and_inventory_completion(self):
        from macip_backup.systemd import install_sync_schedule,install_pull_schedule
        self.own_managed();old=self.record()
        with patch.dict(os.environ,{'MACIP_HOME':str(self.area/'programa')}):
            for kind,install in [('sync',install_sync_schedule),('pull',install_pull_schedule)]:
                result=install(self.area/'estado/agent.db',self.area/'config/central.json','daily')
                self.assertFalse(result.get('enabled',False))
                # The resource exists, but has not yet entered the plan.
                with self.assertRaises(ValueError):units.inventory(self.plan)
                output=self.root/(kind+'-result.json');output.write_text(json.dumps(result))
                self.plan['schedule_results'].append(str(output))
        self.assertEqual(len(units.inventory(self.plan)['units']),6)
        with self.assertRaises(ValueError):units.verify(self.plan,old)
        new=self.root/'new-inventory.json';new.write_text(json.dumps(units.inventory(self.plan)))
        units.verify(self.plan,new)
        output=Path(self.plan['schedule_results'][0]);body=json.loads(output.read_text())
        body['timer_name']='macip-backup-sync-0000000000000000.timer';output.write_text(json.dumps(body))
        with self.assertRaises(ValueError):units.inventory(self.plan)


if __name__=='__main__':unittest.main()
