"""Owned, reserved artifacts with explicit cleanup and no automatic eviction."""
from contextlib import contextmanager
import fcntl
import hashlib
import os
import re
from pathlib import Path
import secrets
import shutil
import stat
import time

from _codex_audit import encoded
from _codex_capture import Budget, ProbeError, json_value, TOTAL_LIMIT
from _paired_trial import validate_audit
from _codex_isolation import FREE_RESERVE
from codex_preflight import private_directory
from _paired_contract import MAX_RUNS, STORE_BYTES, METADATA_BYTES, MAX_TRIALS, reservation, validate_record, initial_record, CONTRACT, LIMITS


@contextmanager
def execution_lock():
    root = Path('/tmp') / f'grepglint-paired-{os.getuid()}'
    private_directory(root)
    fd = os.open(root / 'execution.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ProbeError('another_paired_run_is_active') from error
        yield
    finally:
        os.close(fd)


def identity(path):
    info = path.lstat()
    return [info.st_dev, info.st_ino]


def read(path, limit=METADATA_BYTES):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ProbeError('unsafe_or_oversized_artifact')
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ProbeError('artifact_limit_exceeded')
    return json_value(data)


def hash_file(path, limit):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    result = hashlib.sha256()
    count = 0
    with os.fdopen(fd, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ProbeError('artifact_ownership_changed')
        while data := stream.read(65536):
            count += len(data)
            if count > limit:
                raise ProbeError('artifact_limit_exceeded')
            result.update(data)
    return result.hexdigest()


def write(path, value):
    data = encoded(value) + b'\n'
    if len(data) > METADATA_BYTES:
        raise ProbeError('artifact_metadata_limit_exceeded')
    if shutil.disk_usage(path.parent).free - len(data) < FREE_RESERVE:
        raise ProbeError('corpus_disk_reserve_unavailable')
    fd = os.open(path, os.O_WRONLY | os.O_NOFOLLOW | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(fd)


def owned(run, sealed=False):
    info = run.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ProbeError('artifact_ownership_changed')
    owner = read(run / 'ownership.json')
    files = owner.get('files', {})
    if owner.get('directory') != identity(run) or len(files) > 2 * MAX_TRIALS + 2:
        raise ProbeError('artifact_ownership_changed')
    names = set()
    for entry in run.iterdir():
        names.add(entry.name)
        if len(names) > 2 * MAX_TRIALS + 5:
            raise ProbeError('artifact_contents_changed')
    allowed = set(files) | {'ownership.json'}
    if not owner.get('sealed'):
        allowed |= {'.ownership.next', '.data.next'}
    if names - allowed or not (set(files) | {'ownership.json'}) <= names:
        raise ProbeError('artifact_contents_changed')
    for temporary in names & {'.ownership.next', '.data.next'}:
        info = (run / temporary).lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > METADATA_BYTES:
            raise ProbeError('unsafe_publication_temporary')
    for name, expected in files.items():
        if Path(name).name != name or name == 'ownership.json':
            raise ProbeError('invalid_artifact_filename')
        info = (run / name).lstat()
        pending = owner.get('pending', {})
        candidates = [expected]
        if pending.get('target') == name:
            candidates += [pending.get('old'), pending.get('new')]
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077
                or [info.st_dev, info.st_ino] not in candidates):
            raise ProbeError('artifact_ownership_changed')
        limit = TOTAL_LIMIT if name.endswith('.jsonl') else METADATA_BYTES
        if info.st_size > limit:
            raise ProbeError('artifact_limit_exceeded')
        if sealed and hash_file(run / name, limit) != owner.get('sha256', {}).get(name):
            raise ProbeError('edited_or_unsealed_artifact_preserved')
    if sealed and not owner.get('sealed'):
        raise ProbeError('active_or_unsealed_run_preserved')
    return owner


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def publish_owner(run, owner):
    temporary = run / '.ownership.next'
    write(temporary, owner)
    os.replace(temporary, run / 'ownership.json')
    sync_directory(run)


def create(root, plan):
    private_directory(root)
    entries = []
    for entry in root.iterdir():
        if len(entries) >= MAX_RUNS:
            raise ProbeError('artifact_cap_reached_use_cleanup')
        if not entry.name.startswith('run-'):
            raise ProbeError('unfinished_initialization_or_unrelated_artifact_preserved_inspect_root')
        owner = owned(entry)
        if owner.get('sealed'):
            owned(entry, sealed=True)
        actual = 3 * METADATA_BYTES + sum((entry / name).stat().st_size for name in owner['files'])
        amount = max(actual, owner.get('reserved_bytes', 0))
        if not owner.get('sealed'):
            amount = max(amount, reservation((len(owner['files']) - 2) // 2))
        if type(amount) is not int or not 0 <= amount <= STORE_BYTES:
            raise ProbeError('invalid_artifact_reservation')
        entries.append(amount)
    amount = reservation(len(plan['trials']))
    if sum(entries) + amount > STORE_BYTES or len(entries) >= MAX_RUNS:
        raise ProbeError('artifact_cap_reached_use_cleanup')
    if shutil.disk_usage(root).free < FREE_RESERVE + amount:
        raise ProbeError('insufficient_space_for_run_reservation')
    run = root / ('run-' + secrets.token_hex(12))
    staging = root / ('initializing-' + run.name)
    staging.mkdir(mode=0o700)
    # Nothing executes until the complete initial record set is published.
    write(staging / 'plan.json', plan)
    write(staging / 'run.json', {'schema_version': plan['schema_version'], 'contract': CONTRACT, 'run_id': run.name,
        'simulation': plan['simulation'], 'inference_performed': False, 'status': 'incomplete', 'errors': [],
        'cleanup': {'service_stopped': False}, 'limits': LIMITS})
    names = ['plan.json', 'run.json']
    for trial in plan['trials']:
        name = trial['trial_id']
        write(staging / (name + '.json'), initial_record(run, plan, trial))
        os.close(os.open(staging / (name + '.jsonl'), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
        names += [name + '.json', name + '.jsonl']
    write(staging / 'ownership.json', {'schema_version': 1, 'directory': identity(staging),
        'files': {name: identity(staging / name) for name in names}, 'reserved_bytes': amount,
        'created_at': time.time(), 'sealed': False})
    sync_directory(staging)
    os.rename(staging, run)
    sync_directory(root)
    return run


def save(run, name, value):
    owner = owned(run)
    if owner.get('pending') or owner.get('sealed') or (run / '.ownership.next').exists() or (run / '.data.next').exists():
        raise ProbeError('unfinished_or_sealed_publication_preserved')
    if name not in owner['files']:
        raise ProbeError('unowned_artifact')
    owner['pending'] = {'target': name, 'old': owner['files'][name], 'new': None}
    publish_owner(run, owner)
    temporary = run / '.data.next'
    write(temporary, value)
    owner['pending']['new'] = identity(temporary)
    publish_owner(run, owner)
    os.replace(temporary, run / name)
    sync_directory(run)
    owner['files'][name] = owner['pending']['new']
    del owner['pending']
    publish_owner(run, owner)


def seal(run):
    owner = owned(run)
    if owner.get('pending') or (run / '.ownership.next').exists() or (run / '.data.next').exists():
        raise ProbeError('unfinished_publication_preserved')
    owner['sha256'] = {name: hash_file(run / name, TOTAL_LIMIT if name.endswith('.jsonl') else METADATA_BYTES)
                       for name in owner['files']}
    owner['reserved_bytes'] = 3 * METADATA_BYTES + sum((run / name).stat().st_size for name in owner['files'])
    owner['sealed'] = True
    publish_owner(run, owner)


def cleanup(run):
    owner = owned(run, sealed=True)
    for name in owner['files']:
        (run / name).unlink()
    (run / 'ownership.json').unlink()
    run.rmdir()


def validate_run(run):
    if run.name.startswith('initializing-run-'):
        info = run.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ProbeError('artifact_ownership_changed')
        return {'contract': CONTRACT, 'simulation': True, 'status': 'incomplete',
                'reason': 'initialization_not_published'}
    owner = owned(run)
    if owner.get('sealed'):
        owned(run, sealed=True)
    plan = read(run / 'plan.json')
    if not isinstance(plan.get('trials'), list) or not 1 <= len(plan['trials']) <= MAX_TRIALS:
        raise ProbeError('invalid_plan_trial_count')
    states = []
    budget = Budget(300, total=MAX_TRIALS * TOTAL_LIMIT)
    ids = set()
    for trial in plan['trials']:
        trial_id = trial.get('trial_id')
        if not isinstance(trial_id, str) or not re.fullmatch('t[0-9]{4}', trial_id) or trial_id in ids:
            raise ProbeError('invalid_trial_identity')
        ids.add(trial_id)
        path = run / (trial['trial_id'] + '.json')
        if not path.stat().st_size:
            states.append('incomplete')
            continue
        record = read(path)
        states.append(validate_record(record))
        if not record['simulation'] and record['inference_performed']:
            from _paired_proof import plan_hash
            authorization = read(run / 'run.json').get('authorization', {})
            binding = authorization.get('binding', {})
            if (record.get('authorization_sha256') != authorization.get('confirmation')
                    or binding.get('plan_sha256') != plan_hash(plan)
                    or record.get('offline_proof', {}).get('receipt_sha256') != binding.get('proofs', {}).get('receipt_sha256')):
                raise ProbeError('live_record_authorization_mismatch')
        if record['run_id'] != run.name or record['seed'] != plan['seed']:
            raise ProbeError('trial_plan_identity_mismatch')
        for key in ('trial_id', 'pair_id', 'task_id', 'partition', 'repetition', 'order', 'configuration', 'source'):
            if record[key] != trial[key]:
                raise ProbeError('trial_plan_identity_mismatch')
        if not validate_audit(run / (trial['trial_id'] + '.jsonl'), record, budget):
            states.append('incomplete')
    return {'contract': plan['contract'], 'simulation': plan['simulation'], 'trials': len(plan['trials']),
            'status': 'completed' if owner.get('sealed') and not owner.get('pending') and all(s == 'completed' for s in states) else 'incomplete'}
