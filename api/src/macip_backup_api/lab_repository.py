"""Publicação atômica de repositórios NOVOS dos adaptadores de laboratório."""
import json
import os
from pathlib import Path
import shutil
import stat


def initialize_repository(repository: Path, initialize, verify):
    """Um estágio privado não publicado pode ser retomado, sem tocar repositório válido."""
    from .lab_runtime import json_once
    parent=repository.parent
    stage=parent/('.'+repository.name+'-initializing')
    intent=parent/('.'+repository.name+'-initializing.json')
    expected={'repository':str(repository),'version':1}
    json_once(intent,expected)
    if stage.exists() or stage.is_symlink():
        info=stage.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077:
            raise RuntimeError('estágio de inicialização inseguro')
        if not (stage/'config').exists():
            allowed={'data','index','keys','locks','snapshots'}
            if any(p.name not in allowed for p in stage.iterdir()):
                raise RuntimeError('estágio contém arquivo inesperado')
            for path in stage.rglob('*'):
                if path.is_symlink() or not (path.is_file() or path.is_dir()):
                    raise RuntimeError('estágio contém link ou arquivo especial')
            if (stage/'snapshots').exists() and any((stage/'snapshots').iterdir()):
                raise RuntimeError('estágio contém pontos; intervenção necessária')
            # Sem config e sem snapshots, não existe repositório utilizável nem ponto a excluir.
            shutil.rmtree(stage)
    if not stage.exists():
        stage.mkdir(mode=0o700)
        initialize(stage)
    identity=verify(stage)
    if repository.exists() and any(repository.iterdir()):
        raise RuntimeError('repositório final não está vazio; nunca substituir')
    os.replace(stage,repository)
    descriptor=os.open(parent,os.O_RDONLY|os.O_DIRECTORY)
    try:os.fsync(descriptor)
    finally:os.close(descriptor)
    intent.unlink()
    return identity
