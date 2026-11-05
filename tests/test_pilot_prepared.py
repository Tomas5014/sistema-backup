import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
import uuid

SCRIPT = Path(__file__).resolve().parents[1] / 'tools/pilot_prepared.py'


class PreparedSelectionTests(unittest.TestCase):
    def test_exact_public_cli_and_refusals(self):
        with tempfile.TemporaryDirectory(prefix='pilot prepared ação ') as temporary:
            root = Path(temporary)
            task, client, agent = [str(uuid.uuid4()) for _ in range(3)]
            store = root / 'state/agent.db'
            bond = hashlib.sha256(f'https://localhost:18445|{agent}|{store}'.encode()).hexdigest()
            directory = root / 'state/managed' / bond[:16] / 'tasks.d' / task
            (directory / 'staged').mkdir(parents=True)
            central = root / 'central.json'; central.write_text(json.dumps({'url':'https://localhost:18445','agent_id':agent}))
            summary = root / 'prepared.json'
            good_summary = {'bond':bond[:16],'prepared':[{'task_id':task,'generation':2,'result':'prepared'}],'failed':[]}
            summary.write_text(json.dumps(good_summary))
            document = {'id':task,'client':{'id':client},'device_id':agent,'destinations':[{'id':'sftp-mvp'}], 'sources':[str(root/'origem')]}
            cfg = directory/'config-2.json'; cfg.write_text(json.dumps(document))
            envelope = {'task_id':task,'agent_id':agent,'generation':2,'revision':3,'action':'apply','contract_version':1,'task':document,'schedule':{'kind':'manual'}}
            envelope['hash'] = hashlib.sha256(json.dumps(envelope,sort_keys=True,separators=(',',':')).encode()).hexdigest()
            (directory/'gen-2.json').write_text(json.dumps(envelope))
            markers = [f'# macip-backup: install={root}/programa',f'# macip-backup: managed-task={task}',f'# macip-backup: central-agent={agent}',f'# macip-backup: managed-bond={bond[:16]}']
            (directory/'staged'/f'macip-backup-managed-{task}.service').write_text('\n'.join(markers))
            args = ['python3',str(SCRIPT)]
            for k,v in dict(state=root/'state',central=central,store=store,preparation=summary,task=task,client=client,agent=agent,destination='sftp-mvp',bond=bond,install=root/'programa',source=root/'origem',generation=2,revision=3).items():
                args += ['--'+k,str(v)]
            def run(): return subprocess.run(args,capture_output=True,text=True,timeout=10)
            self.assertEqual(run().stdout.strip(),str(cfg))
            # Earlier unrelated task must never be selected.
            (directory.parent/str(uuid.uuid4())).mkdir()
            for change in ({'bond':'0'*16},{'prepared':good_summary['prepared']*2},{'prepared':[]}):
                summary.write_text(json.dumps(dict(good_summary,**change)))
                self.assertEqual(run().returncode,2)
            summary.write_text(json.dumps(good_summary))
            (directory/'config-02.json').write_text(json.dumps(document))
            self.assertEqual(run().returncode,2); (directory/'config-02.json').unlink()
            for field in ('id','device_id','client','destinations'):
                bad=dict(document); bad[field]=[] if field=='destinations' else {'id':'wrong'} if field=='client' else str(uuid.uuid4())
                cfg.write_text(json.dumps(bad));self.assertEqual(run().returncode,2)
            cfg.write_text(json.dumps(document)); cfg.unlink(); cfg.symlink_to(directory/'gen-2.json')
            self.assertEqual(run().returncode,2)
