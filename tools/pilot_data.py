#!/usr/bin/env python3
"""Dados/referências de laboratório. Não modifica código nem política do produto."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import random
import re
import stat
import tempfile
import uuid


def fail(message):
    raise ValueError(message)


def canonical(path):
    path = Path(path)
    if not path.is_absolute() or path.resolve() != path:
        fail('caminho absoluto sem links/componentes relativos exigido')
    return path


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def publish(path, document):
    """Publicação atômica sem substituir referência existente."""
    data = (json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode()
    fd, name = tempfile.mkstemp(prefix='.publish-', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
        os.link(temporary, path)  # falha se destino existir; nunca sobrescreve
        directory = os.open(path.parent, os.O_DIRECTORY)
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


class Dataset:
    def __init__(self, area):
        self.area = canonical(area)
        info = self.area.stat()
        if self.area in (Path('/'), Path.home()) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            fail('área exclusiva privada de propriedade do executor exigida')
        self.source = canonical(self.area / 'origem')
        self.references = canonical(self.area / 'referencias')
        self.marker = self.area / '.pilot-data.json'

    @contextmanager
    def locked(self, path):
        path = canonical(path)
        if path != self.area / 'locks/maintenance.lock' or not path.is_file():
            fail('lock existente AREA/locks/maintenance.lock exigido')
        install = self.area / 'programa/install.json'
        if install.exists():
            recorded = json.loads(install.read_text()).get('lock_directory')
            if recorded is None or canonical(recorded) != path.parent:
                fail('lock diverge da identidade instalada')
        with path.open('r+') as stream:
            try: fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError: fail('backup ou manutenção em andamento; origem preservada')
            yield

    def generation(self, name):
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', name):
            fail('identificador de geração inválido')
        return self.references / (name + '.json')

    def initialized(self):
        if not self.marker.is_file() or self.marker.is_symlink():
            fail('área não inicializada pelo helper')
        if json.loads(self.marker.read_text()) != {'area': str(self.area), 'version': 1}:
            fail('identidade da área divergente')

    def pending(self):
        self.initialized()
        for manifest in self.references.glob('*.json'):
            if manifest.name.endswith('.binding.json'):
                continue
            if not manifest.with_name(manifest.stem + '.binding.json').exists():
                fail('geração ainda não associada; não modificar origem antes do backup terminar')

    def files(self, root):
        root = canonical(root)
        entries = {}
        if not root.is_dir(): fail('pasta esperada ausente')
        for path in sorted(root.rglob('*')):
            if path.is_symlink() or path.resolve() != path:
                fail('link no conjunto de dados recusado')
            if path.is_file():
                entries[path.relative_to(root).as_posix()] = {
                    'sha256': digest(path), 'bytes': path.stat().st_size}
            elif not path.is_dir(): fail('entrada não regular recusada')
        return entries

    def capture(self, generation):
        self.initialized()
        destination = self.generation(generation)
        if destination.exists(): fail('manifesto já existe')
        entries = {name: value for name, value in self.files(self.source).items()
                   if not PurePosixPath(name).match('*.tmp')}
        if not entries: fail('manifesto vazio recusado')
        document = {'version': 1, 'generation': generation, 'source': str(self.source),
                    'captured_at': datetime.now(timezone.utc).isoformat(), 'files': entries}
        publish(destination, document)
        return destination

    def generate(self, generation, seed, large_bytes):
        self.generation(generation)  # validar identificador antes de qualquer escrita
        if large_bytes < 1: fail('tamanho binário deve ser positivo')
        if self.source.exists() or self.references.exists() or self.marker.exists():
            fail('origem/referências já existem; não sobrescrever')
        self.source.mkdir(mode=0o700); self.references.mkdir(mode=0o700)
        publish(self.marker, {'area': str(self.area), 'version': 1})
        rng = random.Random(seed)
        folders = [self.source]
        for level in range(8):
            folder = folders[-1] / f'nível {level} ação'
            folder.mkdir(); folders.append(folder)
        for number in range(40):
            (rng.choice(folders) / f'arquivo {number:03d} ção.txt').write_bytes(
                (f'geração {seed} arquivo {number}\n' * rng.randrange(1, 10)).encode())
        for name in ('grande-a.bin', 'grande-b.bin'):
            with (self.source / name).open('xb') as stream:
                remaining = large_bytes
                while remaining:
                    size = min(remaining, 1024 * 1024)
                    stream.write(rng.randbytes(size)); remaining -= size
        (self.source / 'ignorar.tmp').write_bytes(b'synthetic exclusion\n')
        return self.capture(generation)

    def mutate(self, generation, day, force_binary=False):
        if type(day) is not int or day < 1: fail('dia deve ser inteiro positivo')
        self.pending()
        if self.generation(generation).exists(): fail('geração já existe')
        entries = self.files(self.source)
        files = [self.source / name for name in entries if not name.endswith('.tmp')]
        rng = random.Random(f'mut-{day}')
        chosen = rng.sample(files, max(1, len(files) // 20))
        if force_binary:
            chosen[0] = self.source / 'grande-a.bin'
            if not chosen[0].is_file(): fail('binário da prova ausente')
        for path in chosen:
            with path.open('ab') as stream:
                stream.write(f'\nmutação dia {day}\n'.encode())
        (self.source / f'novo-dia-{int(day)}.txt').write_bytes(f'novo {day}\n'.encode())
        victims = [p for p in files if p.suffix == '.txt']
        if not victims: fail('nenhum arquivo textual para remoção controlada')
        rng.choice(victims).unlink()
        return self.capture(generation)

    def manifest(self, generation):
        path = canonical(self.generation(generation))
        document = json.loads(path.read_text())
        if document.get('generation') != generation or document.get('source') != str(self.source):
            fail('geração/origem do manifesto divergente')
        entries = document.get('files')
        if not isinstance(entries, dict) or not entries: fail('manifesto vazio/inválido')
        for name, entry in entries.items():
            part = PurePosixPath(name)
            if part.is_absolute() or '..' in part.parts or str(part) != name or name in ('', '.'):
                fail('caminho do manifesto escapa da raiz')
            if part.match('*.tmp') or not isinstance(entry, dict) or not re.fullmatch(r'[a-f0-9]{64}', entry.get('sha256', '')):
                fail('entrada de manifesto inválida')
            if type(entry.get('bytes')) is not int or entry['bytes'] < 0: fail('tamanho inválido')
        return path, document

    def bind(self, generation, result_path, snapshots_path, task, destination, snapshot, execution=None):
        self.initialized()
        path, manifest = self.manifest(generation)
        target = path.with_name(generation + '.binding.json')
        if target.exists(): fail('associação já existe')
        if not re.fullmatch(r'[a-f0-9]{64}', snapshot): fail('snapshot completo exigido')
        uuid.UUID(task)
        result = json.loads(Path(result_path).read_text())
        if isinstance(result, list):
            if execution is None: fail('--execution obrigatório para selecionar no histórico')
            uuid.UUID(execution)
            matches = [item for item in result if item.get('execution_id') == execution]
            if len(matches) != 1: fail('execução ausente ou ambígua no histórico')
            result = matches[0]
        if not isinstance(result, dict) or (execution is not None and result.get('execution_id') != execution):
            fail('execução do resultado divergente')
        points = json.loads(Path(snapshots_path).read_text()).get('snapshots')
        if not isinstance(points, list): fail('inventário snapshots da CLI inválido')
        selected = [p for p in points if p.get('id') == snapshot and f'task:{task}' in (p.get('tags') or [])]
        if len(selected) != 1: fail('snapshot ausente/ambíguo ou de outra tarefa')
        if selected[0].get('paths') and selected[0]['paths'] != [str(self.source)]:
            fail('origens do snapshot divergentes')
        destinations = [d for d in result.get('destinations', []) if d.get('destination_id') == destination]
        if result.get('task_id') != task or result.get('status') != 'success' or len(destinations) != 1:
            fail('resultado de backup não corresponde à tarefa/destino')
        selected_result = destinations[0]
        result_id = selected_result.get('snapshot_id', '')
        if selected_result.get('status') != 'success' or not re.fullmatch(r'[a-f0-9]{8,64}', result_id) or not snapshot.startswith(result_id):
            fail('snapshot não corresponde ao resultado bem-sucedido')
        uuid.UUID(result['execution_id'])
        start = datetime.fromisoformat(result['started_at_utc'])
        finish = datetime.fromisoformat(result['finished_at_utc'])
        if not datetime.fromisoformat(manifest['captured_at']) <= start <= finish:
            fail('resultado anterior à referência ou intervalo inválido')
        current = {n: e for n, e in self.files(self.source).items() if not PurePosixPath(n).match('*.tmp')}
        if current != manifest['files']: fail('origem mudou durante a geração; não associar')
        publish(target, {'version': 1, 'generation': generation, 'manifest': path.name,
                         'manifest_sha256': digest(path), 'execution_id': result['execution_id'],
                         'task_id': task, 'destination_id': destination, 'snapshot_id': snapshot})
        return target

    def compare(self, binding, operation_root, task, destination, snapshot):
        self.initialized()
        binding = canonical(binding)
        if binding.parent != self.references or not binding.name.endswith('.binding.json'):
            fail('associação fora das referências autorizadas')
        doc = json.loads(binding.read_text())
        if (doc.get('task_id'), doc.get('destination_id'), doc.get('snapshot_id')) != (task, destination, snapshot):
            fail('referência não corresponde a snapshot/tarefa/destino')
        path, manifest = self.manifest(doc['generation'])
        if doc.get('manifest') != path.name or doc.get('manifest_sha256') != digest(path):
            fail('manifesto alterado ou associação divergente')
        root = canonical(operation_root)
        if root.parent != self.area/'recuperacoes' or str(uuid.UUID(root.name)) != root.name or not root.is_dir():
            fail('raiz exclusiva da restauração inválida')
        restored = canonical(root / self.source.relative_to('/'))
        if not restored.is_relative_to(root): fail('restauração escapa da raiz')
        actual = self.files(restored)
        for item in root.rglob('*'):
            if item.is_symlink() or (item.is_file() and not item.is_relative_to(restored)):
                fail('conteúdo inesperado fora da origem restaurada')
        if actual != manifest['files']: fail('conteúdo divergente: alterado/ausente/extra/exclusão')
        return {'snapshot': snapshot, 'generation': doc['generation'], 'restored_source': str(restored), 'files': len(actual)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--area', type=Path, required=True)
    parser.add_argument('--lock-file', type=Path)
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('generate', 'mutate', 'capture', 'bind'):
        sub = commands.add_parser(name); sub.add_argument('--generation', required=True)
        if name == 'generate':
            sub.add_argument('--seed', default='pilot-v1'); sub.add_argument('--large-bytes', type=int, default=268435456)
        elif name == 'mutate':
            sub.add_argument('--day', type=int, required=True); sub.add_argument('--force-binary', action='store_true')
        elif name == 'bind':
            sub.add_argument('--result', type=Path, required=True); sub.add_argument('--snapshots', type=Path, required=True)
            sub.add_argument('--execution', help='UUID exato obrigatório quando --result recebe history JSON')
            sub.add_argument('--task', required=True); sub.add_argument('--destination', required=True); sub.add_argument('--snapshot', required=True)
    sub = commands.add_parser('compare')
    sub.add_argument('--binding', type=Path, required=True); sub.add_argument('--operation-root', type=Path, required=True)
    sub.add_argument('--task', required=True); sub.add_argument('--destination', required=True); sub.add_argument('--snapshot', required=True)
    args = parser.parse_args()
    try:
        dataset = Dataset(args.area)
        if args.command == 'compare':
            result = dataset.compare(args.binding, args.operation_root, args.task, args.destination, args.snapshot)
        else:
            if args.lock_file is None: fail('--lock-file obrigatório para alterar/capturar/associar geração')
            with dataset.locked(args.lock_file):
                if args.command == 'generate': result = dataset.generate(args.generation, args.seed, args.large_bytes)
                elif args.command == 'mutate': result = dataset.mutate(args.generation, args.day, args.force_binary)
                elif args.command == 'capture': dataset.pending(); result = dataset.capture(args.generation)
                else: result = dataset.bind(args.generation, args.result, args.snapshots, args.task, args.destination, args.snapshot, args.execution)
        print(json.dumps(result if isinstance(result, dict) else {'path': str(result)}, ensure_ascii=False))
        return 0
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.error(str(error))


if __name__ == '__main__':
    main()
