#!/usr/bin/env python3
"""Prepare complete pinned source trees without executing downloaded code."""
import argparse
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path, PurePosixPath
import selectors
import re
import resource
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time

GIB = 1024 ** 3
SOURCE_LIMIT = GIB
TOTAL_LIMIT = 8 * GIB
FREE_RESERVE = GIB + 576 * 1024 ** 2
FETCH_TIMEOUT = 300
INDEX_TIMEOUT = 45


class PreparationError(ValueError):
    pass


@contextmanager
def exclusive(root):
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = root / '.preparation.lock'
    if lock_path.is_symlink():
        raise PreparationError('Preparation lock must not be a symbolic link')
    with lock_path.open('a+b') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise PreparationError('Another source preparation or coverage check is running') from error
        yield


def safe_path(value):
    if not isinstance(value, str) or not value or '\\' in value or any(ord(c) < 32 for c in value):
        raise PreparationError('Invalid repository-relative path')
    path = PurePosixPath(value)
    if path.is_absolute() or any(p in ('', '.', '..', '.git') for p in value.split('/')):
        raise PreparationError(f'Unsafe archive path: {value}')
    return path


def disk_bytes(root):
    return sum(p.lstat().st_size for p in root.rglob('*') if p.is_file() and not p.is_symlink())


def budget(root, additional=0):
    if disk_bytes(root) + additional > TOTAL_LIMIT:
        raise PreparationError('Preparation would exceed 8 GiB; remove an owned snapshot first')
    if shutil.disk_usage(root).free - additional < FREE_RESERVE:
        raise PreparationError('Preparation needs 1 GiB free beyond the 576 MiB Grepglint reserve')


def command(args, cwd, timeout=30, limit=16 * 1024 ** 2, env=None, output=None, budget_root=None):
    """Drain both pipes with byte caps and a wall deadline."""
    result = [bytearray(), bytearray()]
    counts = [0, 0]
    start = time.monotonic()
    def child_limit():
        resource.setrlimit(resource.RLIMIT_FSIZE, (SOURCE_LIMIT, SOURCE_LIMIT))
    proc = subprocess.Popen(args, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            preexec_fn=child_limit, start_new_session=True)
    checked = start
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(proc.stdout, selectors.EVENT_READ, 0)
            selector.register(proc.stderr, selectors.EVENT_READ, 1)
            while selector.get_map():
                if time.monotonic() - start > timeout:
                    raise PreparationError(f'Command timed out after {timeout}s: {args[0]}')
                if budget_root is not None and time.monotonic() - checked >= 0.25:
                    budget(budget_root)
                    checked = time.monotonic()
                for key, _ in selector.select(0.1):
                    data = os.read(key.fileobj.fileno(), 65536)
                    if not data:
                        selector.unregister(key.fileobj)
                        continue
                    stream = key.data
                    counts[stream] += len(data)
                    if counts[stream] > (limit if stream == 0 else 65536):
                        raise PreparationError(f'Command output exceeds limit: {args[0]}')
                    if stream == 0 and output is not None:
                        output.write(data)
                    else:
                        result[stream].extend(data)
        proc.wait(timeout=max(0.01, timeout - (time.monotonic() - start)))
        if proc.returncode:
            diagnostic = bytes(result[1] or result[0][:65536]).decode('utf-8', errors='replace').strip()
            raise PreparationError(diagnostic or f'{args[0]} exited {proc.returncode}')
        return bytes(result[0])
    finally:
        if proc.poll() is None:
            import signal
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
        proc.stdout.close()
        proc.stderr.close()


def extract(archive, root, expected_bytes):
    root.mkdir(mode=0o700)
    total = 0
    paths = set()
    prefix = None
    with tarfile.open(archive, 'r:*') as tar:
        for index, member in enumerate(tar):
            if index >= 50000:
                raise PreparationError('Archive exceeds 50,000 entries')
            name = member.name.rstrip('/')
            parts = PurePosixPath(name).parts
            if not parts:
                raise PreparationError('Archive entry has no path')
            if prefix is None:
                prefix = parts[0]
                safe_path(prefix)
            if parts[0] != prefix:
                raise PreparationError('Archive contains multiple roots')
            if len(parts) == 1:
                if not member.isdir():
                    raise PreparationError('Archive root must be a directory')
                continue
            rel = str(safe_path('/'.join(parts[1:])))
            if rel in paths:
                raise PreparationError(f'Duplicate archive path: {rel}')
            paths.add(rel)
            target = root / rel
            if any(p.is_symlink() for p in target.parents if p != root.parent):
                raise PreparationError(f'Archive path traverses a link: {rel}')
            target.parent.mkdir(parents=True, exist_ok=True)
            if member.isdir():
                target.mkdir(exist_ok=True)
            elif member.issym():
                link = member.linkname
                if not link or PurePosixPath(link).is_absolute() or '\\' in link:
                    raise PreparationError(f'Unsafe archive link: {rel}')
                resolved = (target.parent / link).resolve()
                if not resolved.is_relative_to(root.resolve()) or '.git' in PurePosixPath(link).parts:
                    raise PreparationError(f'Archive link leaves source: {rel}')
                target.symlink_to(link)
                total += len(link.encode())
            elif member.isfile():
                total += member.size
                if member.size < 0 or total > min(SOURCE_LIMIT, expected_bytes):
                    raise PreparationError('Archive exceeds pinned source byte limit')
                with tar.extractfile(member) as src, target.open('xb') as dst:
                    shutil.copyfileobj(src, dst, 65536)
                target.chmod(0o755 if member.mode & 0o111 else 0o644)
            else:
                raise PreparationError(f'Unsupported archive entry: {rel}')
    if total != expected_bytes:
        raise PreparationError(f'Source byte count differs: expected {expected_bytes}, got {total}')


def git_env():
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
               GIT_TERMINAL_PROMPT='0', GIT_AUTHOR_NAME='Corpus', GIT_AUTHOR_EMAIL='corpus@example.invalid',
               GIT_COMMITTER_NAME='Corpus', GIT_COMMITTER_EMAIL='corpus@example.invalid',
               GIT_AUTHOR_DATE='2000-01-01T00:00:00Z', GIT_COMMITTER_DATE='2000-01-01T00:00:00Z')
    return env


def git(root, *args):
    return command(['git', '-c', 'core.hooksPath=/dev/null', '-c', 'core.autocrlf=false',
                    '-c', 'commit.gpgsign=false', '-c', 'core.fsmonitor=false', *args], root,
                   timeout=120, env=git_env()).decode().strip()


def initialize(root, tree, gitlinks=()):
    git(root, 'init', '--template=', '-b', 'source')
    attributes = root / '.git/info/attributes'
    attributes.parent.mkdir(exist_ok=True)
    attributes.write_text('* -text -filter -ident -working-tree-encoding\n')
    git(root, 'add', '--all', '--force')
    for entry in gitlinks:
        path = str(safe_path(entry['path']))
        if not re.fullmatch('[0-9a-f]{40}', entry['commit']):
            raise PreparationError('Invalid submodule commit')
        git(root, 'update-index', '--add', '--cacheinfo', f"160000,{entry['commit']},{path}")
    actual = git(root, 'write-tree')
    if actual != tree:
        raise PreparationError(f'Archive tree differs from verified Git tree: {actual} != {tree}')
    git(root, 'commit', '-m', 'Pinned source snapshot')
    attributes.unlink()
    return git(root, 'rev-parse', 'HEAD')


def export_objects(repository, source, archive):
    entries = command(['git', 'ls-tree', '-rlz', source['commit']], repository, env=git_env())
    entries = [e for e in entries.split(b'\0') if e]
    if len(entries) != source['path_count']:
        raise PreparationError('Git path count differs from source lock')
    process = subprocess.Popen(['git', 'cat-file', '--batch'], cwd=repository, env=git_env(),
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    timer = threading.Timer(FETCH_TIMEOUT, process.kill)
    timer.start()
    try:
        with tarfile.open(archive, 'w') as tar:
            for entry in entries:
                meta, path = entry.split(b'\t', 1)
                mode, kind, oid, size = meta.split()
                rel = str(safe_path(path.decode('utf-8')))
                if kind == b'commit':
                    continue
                size = int(size)
                if size > SOURCE_LIMIT:
                    raise PreparationError('Git blob exceeds 1 GiB')
                process.stdin.write(oid + b'\n')
                process.stdin.flush()
                header = process.stdout.readline(1024).split()
                if header != [oid, b'blob', str(size).encode()]:
                    raise PreparationError('Git returned an unavailable or mismatched blob')
                member = tarfile.TarInfo('source/' + rel)
                member.mode = int(mode, 8) & 0o777
                if mode == b'120000':
                    if size > 4096:
                        raise PreparationError('Git link exceeds 4096 bytes')
                    data = process.stdout.read(size)
                    member.type = tarfile.SYMTYPE
                    member.linkname = data.decode('utf-8')
                    tar.addfile(member)
                else:
                    member.size = size
                    tar.addfile(member, process.stdout)
                if process.stdout.read(1) != b'\n':
                    raise PreparationError('Git blob stream ended early')
    finally:
        timer.cancel()
        timer.join()
        process.stdin.close()
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=5)
        process.stdout.close()


def prepare(source, output):
    with exclusive(output.resolve()):
        return prepare_locked(source, output)


def prepare_locked(source, output):
    if str(safe_path(source['id'])) != source['id'] or '/' in source['id']:
        raise PreparationError('Source ID must be a directory name')
    for field in ('commit', 'tree', 'upstream_commit'):
        if not re.fullmatch('[0-9a-f]{40}', source[field]):
            raise PreparationError(f'Invalid source {field}')
    if any(type(source[key]) is not int or source[key] < 1 for key in ('path_count', 'source_bytes')):
        raise PreparationError('Source path and byte counts must be positive integers')
    if source['path_count'] > 20000 or source['source_bytes'] > SOURCE_LIMIT:
        raise PreparationError('Source exceeds the 20,000-path or 1 GiB corpus limit')
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination = output / source['id']
    if destination.exists():
        raise PreparationError(f'Snapshot already exists: {destination}')
    budget(output, SOURCE_LIMIT + 3 * source['source_bytes'])
    with tempfile.TemporaryDirectory(prefix='.prepare-', dir=output) as temporary:
        base = Path(temporary)
        archive = base / 'source.tar'
        repo = source.get('mirror') or source['upstream']
        fetched = base / 'objects'
        command(['gh', 'repo', 'clone', f'https://github.com/{repo}', str(fetched), '--no-upstream',
                 '--', '--revision=' + source['commit'], '--depth=1', '--no-checkout', '--no-tags',
                 '--template=', '--config', 'core.hooksPath=/dev/null'], base,
                timeout=FETCH_TIMEOUT, env=git_env(), budget_root=output)
        if git(fetched, 'rev-parse', 'HEAD^{tree}') != source['tree']:
            raise PreparationError('Fetched Git tree differs from source lock')
        budget(output)
        export_objects(fetched, source, archive)
        root = base / 'source'
        extract(archive, root, source['source_bytes'])
        local_commit = initialize(root, source['tree'], source.get('gitlinks', []))
        if disk_bytes(root) > SOURCE_LIMIT:
            raise PreparationError('Source and its minimal Git repository exceed 1 GiB')
        budget(output)
        root.rename(destination)
    return {'source': source['id'], 'tree_verified': source['tree'], 'local_commit': local_commit,
            'path': str(destination), 'upstream_commit': source['upstream_commit']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', help='Source ID from sources.json')
    parser.add_argument('--sources', type=Path, default=Path(__file__).with_name('sources.json'))
    parser.add_argument('--output', type=Path, required=True, help='Disposable directory outside agent inputs')
    args = parser.parse_args()
    try:
        sources = json.loads(args.sources.read_text())
        source = next((s for s in sources if s['id'] == args.source), None)
        if source is None:
            raise PreparationError(f'Unknown source: {args.source}')
        print(json.dumps(prepare(source, args.output), indent=2))
    except (PreparationError, OSError, ValueError, tarfile.TarError) as error:
        parser.exit(1, f'Preparation failed: {error}\n')


if __name__ == '__main__':
    main()
