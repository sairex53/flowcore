"""Durable Fleet rollout state machine over Fabric's existing local cap protocol."""
import copy
import hashlib
import time

from fabric_protocol import Rejected, audit, fields, integer
from fleet_policy import (MODES, MAX_REVISION, approval, binary, configuration, layout, load,
                          schema, selection)

FORWARD = ('lowering', 'applying', 'observing')
ROLLBACK = ('rollback_lowering', 'rollback_applying', 'rollback_blocked')
TERMINAL = ('complete', 'rolled_back')
ACTIONS = ('validate', 'plan', 'rollout', 'status', 'history', 'rollback')
HISTORY_LIMIT = 32


class Fleet:
    def __init__(self, coordinator, cfg, wall=time.time_ns):
        self.c, self.cfg, self.wall = coordinator, configuration(cfg, coordinator.cfg), wall
        self.observation = None
        self.phase_key = None
        self.phase_limit = 0
        self.reconnect_grace = self.c.clock()+5
        if 'fleet' not in self.c.state:
            if self.c.state['pending'] is not None:
                raise ValueError('finish the existing Fabric rollout before enabling Fleet')
            self.c.persist(dict(self.c.state, fleet=dict(highwater={}, active=None, history=[])))
        state = self.data
        fields(state, ('highwater', 'active', 'history'))
        if len(state['highwater']) > 16 or len(state['history']) > HISTORY_LIMIT:
            raise ValueError('invalid persisted Fleet bounds')
        for value in state['highwater'].values():
            integer(value, 1, MAX_REVISION)
        active = state['active']
        if active:
            if active['phase'] not in (*FORWARD, *ROLLBACK, *TERMINAL, 'paused'):
                raise ValueError('invalid persisted rollout phase')
            for key in ('baseline', 'target'):
                self.c.validate_quotas(active[key])
            for stage in active['stages']:
                self.c.validate_quotas(stage['quotas'])
            integer(active['stage'], 0, len(active['stages'])-1)
            integer(active['end'], active['stage'], len(active['stages'])-1)

    @property
    def data(self):
        return self.c.state['fleet']

    def who(self, pin, owner=None):
        actor = self.cfg['operators'].get(pin)
        if actor is None or (owner is not None and owner not in actor['owners']):
            raise Rejected('fleet_operator_scope')
        return dict(name=actor['name'], pin=pin)

    def authorized_active(self, active):
        pin = active['actor']['pin']
        actor = self.cfg['operators'].get(pin, {})
        owner = self.cfg['owners'].get(active['owner'], {})
        repository = self.cfg['repositories'].get(active['source']['repository'])
        if (active['owner'] not in actor.get('owners', ()) or repository is None or
                active['source']['repository'] not in owner.get('repositories', ()) or
                set(active['services']) - set(owner.get('hosts', ()))):
            return False
        for host in active['services']:
            enrolled = self.c.cfg['agents'][host]['services']
            if (set(active['services'][host]) - enrolled.keys() or set(enrolled) - set(owner['services'])
                    or set(enrolled.values()) - set(owner['classes'])):
                return False
        signer = active['source']['signer']
        if signer is None:
            return not repository['require_signature']
        key = repository['keys'].get(signer)
        return bool(key and hashlib.sha256(binary(key, 32)).hexdigest() == active['source']['key_fingerprint'])

    def commit(self, active, event, *, actor=None, quotas=None, evidence=None):
        if self.c.durability_failed:
            raise Rejected('durability_error')
        old = self.c.state
        caps = old['quotas'] if quotas is None else quotas
        generation = old['generation'] + (caps != old['quotas'])
        history = self.data['history']
        record = dict(time_ns=self.wall(), generation=generation, event=event, actor=actor or active['actor'],
                      owner=active['owner'], policy=active['policy'], revision=active['revision'],
                      source=active['source'], approval=active['approval'], approval_state='approved',
                      approved_by=active['actor'], phase=active['phase'], reason=active['reason'],
                      stage=active['stages'][active['stage']]['name'], targets=active['stages'][active['stage']]['hosts'],
                      changes={host: [old['quotas'][host], rate] for host, rate in caps.items()
                               if rate != old['quotas'][host]}, evidence=evidence)
        highwater = dict(self.data['highwater'])
        if event == 'approved':
            if active['owner'] not in highwater and len(highwater) == 16:
                raise Rejected('fleet_owner_history_limit')
            highwater[active['owner']] = active['revision']
        state = dict(old, quotas=caps, generation=generation, pending=None, fleet=dict(
            active=active if event != 'shadow' else self.data['active'], highwater=highwater,
            history=history[-(HISTORY_LIMIT-1):] + [record]))
        self.c.persist(state)
        audit('fleet_'+event, **{key: val for key, val in record.items() if key not in ('time_ns', 'event')})

    def preflight(self):
        return [host for host in self.c.egress if not self.c.verified(host)]

    def plan(self, bundle, source, mode, percent, pin):
        self.who(pin, bundle['owner'])
        authority = self.cfg['owners'][bundle['owner']]
        if source['repository'] not in authority['repositories']:
            raise Rejected('repository_scope')
        services, impact = selection(bundle, self.c, authority)
        active = self.data['active']
        continuation = bool(active and active['phase'] == 'paused' and active['source'] == source and
                            active['owner'] == bundle['owner'] and active['revision'] == bundle['revision'])
        busy = bool(active and active['phase'] not in TERMINAL and not continuation)
        if not continuation and bundle['revision'] <= self.data['highwater'].get(bundle['owner'], 0):
            raise Rejected('stale_policy_revision')
        baseline = active['baseline'] if continuation else self.c.state['quotas']
        target = dict(baseline, **bundle['caps_kbit'])
        self.c.validate_quotas(target)
        stages = []
        for stage in layout(bundle):
            quotas = dict(baseline, **{host: target[host] for host in stage['hosts']})
            try:
                self.c.validate_quotas(quotas)
            except ValueError as error:
                raise Rejected('infeasible_intermediate_stage') from error
            stages.append(dict(stage, quotas=quotas))
        start = active['stage']+1 if continuation else 0
        if mode == 'percentage':
            end = next((i for i, stage in enumerate(stages) if stage['name'] == str(percent)), None)
            if end is None:
                raise Rejected('percentage_not_declared')
        else:
            if percent is not None:
                raise Rejected('percentage_without_mode')
            end = 0 if mode == 'canary' else len(stages)-1
        if start > end:
            raise Rejected('stage_already_completed')
        for host, data in impact.items():
            data.update(current_kbit=self.c.state['quotas'][host], target_kbit=target[host],
                        delta_kbit=target[host]-self.c.state['quotas'][host])
        plan = dict(epoch=self.c.state['epoch'], expected_generation=self.c.state['generation'],
                    policy=bundle['policy'], revision=bundle['revision'], owner=bundle['owner'],
                    source=source, baseline=baseline, target=target, stages=stages, start=start, end=end,
                    services=services, impact=impact, slo=bundle['slo'], settings=bundle['stages'], mode=mode,
                    previous_approval=active['approval'] if continuation else None,
                    requested_by=self.who(pin))
        samples = {}
        for host in services:
            row = self.c.sessions.get(host)
            fresh = self.c.fresh(row)
            data = row['report']['telemetry'] if fresh else {}
            observed = data.get('egress_kbit') if data.get('age_ms', 3001) <= 3000 else None
            samples[host] = dict(fresh=fresh, fleet_metrics_capable=bool(
                row and 'fleet_metrics_v1' in row['capabilities']),
                observed_kbit=observed, cap_below_observed=observed > target[host] if observed is not None else None)
        return plan, dict(waiting=self.preflight(), busy=busy, impact_observations=samples)

    def acquire(self, pin, message):
        fields(message, ('op', 'version', 'repository', 'commit'), ('mode', 'percent', 'approve'))
        operator = self.cfg['operators'].get(pin)
        repository = message['repository']
        if not operator or not any(repository in self.cfg['owners'][owner]['repositories']
                                   for owner in operator['owners']):
            raise Rejected('repository_scope')
        mode = message.get('mode', 'full')
        if mode not in MODES:
            raise Rejected('unknown_rollout_mode')
        percent = message.get('percent')
        if percent is not None:
            integer(percent, 1, 99)
        bundle, source = load(self.cfg['repositories'][repository], message['commit'])
        source['repository'] = repository
        return schema(bundle), source, mode, percent

    def update(self, pin, action, message, admitted):
        bundle, source, mode, percent = admitted
        plan, readiness = self.plan(bundle, source, mode, percent, pin)
        digest = approval(plan)
        if action != 'rollout' or mode == 'dry-run':
            audit('fleet_'+action, actor=self.who(pin)['name'], owner=bundle['owner'],
                  revision=bundle['revision'], source=source, approval_state='unapproved')
            return dict(plan=plan, approval=digest, approval_state='unapproved', validation=readiness)
        if message.get('approve') != digest:
            raise Rejected('approval_mismatch')
        if readiness['busy']:
            raise Rejected('rollout_busy')
        actor = self.who(pin)
        active = {key: copy.deepcopy(plan[key]) for key in (
            'policy', 'revision', 'owner', 'source', 'baseline', 'target', 'stages',
            'services', 'slo', 'settings', 'end')}
        active.update(actor=actor, approval=digest, stage=plan['start'], reason=None,
                      phase='shadow', started_ns=self.wall(), phase_ns=self.wall(), reference=None)
        if mode == 'shadow':
            sample = self.metrics(active, all_hosts=True)
            measurements = None
            if sample is not None:
                violations, measurements = self.evaluate(active, sample, sample)
                measurements.update(violations=violations[:24], violation_count=len(violations),
                                    relative_baseline_checked=False)
            self.commit(active, 'shadow', evidence=dict(validation=readiness, current=measurements))
            return dict(state='shadow', validation=readiness, evidence=measurements, approval=digest)
        if readiness['waiting']:
            raise Rejected('fleet_preflight_unverified')
        if any(not row['fleet_metrics_capable'] for row in readiness['impact_observations'].values()):
            raise Rejected('fleet_metrics_capability_required')
        previous = self.data['active']
        reference = (previous['reference'] if plan['previous_approval'] else self.metrics(active, all_hosts=True))
        if active['slo']['max_p99_increase_us'] is not None and reference is None:
            raise Rejected('relative_slo_baseline_unavailable')
        active['reference'] = reference
        self.begin_stage(active, 'promoted' if plan['previous_approval'] else 'approved',
                         evidence=dict(validation=readiness, approval_state='approved'))
        return self.status(pin)

    def phase(self, active, name):
        active['phase'] = name
        active['phase_ns'] = self.wall()
        self.observation = None

    def begin_stage(self, active, event, evidence=None):
        current = self.c.state['quotas']
        target = active['stages'][active['stage']]['quotas']
        safe = {host: min(rate, target[host]) for host, rate in current.items()}
        if safe != current and safe != target:
            self.phase(active, 'lowering')
            self.commit(active, event, quotas=safe, evidence=evidence)
        else:
            self.phase(active, 'applying')
            self.commit(active, event, quotas=target, evidence=evidence)

    def rollback(self, active, reason, actor=None, evidence=None):
        if active['phase'] in (*ROLLBACK, 'rolled_back'):
            return
        current = self.c.state['quotas']
        target = active['baseline']
        safe = {host: min(rate, target[host]) for host, rate in current.items()}
        self.phase(active, 'rollback_lowering' if safe != target else 'rollback_applying')
        active['reason'] = reason
        self.commit(active, 'rollback_attempted', actor=actor or dict(name='fleet-controller', pin=None),
                    quotas=safe, evidence=evidence)

    def snapshot(self, active):
        now = self.c.clock()
        key = (active['approval'], active['phase'], active['phase_ns'])
        if key != self.phase_key:
            setting = 'observe_seconds' if active['phase'] == 'observing' else 'verify_seconds'
            remaining = min(active['settings'][setting], max(0, active['settings'][setting] -
                            (self.wall()-active['phase_ns'])/1e9))
            self.phase_key, self.phase_limit = key, max(now+remaining, self.reconnect_grace)
        return now >= self.phase_limit

    def readback(self):
        return {host: dict(generation=self.c.state['generation'], rate_kbit=rate)
                for host, rate in self.c.state['quotas'].items() if self.c.verified(host)}

    def kernel_error(self):
        for host in self.c.egress:
            row = self.c.sessions.get(host)
            if self.c.fresh(row):
                ack = row['report']['ack']
                if ack and (ack['state'] == 'error' or (ack['generation'] == self.c.state['generation']
                        and ack['state'] == 'applied' and not self.c.verified(host))):
                    return host
        return None

    def metrics(self, active, all_hosts=False):
        hosts = active['services'] if all_hosts else active['stages'][active['stage']]['hosts']
        guard = active['slo']
        samples = {}
        for host in hosts:
            row = self.c.sessions.get(host)
            if not self.c.fresh(row):
                return None
            data = row['report']['telemetry']
            if data['age_ms'] > 3000 or data.get('sample_end_ns') is None:
                return None
            services = {item['name']: item for item in data['services']}
            selected = {}
            for service in active['services'][host]:
                item = services.get(service)
                if item is None or item['samples'] < guard['min_samples'] or item['p99_us'] is None:
                    return None
                selected[service] = item
            if (guard['min_egress_kbit'] or guard['min_jain_ppm']) and data['egress_kbit'] is None:
                return None
            if guard['max_controller_actions'] is not None and (
                    data.get('controller_actions') is None or data.get('controller_session') is None):
                return None
            samples[host] = dict(end_ns=data['sample_end_ns'], boot_id=row['boot_id'], services=selected,
                                 rate_kbit=data['egress_kbit'], actions=data.get('controller_actions'),
                                 controller_session=data.get('controller_session'))
        return samples

    def evaluate(self, active, sample, first):
        guard = active['slo']
        failures, rates = [], []
        for host, data in sample.items():
            for service, row in data['services'].items():
                key = f'{host}/{service}'
                if row['p99_us'] > guard['max_p99_us']:
                    failures.append(key+'/p99')
                if guard['max_p99_increase_us'] is not None and active['reference'] is not None:
                    baseline = active['reference'][host]['services'][service]['p99_us']
                    if row['p99_us']-baseline > guard['max_p99_increase_us']:
                        failures.append(key+'/p99_delta')
                total = row['samples']+row['errors']
                if row['errors']*1_000_000 > guard['max_error_ppm']*total:
                    failures.append(key+'/errors')
                if row['misses']*1_000_000 > guard['max_deadline_miss_ppm']*total:
                    failures.append(key+'/deadline_misses')
            rate = data['rate_kbit']
            if rate is not None:
                rates.append(rate)
                if rate < guard['min_egress_kbit']:
                    failures.append(host+'/throughput')
            limit = guard['max_controller_actions']
            previous = first[host]
            if limit is not None and (data['controller_session'] != previous['controller_session'] or
                    not 0 <= data['actions']-previous['actions'] <= limit):
                failures.append(host+'/controller_instability')
        jain = sum(rates)**2/(len(rates)*sum(rate*rate for rate in rates)) if any(rates) else None
        if guard['min_jain_ppm'] and (jain is None or jain*1_000_000 < guard['min_jain_ppm']):
            failures.append('cohort/fairness')
        return failures, dict(jain=jain, hosts={
            host: dict(egress_kbit=row['rate_kbit'], actions=row['actions'],
                       worst_p99_us=max(service['p99_us'] for service in row['services'].values()),
                       samples=sum(service['samples'] for service in row['services'].values()),
                       errors=sum(service['errors'] for service in row['services'].values()),
                       misses=sum(service['misses'] for service in row['services'].values()))
            for host, row in sample.items()})

    def observe(self, active):
        sample = self.metrics(active)
        if sample is None:
            return None
        if self.observation is None:
            self.observation = dict(first=sample, previous=sample, good=0, bad={}, evidence=None)
            return None
        observer = self.observation
        for host, data in sample.items():
            before = observer['previous'][host]
            if before['boot_id'] != data['boot_id'] or data['end_ns'] < before['end_ns']:
                self.observation = None
                return None
            if data['end_ns']-before['end_ns'] < max(
                    row['window_seconds'] for row in data['services'].values())*1_000_000_000:
                return None
        failures, evidence = self.evaluate(active, sample, observer['first'])
        observer['previous'] = sample
        observer['good'] = 0 if failures else observer['good']+1
        observer['bad'] = {key: observer['bad'].get(key, 0)+1 for key in failures}
        evidence.update(good_windows=observer['good'], violation_count=len(observer['bad']),
                        repeated_violations=dict(sorted(observer['bad'].items(),
                            key=lambda item: (-item[1], item[0]))[:24]))
        observer['evidence'] = evidence
        limit = active['settings']['confirm_windows']
        if max(observer['bad'].values(), default=0) >= limit:
            return 'failed', evidence
        if observer['good'] >= limit:
            return 'passed', evidence
        return None

    def tick(self):
        active = self.data['active']
        if self.c.durability_failed or not active or active['phase'] not in (*FORWARD, *ROLLBACK):
            return
        active = copy.deepcopy(active)
        phase = active['phase']
        expired = self.snapshot(active)
        waiting = self.preflight()
        verified = not waiting
        evidence = dict(waiting=waiting, readback=self.readback())
        if phase in ROLLBACK:
            if verified:
                if self.c.state['quotas'] != active['baseline']:
                    self.phase(active, 'rollback_applying')
                    self.commit(active, 'rollback_restore', quotas=active['baseline'], evidence=evidence)
                else:
                    self.phase(active, 'rolled_back')
                    self.commit(active, 'rollback_verified', evidence=evidence)
            elif expired and phase != 'rollback_blocked':
                self.phase(active, 'rollback_blocked')
                self.commit(active, 'rollback_blocked', evidence=evidence)
            return
        if not self.authorized_active(active):
            self.rollback(active, 'approval_authority_revoked', evidence=evidence)
            return
        failed = self.kernel_error()
        stale = [host for host in waiting if not self.c.fresh(self.c.sessions.get(host))]
        if failed or (stale and self.c.clock() >= self.reconnect_grace and phase == 'observing'):
            self.rollback(active, 'kernel_unverified' if failed else 'agent_lost', evidence=evidence)
            return
        if expired:
            self.rollback(active, 'guardrail_timeout' if phase == 'observing' else 'verification_timeout',
                          evidence=evidence)
            return
        if not verified:
            return
        if phase == 'lowering':
            self.phase(active, 'applying')
            self.commit(active, 'lowering_verified', quotas=active['stages'][active['stage']]['quotas'],
                        evidence=evidence)
        elif phase == 'applying':
            self.phase(active, 'observing')
            self.commit(active, 'kernel_verified', evidence=evidence)
        else:
            outcome = self.observe(active)
            if outcome is None:
                return
            result, measurements = outcome
            if result == 'failed':
                self.rollback(active, 'guardrail_violation', evidence=measurements)
            elif active['stage'] < active['end']:
                active['stage'] += 1
                self.begin_stage(active, 'stage_advanced', evidence=measurements)
            else:
                self.phase(active, 'complete' if active['stage'] == len(active['stages'])-1 else 'paused')
                self.commit(active, active['phase'], evidence=measurements)

    def status(self, pin):
        actor = self.cfg['operators'][pin]
        active = self.data['active']
        authorized = active is not None and active['owner'] in actor['owners']
        report = dict(generation=self.c.state['generation'], active=active if authorized else None,
                      occupied=active is not None and active['phase'] not in TERMINAL,
                      durability_failed=self.c.durability_failed)
        if authorized:
            report['kernel_verified'] = self.readback()
            report['waiting'] = self.preflight()
            report['guardrail_evidence'] = self.observation['evidence'] if self.observation else None
        return report

    def dispatch(self, pin, message):
        self.who(pin)
        action = message['op'].removeprefix('fleet_')
        if action not in ACTIONS:
            raise Rejected('fleet_operation')
        admitted = self.acquire(pin, message) if action in ('plan', 'validate', 'rollout') else None
        with self.c.lock:
            if admitted is not None:
                return self.update(pin, action, message, admitted)
            fields(message, ('op', 'version', 'generation', 'approval') if action == 'rollback' else ('op', 'version'),
                   ('limit', 'offset') if action == 'history' else ())
            if action == 'status':
                return self.status(pin)
            if action == 'history':
                limit = integer(message.get('limit', 8), 1, 8)
                offset = integer(message.get('offset', 0), 0, HISTORY_LIMIT)
                rows = [row for row in reversed(self.data['history'])
                        if row['owner'] in self.cfg['operators'][pin]['owners']]
                return dict(total=len(rows), events=rows[offset:offset+limit])
            active = copy.deepcopy(self.data['active'])
            if active is None:
                raise Rejected('no_rollout')
            self.who(pin, active['owner'])
            if (integer(message['generation'], 1, MAX_REVISION) != self.c.state['generation'] or
                    message['approval'] != active['approval']):
                raise Rejected('stale_expected_generation')
            self.rollback(active, 'operator', actor=self.who(pin, active['owner']))
            return self.status(pin)
