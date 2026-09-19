"""One approval for an immutable selected run; fresh checks before every submission."""
import time

from _codex_audit import encoded
from _codex_capture import Budget, ProbeError
from _codex_smoke import (ChatGPTProvider, account_check, weekly_quota, quota_confirmation,
                          live_configuration, provider_check, FRESH_SECONDS)
from _paired_attempts import Attempts
from _paired_contract import BASE, LIMITS, tools
from _paired_proof import plan_hash, require_proof
from _paired_source import verify
from codex_preflight import digest
import _paired_store as store


def quota_compatible(before, after):
    old = quota_confirmation(before)
    new = quota_confirmation(after)
    if old['account_identity_sha256'] != new['account_identity_sha256']:
        raise ProbeError('quota_account_changed')
    a = {(b['bucket_id'], b['slot']): b for b in old['buckets']}
    b = {(b['bucket_id'], b['slot']): b for b in new['buckets']}
    if a.keys() != b.keys():
        raise ProbeError('weekly_buckets_changed')
    raw_a = {(item['bucket_id'], item['slot']): item for item in before['buckets']}
    raw_b = {(item['bucket_id'], item['slot']): item for item in after['buckets']}
    for key in a:
        same_reset = (a[key]['resets_at'] == b[key]['resets_at']
                      or raw_a[key]['resets_at'] == raw_b[key]['resets_at'])
        if not same_reset or b[key]['used_percent'] < a[key]['used_percent']:
            raise ProbeError('weekly_reset_or_usage_reversal')
    if any(item['resets_at'] <= after['observed_at'] for item in before['buckets']):
        raise ProbeError('weekly_reset_crossed')


def observation(provider, roles):
    value = {'roles': roles, 'observed_at': time.time(), 'raw': None, 'weekly': None, 'error': None}
    try:
        value['raw'] = provider.quota()
        value['observed_at'] = time.time()
        value['weekly'] = weekly_quota(value['raw'], value['observed_at'])
    except (ProbeError, OSError, ValueError) as error:
        value['error'] = str(error) if isinstance(error, ProbeError) else 'quota_read_failed'
    return value


def fresh(value):
    if value['error'] or value['weekly'] is None:
        raise ProbeError(value['error'] or 'weekly_quota_unavailable')
    if not 0 <= time.time() - value['observed_at'] <= FRESH_SECONDS:
        raise ProbeError('quota_observation_stale')
    return value['weekly']


class Run:
    def __init__(self, args, planned, catalog, ledger, proofs, corpus):
        self.args, self.planned, self.catalog = args, planned, catalog
        self.ledger, self.proofs, self.corpus = ledger, proofs, corpus
        self.run = args.worker
        self.status = store.read(self.run / 'run.json')
        self.previous = None

    def provider(self, trial, audit, budget):
        source = self.args.snapshots / trial['source']['id']
        return ChatGPTProvider(self.args.codex, self.args.grepglint, self.args.auth,
            trial={'source': source, 'source_sha256': self.planned['prepared_sources'][trial['source']['id']]['sha256'], 'base': BASE, 'prompt': trial['question'],
                   'catalog': self.catalog, 'tools': tools(trial['configuration'], self.catalog),
                   'budget': budget, 'audit': audit, 'cache_bytes': LIMITS['cache_bytes']})

    def binding(self, account):
        return {'plan_sha256': plan_hash(self.planned), 'account': account,
                'proofs': self.proofs, 'configuration_sha256': digest(encoded(live_configuration()))}

    def check_identities(self, trial):
        from paired import implementation_hash
        from _codex_isolation import prerequisites, file_hash
        prerequisites(self.args.codex, self.run)
        owner = store.owned(self.args.proof)
        for name in ('plan.json', trial['trial_id'] + '.json', trial['trial_id'] + '.jsonl'):
            limit = LIMITS['events_output_bytes'] if name.endswith('.jsonl') else LIMITS['trial_metadata_bytes']
            if store.hash_file(self.args.proof / name, limit) != owner.get('sha256', {}).get(name):
                raise ProbeError('selected_proof_changed')
        if (self.planned['implementation_sha256'] != implementation_hash()
                or file_hash(self.args.grepglint) != self.planned['grepglint_sha256']
                or store.hash_file(self.args.proof / 'ownership.json', 1024 * 1024) != self.proofs['receipt_sha256']
                or plan_hash(store.read(self.run / 'plan.json')) != self.proofs['plan_sha256']
                or digest(encoded(tools(trial['configuration'], self.catalog))) != self.proofs['sessions'][trial['trial_id']]['catalog_sha256']):
            raise ProbeError('selected_run_identity_changed')

    def readiness(self):
        trial = self.planned['trials'][0]
        self.check_identities(trial)
        budget = Budget(300)
        for source_id, expected in self.planned['prepared_sources'].items():
            trial_source = next(t['source'] for t in self.planned['trials'] if t['source']['id'] == source_id)
            actual = verify(self.args.snapshots / source_id, trial_source, self.corpus, budget)
            if actual['sha256'] != expected['sha256']:
                raise ProbeError('prepared_source_changed')
        provider = self.provider(trial, None, budget)
        try:
            account = account_check(provider.account(), provider.models())
            probe = provider.preflight_probe(trial['configuration'])
            provider_check(provider.evidence(self.proofs, probe))
            quota = observation(provider, ['readiness'])
            fresh(quota)
            return self.binding(account), quota
        finally:
            provider.close()

    def session(self, trial, audit, budget, record):
        self.check_identities(trial)
        if record['source_before']['sha256'] != self.planned['prepared_sources'][trial['source']['id']]['sha256']:
            raise ProbeError('approved_source_changed')
        provider = self.provider(trial, audit, budget)
        reserved = False
        index = trial['order']
        before_roles = ['trial_before'] + (['pair_before'] if index % 2 == 0 else []) + (['run_before'] if index == 0 else [])
        after_roles = ['trial_after'] + (['pair_after'] if index % 2 else [])
        record['quota_observations'] = []
        record['offline_proof'] = {'receipt_sha256': self.proofs.get('receipt_sha256'),
                                   'plan_sha256': self.proofs['plan_sha256'],
                                   'provider_evidence': self.proofs.get('provider_evidence')}
        try:
            identity = account_check(provider.account(), provider.models())
            if self.binding(identity) != self.status['authorization']['binding']:
                raise ProbeError('account_or_authorization_identity_changed')
            probe = provider.preflight_probe(trial['configuration'])
            provider_check(provider.evidence(self.proofs, probe))

            def reserve():
                nonlocal reserved
                self.check_identities(trial)
                before = observation(provider, before_roles)
                record['quota_observations'].append(before)
                weekly = fresh(before)
                quota_compatible(self.previous, weekly)
                self.ledger.trial(self.run.name, trial['trial_id'], 'reserved')
                reserved = True
                record['inference_performed'] = True
                record['authorization_sha256'] = self.status['authorization']['confirmation']
                store.save(self.run, trial['trial_id'] + '.json', record)
            result = provider.session(trial['configuration'], LIMITS['trial_seconds'], LIMITS['tool_calls'], reserve)
            result['reported_model'], result['reported_effort'] = result['model'], result['effort']
            return result
        finally:
            try:
                if reserved:
                    after = observation(provider, after_roles)
                    record['quota_observations'].append(after)
                    # Preserve raw/missing observations even when continuity fails.
                    store.save(self.run, trial['trial_id'] + '.json', record)
                    weekly = fresh(after)
                    quota_compatible(record['quota_observations'][0]['weekly'], weekly)
                    self.previous = weekly
            finally:
                record['reported_model'] = getattr(provider, 'reported_model', None)
                record['reported_effort'] = getattr(provider, 'reported_effort', None)
                provider.close()


def worker(args, catalog, cg):
    from paired import CORPUS, implementation_hash, trial_worker
    planned = store.read(args.worker / 'plan.json')
    status = store.read(args.worker / 'run.json')
    if planned.get('schema_version') != 2 or planned.get('simulation') is not False or planned.get('execution') != 'chatgpt':
        raise ProbeError('live_plan_contract_required')
    ledger = None
    started = False
    try:
        proofs = require_proof(args.proof, args.codex, args.grepglint, implementation_hash())
        if plan_hash(planned) != proofs['plan_sha256']:
            raise ProbeError('changed_selected_plan')
        ledger = Attempts()
        run = Run(args, planned, catalog, ledger, proofs, CORPUS)
        binding, quota = run.readiness()
        if args.live_action == 'readiness':
            previous = ledger.history()
            confirmation = ledger.prepare(args.worker.name, binding, planned['trials'])
            status.update(status='ready', authorization={'binding': binding, 'confirmation': confirmation},
                          readiness=quota, previous_attempt_history=previous, proof_run=str(args.proof))
        else:
            ledger.start(args.worker.name, binding, args.confirm)
            started = True
            quota_compatible(status['readiness']['weekly'], fresh(quota))
            run.previous = fresh(quota)
            for trial in planned['trials']:
                result = trial_worker(args.worker, trial, args, catalog, cg, live=run)
                state = ledger.runs[args.worker.name]['states'][trial['trial_id']]
                if state == 'reserved':
                    ledger.trial(args.worker.name, trial['trial_id'], 'completed' if result['state'] == 'completed' else 'failed')
                status['inference_performed'] |= result['inference_performed']
                if result['state'] != 'completed':
                    status['errors'] = result['errors']
                    break
            else:
                status['status'] = 'completed'
            if result.get('quota_observations'):
                status['quota_after'] = {**result['quota_observations'][-1], 'roles': ['run_after', 'pair_after', 'trial_after']}
            if status['errors']:
                status['status'] = 'incomplete'
    except BaseException as error:
        status['status'] = 'incomplete'
        status['errors'] = [str(error) if isinstance(error, ProbeError) else 'live_run_interrupted']
        raise
    finally:
        if ledger:
            try:
                if started:
                    ledger.stop(args.worker.name)
            finally:
                ledger.close()
        store.save(args.worker, 'run.json', status)
    return status


def public_quota(observations):
    """Allowlist measurements only; never export raw account payloads or identities."""
    import math
    import re
    result = []
    for value in observations[:2]:
        item = {'observed_at': value.get('observed_at'), 'available': value.get('weekly') is not None, 'buckets': []}
        if type(item['observed_at']) not in (int, float) or not math.isfinite(item['observed_at']):
            item['observed_at'] = None
        raw = value.get('raw') or {}
        buckets = raw.get('rateLimitsByLimitId') or {}
        if not buckets and isinstance(raw.get('rateLimits'), dict):
            fallback = raw['rateLimits']
            buckets = {fallback.get('limitId') or 'default': fallback}
        for name, bucket in buckets.items():
            if not isinstance(name, str) or not re.fullmatch('[A-Za-z0-9_-]{1,100}', name) or not isinstance(bucket, dict):
                continue
            for slot in ('primary', 'secondary'):
                window = bucket.get(slot)
                if not isinstance(window, dict) or window.get('windowDurationMins') != 10080:
                    continue
                used, reset = window.get('usedPercent'), window.get('resetsAt')
                if type(used) not in (int, float) or not math.isfinite(used) or not 0 <= used <= 100 or type(reset) is not int:
                    continue
                item['buckets'].append({'bucket_id': name, 'slot': slot, 'window_minutes': 10080,
                    'used_percent': used, 'remaining_percent': 100 - used, 'resets_at': reset})
        result.append(item)
    return result
