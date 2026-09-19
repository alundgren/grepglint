"""Append-only account authorization and attempt history, independent of smoke runs."""
import fcntl
import os
from pathlib import Path
import secrets
import stat
import time

from _codex_audit import encoded
from _codex_capture import ProbeError, json_value
from codex_preflight import digest, private_directory

MAX_BYTES = 16 * 1024 * 1024
MAX_RUNS = 64
EVENT_BYTES = 64 * 1024
RUN_RESERVE = 256 * 1024


def ledger_path():
    return Path.home() / '.local/state/grepglint/paired-attempts-v1.jsonl'


class Attempts:
    def __init__(self, path=None):
        self.path = path or ledger_path()
        private_directory(self.path.parent)
        try:
            self.fd = os.open(self.path, os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            created = True
        except FileExistsError:
            self.fd = os.open(self.path, os.O_RDWR | os.O_APPEND | os.O_NOFOLLOW)
            created = False
        self.poisoned = False
        self.header = False
        self.runs = {}
        self.bytes = 0
        try:
            info = os.fstat(self.fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > MAX_BYTES:
                raise ProbeError('invalid_paired_attempt_ledger')
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise ProbeError('another_account_benchmark_is_active') from error
            with os.fdopen(os.dup(self.fd), 'rb') as stream:
                while line := stream.readline(EVENT_BYTES + 1):
                    if len(line) > EVENT_BYTES or not line.endswith(b'\n'):
                        raise ProbeError('partial_attempt_ledger_preserved')
                    self.bytes += len(line)
                    self.apply(json_value(line))
            if created:
                self.append({'action': 'created'})
            elif not self.header:
                raise ProbeError('empty_attempt_ledger_preserved')
            directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except BaseException:
            self.close()
            raise

    def apply(self, event):
        if not isinstance(event, dict) or event.get('version') != 1:
            raise ProbeError('invalid_paired_attempt_event')
        run_id, action = event.get('run_id'), event.get('action')
        if action == 'created' and not self.header and not self.runs:
            self.header = True
            return
        if not self.header:
            raise ProbeError('attempt_ledger_header_required')
        if action == 'planned':
            if not isinstance(run_id, str) or run_id in self.runs or len(self.runs) >= MAX_RUNS:
                raise ProbeError('invalid_paired_authorization')
            trials = event.get('trials')
            if (not isinstance(trials, list) or not 1 <= len(trials) <= 160
                    or len({t['trial_id'] for t in trials}) != len(trials)):
                raise ProbeError('invalid_attempt_trials')
            self.runs[run_id] = {**event, 'state': 'planned',
                'states': {t['trial_id']: 'not-started' for t in trials}}
            return
        run = self.runs.get(run_id)
        if run is None:
            raise ProbeError('unknown_attempt_run')
        if action == 'cancelled' and run['state'] == 'planned':
            run['state'] = 'stopped'
        elif action == 'started' and run['state'] == 'planned':
            run['state'] = 'started'
        elif action == 'stopped' and run['state'] == 'started':
            run['state'] = 'stopped'
        elif action in ('reserved', 'completed', 'failed') and run['state'] == 'started':
            trial_id = event.get('trial_id')
            states = run['states']
            if action == 'reserved':
                pending = [t for t, state in states.items() if state != 'completed']
                if not pending or pending[0] != trial_id or states[trial_id] != 'not-started':
                    raise ProbeError('trial_already_consumed_or_out_of_order')
            elif states.get(trial_id) != 'reserved':
                raise ProbeError('invalid_attempt_transition')
            states[trial_id] = action
        else:
            raise ProbeError('authorization_consumed_or_invalid_transition')

    def append(self, event):
        if self.poisoned:
            raise ProbeError('failed_attempt_write_preserved')
        event = {'version': 1, 'at': time.time(), **event}
        data = encoded(event) + b'\n'
        if len(data) > EVENT_BYTES or self.bytes + len(data) > MAX_BYTES:
            raise ProbeError('attempt_history_capacity_exhausted_preserve_ledger')
        self.apply(event)
        # A partial write is never repaired or replayed automatically.
        try:
            count = os.write(self.fd, data)
            os.fsync(self.fd)
            self.bytes += count
            if count != len(data):
                raise ProbeError('partial_attempt_ledger_preserved')
        except BaseException:
            self.poisoned = True
            raise

    def history(self):
        return [{'run_id': key, 'state': value['state'], 'plan_sha256': value['binding']['plan_sha256'],
                 'states': value['states']} for key, value in self.runs.items()]

    def prepare(self, run_id, binding, trials):
        if len(self.runs) >= MAX_RUNS or self.bytes + RUN_RESERVE * (1 + sum(r['state'] in ('planned', 'started') for r in self.runs.values())) > MAX_BYTES:
            raise ProbeError('attempt_history_capacity_exhausted_preserve_ledger')
        history = digest(encoded(self.history()))
        token = digest(encoded([binding, run_id, history, secrets.token_hex(32)]))
        self.append({'action': 'planned', 'run_id': run_id, 'binding': binding,
            'confirmation': token, 'previous_history_sha256': history,
            'trials': [{key: t[key] for key in ('trial_id', 'pair_id', 'configuration')} for t in trials]})
        return token

    def start(self, run_id, binding, confirmation):
        run = self.runs.get(run_id)
        if (not run or run['state'] != 'planned' or run['binding'] != binding
                or not isinstance(confirmation, str) or confirmation != run['confirmation']
                or digest(encoded([item for item in self.history() if item['run_id'] != run_id])) != run['previous_history_sha256']):
            raise ProbeError('changed_plan_or_consumed_confirmation')
        self.append({'action': 'started', 'run_id': run_id})

    def trial(self, run_id, trial_id, state):
        self.append({'action': state, 'run_id': run_id, 'trial_id': trial_id})

    def stop(self, run_id):
        self.append({'action': 'stopped', 'run_id': run_id})

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
