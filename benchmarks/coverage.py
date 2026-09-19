#!/usr/bin/env python3
"""Measure static file limits and one cold index for each locked source."""
import argparse
import codecs
from collections import Counter
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import shutil
import tempfile
import time

from prepare import PreparationError, budget, command, exclusive, git, INDEX_TIMEOUT

EXCLUDED_DIRS = {'node_modules', 'vendor', 'dist', 'build', 'coverage', 'target', '.next', '.cache'}
EXCLUDED_FILES = {'pnpm-lock.yaml', 'package-lock.json', 'yarn.lock', 'bun.lock', 'Cargo.lock', '.grepglintignore'}


@contextmanager
def private_cache(snapshots):
    cache = Path(tempfile.mkdtemp(prefix='.coverage-', dir=snapshots))
    try:
        yield cache
    finally:
        if not (cache / 'daemon.sock').exists():
            shutil.rmtree(cache)


def inventory(root):
    records = []
    entries = command(['git', 'ls-tree', '-rlz', 'HEAD'], root)
    for entry in entries.split(b'\0'):
        if not entry:
            continue
        meta, path = entry.split(b'\t', 1)
        mode, kind, oid, size = meta.decode().split()
        path = path.decode('utf-8')
        record = {'path': path, 'mode': mode, 'git_blob': oid, 'bytes': int(size) if size != '-' else 0}
        reason = None
        if Path(path).name in EXCLUDED_FILES or EXCLUDED_DIRS.intersection(Path(path).parts[:-1]):
            reason = 'excluded_path'
        elif mode not in ('100644', '100755'):
            reason = 'not_regular_file'
        if mode in ('100644', '100755'):
            lines, max_line, binary, invalid = 0, 0, False, False
            pending_length = 0
            last_byte = b''
            decoder = codecs.getincrementaldecoder('utf-8')()
            digest = hashlib.sha256()
            with (root / path).open('rb') as stream:
                while data := stream.read(65536):
                    digest.update(data)
                    binary |= b'\0' in data
                    if not invalid:
                        try:
                            decoder.decode(data)
                        except UnicodeDecodeError:
                            invalid = True
                    parts = data.split(b'\n')
                    for index, part in enumerate(parts):
                        pending_length += len(part)
                        if index < len(parts) - 1:
                            tail = part[-1:] if part else last_byte
                            max_line = max(max_line, pending_length - int(tail == b'\r'))
                            lines += 1
                            pending_length = 0
                        last_byte = part[-1:]
            if pending_length:
                lines += 1
                max_line = max(max_line, pending_length - int(last_byte == b'\r'))
            if not invalid:
                try:
                    decoder.decode(b'', final=True)
                except UnicodeDecodeError:
                    invalid = True
            record.update(lines=lines, sha256=digest.hexdigest())
            if reason is None:
                if record['bytes'] > 512 * 1024:
                    reason = 'file_too_large'
                elif binary:
                    reason = 'binary'
                elif invalid:
                    reason = 'invalid_utf8'
                elif lines > 12000:
                    reason = 'too_many_lines'
                elif max_line > 4000:
                    reason = 'line_too_long'
        record['static_skip'] = reason
        records.append(record)
    return records


def measure(source, snapshots, output, binary):
    with exclusive(snapshots):
        return measure_locked(source, snapshots, output, binary)


def measure_locked(source, snapshots, output, binary):
    root = snapshots / source['id']
    if not root.is_dir():
        raise PreparationError(f'Source unavailable: {root}; run prepare.py first')
    if git(root, 'rev-parse', 'HEAD^{tree}') != source['tree'] or git(root, 'status', '--porcelain'):
        raise PreparationError('Snapshot tree is wrong or working files changed')
    rows = inventory(root)
    result = {'source': source['id'], 'tree': source['tree'], 'paths': len(rows),
              'source_bytes': sum(r['bytes'] for r in rows),
              'file_lines': sum(r.get('lines', 0) for r in rows),
              'static_skip_counts': dict(Counter(r['static_skip'] for r in rows if r['static_skip'])),
              'static_policy': 'README defaults; chunk count requires actual indexing',
              'binary_sha256': hashlib.sha256(binary.read_bytes()).hexdigest(),
              'query': 'corpus coverage', 'index_timeout_seconds': INDEX_TIMEOUT}
    if (root / '.grepglintignore').exists():
        raise PreparationError('Snapshot has custom exclusions; static default inventory cannot classify it')
    budget(snapshots, 576 * 1024 ** 2)
    start = time.monotonic()
    with private_cache(snapshots) as cache:
        env = {**os.environ, 'GREPGLINT_CACHE_DIR': str(cache), 'GREPGLINT_IDLE_SECONDS': '1'}
        try:
            response = command([str(binary), 'search', '--json', '--limit', '1', result['query']], root,
                               timeout=INDEX_TIMEOUT, limit=65536, env=env)
            result.update(status='success', stats=json.loads(response)['stats'], response_bytes=len(response))
        except PreparationError as error:
            result.update(status='failed', error=str(error))
        result['cold_wall_seconds'] = round(time.monotonic() - start, 3)
        pid_file = cache / 'daemon.pid'
        if pid_file.exists():
            status = Path('/proc') / pid_file.read_text().strip() / 'status'
            if status.exists():
                result['daemon_memory_kib'] = {line.split(':')[0]: int(line.split()[1])
                    for line in status.read_text().splitlines() if line.startswith(('VmHWM:', 'VmPeak:'))}
        deadline = time.monotonic() + 8
        while (cache / 'daemon.sock').exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        if (cache / 'daemon.sock').exists():
            raise PreparationError(f'Coverage daemon did not exit at idle deadline; cache retained at {cache}')
        result['cache_bytes'] = sum(p.stat().st_size for p in cache.iterdir() if p.is_file())
        indexed = {}
        database = cache / 'index.sqlite'
        if database.exists():
            with sqlite3.connect(f'file:{database}?mode=ro', uri=True) as connection:
                indexed = dict(connection.execute('SELECT p.path,count(c.id) FROM base_files b JOIN paths p ON b.path_id=p.id LEFT JOIN chunks c ON c.content_id=b.content_id GROUP BY p.path'))
        for row in rows:
            row['indexed_chunks'] = indexed.get(row['path'], 0)
        result['indexed_files'] = sum(r['indexed_chunks'] > 0 for r in rows)
    output.mkdir(exist_ok=True, parents=True)
    (output / (source['id'] + '.json')).write_text(json.dumps({'summary': result, 'files': rows}, indent=2) + '\n')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshots', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--binary', required=True, type=Path)
    parser.add_argument('--source')
    args = parser.parse_args()
    sources = json.loads(Path(__file__).with_name('sources.json').read_text())
    try:
        for source in sources:
            if args.source is None or args.source == source['id']:
                print(json.dumps(measure(source, args.snapshots.resolve(), args.output.resolve(), args.binary.resolve())), flush=True)
    except (PreparationError, OSError, ValueError, sqlite3.Error) as error:
        parser.exit(1, f'Coverage failed: {error}\n')


if __name__ == '__main__':
    main()
