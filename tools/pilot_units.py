#!/usr/bin/env python3
"""Units e inventário estritos para o piloto; não instala nem altera o produto."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import uuid


def digest(data):
    return hashlib.sha256(data).hexdigest()


def exclusive(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('xb') as stream:
        stream.write(data)


def load_plan(path):
    plan = json.loads(path.read_text())
    area = Path(plan['area'])
    if not area.is_absolute() or area.resolve() != area or not area.is_dir():
        raise ValueError('área deve ser diretório absoluto existente sem links')
    if area.stat().st_uid != os.getuid() or area.stat().st_mode & 0o077:
        raise ValueError('área deve ser privada e do usuário executor')
    for key in ('task_id', 'agent_id'):
        if str(uuid.UUID(plan[key])) != plan[key]:
            raise ValueError('UUID não canônico')
    if not re.fullmatch(r'[0-9a-f]{16}|[0-9a-f]{64}', plan['bond']):
        raise ValueError('vínculo inválido')
    if type(plan['managed_timer']) is not bool:
        raise ValueError('managed_timer exige booleano')
    for key, default in [('poll_name','macip-mvp-poll'),('work_name','macip-mvp-work')]:
        plan.setdefault(key, default)
        if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{1,100}', plan[key]):
            raise ValueError('nome de unit inválido')
    if plan['poll_name'] == plan['work_name']:
        raise ValueError('nomes de units colidem')
    plan.setdefault('schedule_results', [])
    if any(c in str(area) for c in '\r\n\x00'):
        raise ValueError('caminho incompatível com unit')
    return plan


def unit_directory():
    # Mesma regra do produto congelado: XDG_CONFIG_HOME não é consultado.
    return Path.home() / '.config/systemd/user'


def validate_context(plan):
    area=Path(plan['area'])
    central=area/'config/central.json'
    if not central.exists():
        raise ValueError('configuração central ausente')
    document=json.loads(central.read_text())
    if document.get('agent_id') != plan['agent_id'] or not isinstance(document.get('url'),str):
        raise ValueError('identidade central divergente')
    material=f"{document['url'].rstrip('/')}|{plan['agent_id']}|{(area/'estado/agent.db').resolve()}"
    bond=hashlib.sha256(material.encode()).hexdigest()
    if plan['bond'] != bond[:len(plan['bond'])]:
        raise ValueError('vínculo não corresponde ao agente/central/banco')


def marker_map(text):
    markers = {}
    for line in text.splitlines():
        if line.startswith('# macip-backup: '):
            key, separator, value = line[len('# macip-backup: '):].partition('=')
            if not separator or key in markers:
                raise ValueError('marcador ausente/ambíguo')
            markers[key] = value
    return markers


def identities(plan):
    area = Path(plan['area'])
    install = {'install': str(area/'programa')}
    managed = dict(install, **{'managed-task': plan['task_id'], 'central-agent': plan['agent_id'],
                              'managed-bond': plan['bond'][:16]})
    prefix = f"macip-backup-managed-{plan['task_id']}"
    expected = {prefix+'.service': managed}
    if plan['managed_timer']:
        expected[prefix+'.timer'] = managed
    store, central = str(area/'estado/agent.db'), str(area/'config/central.json')
    for result_file in plan['schedule_results']:
        result = json.loads(Path(result_file).read_text())
        if result.get('store') != store or result.get('central') != central:
            raise ValueError('resultado CLI pertence a outro layout')
        name = result.get('timer_name', '')
        kinds = [('pull', hashlib.sha256(f'pull\0{store}\0{central}'.encode()).hexdigest()),
                 ('sync', hashlib.sha256(f'{store}\0{central}'.encode()).hexdigest())]
        selected = [(kind, identity) for kind, identity in kinds
                    if name == f'macip-backup-{kind}-{identity[:16]}.timer']
        if len(selected) != 1:
            raise ValueError('nome retornado pela CLI não corresponde à identidade')
        kind, identity = selected[0]
        for suffix in ('service','timer'):
            path = unit_directory()/name.replace('.timer', '.'+suffix)
            if result.get(suffix) != str(path):
                raise ValueError('resultado CLI aponta outro diretório de units')
            if path.name in expected:
                raise ValueError('resultado CLI duplicado')
            expected[path.name] = dict(install, schedule=kind, **{kind+'-id': identity})
    return expected


def recovery_identities(plan):
    result = {}
    for role in ('poll','work'):
        markers = {'install': str(Path(plan['area'])/'programa'),
                   'managed-task': plan['task_id'], 'central-agent': plan['agent_id'],
                   'managed-bond': plan['bond'][:16], 'pilot-recovery': role}
        for suffix in ('service','timer'):
            result[plan[role+'_name']+'.'+suffix] = markers
    return result


def quote(value, execution=False):
    value = str(value).replace('%', '%%').replace('\\','\\\\').replace('"','\\"')
    if execution:
        value = value.replace('$', '$$')
    return '"'+value+'"'


def render(plan):
    area = Path(plan['area']); files = {}
    for role in ('poll','work'):
        prefix = plan[role+'_name']
        markers = recovery_identities(plan)[prefix+'.service']
        header = ''.join(f'# macip-backup: {k}={v}\n' for k,v in markers.items())
        environment = ''.join('Environment='+quote(f'MACIP_{key}={area/sub}')+'\n' for key,sub in
            [('HOME','programa'),('BIN','bin'),('CONFIG','config'),('STATE','estado'),('CACHE','cache'),('LOCK_DIR','locks')])
        args = [area/'programa/current/bin/python', '-m','macip_backup.manual', '--env-file', area/'config/agent.env',
                '--', 'recovery-'+role, '--central', area/'config/central.json', '--queue', area/'estado/recovery.db']
        if role == 'work':
            args += ['--policy', area/'config/recovery.json', '--limit','1']
        files[prefix+'.service'] = (header+'[Unit]\nDescription=MACIP piloto recuperação '+role+
            '\nAfter=network-online.target\nWants=network-online.target\n[Service]\nType=oneshot\nUMask=0077\n'+
            environment+'EnvironmentFile='+str(area/'config/agent.env').replace('%','%%')+'\n'+
            'KillMode=control-group\nTimeoutStartSec=infinity\nTimeoutStopSec=45s\nExecStart='+
            ' '.join(quote(arg, True) for arg in args)+'\n')
        calendar = '*-*-* *:*:00,30' if role == 'poll' else '*-*-* *:*:15,45'
        files[prefix+'.timer'] = header+f'[Unit]\nDescription=MACIP piloto {role}\n[Timer]\nOnCalendar={calendar}\nPersistent=true\n[Install]\nWantedBy=timers.target\n'
    return files


def validate_files(expected, require=True):
    folder = unit_directory()
    for name, markers in expected.items():
        path = folder/name
        if path.is_symlink():
            raise ValueError('unit não pode ser link')
        if not path.exists() and not require:
            continue
        actual = marker_map(path.read_text())
        if actual != markers:
            raise ValueError('propriedade incompleta/divergente: '+name)


def current_expected(plan):
    expected = identities(plan)
    recovery = recovery_identities(plan)
    present = [name for name in recovery if (unit_directory()/name).exists()]
    if present and len(present) != 4:
        raise ValueError('instalação parcial de recuperação')
    if present:
        expected.update(recovery)
    return expected


def scan_owned(plan, expected):
    folder = unit_directory()
    if not folder.exists():
        return
    exact = '# macip-backup: install='+str(Path(plan['area'])/'programa')
    for path in folder.iterdir():
        if path.is_symlink():
            continue
        if path.suffix not in ('.service','.timer') or not path.is_file():
            continue
        if exact in path.read_text().splitlines() and path.name not in expected:
            raise ValueError('unit própria fora do inventário: '+path.name)


def inventory(plan):
    validate_context(plan)
    expected = current_expected(plan)
    validate_files(expected)
    scan_owned(plan, expected)
    return {'plan_sha256':digest(json.dumps(plan,sort_keys=True).encode()),
            'units': {name: {'markers':markers, 'sha256':digest((unit_directory()/name).read_bytes())}
                      for name,markers in expected.items()}}


def verify(plan, file):
    previous = json.loads(file.read_text())
    actual = inventory(plan)
    if previous != actual:
        raise ValueError('inventário incompleto/desatualizado; gere novo arquivo')
    return actual


def ctl(*args):
    return subprocess.run(['systemctl','--user',*args], capture_output=True,text=True,check=True,timeout=60)


def install(plan):
    validate_context(plan)
    files = render(plan)
    validate_files(identities(plan), require=False)
    scan_owned(plan, identities(plan))
    if any((unit_directory()/name).exists() or (unit_directory()/name).is_symlink() for name in files):
        raise ValueError('colisão: nenhuma unit existente pode ser sobrescrita')
    area=Path(plan['area'])
    manifest=json.loads((area/'programa/install.json').read_text())
    if (manifest.get('lock_directory') != str(area/'locks')
            or manifest.get('commit') != 'ea74d5d5bf8792b374addbc6addf98345c3327a7'
            or manifest.get('version') != '0.2.1'):
        raise ValueError('identidade de locks da instalação divergente')
    for path in (area/'config/agent.env',area/'config/central.json',area/'config/recovery.json'):
        if not path.is_file() or path.is_symlink(): raise ValueError('configuração ausente/link')
    secret=area/'config/agent.env'
    if secret.stat().st_uid != os.getuid() or secret.stat().st_mode & 0o077:
        raise ValueError('credenciais devem ser privadas')
    if json.loads((area/'config/central.json').read_text()).get('agent_id') != plan['agent_id']:
        raise ValueError('configuração central pertence a outro agente')
    if json.loads((area/'config/recovery.json').read_text()) != {'restore_root':str(area/'recuperacoes')}:
        raise ValueError('política de restauração pertence a outra área')
    with tempfile.TemporaryDirectory(prefix='pilot-units-') as staging:
        paths=[]
        for name,content in files.items():
            path=Path(staging)/name;path.write_text(content);paths.append(str(path))
        subprocess.run(['systemd-analyze','--user','verify',*paths],check=True,capture_output=True,text=True,timeout=30)
    created=[]
    try:
        for name,content in files.items():
            path=unit_directory()/name
            exclusive(path, content.encode());created.append(path)
    except BaseException:
        for path in created: path.unlink()
        raise


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan',type=Path,required=True)
    parser.add_argument('--inventory',type=Path)
    parser.add_argument('--allow-stop',action='store_true')
    parser.add_argument('command',choices=['preflight','install-recovery','inventory','verify','activate','stop'])
    args=parser.parse_args()
    try:
        plan=load_plan(args.plan)
        if args.command=='preflight':
            names={**identities(plan),**recovery_identities(plan)}
            if any((unit_directory()/n).exists() or (unit_directory()/n).is_symlink() for n in names):
                raise ValueError('colisão: nome proposto já existe')
            scan_owned(plan,{})
        elif args.command=='install-recovery': install(plan)
        elif args.command=='inventory':
            if args.inventory is None:raise ValueError('--inventory obrigatório')
            exclusive(args.inventory,(json.dumps(inventory(plan),indent=2)+'\n').encode())
        else:
            if args.inventory is None:raise ValueError('--inventory obrigatório')
            record=verify(plan,args.inventory)
            if args.command in ('activate','stop'):
                names=list(record['units'])
                # Activating oneshots are executing, not idle.
                running=[]
                for name in names:
                    if name.endswith('.service'):
                        state=ctl('show',name,'--property=ActiveState','--value').stdout.strip()
                        if state not in ('inactive','failed','active','activating','deactivating'):
                            raise ValueError('estado do serviço não verificável')
                        if state in ('active','activating','deactivating'):running.append(name)
                if running and (args.command=='activate' or not args.allow_stop):
                    raise ValueError('serviço executando/ativando: parada controlada exige --allow-stop')
                verify(plan,args.inventory)
                timers=[n for n in names if n.endswith('.timer')]
                if args.command=='activate':
                    ctl('daemon-reload')
                    if timers:ctl('enable','--now',*timers)
                else:
                    if timers:ctl('disable','--now',*timers)
                    if running:ctl('stop',*running)
        print(json.dumps({'status':'ok','command':args.command}))
    except (OSError,ValueError,KeyError,subprocess.SubprocessError) as error:
        parser.exit(2,'recusa: '+str(error)+'\n')


if __name__=='__main__': main()
