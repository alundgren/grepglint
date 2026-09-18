"""Fixed source tools. Invoked only inside the Linux handler mount namespace."""
import json
import os
from pathlib import Path
import selectors
import socket
import stat
import subprocess
import sys
import time
import uuid

ARGUMENT_BYTES = 16 * 1024
RESPONSE_BYTES = 64 * 1024
SOURCE = Path('/source')


class HandlerError(ValueError):
    pass


def relative_path(value, glob=False):
    if (not isinstance(value, str) or not value or len(value.encode()) > 4096
            or value.startswith(('/', '-')) or '\\' in value
            or any(ord(c) < 32 for c in value)
            or any(p in ('', '.', '..', '.git') for p in value.split('/'))):
        raise HandlerError('invalid_source_path')
    if not glob and any(c in value for c in '*?[]'):
        raise HandlerError('invalid_source_path')
    return value


def query_text(value):
    if not isinstance(value, str) or not value or len(value.encode()) > 2000 or '\x00' in value:
        raise HandlerError('invalid_query')
    return value


def command(arguments, seconds=32):
    process = subprocess.Popen(arguments, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    output, diagnostic = bytearray(), bytearray()
    deadline = time.monotonic() + seconds
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ, output)
            selector.register(process.stderr, selectors.EVENT_READ, diagnostic)
            while selector.get_map():
                if time.monotonic() >= deadline:
                    raise HandlerError('handler_deadline_exceeded')
                for key, _ in selector.select(0.05):
                    data = os.read(key.fileobj.fileno(), 4096)
                    if not data:
                        selector.unregister(key.fileobj)
                        continue
                    key.data.extend(data)
                    if len(output) + len(diagnostic) > RESPONSE_BYTES:
                        raise HandlerError('handler_output_limit_exceeded')
        process.wait(timeout=max(0.01, deadline - time.monotonic()))
        return process.returncode, output.decode('utf-8'), diagnostic.decode('utf-8')
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=2)
        process.stdout.close()
        process.stderr.close()


def read_range(arguments):
    path = relative_path(arguments['path'])
    start, end = arguments['start'], arguments['end']
    if type(start) is not int or type(end) is not int or not 1 <= start <= end <= 12000:
        raise HandlerError('invalid_line_range')
    directory = os.open(SOURCE, os.O_PATH | os.O_DIRECTORY)
    try:
        parts = path.split('/')
        for part in parts[:-1]:
            next_fd = os.open(part, os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = next_fd
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(fd, 'rb') as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 512 * 1024:
                raise HandlerError('source_file_limit_exceeded')
            data = source.read(512 * 1024 + 1)
        if len(data) > 512 * 1024 or b'\0' in data:
            raise HandlerError('invalid_source_file')
        lines = data.splitlines(keepends=True)
        if start > len(lines):
            raise HandlerError('line_range_outside_file')
        before = sum(map(len, lines[:start - 1]))
        content = b''.join(lines[start - 1:end])
        return {'path': path, 'start': start, 'end': min(end, len(lines)),
                'byte_start': before, 'byte_end': before + len(content),
                'bytes': len(content), 'content': content.decode('utf-8')}
    finally:
        os.close(directory)


def execute(name, arguments):
    fields = {'text_search': {'query'}, 'file_list': {'glob'},
              'read_file': {'path', 'start', 'end'}, 'grepglint_search': {'query'}}
    if name not in fields:
        raise HandlerError('unknown_handler')
    if not isinstance(arguments, dict) or set(arguments) != fields[name]:
        raise HandlerError('invalid_arguments')
    if name == 'read_file':
        return read_range(arguments)
    if name == 'file_list':
        pattern = relative_path(arguments['glob'], glob=True)
        code, output, errors = command(['/opt/rg', '--files', '--hidden', '--no-config',
                                         '--glob', pattern, '--glob', '!.git', '.'])
    elif name == 'text_search':
        code, output, errors = command(['/opt/rg', '--json', '--fixed-strings', '--hidden',
            '--no-config', '--glob', '!.git', '--', query_text(arguments['query']), '.'])
    else:
        code, output, errors = command(['/opt/grepglint', 'search', '--json', '--',
                                        query_text(arguments['query'])])
    if name != 'grepglint_search' and code not in (0, 1):
        raise HandlerError('search_failed')
    return {'content': output, 'bytes': len(output.encode()), 'stderr': errors,
            'exit_code': code, 'success': code == 0 or name != 'grepglint_search'}


def negative_checks(forbidden):
    checks = {}
    for name, path in forbidden.items():
        try:
            Path(path).read_bytes()
            checks[name] = False
        except OSError:
            checks[name] = True
    try:
        (SOURCE / 'write-attempt').write_text('forbidden')
        checks['source_write'] = False
    except OSError:
        checks['source_write'] = True
    try:
        subprocess.run(['/source/execute.sh'], check=False, timeout=1,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        checks['repository_execution'] = False
    except PermissionError:
        checks['repository_execution'] = True
    for family in (socket.AF_INET, socket.AF_INET6):
        try:
            with socket.socket(family, socket.SOCK_STREAM):
                checks['network_' + str(family)] = False
        except PermissionError:
            checks['network_' + str(family)] = True
    (Path('/cache') / 'write-check').write_text('owned')
    checks['private_cache_write'] = True
    return checks


def main():
    sys.path.insert(0, '/opt')
    from _codex_isolation import UnsupportedHost, restrict_handler
    try:
        restrict_handler()
    except UnsupportedHost as error:
        print(json.dumps({'ready': False, 'unsupported': True, 'error': str(error)}), flush=True)
        return 2
    cache_identity = uuid.uuid4().hex
    Path('/cache/identity').write_text(cache_identity)
    print(json.dumps({'ready': True, 'cache_identity': cache_identity,
                      'checks': negative_checks(json.loads(sys.argv[1]))}), flush=True)
    while line := sys.stdin.buffer.readline(ARGUMENT_BYTES + 1):
        try:
            if len(line) > ARGUMENT_BYTES:
                raise HandlerError('arguments_limit_exceeded')
            request = json.loads(line)
            result = execute(request['name'], request['arguments'])
            response = {'ok': True, 'result': result}
        except (ValueError, OSError, KeyError, UnicodeError) as error:
            response = {'ok': False, 'error': str(error) if isinstance(error, HandlerError)
                        else 'source_read_failed'}
        data = json.dumps(response, separators=(',', ':')).encode()
        if len(data) > RESPONSE_BYTES:
            data = b'{"ok":false,"error":"handler_response_limit_exceeded"}'
        sys.stdout.buffer.write(data + b'\n')
        sys.stdout.buffer.flush()
        if len(line) > ARGUMENT_BYTES:
            return


if __name__ == '__main__':
    raise SystemExit(main())
