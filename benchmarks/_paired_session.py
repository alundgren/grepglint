"""Native Codex exploration with the same client configuration in both trials."""
import json
import os
from pathlib import Path
import re
import tempfile
import subprocess
import time

from _codex_audit import encoded
from _codex_capture import Child, ProbeError, ResponsesStub
from _codex_isolation import prerequisites
from _codex_session import configuration as controlled_configuration, AUXILIARY
from _codex_smoke import ChatGPTProvider, CHATGPT_BASE_URL, PROVIDER_ID
from codex_preflight import MODEL, EFFORT, digest, instruction_blocks, tool_catalog, toml_value, MARKERS
from _paired_contract import BASE, LIMITS, NATIVE_FRAME_BYTES, tools

NATIVE_TOOLS = {'exec_command', 'write_stdin', 'apply_patch', 'view_image', 'update_plan'}
NATIVE_MODEL_INSTRUCTIONS_SHA256 = 'cbefa6b0bede0e332d957fca70ccacf9f12f4c0ecdf81b819e5cbe1a3b16e265'


def configuration(url='<loopback>', source='<source>', runtime='<runtime>', temporary='<temporary>', *, live=False):
    values = controlled_configuration(url)
    # Preserve the client's own model instructions and default local environment.
    for key in ('model_instructions_file', 'sandbox_mode'):
        values.pop(key)
    values.update({'developer_instructions': BASE, 'features.shell_tool': True,
        'features.unified_exec': True, 'tools.update_plan.enabled': True,
        'default_permissions': 'discovery',
        'permissions': {'discovery': {'filesystem': {
            ':minimal': 'read', '/usr': 'read', '/bin': 'read', '/lib': 'read', '/lib64': 'read',
            '/etc': 'read', runtime: 'read', source: 'read',
            temporary + '/user': 'write', temporary + '/tmp': 'write'},
            'network': {'enabled': False}}}})
    if live:
        values['model_provider'] = PROVIDER_ID
        values.pop('model_providers.preflight')
        values[f'model_providers.{PROVIDER_ID}'] = {
            'name': PROVIDER_ID, 'base_url': CHATGPT_BASE_URL, 'wire_api': 'responses',
            'requires_openai_auth': True, 'request_max_retries': 0, 'stream_max_retries': 0,
            'supports_websockets': False, 'stream_idle_timeout_ms': 120000}
    return values


def live_configuration():
    return configuration(live=True)


def normalize_instructions(request):
    # Only disposable directory identities differ between launches. Keep all
    # other instruction text, including native model guidance, in the identity.
    text = json.dumps(request)
    text = re.sub(r'/run/user/\d+/grepglint-paired-[0-9a-f]+/grepglint-native-[A-Za-z0-9_-]+', '<temporary>', text)
    text = re.sub(r'/dev/shm/grepglint-native-[A-Za-z0-9_-]+', '<temporary>', text)
    text = re.sub(r'<temporary>/codex/tmp/arg0/codex-arg0[A-Za-z0-9]+', '<temporary>/codex/tmp/arg0/<launcher>', text)
    return json.loads(text)


def inspect_policy(request, treatment, base=BASE, prompt=None):
    catalog = tool_catalog(request)
    expected = NATIVE_TOOLS | ({'grepglint_search'} if treatment == 'grepglint' else set())
    allowed = expected | {'functions.' + name for name in expected} | AUXILIARY
    if catalog['errors'] or set(catalog['names']) - allowed or not expected <= {n.removeprefix('functions.') for n in catalog['names']}:
        raise ProbeError('unexpected_or_missing_native_tool')
    for entry in catalog['runtime_registry']:
        alias = entry['code_mode_name']
        if (entry['name'] not in {'functions.' + name for name in expected} | AUXILIARY
                or type(entry['direct']) is not bool or type(entry['deferred']) is not bool
                or alias is not None and entry['name'] != 'functions.' + alias):
            raise ProbeError('unexpected_native_registration')
    if request.get('model') != MODEL or request.get('reasoning', {}).get('effort') != EFFORT:
        raise ProbeError('model_or_effort_mismatch')
    blocks = instruction_blocks(normalize_instructions(request))
    if not {NATIVE_MODEL_INSTRUCTIONS_SHA256, digest(base), digest(prompt)} <= {b['sha256'] for b in blocks}:
        raise ProbeError('expected_instructions_missing')
    if any(marker in json.dumps(request) for marker in MARKERS.values()):
        raise ProbeError('instruction_contamination')
    return {'observed': catalog, 'instruction_blocks': blocks, 'model': MODEL, 'effort': EFFORT,
            'catalog_sha256': digest(encoded(catalog)), 'prompt_sha256': digest(encoded(blocks))}


class NativeProvider(ChatGPTProvider):
    """Use Codex's OS sandbox for native tools; keep auth outside its readable roots."""
    def __init__(self, binary, grepglint, auth_file=None, *, trial, url=None):
        prerequisites(binary, trial['source'])
        temporary_parent = os.environ.get('RUNTIME_DIRECTORY', '/dev/shm')
        if subprocess.check_output(['stat', '-f', '-c', '%T', temporary_parent], timeout=2).strip() != b'tmpfs':
            raise ProbeError('native_temporary_storage_requires_tmpfs')
        self.binary, self.grepglint, self.trial = binary, grepglint, trial
        # tmpfs writes are charged to the same cgroup as commands and the daemon.
        self.temp = tempfile.TemporaryDirectory(prefix='grepglint-native-', dir=temporary_parent)
        self.root = Path(self.temp.name)
        self.source, self.prompt = trial['source'], trial['prompt']
        self.source_before = trial['source_sha256']
        self.budget, self.audit = trial['budget'], trial['audit']
        self.client = self.handlers = None
        self.request_id = 10
        try:
            for name in ('user', 'tmp', 'codex', 'outside'):
                (self.root / name).mkdir(mode=0o700)
            if auth_file is not None:
                import stat
                with os.fdopen(os.open(auth_file, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as original:
                    info = os.fstat(original.fileno())
                    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                        raise ProbeError('chatgpt_auth_file_must_be_private_and_owned')
                    authentication = original.read(1024 * 1024 + 1)
                if len(authentication) > 1024 * 1024:
                    raise ProbeError('chatgpt_auth_file_limit_exceeded')
                path = self.root / 'codex' / 'auth.json'
                with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as copied:
                    copied.write(authentication)
            self.forbidden = {}
            for name in ('source', 'oracle', 'credentials', 'history', 'controller'):
                path = self.root / 'outside' / name
                path.write_text('PRIVATE_' + name)
                self.forbidden[name] = str(path)
            self.config = configuration(live=auth_file is not None)
            self.config_hash = digest(encoded(self.config))
            actual = configuration(url or CHATGPT_BASE_URL, str(self.source), str(binary.parent),
                                   str(self.root), live=auth_file is not None)
            arguments = []
            for key, value in actual.items():
                arguments += ['-c', f'{key}={toml_value(value)}']
            environment = {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8', 'SHELL': '/bin/bash',
                'HOME': str(self.root / 'user'), 'CODEX_HOME': str(self.root / 'codex'),
                'TMPDIR': str(self.root / 'tmp'), 'XDG_CONFIG_HOME': str(self.root / 'user'),
                'XDG_CACHE_HOME': str(self.root / 'tmp'), 'XDG_DATA_HOME': str(self.root / 'tmp'),
                'XDG_STATE_HOME': str(self.root / 'tmp'), 'RUST_LOG': 'off', 'NO_COLOR': '1',
                'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null',
                'GIT_TERMINAL_PROMPT': '0', 'PYTHONDONTWRITEBYTECODE': '1'}
            self.client = Child([str(binary), *arguments, 'app-server', '--listen', 'stdio://'],
                self.source, environment, self.budget, file_limit=LIMITS['aggregate_memory_bytes'],
                frame_limit=NATIVE_FRAME_BYTES)
            self._send('initialize', {'clientInfo': {'name': 'grepglint_paired', 'version': '2'},
                                     'capabilities': {'experimentalApi': True}})
            self.client.send({'method': 'initialized', 'params': {}})
        except BaseException:
            self.close()
            raise

    def thread_parameters(self):
        return {'cwd': str(self.source)}

    def turn_parameters(self):
        return {}

    def allowed_tools(self, treatment, catalog):
        return {'functions.' + name for name in NATIVE_TOOLS} | AUXILIARY | {
            'functions.' + item['name'] for item in tools(treatment, catalog)}

    def finish_turn(self, started, treatment):
        deadline = self.budget.deadline
        self.budget.deadline = min(deadline, time.monotonic() + 3)
        try:
            self._send('thread/backgroundTerminals/clean', {'threadId': started['thread']['id']}, treatment)
            while any('completion' not in state for state in self.audit.native_items.values()):
                self._receive(method='item/completed', session=treatment)
        finally:
            self.budget.deadline = deadline

    def evidence(self, proofs, probe):
        checks = proofs.get('checks', {})
        from _codex_smoke import EVIDENCE_REQUIRED
        passed = (all(checks.get(k) is True for k in ('native_tools', 'native_isolation', 'prompt_and_catalog'))
                  and probe.get('configuration_sha256') == self.config_hash
                  and probe.get('isolation') is True)
        return {'origin': 'pinned_client_request_and_protocol_observation',
                **{key: passed for key in EVIDENCE_REQUIRED}}


def local_session(binary, grepglint, source, catalog, treatment, audit, budget, script, prompt, source_sha256):
    initial = []
    def observe(request):
        audit.record('responses.request', request, treatment)
        current = inspect_policy(request, treatment, prompt=prompt)
        if initial:
            def stable(value):
                return [{k: v for k, v in b.items() if k != 'location'} for b in value['instruction_blocks']]
            if current['observed'] != initial[0]['observed'] or stable(current) != stable(initial[0]):
                raise ProbeError('effective_request_changed')
        else:
            initial.append(current)
    stub = ResponsesStub(budget, responder=script, observe=observe, frame_limit=NATIVE_FRAME_BYTES)
    provider = None
    try:
        stub.start()
        provider = NativeProvider(binary, grepglint, trial={'source': source, 'source_sha256': source_sha256,
            'prompt': prompt, 'catalog': catalog, 'tools': tools(treatment, catalog),
            'budget': budget, 'audit': audit, 'cache_bytes': LIMITS['cache_bytes']}, url=stub.url)
        script.source, script.temporary = source, provider.root
        result = provider.session(treatment, LIMITS['trial_seconds'], LIMITS['tool_calls'], lambda: None)
        if stub.error:
            raise ProbeError(stub.error)
        result.update(initial[0], reported_model=result['model'], reported_effort=result['effort'],
                      negative_checks=script.check(audit, treatment) if hasattr(script, 'check') else {})
        return result
    except ProbeError as error:
        if stub.error:
            raise ProbeError(stub.error) from error
        raise
    finally:
        try:
            if provider:
                provider.close()
        finally:
            stub.close()
