"""Regra única de identidade de manutenção, também executável sem imports do pacote."""
import json
import os
from pathlib import Path
import sys


def resolve_lock_directory(*, maintenance=False):
    prefix = Path(sys.prefix).resolve()
    installed_home = prefix.parent.parent if prefix.parent.name == 'releases' else None
    configured_home = os.environ.get('MACIP_HOME')
    if installed_home and configured_home and Path(configured_home).resolve() != installed_home:
        raise ValueError('MACIP_HOME diverge do Python instalado')
    home = installed_home or Path(configured_home or Path.home() / '.local/share/macip-backup')
    manifest = home / 'install.json'
    registered = None
    legacy = False
    if manifest.exists():
        try:
            data = json.loads(manifest.read_text())
            if not isinstance(data, dict):
                raise ValueError()
            legacy = 'lock_directory' not in data
            if not legacy:
                value = data['lock_directory']
                if not isinstance(value, str) or not value or not Path(value).is_absolute():
                    raise ValueError()
                registered = Path(value).resolve()
        except (OSError, ValueError, TypeError):
            raise ValueError('manifesto de instalação inválido para identidade de lock') from None
    default = Path(os.environ.get('MACIP_STATE') or Path.home() / '.local/state/macip-backup') / 'locks'
    override = os.environ.get('MACIP_LOCK_DIR')
    selected = Path(override) if override else registered or default
    if not selected.is_absolute():
        raise ValueError('diretório de locks deve ser absoluto')
    selected = selected.resolve()
    if registered and selected != registered:
        raise ValueError('diretório de locks diverge da instalação existente')
    if maintenance and legacy and selected != default.resolve():
        raise ValueError('instalação legada sem identidade de lock customizado; manutenção recusada')
    return selected


if __name__ == '__main__':
    try:
        print(resolve_lock_directory(maintenance='--maintenance' in sys.argv))
    except (ValueError, OSError):
        # Não imprimir conteúdo arbitrário do manifesto nem dados operacionais.
        print('erro: identidade de lock inválida, divergente ou legada não verificável', file=sys.stderr)
        raise SystemExit(1)
