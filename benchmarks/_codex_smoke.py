"""Explicit human smoke gate. The pinned metadata API cannot attest a provider request."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import stat
import time

from _codex_capture import FRAME_LIMIT, ProbeError, json_value
from _codex_artifacts import owned
from _codex_isolation import CLIENT_SHA256, file_hash
from _codex_verify import CONTRACT, implementation_hash
from codex_preflight import EFFORT, MODEL, private_directory, write_receipt

SESSION_SECONDS = 120
SESSION_CALLS = 20
MAX_ATTEMPTS = 2
EVIDENCE_REQUIRED = ('effective_tools', 'effective_instructions', 'all_skill_packages',
                     'hidden_direct_handlers', 'nested_aliases', 'zero_transport_retries')


class Attempts:
    """A fixed account-local ledger. A new receipt or quota reset grants no attempts."""
    def __init__(self, path):
        private_directory(path.parent)
        try:
            self.fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            created = True
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
        try:
            self.entries = json_value(raw) if raw else []
        except ProbeError:
            self.close()
            raise
        if (not isinstance(self.entries, list) or len(self.entries) > MAX_ATTEMPTS
                or any(not isinstance(entry, dict) or entry.get('configuration') not in
                       ('control', 'grepglint') for entry in self.entries)):
            self.close()
            raise ProbeError('invalid_attempt_ledger')

    def reserve(self, configuration, receipt_hash):
        if configuration not in ('control', 'grepglint') or len(self.entries) >= MAX_ATTEMPTS or any(
                entry.get('configuration') == configuration for entry in self.entries):
            raise ProbeError('smoke_attempts_exhausted')
        self.entries.append({'configuration': configuration, 'local_receipt_sha256': receipt_hash,
                             'started_at': time.time(), 'status': 'reserved'})
        data = json.dumps(self.entries).encode()
        os.lseek(self.fd, 0, os.SEEK_SET)
        os.write(self.fd, data)
        os.ftruncate(self.fd, len(data))
        os.fsync(self.fd)

    def close(self):
        os.close(self.fd)


def require_local(path, binary, grepglint):
    owned(path.parent, sealed=True)
    if path.is_symlink() or path.stat().st_size > FRAME_LIMIT:
        raise ProbeError('invalid_local_receipt')
    raw = path.read_bytes()
    receipt = json_value(raw)
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
    if receipt['audit'].get('sha256') != file_hash(path.parent / 'audit.jsonl'):
        raise ProbeError('local_audit_identity_mismatch')
    return file_hash(path)


def account_check(account, models):
    if (not isinstance(account.get('account'), dict) or account['account'].get('type') != 'chatgpt'
            or account.get('requiresOpenaiAuth') is not True):
        raise ProbeError('chatgpt_authentication_required')
    candidates = [model for model in models if model.get('model') == MODEL]
    if len(candidates) != 1 or EFFORT not in {
            effort.get('reasoningEffort') for effort in candidates[0].get('supportedReasoningEfforts', [])}:
        raise ProbeError('exact_model_and_effort_required')


def weekly_quota(observation, now):
    if observation.get('ordinaryUsageAllowed') is not True:
        raise ProbeError('included_weekly_quota_unavailable')
    buckets = observation.get('rateLimitsByLimitId')
    snapshot = buckets.get('codex') if isinstance(buckets, dict) else observation.get('rateLimits')
    if not isinstance(snapshot, dict) or snapshot.get('limitId') not in (None, 'codex'):
        raise ProbeError('weekly_quota_unavailable')
    windows = [snapshot.get('primary'), snapshot.get('secondary')]
    weekly = [window for window in windows if isinstance(window, dict)
              and window.get('windowDurationMins') == 7 * 24 * 60]
    if len(weekly) != 1:
        raise ProbeError('weekly_quota_unavailable')
    window = weekly[0]
    percent, reset = window.get('usedPercent'), window.get('resetsAt')
    if (type(percent) not in (int, float) or not 0 <= percent < 100
            or type(reset) is not int or reset <= now):
        raise ProbeError('weekly_quota_unavailable')
    return {'observed_at': now, 'used_percent': percent, 'resets_at': reset,
            'window_minutes': 10080, 'units': 'percentage_only'}


def provider_check(evidence):
    if (evidence.get('origin') != 'supported_provider_metadata_and_pinned_source'
            or any(evidence.get(key) is not True for key in EVIDENCE_REQUIRED)):
        raise ProbeError('provider_effective_request_unverified')


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
    if after['resets_at'] != before['resets_at'] or after['used_percent'] < before['used_percent']:
        raise ProbeError('weekly_quota_reset_during_smoke')
    return {'status': 'passed', 'usage': usage, 'quota_before': before, 'quota_after': after}


def smoke_pair(provider, attempts, receipt_hash):
    """Fakes exercise the full allowance; real metadata must pass before any turn."""
    sessions = []
    for configuration in ('control', 'grepglint'):
        attempts.reserve(configuration, receipt_hash)
        account_check(provider.account(), provider.models())
        before = weekly_quota(provider.quota(), time.time())
        provider_check(provider.evidence())
        result = provider.session(configuration, seconds=SESSION_SECONDS, calls=SESSION_CALLS)
        after = weekly_quota(provider.quota(), time.time())
        sessions.append(completed_session(result, before, after))
    return sessions


def stock_provider_evidence():
    # ModelListResponse exposes picker data and effort choices. CapabilitiesRead
    # exposes three booleans. Neither returns assembled instructions/registrations,
    # hidden dispatch handlers, or complete package catalogs for a provider turn.
    return {'origin': 'pinned_source', **{key: False for key in EVIDENCE_REQUIRED},
            'source': 'benchmarks/verification-evidence.json'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--provider', choices=['offline', 'chatgpt'], default='offline')
    parser.add_argument('--local-receipt', type=Path)
    parser.add_argument('--codex', type=Path)
    parser.add_argument('--grepglint', type=Path)
    parser.add_argument('--receipt', type=Path)
    args = parser.parse_args(argv)
    result = {'schema_version': 2, 'contract': CONTRACT, 'status': 'incomplete',
              'provider_verification': 'pending', 'inference_performed': False,
              'limits': {'sessions_total': MAX_ATTEMPTS, 'seconds_per_session': SESSION_SECONDS,
                         'calls_per_session': SESSION_CALLS, 'automatic_retry': False}}
    try:
        if args.provider != 'chatgpt':
            raise ProbeError('explicit_chatgpt_selection_required')
        if not all((args.local_receipt, args.codex, args.grepglint, args.receipt)):
            raise ProbeError('local_receipt_and_binary_paths_required')
        require_local(args.local_receipt, args.codex.resolve(), args.grepglint.resolve())
        # This is deliberately before reading authentication or launching a turn.
        # Accepting model prose or a local-provider receipt here would hide the gap.
        provider_check(stock_provider_evidence())
    except (ProbeError, OSError, ValueError) as error:
        result['errors'] = [str(error) if isinstance(error, ProbeError) else 'smoke_input_unavailable']
    result['next_action'] = ('Keep #16 unresolved. The pinned app-server metadata does not establish '
                             'the ChatGPT effective request. Do not start a provider turn.')
    if args.receipt:
        try:
            write_receipt(args.receipt, result)
        except (OSError, ProbeError):
            result['errors'] = ['smoke_receipt_unavailable']
    print(json.dumps(result))
    return 3
