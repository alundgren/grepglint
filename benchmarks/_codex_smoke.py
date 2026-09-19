"""Explicit, confirmed ChatGPT smoke sessions for the pinned Codex client."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import secrets
import stat
import subprocess
import sys
import tempfile
import time

from _codex_audit import Audit, encoded
from _codex_capture import Budget, Child, FRAME_LIMIT, ProbeError, json_value
from _codex_artifacts import owned
from _codex_isolation import (CLIENT_SHA256, authenticated_client_command, file_hash,
                              cgroup_limits, check_user_manager, prerequisites, service_command)
from _codex_session import (BASE, Handlers, NO_ASSISTANCE, PROMPT, definitions,
                            AUXILIARY, configuration)
from _codex_verify import CONTRACT, implementation_hash, prepare_fixture, source_hash
from codex_preflight import (EFFORT, MODEL, ProbeCancelled, cancellation_signals, digest,
                             private_directory, toml_value)

SESSION_SECONDS = 120
SESSION_CALLS = 20
MAX_ATTEMPTS = 2
FRESH_SECONDS = 300
RESET_OBSERVATION_SECONDS = 5
PROVIDER_ID = 'grepglint-chatgpt-smoke'
CHATGPT_BASE_URL = 'https://chatgpt.com/backend-api/codex'
EVIDENCE_REQUIRED = ('effective_tools', 'effective_instructions', 'all_skill_packages',
                     'hidden_direct_handlers', 'nested_aliases', 'zero_transport_retries')


def _sync_json(fd, value):
    data = json.dumps(value, sort_keys=True, separators=(',', ':')).encode()
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, data)
    os.ftruncate(fd, len(data))
    os.fsync(fd)


class Attempts:
    """A fixed account-local ledger. Receipts and quota resets grant no attempts."""
    def __init__(self, path):
        private_directory(path.parent)
        try:
            self.fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            created = True
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except FileExistsError:
            self.fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
            created = False
        info = os.fstat(self.fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            os.close(self.fd)
            raise ProbeError('invalid_attempt_ledger')
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            os.close(self.fd)
            raise ProbeError('another_smoke_is_running') from error
        raw = os.read(self.fd, 16385)
        if len(raw) > 16384 or not raw and not created:
            self.close()
            raise ProbeError('invalid_attempt_ledger')
        self.entries = json_value(raw) if raw else []
        valid = {'reserved', 'passed', 'failed'}
        if (not isinstance(self.entries, list) or len(self.entries) > MAX_ATTEMPTS
                or any(not isinstance(entry, dict)
                       or entry.get('configuration') not in ('control', 'grepglint')
                       or entry.get('status') not in valid
                       or not isinstance(entry.get('local_receipt_sha256'), str)
                       for entry in self.entries)
                or len({entry['configuration'] for entry in self.entries}) != len(self.entries)):
            self.close()
            raise ProbeError('invalid_attempt_ledger')
        if created:
            _sync_json(self.fd, self.entries)

    def snapshot(self):
        return [{'configuration': entry['configuration'], 'status': entry['status'],
                 'local_receipt_sha256': entry['local_receipt_sha256']}
                for entry in self.entries]

    def next_configuration(self):
        by_name = {entry['configuration']: entry for entry in self.entries}
        if 'control' not in by_name:
            return 'control'
        if by_name['control']['status'] != 'passed':
            raise ProbeError('baseline_not_passed')
        if 'grepglint' not in by_name:
            return 'grepglint'
        raise ProbeError('smoke_attempts_exhausted')

    def reserve(self, configuration, receipt_hash):
        if configuration != self.next_configuration() or len(self.entries) >= MAX_ATTEMPTS:
            raise ProbeError('smoke_attempts_exhausted')
        self.entries.append({'configuration': configuration, 'local_receipt_sha256': receipt_hash,
                             'started_at': time.time(), 'status': 'reserved'})
        _sync_json(self.fd, self.entries)

    def finish(self, configuration, status):
        if status not in ('passed', 'failed'):
            raise ProbeError('invalid_attempt_status')
        matches = [entry for entry in self.entries if entry['configuration'] == configuration]
        if len(matches) != 1 or matches[0]['status'] != 'reserved':
            raise ProbeError('invalid_attempt_transition')
        matches[0]['status'] = status
        matches[0]['finished_at'] = time.time()
        _sync_json(self.fd, self.entries)

    def close(self):
        os.close(self.fd)


def ledger_path():
    root = Path(os.environ.get('XDG_STATE_HOME', Path.home() / '.local/state'))
    return root / 'grepglint' / 'codex-smoke-attempts-v1.json'


def save_receipt(path, receipt, create=False, expected_identity=None):
    private_directory(path.parent)
    data = (json.dumps(receipt, indent=2, sort_keys=True) + '\n').encode()
    if len(data) > FRAME_LIMIT:
        raise ProbeError('receipt_limit_exceeded')
    flags = os.O_WRONLY | os.O_NOFOLLOW | (os.O_CREAT | os.O_EXCL if create else 0)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, 'wb') as output:
        info = os.fstat(output.fileno())
        identity = [info.st_dev, info.st_ino]
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077
                or expected_identity is not None and identity != expected_identity):
            raise ProbeError('smoke_receipt_unavailable')
        output.write(data)
        output.truncate()
        output.flush()
        os.fsync(output.fileno())
    return identity


def recover_receipt(path, invocation_id):
    try:
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or path.stat().st_size > FRAME_LIMIT):
            return None, None
        value = json_value(path.read_bytes())
        if value.get('invocation_id') != invocation_id:
            return None, None
        return value, [info.st_dev, info.st_ino]
    except (OSError, ProbeError):
        return None, None


def children_in_current_cgroup(processes):
    group = Path('/proc/self/cgroup').read_text()
    return all(Path(f'/proc/{process.proc.pid}/cgroup').read_text() == group
               for process in processes)


def require_local(path, binary, grepglint):
    owned(path.parent, sealed=True)
    if path.is_symlink() or path.stat().st_size > FRAME_LIMIT:
        raise ProbeError('invalid_local_receipt')
    receipt = json_value(path.read_bytes())
    if (receipt.get('schema_version') != 2 or receipt.get('contract') != CONTRACT
            or receipt.get('status') != 'passed'
            or any(receipt.get(key, {}).get('status') != 'passed'
                   for key in ('local_isolation', 'tool_policy', 'audit'))
            or not all(receipt.get('cleanup', {}).get(key) is True for key in
                       ('temporary_files_removed', 'owned_client_groups_stopped', 'service_stopped'))
            or receipt.get('implementation_sha256') != implementation_hash()
            or receipt.get('client', {}).get('binary_sha256') != CLIENT_SHA256
            or file_hash(binary) != CLIENT_SHA256
            or receipt.get('grepglint_sha256') != file_hash(grepglint)):
        raise ProbeError('matching_successful_local_receipt_required')
    audit_hash = file_hash(path.parent / 'audit.jsonl')
    if receipt['audit'].get('sha256') != audit_hash:
        raise ProbeError('local_audit_identity_mismatch')
    sessions = receipt.get('sessions', [])
    expected_configuration = digest(encoded(configuration('<loopback>')))
    required_negative = {'orchestrator_skills', 'executor_skills', 'missing_skill',
                         'hidden_skill', 'shell', 'patch', 'delegation', 'resource',
                         'invalid_name', 'absolute_path', 'traversal', 'symlink', 'history',
                         'javascript_restrictions', 'nested_catalog'}
    if ([item.get('configuration') for item in sessions] != ['control', 'grepglint']
            or any(not all(item.get(key) for key in
                           ('configuration_sha256', 'prompt_sha256', 'catalog_sha256'))
                   for item in sessions)
            or any(item['configuration_sha256'] != expected_configuration for item in sessions)
            or len({item['prompt_sha256'] for item in sessions}) != 1
            or any(not required_negative <= {name for name, passed in
                       item.get('negative_checks', {}).items() if passed is True} for item in sessions)
            or any(item.get('observed', {}).get('errors') for item in sessions)
            or any(not isinstance(item.get('observed', {}).get('runtime_registry'), list)
                   for item in sessions)
            or receipt.get('tool_policy', {}).get('same_configuration_and_prompt') is not True
            or receipt.get('tool_policy', {}).get('only_grepglint_added') is not True):
        raise ProbeError('local_proof_incomplete')
    return {'receipt_sha256': file_hash(path), 'audit_sha256': audit_hash,
            'implementation_sha256': receipt['implementation_sha256'],
            'grepglint_sha256': receipt['grepglint_sha256'],
            'client_sha256': receipt['client']['binary_sha256'],
            'sessions': [{key: item[key] for key in
                          ('configuration', 'configuration_sha256', 'prompt_sha256', 'catalog_sha256')}
                         for item in sessions],
            'checks': {'normalized_configuration': True, 'prompt_and_catalog': True,
                       'runtime_registrations': True, 'direct_skill_handlers': True,
                       'nested_aliases': True, 'negative_capabilities': True}}


def _account_identity(account):
    value = account.get('account')
    if (not isinstance(value, dict) or value.get('type') != 'chatgpt'
            or account.get('requiresOpenaiAuth') is not True):
        raise ProbeError('chatgpt_authentication_required')
    identity = {'type': 'chatgpt', 'email': value.get('email'), 'plan_type': value.get('planType')}
    if not isinstance(identity['email'], str) or not identity['email']:
        raise ProbeError('chatgpt_account_identity_unavailable')
    return identity


def _model_check(models):
    candidates = [model for model in models if model.get('model') == MODEL]
    if len(candidates) != 1 or EFFORT not in {
            effort.get('reasoningEffort') for effort in candidates[0].get('supportedReasoningEfforts', [])}:
        raise ProbeError('exact_model_and_effort_required')


def account_check(account, models):
    identity = _account_identity(account)
    _model_check(models)
    return identity


def weekly_quota(observation, now):
    """Return every available included-usage weekly bucket in stable order."""
    if observation.get('ordinaryUsageAllowed') is not True:
        raise ProbeError('included_weekly_quota_unavailable')
    buckets = observation.get('rateLimitsByLimitId')
    if not isinstance(buckets, dict) or not buckets:
        fallback = observation.get('rateLimits')
        buckets = {fallback.get('limitId') or 'default': fallback} if isinstance(fallback, dict) else {}
    weekly = []
    for bucket_id, snapshot in buckets.items():
        if not isinstance(bucket_id, str) or not isinstance(snapshot, dict):
            raise ProbeError('weekly_quota_unavailable')
        for slot in ('primary', 'secondary'):
            window = snapshot.get(slot)
            if not isinstance(window, dict) or window.get('windowDurationMins') != 10080:
                continue
            percent, reset = window.get('usedPercent'), window.get('resetsAt')
            if (type(percent) not in (int, float) or not 0 <= percent < 100
                    or type(reset) is not int or reset <= now):
                raise ProbeError('weekly_quota_unavailable')
            weekly.append({'bucket_id': bucket_id, 'slot': slot, 'used_percent': percent,
                           'available': True, 'resets_at': reset, 'window_minutes': 10080,
                           'units': 'percentage_only'})
    if not weekly:
        raise ProbeError('weekly_quota_unavailable')
    account_id = observation.get('accountId')
    if account_id is not None and not isinstance(account_id, str):
        raise ProbeError('weekly_quota_unavailable')
    return {'observed_at': now,
            'account_identity_sha256': digest(account_id) if account_id is not None else None,
            'ordinary_usage_allowed': True,
            'buckets': sorted(weekly, key=lambda item: (item['bucket_id'], item['slot']))}


def provider_check(evidence):
    if (evidence.get('origin') != 'pinned_client_request_and_protocol_observation'
            or any(evidence.get(key) is not True for key in EVIDENCE_REQUIRED)):
        raise ProbeError('provider_effective_request_unverified')


def quota_confirmation(quota):
    result = {key: value for key, value in quota.items() if key != 'observed_at'}
    result['buckets'] = []
    for bucket in quota['buckets']:
        value = dict(bucket)
        # An unused bucket can report a full window from every observation.
        # Preserve the actual timestamp in the receipt, not in this comparison.
        if (bucket['used_percent'] == 0
                and abs(bucket['resets_at'] - quota['observed_at']
                        - bucket['window_minutes'] * 60) <= RESET_OBSERVATION_SECONDS):
            value['resets_at'] = 'zero_usage_full_window'
        result['buckets'].append(value)
    return result


def completed_session(result, before, after):
    if result.get('model') != MODEL or result.get('effort') != EFFORT:
        raise ProbeError('provider_model_or_effort_mismatch')
    if (type(result.get('calls')) is not int or not 0 <= result['calls'] <= SESSION_CALLS
            or type(result.get('elapsed_seconds')) not in (float, int)
            or not 0 <= result['elapsed_seconds'] <= SESSION_SECONDS
            or result.get('audit_status') != 'passed'):
        raise ProbeError('smoke_session_limit_or_audit_failure')
    usage = result.get('usage')
    if not isinstance(usage, dict) or any(type(usage.get(key)) is not int or usage[key] < 0
                                       for key in ('inputTokens', 'outputTokens', 'totalTokens')):
        raise ProbeError('provider_usage_missing')
    before_by_id = {(item['bucket_id'], item['slot']): item
                    for item in quota_confirmation(before)['buckets']}
    after_by_id = {(item['bucket_id'], item['slot']): item
                   for item in quota_confirmation(after)['buckets']}
    if before_by_id.keys() != after_by_id.keys() or any(
            after_by_id[key]['resets_at'] != value['resets_at']
            or after_by_id[key]['used_percent'] < value['used_percent']
            for key, value in before_by_id.items()) or any(
                item['resets_at'] <= after['observed_at'] for item in before['buckets']):
        raise ProbeError('weekly_quota_reset_during_smoke')
    return {'status': 'passed', 'usage': usage, 'quota_before': before, 'quota_after': after}


def live_configuration():
    values = configuration(CHATGPT_BASE_URL)
    values['model_provider'] = PROVIDER_ID
    values.pop('model_providers.preflight')
    values[f'model_providers.{PROVIDER_ID}'] = {
        'name': PROVIDER_ID, 'base_url': CHATGPT_BASE_URL, 'wire_api': 'responses',
        'requires_openai_auth': True, 'request_max_retries': 0, 'stream_max_retries': 0,
        'supports_websockets': False, 'stream_idle_timeout_ms': 120000,
    }
    return values


def confirmation_value(account, quota, proofs, config_hash, ledger):
    bound = {'account': account, 'quota': quota_confirmation(quota),
             'proofs': proofs, 'configuration_sha256': config_hash, 'ledger': ledger}
    return digest(encoded(bound))


def stock_provider_evidence(proofs=None, probe=None):
    checks = proofs.get('checks', {}) if isinstance(proofs, dict) else {}
    live = live_configuration()
    provider = live.get(f'model_providers.{PROVIDER_ID}', {})
    transport_only = configuration('<loopback>')
    for key in ('model_provider', 'model_providers.preflight'):
        transport_only.pop(key, None)
    live_shared = dict(live)
    for key in ('model_provider', f'model_providers.{PROVIDER_ID}'):
        live_shared.pop(key, None)
    flags = {
        'effective_tools': checks.get('runtime_registrations') is True,
        'effective_instructions': (checks.get('normalized_configuration') is True
                                   and checks.get('prompt_and_catalog') is True),
        'all_skill_packages': checks.get('direct_skill_handlers') is True,
        'hidden_direct_handlers': checks.get('direct_skill_handlers') is True,
        'nested_aliases': checks.get('nested_aliases') is True,
        'zero_transport_retries': (transport_only == live_shared
            and provider.get('request_max_retries') == 0
            and provider.get('stream_max_retries') == 0
            and provider.get('supports_websockets') is False
            and isinstance(probe, dict) and probe.get('configuration_sha256') == digest(encoded(live))
            and probe.get('isolation') is True),
    }
    return {'origin': 'pinned_client_request_and_protocol_observation', **flags,
            'source': 'matching offline receipt and live protocol audit'}


class ChatGPTProvider:
    """One pinned app-server process with read-only access to installed ChatGPT auth."""
    def __init__(self, binary, grepglint, auth_file, audit_path=None, *, trial=None):
        prerequisites(binary, auth_file.parent)
        self.binary, self.grepglint = binary, grepglint
        self.temp = tempfile.TemporaryDirectory(prefix='grepglint-smoke-')
        self.root = Path(self.temp.name)
        self.config_dir = self.root / 'configuration'
        self.config_dir.mkdir(mode=0o700)
        self.trial = trial
        self.prompt = trial['prompt'] if trial else PROMPT
        (self.config_dir / 'base.md').write_text(trial['base'] if trial else BASE)
        fixture = self.root / 'fixture'
        fixture.mkdir(mode=0o700)
        self.source = trial['source'] if trial else prepare_fixture(fixture)
        self.forbidden = None
        if trial:
            outside = self.root / 'outside'
            outside.mkdir(mode=0o700)
            self.forbidden = {}
            for name in ('source', 'oracle', 'credentials', 'history', 'controller'):
                path = outside / name
                path.write_text('PRIVATE_' + name)
                self.forbidden[name] = str(path)
        self.source_before = trial['source_sha256'] if trial else source_hash(self.source)
        self.config = live_configuration()
        self.config_hash = digest(encoded(self.config))
        args = []
        for key, value in self.config.items():
            args += ['-c', f'{key}={toml_value(value)}']
        args += ['app-server', '--listen', 'stdio://']
        self.budget = trial['budget'] if trial else Budget(SESSION_SECONDS + 20)
        self.client = None
        self.audit = trial['audit'] if trial else None
        self.handlers = None
        self.request_id = 10
        if audit_path:
            private_directory(audit_path.parent)
            fd = os.open(audit_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            os.close(fd)
            self.audit = Audit(audit_path, self.budget, call_limit=SESSION_CALLS)
        try:
            command = authenticated_client_command(binary, self.config_dir, auth_file, args)
            self.client = Child(command, self.source, {}, self.budget)
            self._send('initialize', {'clientInfo': {'name': 'grepglint_smoke', 'version': '1'},
                                      'capabilities': {'experimentalApi': True}})
            self.client.send({'method': 'initialized', 'params': {}})
        except BaseException:
            self.close()
            raise

    def _send(self, method, params, session=None):
        self.request_id += 1
        request_id = self.request_id
        message = {'id': request_id, 'method': method, 'params': params}
        if self.audit and session:
            self.audit.record('rpc.sent', message, session)
        self.client.send(message)
        return self._receive(request_id=request_id, session=session)

    def _receive(self, request_id=None, method=None, session=None):
        while True:
            try:
                event = json_value(self.client.line())
            except ProbeError as error:
                # A handler terminates the client to unblock protocol reads.
                if session and str(error) == 'client_exited' and self.handlers and self.handlers.error:
                    raise ProbeError(self.handlers.error) from error
                raise
            if not isinstance(event, dict):
                raise ProbeError('invalid_rpc_event')
            if self.audit and session:
                self.audit.receive(event, session)
            if session and self.handlers and self.handlers.error:
                raise ProbeError(self.handlers.error)
            if event.get('method') == 'item/tool/call':
                if not self.handlers:
                    raise ProbeError('unexpected_tool_before_turn')
                self.handlers.submit(event)
            elif 'id' in event and 'method' in event:
                if event['method'] != 'item/tool/requestUserInput' or not self.handlers:
                    raise ProbeError('unexpected_server_request')
                answer = {q['id']: {'answers': [NO_ASSISTANCE]}
                          for q in event['params']['questions']}
                self.handlers.send({'id': event['id'], 'result': {'answers': answer}})
            elif request_id is not None and event.get('id') == request_id:
                if 'error' in event:
                    raise ProbeError('rpc_request_rejected')
                return event.get('result', {})
            elif method and event.get('method') == method:
                return event.get('params', {})

    def account(self):
        return self._send('account/read', {'refreshToken': False})

    def models(self):
        models, cursor = [], None
        for _ in range(20):
            page = self._send('model/list', {'cursor': cursor, 'limit': 100, 'includeHidden': True})
            models.extend(page.get('data', []))
            cursor = page.get('nextCursor')
            if cursor is None:
                return models
        raise ProbeError('model_catalog_page_limit_exceeded')

    def quota(self):
        return self._send('account/rateLimits/read', {})

    def evidence(self, proofs, probe):
        return stock_provider_evidence(proofs, probe)

    def thread_parameters(self):
        return {'cwd': '/work', 'environments': []}

    def turn_parameters(self):
        return {'environments': []}

    def allowed_tools(self, treatment, catalog):
        return {'functions.' + item['name'] for item in definitions(treatment, catalog)} | AUXILIARY

    def finish_turn(self, started, treatment):
        pass

    def _start_thread(self, treatment, audit):
        catalog = self.trial['catalog'] if self.trial else json_value(subprocess.check_output(
            [str(self.grepglint), 'tools', '--json'], cwd=self.source, timeout=3))
        options = {'forbidden': self.forbidden, 'file_limit': self.trial['cache_bytes']} if self.trial else {}
        self.handlers = Handlers(self.source, self.grepglint, self.budget, audit, treatment, **options)
        self.handlers.start(self.client)
        if not children_in_current_cgroup((self.client, self.handlers.child)):
            raise ProbeError('child_outside_resource_group')
        started = self._send('thread/start', {'model': MODEL, 'modelProvider': self.config['model_provider'],
            **self.thread_parameters(), 'ephemeral': True, 'experimentalRawEvents': True,
            'dynamicTools': self.trial['tools'] if self.trial else definitions(treatment, catalog)}, treatment if audit else None)
        if started.get('model') != MODEL or started.get('reasoningEffort') != EFFORT:
            raise ProbeError('reported_model_or_effort_mismatch')
        self.reported_model = started['model']
        self.reported_effort = started['reasoningEffort']
        return catalog, started

    def preflight_probe(self, treatment):
        _catalog, started = self._start_thread(treatment, None)
        if not self.trial and source_hash(self.source) != self.source_before:
            raise ProbeError('source_changed')
        self.handlers.close()
        self.handlers = None
        return {'configuration_sha256': self.config_hash, 'isolation': True,
                'reported_model': started['model'], 'reported_effort': started['reasoningEffort']}

    def session(self, treatment, seconds, calls, reserve):
        if not self.audit:
            raise ProbeError('live_audit_required')
        catalog, started = self._start_thread(treatment, self.audit)
        reserve()
        turn_started = time.monotonic()
        aggregate_deadline = self.budget.deadline
        self.budget.deadline = min(aggregate_deadline, turn_started + seconds)
        self.request_id += 1
        request_id = self.request_id
        # The durable reservation intentionally survives any uncertain write.
        message = {'id': request_id, 'method': 'turn/start', 'params': {
            'threadId': started['thread']['id'], 'model': MODEL, 'effort': EFFORT,
            **self.turn_parameters(), 'input': [{'type': 'text', 'text': self.prompt}]}}
        self.audit.record('rpc.sent', message, treatment)
        self.client.send(message)
        self._receive(request_id=request_id, session=treatment)
        completed = self._receive(method='turn/completed', session=treatment)
        self.budget.deadline = aggregate_deadline
        if completed.get('turn', {}).get('status') != 'completed':
            raise ProbeError('turn_not_completed')
        self.finish_turn(started, treatment)
        if not self.trial and source_hash(self.source) != self.source_before:
            raise ProbeError('source_changed')
        allowed = self.allowed_tools(treatment, catalog)
        observed = {call['name'] for (name, _), call in self.audit.calls.items()
                    if name == treatment}
        if observed - allowed:
            raise ProbeError('unexpected_live_tool')
        expected = [call_id for (name, call_id), call in self.audit.calls.items()
                    if name == treatment and call['kind'] == 'top_level']
        checked = self.audit.verify(treatment, expected)
        result = {'model': started['model'], 'effort': started['reasoningEffort'],
                'calls': checked['calls'], 'elapsed_seconds': time.monotonic() - turn_started,
                'audit_status': checked['status'], 'usage': self.audit.usage_summary() if self.trial else self.audit.usage(treatment),
                'audit_sha256': self.audit.sha.hexdigest(), 'source_sha256': self.source_before,
                'configuration_sha256': self.config_hash, 'normalized_configuration': self.config}
        result['isolation_checks'] = self.handlers.checks if self.trial else None
        result['cache_identity'] = self.handlers.cache_identity if self.trial else None
        self.handlers.close()
        self.handlers = None
        return result

    def close(self):
        failure = None
        for resource in (self.handlers, self.client, None if self.trial else self.audit):
            if resource:
                try:
                    resource.close()
                except (OSError, ProbeError) as error:
                    failure = failure or error
        self.handlers = self.client = self.audit = None
        if hasattr(self, 'temp'):
            self.temp.cleanup()
        if failure:
            raise failure


def preturn(provider, proofs, attempts, now=None, checks=None):
    live_clock = now is None
    now = time.time() if live_clock else now
    checks = checks if checks is not None else {
        'local_proof': 'passed', 'chatgpt_auth': 'not_run', 'model_effort': 'not_run',
        'included_weekly_quota': 'not_run', 'client_request_evidence': 'not_run',
        'attempt_ledger': 'not_run'}
    phase = 'chatgpt_auth'
    try:
        account = _account_identity(provider.account())
        checks[phase] = 'passed'
        phase = 'model_effort'
        _model_check(provider.models())
        checks[phase] = 'passed'
        phase = 'included_weekly_quota'
        observation = provider.quota()
        quota = weekly_quota(observation, time.time() if live_clock else now)
        if live_clock and time.time() - quota['observed_at'] > FRESH_SECONDS:
            raise ProbeError('quota_observation_stale')
        checks[phase] = 'passed'
        phase = 'attempt_ledger'
        configuration_name = attempts.next_configuration()
        checks[phase] = 'passed'
        phase = 'client_request_evidence'
        probe = provider.preflight_probe(configuration_name)
        provider_check(provider.evidence(proofs, probe))
        checks[phase] = 'passed'
    except ProbeError:
        checks[phase] = 'failed'
        raise
    config_hash = provider.config_hash
    token = confirmation_value(account, quota, proofs, config_hash, attempts.snapshot())
    return {'configuration': configuration_name, 'quota': quota, 'confirmation': token,
            'configuration_sha256': config_hash,
            'remaining_attempts': MAX_ATTEMPTS - len(attempts.entries),
            'checks': checks}


def run_confirmed(provider, attempts, proofs, confirmation, checks=None,
                  on_prepared=None, on_reserved=None, on_result=None):
    prepared = preturn(provider, proofs, attempts, checks=checks)
    if not isinstance(confirmation, str) or confirmation != prepared['confirmation']:
        raise ProbeError('stale_or_missing_confirmation')
    name = prepared['configuration']
    if on_prepared:
        on_prepared(prepared)
    reserved = False

    def reserve():
        nonlocal reserved
        attempts.reserve(name, proofs['receipt_sha256'])
        reserved = True
        if on_reserved:
            on_reserved(name)
    try:
        result = provider.session(name, SESSION_SECONDS, SESSION_CALLS, reserve)
        if on_result:
            on_result(result)
        after = weekly_quota(provider.quota(), time.time())
        session = completed_session(result, prepared['quota'], after)
        local = next((item for item in proofs.get('sessions', [])
                      if item.get('configuration') == name), {})
        return {'configuration': name, **session, 'result': result,
                'local_proof': local, 'local_receipt_sha256': proofs['receipt_sha256']}
    except BaseException:
        if reserved:
            attempts.finish(name, 'failed')
        raise


def smoke_pair(provider, attempts, receipt_hash):
    """Compatibility helper for fake-provider tests; confirmation remains per session."""
    proofs = {'receipt_sha256': receipt_hash}
    sessions = []
    for _ in range(2):
        prepared = preturn(provider, proofs, attempts)
        session = run_confirmed(provider, attempts, proofs, prepared['confirmation'])
        attempts.finish(session['configuration'], 'passed')
        sessions.append(session)
    return sessions


def _public_dry_run(prepared, proofs, attempts):
    return {'status': 'ready', 'inference_performed': False,
            'next_configuration': prepared['configuration'],
            'remaining_attempts': prepared['remaining_attempts'],
            'confirmation': prepared['confirmation'], 'checks': prepared['checks'],
            'proof_hashes': {key: value for key, value in proofs.items()
                             if key.endswith('_sha256') and isinstance(value, str)},
            'weekly_bucket_count': len(prepared['quota']['buckets']),
            'existing_attempts': [{'configuration': item['configuration'], 'status': item['status']}
                                  for item in attempts.snapshot()]}


def _worker(args):
    result = {'schema_version': 2, 'contract': CONTRACT, 'status': 'incomplete',
              'invocation_id': args.invocation,
              'provider_verification': {'status': 'pending', 'provider': args.provider},
              'inference_performed': False,
              'limits': {'sessions_total': MAX_ATTEMPTS, 'seconds_per_session': SESSION_SECONDS,
                         'calls_per_session': SESSION_CALLS, 'automatic_retry': False}}
    result['checks'] = {'local_proof': 'not_run', 'chatgpt_auth': 'not_run',
                        'model_effort': 'not_run', 'included_weekly_quota': 'not_run',
                        'client_request_evidence': 'not_run', 'attempt_ledger': 'not_run'}
    provider = attempts = None
    receipt_created = False
    receipt_identity = None
    completed_configuration = None

    def persist():
        clean = {key: value for key, value in result.items() if key != '_receipt_identity'}
        save_receipt(args.receipt, clean, expected_identity=receipt_identity)

    try:
        if not isinstance(args.invocation, str) or len(args.invocation) != 32:
            raise ProbeError('worker_invocation_required')
        if args.receipt:
            receipt_identity = save_receipt(args.receipt, result, create=True)
            receipt_created = True
        cgroup_limits()
        binary, grepglint = args.codex.resolve(), args.grepglint.resolve()
        try:
            proofs = require_local(args.local_receipt.resolve(), binary, grepglint)
            result['checks']['local_proof'] = 'passed'
        except (ProbeError, OSError):
            result['checks']['local_proof'] = 'failed'
            raise
        attempts = Attempts(ledger_path())
        audit_path = None if args.dry_run else args.receipt.with_suffix(args.receipt.suffix + '.audit.jsonl')
        provider = ChatGPTProvider(binary, grepglint, args.auth.resolve(), audit_path)
        prepared = preturn(provider, proofs, attempts, checks=result['checks'])
        if args.dry_run:
            result.update(_public_dry_run(prepared, proofs, attempts))
        else:
            def prepared_record(value):
                result['attempt'] = {'configuration': value['configuration'],
                    'status': 'prechecked', 'quota_before': value['quota'],
                    'configuration_sha256': value['configuration_sha256'],
                    'local_receipt_sha256': proofs['receipt_sha256']}
                persist()

            def reserved_record(configuration_name):
                result['inference_performed'] = True
                result['attempt'].update(status='reserved', submission_status='uncertain')
                persist()

            def result_record(value):
                result['attempt'].update(status='response_completed', submission_status='completed')
                result['partial_session'] = value
                persist()

            session = run_confirmed(provider, attempts, proofs, args.confirm, result['checks'],
                                    prepared_record, reserved_record, result_record)
            completed_configuration = session['configuration']
            result.pop('partial_session', None)
            result['attempt'].update(status='awaiting_cleanup', submission_status='completed')
            result.update(status='pending_cleanup', sessions=[session],
                          provider_verification={'status': 'passed', 'provider': 'chatgpt',
                              'boundary': 'pinned client request and observed protocol behavior'})
    except ProbeCancelled:
        result['errors'] = ['cancelled']
    except (ProbeError, OSError, ValueError, subprocess.SubprocessError) as error:
        result['errors'] = [str(error) if isinstance(error, ProbeError) else 'smoke_input_unavailable']
    finally:
        if result.get('attempt', {}).get('status') in ('reserved', 'response_completed'):
            result['attempt']['status'] = 'failed'
        if provider:
            try:
                provider.close()
                result.setdefault('cleanup', {})['temporary_files_removed'] = True
                result['cleanup']['owned_client_groups_stopped'] = True
            except (ProbeError, OSError):
                result['status'] = 'incomplete'
                if result.get('attempt'):
                    result['attempt']['status'] = 'failed'
                result.setdefault('errors', []).append('smoke_cleanup_failed')
        if completed_configuration and result.get('status') != 'pending_cleanup' and attempts:
            try:
                attempts.finish(completed_configuration, 'failed')
            except ProbeError:
                result.setdefault('errors', []).append('attempt_ledger_finalize_failed')
    result['next_action'] = ('Use the printed confirmation in one live command.' if args.dry_run
        and result.get('status') == 'ready' else 'Stop and inspect this receipt before any further attempt.'
        if result.get('status') != 'passed' else 'Return to issue #39 for the next confirmed session.')
    if receipt_created:
        try:
            persist()
        except (OSError, ProbeError):
            result['status'] = 'incomplete'
            if result.get('attempt'):
                result['attempt']['status'] = 'failed'
            result['errors'] = ['smoke_receipt_unavailable']
            if completed_configuration and attempts:
                try:
                    attempts.finish(completed_configuration, 'failed')
                except ProbeError:
                    result['errors'].append('attempt_ledger_finalize_failed')
    if attempts:
        attempts.close()
    if receipt_identity:
        result['_receipt_identity'] = receipt_identity
    print(json.dumps(result), flush=True)
    return 0 if result.get('status') in ('ready', 'pending_cleanup') else 3


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--provider', choices=['offline', 'chatgpt'], default='offline')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--confirm')
    parser.add_argument('--local-receipt', type=Path)
    parser.add_argument('--codex', type=Path)
    parser.add_argument('--grepglint', type=Path)
    parser.add_argument('--auth', type=Path, default=Path.home() / '.codex/auth.json')
    parser.add_argument('--receipt', type=Path)
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--invocation', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.provider != 'chatgpt':
        result = {'status': 'incomplete', 'inference_performed': False,
                  'errors': ['explicit_chatgpt_selection_required'],
                  'next_action': 'Select ChatGPT explicitly; no provider was contacted.'}
        print(json.dumps(result))
        return 3
    if not all((args.local_receipt, args.codex, args.grepglint, args.receipt)):
        result = {'status': 'incomplete', 'inference_performed': False,
                  'errors': ['local_receipt_and_binary_paths_required'],
                  'next_action': 'Supply the local proof, pinned binaries and a new private receipt.'}
        print(json.dumps(result))
        return 3
    if args.worker:
        with cancellation_signals():
            return _worker(args)
    unit = 'grepglint-smoke-' + secrets.token_hex(12)
    child = None
    service_requested = False
    receipt_identity = None
    invocation_id = secrets.token_hex(16)
    try:
        with cancellation_signals():
            check_user_manager()
            raw = list(sys.argv[1:] if argv is None else argv)
            raw += ['--worker', '--invocation', invocation_id]
            command = [sys.executable, str(Path(__file__).resolve()), *raw]
            service_requested = True
            child = Child(service_command(unit, command, seconds=145), Path.cwd(),
                          os.environ.copy(), Budget(150))
            result = json_value(child.line())
            receipt_identity = result.pop('_receipt_identity', None)
            if result.get('status') not in ('ready', 'pending_cleanup', 'incomplete'):
                raise ProbeError('invalid_smoke_worker_status')
    except ProbeCancelled:
        result = {'status': 'incomplete', 'inference_performed': False,
                  'errors': ['cancelled'],
                  'next_action': 'Inspect the retained receipt and stop before any further attempt.'}
    except (ProbeError, OSError, subprocess.SubprocessError) as error:
        result = {'status': 'incomplete', 'inference_performed': False,
                  'errors': [str(error) if isinstance(error, ProbeError) else 'smoke_launch_unavailable'],
                  'next_action': 'Inspect the retained receipt and stop before any further attempt.'}
    finally:
        if child:
            child.close()
        stopped = not service_requested
        if service_requested:
            try:
                subprocess.run(['systemctl', '--user', 'stop', unit], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=2)
                active = subprocess.run(['systemctl', '--user', 'is-active', '--quiet', unit],
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2)
                stopped = active.returncode in (3, 4)
            except (OSError, subprocess.TimeoutExpired):
                stopped = False
        if not stopped:
            result['status'] = 'incomplete'
            result.setdefault('errors', []).append('smoke_service_cleanup_failed')
        result.setdefault('cleanup', {})['service_stopped'] = stopped
        if receipt_identity is None:
            retained, retained_identity = recover_receipt(args.receipt, invocation_id)
            if retained is not None:
                result, receipt_identity = retained, retained_identity
                result.setdefault('cleanup', {})['service_stopped'] = stopped
                if not stopped:
                    result['status'] = 'incomplete'
                    result.setdefault('errors', []).append('smoke_service_cleanup_failed')
        if receipt_identity:
            try:
                if result.get('status') == 'pending_cleanup' and stopped:
                    result['status'] = 'passed'
                    result['attempt']['status'] = 'passed'
                    result['next_action'] = 'Return to issue #39 for the next confirmed session.'
                    save_receipt(args.receipt, result, expected_identity=receipt_identity)
                    ledger = Attempts(ledger_path())
                    try:
                        ledger.finish(result['attempt']['configuration'], 'passed')
                    finally:
                        ledger.close()
                else:
                    save_receipt(args.receipt, result, expected_identity=receipt_identity)
            except (OSError, ProbeError):
                result['status'] = 'incomplete'
                result['errors'] = ['smoke_receipt_unavailable']
                try:
                    save_receipt(args.receipt, result, expected_identity=receipt_identity)
                except (OSError, ProbeError):
                    pass
    print(json.dumps(result))
    return 0 if result.get('status') in ('ready', 'passed') else 3


if __name__ == '__main__':
    raise SystemExit(main())
