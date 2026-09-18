"""Read-only verification of prepared, complete source and minimal Git history."""
import hashlib
import os
from pathlib import Path
import stat
import time

from _codex_capture import ProbeError
from prepare import command, safe_path
from validate import load, git_tree


def entries(root, budget):
    pending = [root]
    count = 0
    while pending:
        directory = pending.pop()
        children = []
        with os.scandir(directory) as scan:
            for entry in scan:
                budget.remaining()
                count += 1
                path = Path(entry.path)
                if count > 100000 or len(path.relative_to(root).as_posix().encode()) > 4096:
                    raise ProbeError('prepared_source_entry_limit')
                children.append(path)
        for path in sorted(children):
            info = path.lstat()
            if stat.S_ISDIR(info.st_mode):
                pending.append(path)
            yield path, info


def verify(source, identity, corpus, budget):
    started = time.monotonic()
    root = source.absolute()
    if root.is_symlink() or not root.is_dir():
        raise ProbeError('prepared_source_required')
    inventory = load(corpus, 'coverage/' + identity['id'] + '.json')['files']
    if len(inventory) > 20000 or git_tree(inventory) != identity['tree']:
        raise ProbeError('source_inventory_mismatch')
    expected = {f['path']: f for f in inventory}
    observed = set()
    sha = hashlib.sha256()
    total = 0
    # Hash source and Git metadata so postchecks catch changes beyond tracked files.
    for path, info in entries(root, budget):
        rel = path.relative_to(root).as_posix()
        mode = stat.S_IFMT(info.st_mode)
        sha.update(rel.encode() + b'\0' + str(info.st_mode).encode() + b'\0')
        git = rel == '.git' or rel.startswith('.git/')
        if stat.S_ISDIR(mode):
            continue
        if not stat.S_ISREG(mode) and not stat.S_ISLNK(mode):
            raise ProbeError('unsupported_source_entry')
        if git and stat.S_ISLNK(mode):
            raise ProbeError('linked_git_metadata')
        total += info.st_size
        if total > 1024 ** 3:
            raise ProbeError('prepared_source_exceeds_1_gib')
        blob = hashlib.sha1(b'blob ' + str(info.st_size).encode() + b'\0')
        if stat.S_ISLNK(mode):
            data = os.readlink(path).encode()
            if not path.resolve().is_relative_to(root) or '.git' in Path(os.readlink(path)).parts:
                raise ProbeError('source_link_escape')
            blob.update(data)
            sha.update(data)
            actual_mode = '120000'
        else:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, 'rb') as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise ProbeError('source_changed')
                count = 0
                while data := stream.read(65536):
                    budget.remaining()
                    count += len(data)
                    if count > info.st_size:
                        raise ProbeError('source_changed')
                    blob.update(data)
                    sha.update(data)
            if count != info.st_size:
                raise ProbeError('source_changed')
            actual_mode = '100755' if info.st_mode & 0o111 else '100644'
        sha.update(b'\0')
        if not git:
            observed.add(rel)
            entry = expected.get(rel)
            if not entry or entry['git_blob'] != blob.hexdigest() or entry['mode'] != actual_mode:
                raise ProbeError('source_content_mismatch')
    wanted = {p for p, entry in expected.items() if entry['mode'] != '160000'}
    if observed != wanted:
        raise ProbeError('source_paths_mismatch')
    # Preparation creates a known config and a deterministic one-commit history.
    config = root / '.git/config'
    if config.read_bytes() != b'[core]\n\trepositoryformatversion = 0\n\tfilemode = true\n\tbare = false\n\tlogallrefupdates = true\n':
        raise ProbeError('prepared_git_configuration_changed')
    for relative in ('objects/info/alternates', 'shallow', 'info/grafts'):
        if (root / '.git' / relative).exists():
            raise ProbeError('prepared_git_history_changed')
    env = {'PATH': '/usr/bin:/bin', 'HOME': '/nonexistent', 'GIT_CONFIG_NOSYSTEM': '1',
           'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_OPTIONAL_LOCKS': '0', 'GIT_NO_LAZY_FETCH': '1',
           'GIT_NO_REPLACE_OBJECTS': '1', 'GIT_TERMINAL_PROMPT': '0'}
    def git(*args):
        return command(['git', '-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false',
                        '-c', 'protocol.allow=never', *args], root, env=env,
                       timeout=min(30, budget.remaining()), limit=1024 * 1024).decode().strip()
    local = git('rev-parse', 'HEAD')
    commit = git('cat-file', 'commit', 'HEAD')
    expected_commit = (f"tree {identity['tree']}\n"
        'author Corpus <corpus@example.invalid> 946684800 +0000\n'
        'committer Corpus <corpus@example.invalid> 946684800 +0000\n\nPinned source snapshot')
    if commit != expected_commit or git('rev-list', '--all', '--count') != '1' or git('remote'):
        raise ProbeError('prepared_git_history_changed')
    if git('rev-parse', 'HEAD^{tree}') != identity['tree']:
        raise ProbeError('prepared_git_tree_mismatch')
    return {'commit': identity['commit'], 'tree': identity['tree'], 'local_commit': local,
            'sha256': sha.hexdigest(), 'bytes': total, 'seconds': time.monotonic() - started}
