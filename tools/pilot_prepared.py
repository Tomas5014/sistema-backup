#!/usr/bin/env python3
"""Seleciona uma configuração preparada exata; nunca procura a primeira disponível."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
import uuid


def read(path):
    path = Path(path)
    if not path.is_absolute() or path.resolve() != path or not path.is_file():
        raise ValueError('arquivo ausente, relativo ou com links')
    return json.loads(path.read_text())


def select(args):
    for name in ('task', 'client', 'agent'):
        if str(uuid.UUID(getattr(args, name))) != getattr(args, name):
            raise ValueError('UUID não canônico')
    if args.generation < 1 or args.revision < 1:
        raise ValueError('geração/revisão deve ser positiva')
    central = read(args.central)
    if central['agent_id'] != args.agent:
        raise ValueError('agente da central diverge')
    store = Path(args.store)
    if not store.is_absolute() or store.resolve() != store:
        raise ValueError('caminho do banco inválido')
    bond = hashlib.sha256(f"{central['url'].rstrip('/')}|{args.agent}|{store}".encode()).hexdigest()
    if args.bond not in (bond, bond[:16]):
        raise ValueError('vínculo informado diverge do vínculo calculado')
    summary = read(args.preparation)
    selected = [p for p in summary.get('prepared', []) if p.get('task_id') == args.task]
    if (summary.get('bond') != bond[:16] or summary.get('failed')
            or len(selected) != 1 or selected[0].get('generation') != args.generation
            or selected[0].get('result') != 'prepared'):
        raise ValueError('preparação ausente, divergente ou ambígua')
    directory = Path(args.state) / 'managed' / bond[:16] / 'tasks.d' / args.task
    if directory.resolve() != directory or not directory.is_absolute():
        raise ValueError('diretório gerenciado inválido')
    for prefix in ('config', 'gen'):
        candidates = [p for p in directory.iterdir()
                      if re.fullmatch(prefix + r'-\d+\.json', p.name)
                      and int(p.stem.split('-')[1]) == args.generation]
        if len(candidates) != 1 or candidates[0].name != f'{prefix}-{args.generation}.json':
            raise ValueError('candidato ausente ou ambíguo')
    config = directory / f'config-{args.generation}.json'
    envelope = read(directory / f'gen-{args.generation}.json')
    body = {k: v for k, v in envelope.items() if k != 'hash'}
    if hashlib.sha256(json.dumps(body, sort_keys=True, separators=(',', ':')).encode()).hexdigest() != envelope.get('hash'):
        raise ValueError('hash do envelope inválido')
    if (envelope.get('task_id') != args.task or envelope.get('agent_id') != args.agent
            or envelope.get('action') != 'apply' or envelope.get('generation') != args.generation
            or envelope.get('revision') != args.revision or envelope.get('contract_version') != 1):
        raise ValueError('identidade/revisão/geração do envelope diverge')
    document = read(config)
    destinations = document.get('destinations', [])
    if (document != envelope.get('task') or document.get('id') != args.task
            or document.get('device_id') != args.agent or document.get('client', {}).get('id') != args.client
            or sum(d.get('id') == args.destination for d in destinations) != 1):
        raise ValueError('configuração não corresponde ao contexto autorizado')
    if args.source:
        sources = [p if isinstance(p, str) else p['path'] for p in document['sources']]
        if sources != [str(Path(args.source))]:
            raise ValueError('origem diverge da área do piloto')
    if (directory / 'current.json').exists():
        raise ValueError('tarefa já aplicada: roteiro inicial exige preparação nova')
    markers = [f'# macip-backup: install={args.install}', f'# macip-backup: managed-task={args.task}',
               f'# macip-backup: central-agent={args.agent}', f'# macip-backup: managed-bond={bond[:16]}']
    suffixes = ['service'] + ([] if envelope.get('schedule', {}).get('kind') == 'manual' else ['timer'])
    for suffix in suffixes:
        unit = directory / 'staged' / f'macip-backup-managed-{args.task}.{suffix}'
        if unit.is_symlink() or unit.resolve() != unit:
            raise ValueError('unit candidata com links')
        lines = unit.read_text().splitlines()
        for marker in markers:
            key = marker.split('=', 1)[0] + '='
            if [line for line in lines if line.startswith(key)] != [marker]:
                raise ValueError('unit candidata sem propriedade completa')
    return config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for field in ('state', 'central', 'store', 'preparation', 'task', 'client', 'agent', 'destination', 'bond', 'install'):
        parser.add_argument('--' + field, required=True)
    parser.add_argument('--source')
    parser.add_argument('--generation', type=int, required=True)
    parser.add_argument('--revision', type=int, required=True)
    args = parser.parse_args()
    try:
        print(select(args))
    except (ValueError, KeyError, TypeError, OSError):
        parser.exit(2, 'recusado: preparação não corresponde ao contexto exato\n')


if __name__ == '__main__':
    main()
