#!/usr/bin/env python3
"""Administrador local: pré-verificar, atualizar, conferir e recuperar. Lab somente."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'api/src'))
from macip_backup_api.lab_updates import LaboratoryUpdate,UpdateError,stage,selected
from macip_backup_api.lab_supervisor import atomic_json,control,identity


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=('stage','adopt','preflight','apply','rollback','status','recover','restore','reconcile'))
    p.add_argument('area',type=Path);p.add_argument('--artifact');p.add_argument('--package',type=Path)
    p.add_argument('--sha256');p.add_argument('--commit');p.add_argument('--controller');p.add_argument('--backup')
    p.add_argument('--confirm',action='store_true');p.add_argument('--wait',type=int,default=30)
    a=p.parse_args()
    if not 1<=a.wait<=300:p.error('wait deve estar entre 1 e 300 segundos')
    try:
        updater=LaboratoryUpdate(a.area)
        if a.action=='stage':
            if not all((a.package,a.sha256,a.commit)):p.error('informe package, sha256 e commit conferidos por canal confiável')
            value=dict(artifact=stage(a.area,a.package,a.sha256,a.commit))
        elif a.action=='adopt':
            _,m=selected(a.area,a.artifact);_,controller=selected(a.area,a.controller)
            if not (a.area/'updates/releases'/a.controller/'source/tools/lab_guard.py').is_file():raise UpdateError('guardião aprovado ausente')
            evidence=json.loads((a.area/'evidence/base.json').read_text())
            if evidence['central']!=m['commit']:raise UpdateError('artefato anterior não corresponde à instalação registrada')
            if updater.read('active.json'):raise UpdateError('área já adotada')
            token=control(a.area,'status').get('children',{}).get('central')
            if not token or identity(int(token.split(':')[0]))!=token:raise UpdateError('identidade do processo central inconclusiva')
            command=(Path('/proc')/token.split(':')[0]/'cmdline').read_bytes().decode().split('\0')
            helper=Path(command[1])
            if helper.name!='windows-lab-server.py' or not helper.is_absolute():raise UpdateError('layout legado não reconhecido para adoção')
            live=helper.parent.parent
            from macip_backup_api.lab_updates import sha
            for name,digest in m['files'].items():
                actual=live/name
                if not actual.is_file() or actual.resolve()!=actual or sha(actual)!=digest:raise UpdateError('código em uso diverge do artefato anterior aprovado')
            active=dict(id=a.artifact,source=str(a.area/'updates/releases'/a.artifact/'source'),commit=m['commit'],schema=updater.schema(),windows=m['windows'])
            atomic_json(a.area/'updates/controller.json',dict(id=a.controller,commit=controller['commit']))
            atomic_json(a.area/'updates/active.json',active);value=dict(adopted=True,commit=m['commit'])
        elif a.action=='status':
            value=dict(active=updater.read('active.json'),transaction=updater.read('transaction.json'),gate=updater.read('gate.json'))
        elif a.action=='preflight':value=updater.preflight(a.artifact)
        elif a.action in ('apply','rollback'):
            if not a.confirm and input('Atualizar somente esta área descartável? Digite ATUALIZAR: ')!='ATUALIZAR':raise UpdateError('cancelado; instalação preservada')
            value=updater.apply(a.artifact,rollback=a.action=='rollback',wait=a.wait)
        elif a.action=='recover':value=updater.recover()
        elif a.action=='restore':
            print(json.dumps(updater.restore_preview(a.backup),ensure_ascii=False))
            if not a.confirm and input('Escritas posteriores sairão da central ativa e serão guardadas à parte. Restaurar somente este laboratório? Digite RESTAURAR: ')!='RESTAURAR':raise UpdateError('cancelado')
            value=updater.restore(a.backup)
        else:value=updater.reconcile()
        print(json.dumps(value,ensure_ascii=False,default=str))
    except Exception as error:
        # Nunca interpolar exceções SQL, URLs, linhas de comando ou credenciais.
        print(str(error) if isinstance(error,UpdateError) else 'Operação recusada; confira status, etapa e roteiro ('+type(error).__name__+').',file=sys.stderr)
        return 1
    return 0
if __name__=='__main__':raise SystemExit(main())
