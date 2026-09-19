"""Linux launch restrictions for benchmark verification and fixture smoke sessions."""
import ctypes
import errno
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import stat
import subprocess

from _codex_capture import ProbeError

MEMORY_BYTES = 1024 * 1024 * 1024
CPU_QUOTA = '100000 100000'
TASKS = 128
FREE_RESERVE = 1024 ** 3 + 576 * 1024 ** 2
CLIENT_SHA256 = '3188814c35471432d4123203e0eb38e5bddc60226e3d7ddf0e59e649ea140022'
JAVASCRIPT_HOST_SHA256 = '0c57be435e73b70d9106c850d751cd259a7f04da958a453d7ef59090d82b70f1'


class UnsupportedHost(ProbeError):
    """A required launch restriction is unavailable before a verification turn."""


def file_hash(path):
    result = hashlib.sha256()
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 512 * 1024 ** 2:
            raise ProbeError('invalid_identity_file')
        total = 0
        while data := source.read(1024 * 1024):
            total += len(data)
            if total > 512 * 1024 ** 2:
                raise ProbeError('identity_file_limit_exceeded')
            result.update(data)
    return result.hexdigest()


def prerequisites(binary, root):
    if platform.system() != 'Linux' or platform.machine() != 'x86_64':
        raise UnsupportedHost('requires_linux_x86_64')
    for command in ('bwrap', 'systemd-run', 'systemctl', 'rg', 'git'):
        if not shutil.which(command):
            raise UnsupportedHost('missing_' + command.replace('-', '_'))
    if file_hash(binary) != CLIENT_SHA256:
        raise UnsupportedHost('client_binary_pin_mismatch')
    if file_hash(binary.parent / 'codex-code-mode-host') != JAVASCRIPT_HOST_SHA256:
        raise UnsupportedHost('javascript_host_pin_mismatch')
    if shutil.disk_usage(root).free < FREE_RESERVE:
        raise UnsupportedHost('corpus_disk_reserve_unavailable')


def check_user_manager():
    try:
        subprocess.run(['systemctl', '--user', 'show', '--property=Version', '--value'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2, check=True)
    except (OSError, subprocess.SubprocessError) as error:
        raise UnsupportedHost('systemd_user_manager_unavailable') from error


def service_command(unit, command, seconds=60, memory=MEMORY_BYTES, runtime=None):
    temporary = ['-p', 'RuntimeDirectory=' + runtime, '-p', 'RuntimeDirectoryMode=0700'] if runtime else []
    return ['systemd-run', '--user', '--quiet', '--pipe', '--wait', '--collect',
            '--unit', unit, '-p', f'MemoryMax={memory}', '-p', 'MemorySwapMax=0',
            '-p', 'CPUQuota=100%', '-p', 'CPUQuotaPeriodSec=100ms',
            '-p', f'TasksMax={TASKS}', '-p', f'RuntimeMaxSec={seconds}',
            '-p', 'TimeoutStopSec=2', '-p', 'KillMode=control-group',
            '-p', 'OOMPolicy=continue',
            '-p', 'LimitCORE=0', '-p', 'UMask=0077', *temporary, '--', *command]


def cgroup_limits(memory=MEMORY_BYTES):
    try:
        line = Path('/proc/self/cgroup').read_text().strip()
    except OSError as error:
        raise UnsupportedHost('cgroup_v2_required') from error
    if not line.startswith('0::/') or '\n' in line or '..' in line:
        raise UnsupportedHost('cgroup_v2_required')
    root = Path('/sys/fs/cgroup') / line[4:]
    try:
        values = {name: (root / name).read_text().strip()
                  for name in ('memory.max', 'memory.swap.max', 'cpu.max', 'pids.max')}
    except OSError as error:
        raise UnsupportedHost('cgroup_controllers_unavailable') from error
    if values != {'memory.max': str(memory), 'memory.swap.max': '0',
                  'cpu.max': CPU_QUOTA, 'pids.max': str(TASKS)}:
        raise UnsupportedHost('aggregate_resource_limits_not_enforced')
    return root, values


def bubblewrap():
    return ['bwrap', '--unshare-all', '--unshare-user', '--die-with-parent', '--new-session',
            '--disable-userns', '--cap-drop', 'ALL', '--clearenv',
            '--ro-bind', '/usr', '/usr', '--symlink', 'usr/bin', '/bin',
            '--symlink', 'usr/lib', '/lib', '--symlink', 'usr/lib64', '/lib64',
            '--proc', '/proc', '--dev', '/dev', '--dir', '/tmp',
            '--setenv', 'PATH', '/usr/bin:/bin:/opt', '--setenv', 'LANG', 'C.UTF-8']


def client_command(binary, configuration, arguments):
    # Only the model transport shares host networking. JavaScript has no I/O APIs.
    # Neither the prepared source nor the controller's files enter this mount tree.
    command = bubblewrap() + ['--share-net', '--ro-bind', str(binary.parent), '/opt',
        '--ro-bind', str(configuration), '/config', '--size', str(64 * 1024 ** 2),
        '--tmpfs', '/work']
    for name in ('user', 'codex', 'tmp', 'cache', 'data', 'state'):
        command += ['--dir', '/work/' + name]
    for key, value in {'HOME': '/work/user', 'CODEX_HOME': '/work/codex',
                       'TMPDIR': '/work/tmp', 'XDG_CACHE_HOME': '/work/cache',
                       'XDG_CONFIG_HOME': '/work/user', 'XDG_DATA_HOME': '/work/data',
                       'XDG_STATE_HOME': '/work/state', 'RUST_LOG': 'off'}.items():
        command += ['--setenv', key, value]
    return command + ['--chdir', '/work', '--remount-ro', '/', '--', '/opt/codex', *arguments]


def authenticated_client_command(binary, configuration, auth_file, arguments):
    """Launch Codex with only its installed auth file added to the empty home."""
    info = auth_file.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077 or auth_file.is_symlink()):
        raise UnsupportedHost('chatgpt_auth_file_must_be_private_and_owned')
    command = bubblewrap() + ['--share-net', '--ro-bind', str(binary.parent), '/opt',
        '--ro-bind', str(configuration), '/config', '--size', str(64 * 1024 ** 2),
        '--tmpfs', '/work']
    resolver = Path('/etc/resolv.conf').resolve()
    certificates = Path('/etc/ssl/certs')
    for path, directory in ((resolver, False), (certificates, True)):
        info = path.stat()
        expected = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
        if not expected or info.st_mode & 0o022:
            raise UnsupportedHost('provider_transport_files_unavailable')
    command += ['--dir', '/etc', '--ro-bind', str(resolver), '/etc/resolv.conf',
                '--dir', '/etc/ssl', '--ro-bind', str(certificates), '/etc/ssl/certs']
    for name in ('user', 'codex', 'tmp', 'cache', 'data', 'state'):
        command += ['--dir', '/work/' + name]
    command += ['--ro-bind', str(auth_file), '/work/codex/auth.json']
    for key, value in {'HOME': '/work/user', 'CODEX_HOME': '/work/codex',
                       'TMPDIR': '/work/tmp', 'XDG_CACHE_HOME': '/work/cache',
                       'XDG_CONFIG_HOME': '/work/user', 'XDG_DATA_HOME': '/work/data',
                       'XDG_STATE_HOME': '/work/state', 'RUST_LOG': 'off',
                       'NO_COLOR': '1'}.items():
        command += ['--setenv', key, value]
    return command + ['--chdir', '/work', '--remount-ro', '/', '--', '/opt/codex', *arguments]


def diagnostic_command(binary, fixture, arguments, environment):
    command = bubblewrap() + ['--share-net', '--ro-bind', str(binary.parent), '/opt',
                              '--bind', str(fixture), str(fixture)]
    for key, value in environment.items():
        command += ['--setenv', key, value]
    return command + ['--chdir', str(fixture / 'source'), '--remount-ro', '/',
                      '--', '/opt/codex', *arguments[1:]]


def handler_command(source, grepglint, module, forbidden):
    # Keep the 576 MiB write reserve available beside a full 256 MiB database.
    command = bubblewrap() + ['--ro-bind', str(source), '/source',
        '--ro-bind', str(grepglint), '/opt/grepglint',
        '--ro-bind', str(Path(shutil.which('rg')).resolve()), '/opt/rg',
        '--ro-bind', str(module), '/opt/handler.py',
        '--ro-bind', '/usr/bin/true', '/source-execution-probe',
        '--size', str(896 * 1024 ** 2), '--tmpfs', '/cache', '--dir', '/cache/tmp']
    for name in ('_codex_isolation.py', '_codex_capture.py'):
        command += ['--ro-bind', str(module.parent / name), '/opt/' + name]
    for key, value in {'HOME': '/cache', 'TMPDIR': '/cache/tmp',
                       'GREPGLINT_CACHE_DIR': '/cache/grepglint',
                       'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null',
                       'GIT_CONFIG_COUNT': '3', 'GIT_CONFIG_KEY_0': 'core.hooksPath',
                       'GIT_CONFIG_VALUE_0': '/dev/null', 'GIT_CONFIG_KEY_1': 'core.fsmonitor',
                       'GIT_CONFIG_VALUE_1': 'false', 'GIT_CONFIG_KEY_2': 'protocol.allow',
                       'GIT_CONFIG_VALUE_2': 'never', 'GIT_TERMINAL_PROMPT': '0',
                       'GIT_NO_LAZY_FETCH': '1', 'GIT_NO_REPLACE_OBJECTS': '1'}.items():
        command += ['--setenv', key, value]
    return command + ['--chdir', '/source', '--remount-ro', '/', '--',
                      '/usr/bin/python3', '-I', '/opt/handler.py', json.dumps(forbidden)]


def restrict_handler():
    """Deny source execution with Landlock and internet sockets with seccomp."""
    libc = ctypes.CDLL(None, use_errno=True)
    execute = ctypes.c_uint64(1)
    fd = libc.syscall(444, ctypes.byref(execute), ctypes.sizeof(execute), 0)
    if fd < 0:
        raise UnsupportedHost('landlock_unavailable')

    class Rule(ctypes.Structure):
        _pack_ = 1
        _fields_ = [('allowed_access', ctypes.c_uint64), ('parent_fd', ctypes.c_int)]

    try:
        for path in ('/usr', '/opt'):
            parent = os.open(path, os.O_PATH | os.O_CLOEXEC)
            try:
                rule = Rule(1, parent)
                if libc.syscall(445, fd, 1, ctypes.byref(rule), 0) != 0:
                    raise UnsupportedHost('landlock_rule_failed')
            finally:
                os.close(parent)
        if libc.prctl(38, 1, 0, 0, 0) != 0 or libc.syscall(446, fd, 0) != 0:
            raise UnsupportedHost('landlock_restrict_failed')
    finally:
        os.close(fd)
    # x86_64 socket and socketpair permit AF_UNIX only. The worker needs Unix
    # sockets for the real daemon. It has a separate empty network namespace too.
    class Filter(ctypes.Structure):
        _fields_ = [('code', ctypes.c_ushort), ('jt', ctypes.c_ubyte),
                    ('jf', ctypes.c_ubyte), ('k', ctypes.c_uint)]

    class Program(ctypes.Structure):
        _fields_ = [('length', ctypes.c_ushort), ('filters', ctypes.POINTER(Filter))]

    instructions = [
        (0x20, 0, 0, 4), (0x15, 1, 0, 0xc000003e), (0x06, 0, 0, 0x80000000),
        (0x20, 0, 0, 0), (0x45, 0, 1, 0x40000000), (0x06, 0, 0, 0x80000000),
        (0x15, 2, 0, 41), (0x15, 1, 0, 53),
        (0x06, 0, 0, 0x7fff0000), (0x20, 0, 0, 16),
        (0x15, 0, 1, 1), (0x06, 0, 0, 0x7fff0000),
        (0x06, 0, 0, 0x00050000 | errno.EPERM)]
    filters = (Filter * len(instructions))(*(Filter(*i) for i in instructions))
    program = Program(len(instructions), filters)
    if libc.prctl(22, 2, ctypes.byref(program), 0, 0) != 0:
        raise UnsupportedHost('seccomp_unavailable')
