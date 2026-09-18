"""Private bounded receipts with ownership checked before retention cleanup."""
import json
import hashlib
import os
from pathlib import Path
import secrets
import shutil
import stat
import time

from _codex_capture import FRAME_LIMIT, TOTAL_LIMIT, ProbeError, json_value
from _codex_isolation import FREE_RESERVE
from codex_preflight import private_directory

RETENTION_SECONDS = 3600
MAX_RUNS = 4
FILES = {'ownership.json', 'receipt.json', 'audit.jsonl'}


def identity(path):
    info = path.lstat()
    return [info.st_dev, info.st_ino]


def owned(run, sealed=False):
    info = run.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ProbeError('artifact_ownership_changed')
    if {p.name for p in run.iterdir()} != FILES:
        raise ProbeError('artifact_contents_changed')
    for name in FILES:
        info = (run / name).lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ProbeError('artifact_ownership_changed')
    if (run / 'ownership.json').stat().st_size > 4096:
        raise ProbeError('invalid_artifact_owner')
    owner = json_value((run / 'ownership.json').read_bytes())
    if owner.get('directory') != identity(run) or any(
            owner.get('files', {}).get(name) != identity(run / name)
            for name in ('receipt.json', 'audit.jsonl')):
        raise ProbeError('artifact_ownership_changed')
    if sealed:
        if 'sha256' not in owner:
            raise ProbeError('partial_artifact_requires_inspection')
        for name in ('receipt.json', 'audit.jsonl'):
            if (run / name).stat().st_size > TOTAL_LIMIT:
                raise ProbeError('artifact_contents_changed')
            if hashlib.sha256((run / name).read_bytes()).hexdigest() != owner['sha256'].get(name):
                raise ProbeError('artifact_contents_changed')
    return owner


def create(root):
    private_directory(root)
    runs = []
    for entry in root.iterdir():
        if entry.name.startswith('run-'):
            owner = owned(entry)
            runs.append((owner['created_at'], entry))
    runs.sort()
    for created, run in list(runs):
        if time.time() - created >= RETENTION_SECONDS or len(runs) >= MAX_RUNS:
            owned(run, sealed=True)
            for name in FILES:
                (run / name).unlink()
            run.rmdir()
            runs.remove((created, run))
    if sum(p.stat().st_size for _, run in runs for p in run.iterdir()) > MAX_RUNS * TOTAL_LIMIT:
        raise ProbeError('artifact_disk_limit_exceeded')
    run = root / ('run-' + secrets.token_hex(12))
    run.mkdir(mode=0o700)
    for name in ('receipt.json', 'audit.jsonl'):
        fd = os.open(run / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
    owner = {'schema_version': 1, 'created_at': time.time(), 'directory': identity(run),
             'files': {name: identity(run / name) for name in ('receipt.json', 'audit.jsonl')}}
    fd = os.open(run / 'ownership.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as output:
        json.dump(owner, output)
    save(run, {'schema_version': 2, 'contract': 'stock-codex-local-v2', 'status': 'incomplete',
               'errors': ['execution_not_finished'], 'inference_performed': False})
    return run


def seal(run):
    owner = owned(run)
    owner['sha256'] = {name: hashlib.sha256((run / name).read_bytes()).hexdigest()
                       for name in ('receipt.json', 'audit.jsonl')}
    fd = os.open(run / 'ownership.json', os.O_WRONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'w') as output:
        json.dump(owner, output)
        output.truncate()
        output.flush()
        os.fsync(output.fileno())


def save(run, receipt):
    owner = owned(run)
    data = (json.dumps(receipt, indent=2, sort_keys=True) + '\n').encode()
    if len(data) > FRAME_LIMIT:
        raise ProbeError('receipt_limit_exceeded')
    if shutil.disk_usage(run).free - len(data) < FREE_RESERVE:
        raise ProbeError('corpus_disk_reserve_unavailable')
    fd = os.open(run / 'receipt.json', os.O_WRONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'wb') as output:
        info = os.fstat(output.fileno())
        if [info.st_dev, info.st_ino] != owner['files']['receipt.json']:
            raise ProbeError('artifact_ownership_changed')
        output.write(data)
        output.truncate()
        output.flush()
        os.fsync(output.fileno())


def export(receipt):
    # Never copy free-form errors, subprocess text, calls, source, or instructions.
    result = {key: receipt[key] for key in ('schema_version', 'contract', 'status',
              'inference_performed') if key in receipt}
    result['client'] = {key: receipt.get('client', {}).get(key) for key in
                         ('version', 'binary_sha256', 'javascript_host_sha256', 'source_commit', 'pin_verified')}
    result['implementation_sha256'] = receipt.get('implementation_sha256')
    for key in ('grepglint_sha256', 'grepglint_catalog_sha256'):
        result[key] = receipt.get(key)
    result['audit'] = {key: receipt.get('audit', {}).get(key) for key in
                       ('status', 'sha256', 'records', 'calls', 'outer_cell_association')}
    for key in ('elapsed_seconds', 'captured_bytes'):
        if type(receipt.get(key)) in (int, float):
            result[key] = receipt[key]
    result['provider_verification'] = {'status': receipt.get('provider_verification', {}).get('status')}
    result['limits'] = {key: value for key, value in receipt.get('limits', {}).items()
                        if key in ('deadline_seconds', 'frame_bytes', 'total_bytes', 'tool_calls',
                            'argument_bytes', 'response_bytes', 'aggregate_memory_bytes',
                            'aggregate_cpu_cores', 'retention_seconds', 'retained_runs')
                        and type(value) in (int, float)}
    result['checks'] = {key: receipt.get(key, {}).get('status', 'incomplete')
                        for key in ('local_isolation', 'tool_policy', 'audit')}
    result['cleanup'] = {key: value for key, value in receipt.get('cleanup', {}).items()
                         if key in ('temporary_files_removed', 'owned_client_groups_stopped',
                                    'service_stopped') and type(value) is bool}
    fixtures = receipt.get('local_isolation', {}).get('resource_fixtures', {})
    result['resource_fixtures'] = {key: value for key, value in fixtures.items()
        if key in ('allocation_oom_denied', 'concurrent_cpu_loops_throttled',
                   'allocation_requested_bytes', 'memory_peak_including_fixture', 'cpu_usec')
        and type(value) in (bool, int)}
    result['sessions'] = [{**{key: session[key] for key in ('configuration', 'status',
                          'configuration_sha256', 'prompt_sha256', 'catalog_sha256',
                          'source_sha256') if key in session},
                          'audit': {key: session.get('audit', {}).get(key) for key in
                                    ('status', 'calls', 'controlled_calls', 'outer_cell_association')}}
                          for session in receipt.get('sessions', [])]
    permitted_checks = {'orchestrator_skills', 'executor_skills', 'missing_skill', 'hidden_skill',
        'shell', 'patch', 'delegation', 'resource', 'invalid_name', 'absolute_path', 'traversal',
        'symlink', 'history', 'deterministic_input', 'javascript_restrictions', 'nested_catalog',
        'yield_and_wait', 'javascript_allocation_and_cpu_loops', 'discarded_calls_retained',
        'grepglint_ok', 'grepglint_empty', 'real_grepglint_result', 'grepglint_error',
        'source', 'oracle', 'credentials', 'controller', 'source_write', 'repository_execution',
        'network_2', 'network_10', 'private_cache_write'}
    for original, clean in zip(receipt.get('sessions', []), result['sessions']):
        for field in ('negative_checks', 'isolation_checks'):
            clean[field] = {name: value for name, value in original.get(field, {}).items()
                            if name in permitted_checks and type(value) is bool}
    return result
