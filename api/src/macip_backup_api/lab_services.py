"""Inicialização única dos serviços locais exclusivos da área de laboratório."""
import hashlib
import json
import os
from pathlib import Path
import uuid

from .lab_runtime import command, json_once
from .protector import _directory, _private_file


def configure_services(root, cfg, *, certificate, key, ca, delivery_port, mailpit_image):
    if not cfg.get('activation_public'):
        raise RuntimeError('configure primeiro os endereços HTTPS de laboratório')
    services=root/'private/services'
    services.mkdir(mode=0o700,exist_ok=True);_directory(services)
    persisted=services/'installation.json'
    if persisted.exists():
        existing=json.loads(_private_file(persisted))
        # Recomeçar worker não recria volume, cofre, portas ou identidades.
        if (not Path(existing['volume']).exists() or
            _private_file(Path(existing['volume'])/'.macip-volume').decode()!=existing['volume_id']):
            raise RuntimeError('volume registrado ausente ou divergente; não recriar')
        return existing
    if not certificate or not key or not ca:
        raise RuntimeError('informe certificate, key e ca do laboratório')
    for file in (certificate,key,ca):
        path=Path(file)
        if not path.is_absolute() or path.resolve()!=path or not path.is_file():
            raise RuntimeError('TLS exige arquivos absolutos sem links')
    _private_file(Path(key))
    token=hashlib.sha256(os.fsencode(root)).hexdigest()[:16]
    volume=Path('/dev/shm')/('macip-auto-'+token)
    intent=services/'intent.json'
    if intent.exists():
        plan=json.loads(_private_file(intent))
        if plan['root']!=str(root) or plan['volume']!=str(volume):
            raise RuntimeError('intenção de laboratório divergente')
        volume_id=plan['volume_id']
    else:
        if volume.exists():
            raise RuntimeError('volume proposto já existe sem registro; inspecione sem sobrescrever')
        volume_id=str(uuid.uuid4())
        plan={'volume':str(volume),'volume_id':volume_id,'root':str(root),'volume_created':False}
        json_once(intent,plan)
    if plan.get('volume_created') and not volume.exists():
        raise RuntimeError('volume registrado desapareceu; não recriar')
    volume.mkdir(mode=0o700,exist_ok=True);_directory(volume)
    from .lab_runtime import write_once
    marker=volume/'.macip-volume'
    if not marker.exists() and any(volume.iterdir()):
        raise RuntimeError('volume parcial contém material inesperado')
    write_once(marker,volume_id.encode())
    from .lab_supervisor import atomic_json
    atomic_json(intent,{**plan,'volume_created':True})
    name='macip-auto-mail-'+token
    import subprocess
    exists=subprocess.run(['docker','inspect',name],capture_output=True,timeout=30).returncode==0
    if exists:
        label=command('docker','inspect','-f','{{index .Config.Labels "macip.windows-lab"}}',name).decode().strip()
        if label!=str(root):raise RuntimeError('Mailpit homônimo alheio')
        command('docker','start',name)
    else:
        command('docker','run','--pull=never','-d','--name',name,'--label','macip.windows-lab='+str(root),
                '-p','127.0.0.1::1025','-p','127.0.0.1::8025',mailpit_image)
    smtp_port=int(command('docker','port',name,'1025/tcp').decode().strip().rsplit(':',1)[1])
    web_port=int(command('docker','port',name,'8025/tcp').decode().strip().rsplit(':',1)[1])
    settings=dict(volume=str(volume),volume_id=volume_id,mailpit=name,mailpit_web_port=web_port)
    # Configuração local do administrador: somente a central recebe todos os tokens.
    cfg={**cfg,'automation':True}
    from .lab_supervisor import atomic_json
    atomic_json(root/'private/config.json',cfg)
    common={'root':str(root),'api':cfg['api'],'ca':ca}
    tls={'certificate':certificate,'key':key,'ca':ca}
    json_once(services/'central.json',tls)
    json_once(services/'activation.json',{**tls,'port':delivery_port})
    json_once(services/'primary.json',{**common,'token':cfg['a_token'],'ip':cfg['ip'],
              'prefix':'macip-auto-a-'+token,'image':cfg['sftp_image']})
    json_once(services/'protection.json',{**common,'token':cfg['b_token'],'mount':'/dev/shm',
              'volume':str(volume),'volume_id':volume_id})
    json_once(services/'alerts.json',{'database':cfg['database'],'smtp_port':smtp_port,'interval_seconds':5})
    json_once(persisted,settings)
    return settings
