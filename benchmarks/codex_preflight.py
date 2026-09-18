#!/usr/bin/env python3
"""Check Codex request construction without inference. See docs/benchmark-preflight.md."""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import resource
import shlex
import signal
import stat
from subprocess import SubprocessError
import sys
import tempfile
import time

from _codex_capture import (Budget, Child, DEADLINE_SECONDS, FRAME_LIMIT, ProbeError,
                            ResponsesStub, TOTAL_LIMIT)

CLIENT_VERSION = 'codex-cli 0.154.0'
MODEL = 'gpt-5.6-luna'
EFFORT = 'high'
BASE = 'This is a local request-construction check. Reply preflight-complete without calling tools.'
MARKERS = {name: f'GREPGLINT_PREFLIGHT_{name.upper()}_SENTINEL'
           for name in ['global_instruction', 'project_instruction', 'skill', 'hook',
                        'configuration', 'outside_source']}
DISABLED_FEATURES = [
    'apps', 'plugins', 'remote_plugin', 'recommended_plugins', 'connectors',
    'browser_use', 'browser_use_external', 'computer_use', 'image_generation',
    'imagegenext', 'web_search', 'standalone_web_search', 'multi_agent', 'multi_agent_v2',
    'collab', 'collaboration_modes', 'goals', 'memories', 'memory_tool', 'remote_models',
    'remote_control', 'external_migration', 'external_agent_memory_import',
    'shell_snapshot', 'shell_snapshot_v2', 'shell_tool', 'unified_exec', 'js_repl',
    'code_mode_prewarm', 'responses_websockets', 'responses_websockets_v2',
    'enable_request_compression', 'unbounded_connection_retries', 'respect_system_proxy',
    'skill_mcp_dependency_install', 'skill_env_var_dependency_prompt', 'plugin_hooks',
]


class ProbeCancelled(BaseException):
    pass


@contextmanager
def cancellation_signals():
    watched = [signal.SIGINT, signal.SIGTERM]
    previous = {number: signal.getsignal(number) for number in watched}

    def cancel(_number, _frame):
        # A second ordinary cancellation must not interrupt process cleanup.
        for number in watched:
            signal.signal(number, signal.SIG_IGN)
        raise ProbeCancelled()

    try:
        for number in watched:
            signal.signal(number, cancel)
        yield
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def digest(data):
    return hashlib.sha256(data if isinstance(data, bytes) else data.encode()).hexdigest()


def private_directory(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ProbeError('receipt_directory_must_be_private_and_owned')


@contextmanager
def exclusive_probe():
    root = Path('/tmp') / f'grepglint-codex-preflight-{os.getuid()}'
    private_directory(root)
    fd = os.open(root / 'lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ProbeError('another_preflight_is_running') from error
        yield
    finally:
        os.close(fd)


def write_receipt(path, receipt):
    private_directory(path.parent)
    data = (json.dumps(receipt, indent=2, sort_keys=True) + '\n').encode()
    if len(data) > FRAME_LIMIT:
        raise ProbeError('receipt_limit_exceeded')
    # Exclusive creation preserves existing or unexpectedly replaced receipts.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as output:
        output.write(data)


def dynamic_tools(treatment, contaminated=False):
    definitions = [
        ('text_search', 'Search literal text inside the prepared source root.', {'query': {'type': 'string'}}),
        ('file_list', 'List paths inside the prepared source root.', {'glob': {'type': 'string'}}),
        ('read_file', 'Read a bounded file range inside the prepared source root.',
         {'path': {'type': 'string'}, 'start': {'type': 'integer'}, 'end': {'type': 'integer'}}),
    ]
    if treatment == 'grepglint':
        definitions.append(('grepglint_search', 'Search indexed code inside the same prepared source root.',
                            {'query': {'type': 'string'}}))
    if contaminated:
        definitions.append(('outside_source_read', 'Deliberately forbidden fixture tool.',
                            {'path': {'type': 'string'}}))
    return [{'type': 'function', 'name': name, 'description': description, 'deferLoading': False,
             'inputSchema': {'type': 'object', 'properties': properties,
                             'required': list(properties), 'additionalProperties': False}}
            for name, description, properties in definitions]


def tool_catalog(request):
    """Read declared catalogs, including namespaces and input.additional_tools."""
    names, catalogs, errors = set(), [], []

    def visit(value, path='$', namespace=''):
        if isinstance(value, list):
            for index, item in enumerate(value):
                visit(item, f'{path}[{index}]', namespace)
        elif isinstance(value, dict):
            for key, child in value.items():
                if key in ('parameters', 'inputSchema', 'input_schema'):
                    continue
                if key in ('tools', 'additional_tools'):
                    location = f'{path}.{key}'
                    if not isinstance(child, list):
                        errors.append('invalid_tool_catalog')
                        continue
                    catalogs.append({'location': location, 'entries': len(child)})
                    for index, tool in enumerate(child):
                        if not isinstance(tool, dict):
                            errors.append('invalid_tool_declaration')
                            continue
                        spec = tool.get('function', tool)
                        name = spec.get('name', tool.get('type'))
                        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_.:/-]{1,160}', name):
                            errors.append('invalid_tool_name')
                            continue
                        qualified = f'{namespace}.{name}' if namespace else name
                        if tool.get('type') == 'namespace':
                            visit(tool, f'{location}[{index}]', qualified)
                        else:
                            names.add(qualified)
                            if qualified == 'functions.exec':
                                description = spec.get('description', '')
                                nested = re.findall(r'^declare const tools: \{ ([A-Za-z_][A-Za-z0-9_]*)\(',
                                                    description, re.MULTILINE)
                                headings = re.findall(r'^### `([A-Za-z_][A-Za-z0-9_]*)`$',
                                                      description, re.MULTILINE)
                                if not nested or set(nested) != set(headings):
                                    errors.append('unparsed_code_mode_catalog')
                                names.update(nested)
                                catalogs.append({'location': f'{location}[{index}].description',
                                                 'entries': len(nested)})
                            visit(tool, f'{location}[{index}]', namespace)
                else:
                    visit(child, f'{path}.{key}', namespace)
    visit(request)
    if not catalogs:
        errors.append('missing_tool_catalog')
    declared = sorted(names)
    registry = []
    metadata = request.get('client_metadata', {}).get('x-codex-turn-metadata')
    if metadata is not None:
        from _codex_capture import json_value
        decoded = json_value(metadata)
        info = decoded.get('tool_namespaces_info') if isinstance(decoded, dict) else None
        if not isinstance(info, dict):
            errors.append('missing_runtime_tool_registry')
        else:
            for namespace, group in info.items():
                if not isinstance(group, dict) or not isinstance(group.get('functions'), dict):
                    errors.append('invalid_runtime_tool_registry')
                    continue
                for name, entry in group.get('functions', {}).items():
                    if not isinstance(entry, dict):
                        errors.append('invalid_runtime_tool_registry')
                        continue
                    qualified = namespace + '.' + name
                    alias = entry.get('code_mode_name')
                    if not re.fullmatch(r'[A-Za-z0-9_.:/-]{1,160}', qualified) or (
                            alias is not None and not re.fullmatch(r'[A-Za-z0-9_]{1,160}', alias)):
                        errors.append('invalid_registry_tool_name')
                        continue
                    registry.append({'name': qualified, 'code_mode_name': alias,
                                     'direct': entry.get('direct'), 'deferred': entry.get('deferred')})
                    names.add(alias or qualified)
    else:
        errors.append('missing_runtime_tool_registry')
    return {'names': sorted(names), 'declared_names': declared, 'runtime_registry': registry,
            'catalogs': catalogs, 'errors': sorted(set(errors))}


def instruction_blocks(request, fixture_root=''):
    blocks = []

    def record(text, location, role):
        if not isinstance(text, str):
            return
        origins = [f'fixture:{name}' for name, marker in MARKERS.items() if marker in text]
        if text == BASE:
            origins.append('launch:model_instructions_file')
        if text == 'Reply preflight-complete.':
            origins.append('probe:user_prompt')
        for tag, origin in [('permissions instructions', 'client:permission_instructions'),
                            ('environment_context', 'client:environment_context')]:
            if f'<{tag}>' in text:
                origins.append(origin)
        blocks.append({'location': location, 'role': role, 'bytes': len(text.encode()),
                       'sha256': digest(text),
                       'normalized_sha256': digest(text.replace(fixture_root, '<fixture>') if fixture_root else text),
                       'origins': origins or ['client_generated_or_unattributed']})

    record(request.get('instructions'), '$.instructions', 'system')
    for index, item in enumerate(request.get('input', [])):
        if not isinstance(item, dict) or item.get('role') not in ['system', 'developer', 'user']:
            continue
        content = item.get('content', [])
        if isinstance(content, str):
            record(content, f'$.input[{index}].content', item['role'])
        elif isinstance(content, list):
            for part, value in enumerate(content):
                if isinstance(value, dict):
                    record(value.get('text'), f'$.input[{index}].content[{part}]', item['role'])
    return blocks


def inspect_request(request, treatment, hook_ran, fixture_root=''):
    catalog = tool_catalog(request)
    expected = {tool['name'] for tool in dynamic_tools(treatment)}
    aliases = expected | {'functions.' + name for name in expected}
    observed = {name.removeprefix('functions.') for name in catalog['names']}
    blocks = instruction_blocks(request, fixture_root)
    encoded = json.dumps(request)
    sentinels = {name: marker in encoded for name, marker in MARKERS.items()}
    extra = sorted(name for name in catalog['names'] if name not in aliases)
    blockers = []
    if extra:
        blockers.append('additional_callable_tools')
    if any(sentinels.values()):
        blockers.append('fixture_content_in_request')
    if hook_ran:
        blockers.append('fixture_hook_executed')
    if request.get('model') != MODEL or request.get('reasoning', {}).get('effort') != EFFORT:
        blockers.append('model_or_effort_mismatch')
    errors = catalog['errors'] + ([] if expected <= observed else ['controlled_tools_missing'])
    return {'request_status': 'incomplete' if errors else 'unsupported' if blockers else 'supported',
            'blockers': blockers, 'errors': errors, 'effective_model': request.get('model'),
            'effective_effort': request.get('reasoning', {}).get('effort'), 'tools': catalog,
            'additional_tools': extra, 'instruction_blocks': blocks, 'sentinels': sentinels,
            'hook_ran': hook_ran,
            'tool_accounting': [{
                'name': name,
                'assessment': ('utility; asks for input and does not grant source access'
                               if name == 'functions.request_user_input' else
                               'utility; waits on an existing Code Mode call'
                               if name == 'functions.wait' else
                               'JavaScript execution wrapper; includes the reported nested tools'
                               if name == 'functions.exec' else
                               'additional source or delegation capability; blocks the contract')}
                               for name in extra]}


def child_environment(root, codex_dir):
    return {'PATH': '/usr/bin:/bin', 'HOME': str(root / 'user'), 'CODEX_HOME': str(codex_dir),
            'XDG_CONFIG_HOME': str(root / 'user' / '.config'), 'XDG_CACHE_HOME': str(root / 'cache'),
            'XDG_DATA_HOME': str(root / 'data'), 'XDG_STATE_HOME': str(root / 'state'),
            'TMPDIR': str(root / 'tmp'), 'LANG': 'C.UTF-8', 'NO_COLOR': '1',
            'RUST_LOG': 'off'}


def fixtures(root, url, contaminated):
    for name in ['user', 'cache', 'data', 'state', 'tmp', 'source', 'clean', 'polluted']:
        (root / name).mkdir(mode=0o700)
    (root / 'source' / '.git').mkdir()
    (root / 'source' / 'example.py').write_text('def example():\n    return 1\n')
    (root / 'source' / 'AGENTS.md').write_text(MARKERS['project_instruction'])
    (root / 'polluted' / 'AGENTS.md').write_text(MARKERS['global_instruction'])
    skill = root / 'polluted' / 'skills' / 'preflight-fixture'
    skill.mkdir(parents=True)
    (skill / 'SKILL.md').write_text('---\nname: preflight-fixture\ndescription: '
                                  + MARKERS['skill'] + '\n---\nHarmless fixture.\n')
    (root / 'outside.txt').write_text(BASE + '\n' + MARKERS['outside_source'])
    (root / 'clean' / 'base.md').write_text(BASE)
    command = 'printf ran > ' + shlex.quote(str(root / 'hook-ran'))
    command += '; printf ' + shlex.quote(json.dumps({'hookSpecificOutput': {
        'hookEventName': 'SessionStart', 'additionalContext': MARKERS['hook']}}))
    hook = {'hooks': {'SessionStart': [{'hooks': [{'type': 'command', 'command': command, 'timeout': 2}]}]}}
    (root / 'polluted' / 'hooks.json').write_text(json.dumps(hook))
    codex_dir = root / ('polluted' if contaminated else 'clean')
    config = {
        'model': MODEL, 'model_provider': 'preflight', 'model_reasoning_effort': EFFORT,
        'approval_policy': 'never', 'sandbox_mode': 'read-only', 'web_search': 'disabled',
        'project_doc_max_bytes': 0, 'model_instructions_file': str(root / 'outside.txt' if contaminated
                                                               else root / 'clean' / 'base.md'),
        'developer_instructions': MARKERS['configuration'] if contaminated else '',
        'history.persistence': 'none', 'analytics.enabled': False, 'feedback.enabled': False,
        'otel.exporter': 'none', 'otel.trace_exporter': 'none', 'otel.metrics_exporter': 'none',
        'skills.bundled.enabled': False, 'skills.include_instructions': contaminated,
        'features.skip_host_skill_discovery': not contaminated,
        'features.hooks': contaminated, 'features.codex_hooks': contaminated,
        'features.tool_registry.turn_metadata_includes_tool_info': True,
        'mcp_servers': {},
        'model_providers.preflight': {'name': 'preflight', 'base_url': url, 'wire_api': 'responses',
            'requires_openai_auth': False, 'request_max_retries': 0, 'stream_max_retries': 0,
            'supports_websockets': False, 'stream_idle_timeout_ms': 2000},
    }
    config.update({f'features.{feature}': False for feature in DISABLED_FEATURES})
    if contaminated:
        del config['developer_instructions']
    # User config contamination has its own origin, independent of instructions.
    (root / 'polluted' / 'config.toml').write_text('developer_instructions = '
                                                  + json.dumps(MARKERS['configuration']) + '\n')
    return codex_dir, config, command


def toml_value(value):
    if isinstance(value, dict):
        return '{' + ','.join(f'{json.dumps(k)}={toml_value(v)}' for k, v in value.items()) + '}'
    return json.dumps(value)


def run_probe(binary, root, treatment, contaminated, budget):
    stub = ResponsesStub(budget)
    child = None
    result = {'treatment': treatment, 'fixture_mode': 'contamination' if contaminated else 'exclusion'}
    try:
        codex_dir, config, hook_command = fixtures(root, stub.url, contaminated)
        normalized = json.dumps(config, sort_keys=True).replace(str(root), '<fixture>').replace(stub.url, '<loopback>/v1')
        result['launch_config_sha256'] = digest(normalized)
        result['supplied_tools'] = dynamic_tools(treatment, contaminated)
        args = [str(binary)]
        for key, value in config.items():
            args += ['-c', f'{key}={toml_value(value)}']
        args += ['app-server', '--listen', 'stdio://']
        stub.start()
        child = Child(args, root / 'source', child_environment(root, codex_dir), budget)
        child.send({'id': 1, 'method': 'initialize', 'params': {
            'clientInfo': {'name': 'grepglint_preflight', 'version': '1'},
            'capabilities': {'experimentalApi': True}}})
        child.receive(request_id=1)
        child.send({'method': 'initialized', 'params': {}})
        thread_config = {}
        if contaminated:
            child.send({'id': 2, 'method': 'hooks/list', 'params': {'cwds': [str(root / 'source')]}})
            hooks = child.receive(request_id=2)
            # Trust only the exact hook created above, in this disposable config.
            def find_hook(value):
                if isinstance(value, list):
                    for item in value:
                        find_hook(item)
                elif isinstance(value, dict):
                    if value.get('command') == hook_command and 'currentHash' in value:
                        thread_config['hooks.state.' + json.dumps(value['key'])] = {
                            'enabled': True, 'trusted_hash': value['currentHash']}
                    for item in value.values():
                        find_hook(item)
            find_hook(hooks)
            result['fixture_hook_trust_entries'] = len(thread_config)
            for index, (key, value) in enumerate(thread_config.items(), 10):
                child.send({'id': index, 'method': 'config/value/write', 'params': {
                    'filePath': str(codex_dir / 'config.toml'), 'keyPath': key,
                    'value': value, 'mergeStrategy': 'replace'}})
                child.receive(request_id=index)
        params = {'model': MODEL, 'modelProvider': 'preflight', 'cwd': str(root / 'source'),
                  'approvalPolicy': 'never', 'sandbox': 'read-only', 'ephemeral': True,
                  'environments': [], 'dynamicTools': dynamic_tools(treatment, contaminated),
                  'config': {}}
        child.send({'id': 3, 'method': 'thread/start', 'params': params})
        started = child.receive(request_id=3)
        thread_id = started['thread']['id']
        child.send({'id': 4, 'method': 'turn/start', 'params': {'threadId': thread_id,
                    'model': MODEL, 'effort': EFFORT, 'environments': [],
                    'input': [{'type': 'text', 'text': 'Reply preflight-complete.'}]}})
        child.receive(request_id=4)
        completed = child.receive(method='turn/completed')
        result['turn_status'] = completed.get('turn', {}).get('status')
        if result['turn_status'] != 'completed':
            raise ProbeError('turn_did_not_complete')
        if stub.error:
            raise ProbeError(stub.error)
        if stub.request is None:
            raise ProbeError('request_not_captured')
        result.update(inspect_request(stub.request, treatment, (root / 'hook-ran').exists(), str(root)))
        result['request_bytes'] = stub.request_bytes
        result['credentials_present'] = stub.credentials_present
    except ProbeError as error:
        result.update(request_status='incomplete', errors=[stub.error or str(error)])
    except (OSError, SubprocessError, KeyError, TypeError, AttributeError, RecursionError):
        result.update(request_status='incomplete', errors=['client_or_protocol_failure'])
    finally:
        if child is not None:
            child.close()
            result['subprocess_output'] = dict(child.counts, stderr_sha256=child.stderr_hash.hexdigest())
        stub.close()
        result['owned_process_group_terminated'] = child is not None
    return result


def run(binary):
    start = time.monotonic()
    budget = Budget(seconds=DEADLINE_SECONDS - 4)
    receipt = {'schema_version': 1, 'status': 'incomplete', 'inference_performed': False,
               'intended': {'model': MODEL, 'effort': EFFORT}, 'probes': [],
               'limits': {'deadline_seconds': DEADLINE_SECONDS, 'frame_bytes': FRAME_LIMIT,
                          'total_capture_bytes': TOTAL_LIMIT, 'concurrent_clients': 1},
               'provider': {'captured': 'credential-free loopback Responses stub',
                            'chatgpt_verified': False, 'unresolved': [
                                'account_model_availability_and_effort_support',
                                'chatgpt_catalog_prompts_and_provider_capabilities',
                                'authentication_transport_and_server_side_tools']},
               'host_file_isolation': {'established': False,
                   'reason': 'private_configuration_and_empty_environments_are_not_OS_file_confinement'},
               'source_evidence': 'benchmarks/preflight-evidence.json'}
    try:
        if sys.platform != 'linux':
            raise ProbeError('real_client_probe_requires_linux')
        if any(Path(p).exists() for p in ['/etc/codex/config.toml', '/etc/codex/requirements.toml',
                                       '/etc/codex/managed_config.toml']):
            raise ProbeError('system_codex_configuration_requires_separate_isolation')
        binary = binary.resolve(strict=True)
        if not binary.is_file() or binary.stat().st_size > 512 * FRAME_LIMIT:
            raise ProbeError('invalid_client_binary')
        sha = hashlib.sha256()
        with binary.open('rb') as source:
            while chunk := source.read(FRAME_LIMIT):
                budget.remaining()
                sha.update(chunk)
        receipt['client'] = {'binary_sha256': sha.hexdigest(), 'binary_bytes': binary.stat().st_size}
        with tempfile.TemporaryDirectory(prefix='grepglint-preflight-') as temporary:
            root = Path(temporary)
            version = Child([str(binary), '--version'], root, {'HOME': temporary, 'CODEX_HOME': temporary,
                            'PATH': '/usr/bin:/bin'}, budget)
            try:
                text = version.line().decode().strip()
                if text != CLIENT_VERSION:
                    raise ProbeError('client_version_mismatch')
                receipt['client']['version'] = text
            finally:
                version.close()
            for contaminated in [False, True]:
                for treatment in ['control', 'grepglint']:
                    probe_dir = root / f'{treatment}-{contaminated}'
                    probe_dir.mkdir(mode=0o700)
                    receipt['probes'].append(run_probe(binary, probe_dir, treatment, contaminated, budget))
            receipt['observed_temporary_bytes'] = sum(p.lstat().st_size for p in root.rglob('*') if p.is_file())
        receipt['child_peak_rss_kib_linux'] = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
        complete = all(p['request_status'] != 'incomplete' for p in receipt['probes'])
        positive = all(all(p.get('sentinels', {}).get(name) for name in MARKERS if name != 'project_instruction')
                       and p.get('hook_ran')
                       and 'outside_source_read' in {n.rsplit('.', 1)[-1] for n in p.get('additional_tools', [])}
                       for p in receipt['probes'] if p['fixture_mode'] == 'contamination')
        negative = all(not any(p.get('sentinels', {}).values()) and not p.get('hook_ran')
                       for p in receipt['probes'] if p['fixture_mode'] == 'exclusion')
        receipt['fixture_checks'] = {'contamination_detected': positive, 'exclusion_observed': negative}
        pairs = []
        for offset in [0, 2]:
            control, grepglint = receipt['probes'][offset:offset + 2]
            delta = sorted(set(grepglint.get('tools', {}).get('names', [])) - set(control.get('tools', {}).get('names', [])))
            same = (control.get('launch_config_sha256') is not None
                    and control.get('launch_config_sha256') == grepglint.get('launch_config_sha256')
                    and [b['normalized_sha256'] for b in control.get('instruction_blocks', [])]
                    == [b['normalized_sha256'] for b in grepglint.get('instruction_blocks', [])])
            pairs.append({'fixture_mode': control['fixture_mode'], 'same_configuration_and_instructions': same,
                          'added_tools': delta, 'only_grepglint_added': delta == ['grepglint_search']
                          and set(control.get('tools', {}).get('names', [])) <= set(grepglint.get('tools', {}).get('names', []))})
        receipt['pair_checks'] = pairs
        paired = all(p['same_configuration_and_instructions'] and p['only_grepglint_added'] for p in pairs)
        receipt['status'] = 'unsupported' if complete and positive and negative and paired else 'incomplete'
        receipt['blockers'] = ['host_file_isolation_unproven', 'chatgpt_provider_equivalence_unproven']
        if any(p.get('additional_tools') for p in receipt['probes'] if p['fixture_mode'] == 'exclusion'):
            receipt['blockers'].append('additional_callable_tools')
    except ProbeCancelled:
        receipt['errors'] = ['cancelled']
    except ProbeError as error:
        receipt['errors'] = [str(error)]
    except (OSError, SubprocessError):
        receipt['errors'] = ['client_or_fixture_unavailable']
    receipt['captured_bytes'] = budget.used
    receipt['elapsed_seconds'] = round(time.monotonic() - start, 3)
    receipt['next_action'] = 'Use the receipt to refine runner isolation. Do not start a model trial.'
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--codex', type=Path, required=True, help='Pinned Codex 0.154.0 binary')
    parser.add_argument('--receipt', type=Path, required=True, help='New JSON file in a private directory')
    parser.add_argument('--export', type=Path, help='Optional second copy of the sanitized receipt')
    args = parser.parse_args()
    try:
        with cancellation_signals(), exclusive_probe():
            if args.receipt.exists() or (args.export and args.export.exists()):
                raise ProbeError('receipt_already_exists')
            receipt = run(args.codex)
            write_receipt(args.receipt, receipt)
            if args.export:
                write_receipt(args.export, receipt)
        print(json.dumps({'status': receipt['status'], 'inference_performed': False,
                          'errors': receipt.get('errors', []),
                          'next_action': receipt['next_action']}))
        return {'supported': 0, 'unsupported': 2, 'incomplete': 3}[receipt['status']]
    except ProbeCancelled:
        print(json.dumps({'status': 'incomplete', 'errors': ['cancelled'], 'inference_performed': False}))
        return 3
    except (ProbeError, OSError) as error:
        code = str(error) if isinstance(error, ProbeError) else 'receipt_or_lock_unavailable'
        print(json.dumps({'status': 'incomplete', 'errors': [code], 'inference_performed': False}))
        return 3


if __name__ == '__main__':
    raise SystemExit(main())
