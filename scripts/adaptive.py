"""Deterministic, single-writer adaptive controller for the owned HTB lab."""
import json
import time
from collections import deque

import adaptive_tc
from intents import Registry

NANO = 10**9
LATENCY_MIN_SAMPLES = 100


def static(*_):
    return False


def threshold(slo, pressure, _intent):
    return pressure and slo['state'] != 'insufficient_data'


def slo_aware(slo, pressure, _intent):
    return pressure and slo['outside_deadband']


STRATEGIES = {'static': static, 'threshold': threshold, 'slo': slo_aware}
DEFAULT = dict(window_seconds=4, minimum_samples=20, enter_windows=2, cooldown_seconds=4,
               background_floor_pct=20, step_pct=5, maximum_boost_pct=15, maximum_actions=8, recovery_windows=3, maximum_reversals=2)


def objective(window, name, intent, minimum):
    app = window['application'].get(name, {}).get('application', {})
    samples = app.get('samples', 0)
    p99 = app.get('p99_us')
    rate = window['queue'].get(intent['priority'], {}).get('bytes_per_second')
    kbit = rate / 125 if rate is not None else None
    target = intent['target_p99_ms'] * 1000
    desired = intent['desired_kbit']
    valid = samples >= minimum and p99 is not None and (not desired or kbit is not None)
    bad = valid and (p99 > target or (desired and kbit < desired))
    outside_deadband = bool(valid and (p99 > target * 1.1 or (desired and kbit < desired * .9)))
    failures = app.get('failure', 0) + app.get('timeout', 0)
    return dict(state='violated' if bad else 'met' if valid else 'insufficient_data',
                outside_deadband=outside_deadband, samples=samples, p99_us=p99, p50_us=app.get('p50_us'), p95_us=app.get('p95_us'),
                error_ratio=app.get('error_ratio'), deadline_miss=app.get('deadline_miss') or 0,
                failures=failures, reports=app.get('ok', samples)+failures,
                probe_overlap=app.get('probe_overlap', 0),
                target_p99_us=target, throughput_kbit=kbit, desired_kbit=desired)


class Controller:
    def __init__(self, service):
        self.service = service
        self.registry = Registry(service.observer)
        self.cfg = service.config.get('adaptive', DEFAULT)
        self.history = deque(maxlen=128)
        self.path = None
        self.mode = 'off'
        self.strategy = 'slo'
        self.base = self.current = None
        self.pending = None
        self.revision = 0
        self.last_tick = 0
        self.next_action = 0
        self.streak = {}
        self.actions = 0
        self.reversals = 0
        self.recovery = 0
        self.error = None
        self.starvation = 0
        self.timing = {}

    def restore(self, runtime):
        self.registry.restore(runtime)
        self.path = runtime / 'decisions.json'
        if self.path.exists():
            with self.path.open() as stream:
                self.history.extend(json.loads(stream.read(524288)))

    def record(self, **fields):
        self.history.append(dict(time_unix=time.time(), time_ns=time.monotonic_ns(), generation=self.service.observer.generation, **fields))
        if self.path:
            try:
                tmp = self.path.with_suffix('.tmp')
                tmp.write_text(json.dumps(list(self.history)))
                tmp.replace(self.path)
            except OSError as error:
                self.service.log('error', detail=f'adaptive journal: {error}')

    def policy(self):
        return self.service.config['policies'][self.service.active]

    def capacity(self):
        return self.service.profile['bandwidth_kbit']

    def start(self, mode, strategy):
        if strategy not in STRATEGIES:
            raise ValueError('unknown controller strategy')
        if not self.service.lab or self.policy()['scheduler'] != 'htb':
            raise ValueError('controller requires an owned HTB lab')
        if not self.service.observer.cfg['enabled']:
            raise ValueError('controller requires observability')
        if mode == 'shadow' and self.mode == 'active':
            raise ValueError('stop the active controller before entering shadow mode')
        self.stop('mode change')
        capacity = self.capacity()
        installed = adaptive_tc.read(self.service.lab, capacity, self.policy())
        configured = {name: (capacity*cls['min_pct']//100, capacity*cls['max_pct']//100)
                      for name, cls in self.policy()['classes'].items()}
        if installed != configured:
            raise ValueError('installed HTB allocation differs from operator baseline')
        self.base = installed
        if self.base['background'][0] < self.capacity()*self.cfg['background_floor_pct']//100:
            raise ValueError('operator background floor exceeds installed reservation')
        self.current = dict(self.base)
        self.mode, self.strategy = mode, strategy
        self.revision = self.registry.revision
        self.last_tick = time.monotonic()
        self.next_action = 0
        self.streak.clear()
        self.actions = 0
        self.reversals = 0
        self.recovery = 0
        self.error = None
        self.starvation = 0
        self.timing = {}

    def write(self, target):
        timing = adaptive_tc.change(self.service.lab, self.capacity(), self.policy(), self.current, target)
        self.current = dict(target)
        self.service.poll_metrics()
        return dict(timing, refresh_ns=time.monotonic_ns()-timing['verified_ns'])

    def stop(self, reason):
        if self.mode == 'active' and self.current != self.base and self.service.lab:
            self.write(self.base)
            self.record(result='restored_baseline', reason=reason, allocation=self.base)
        self.mode = 'off'
        self.pending = None
        self.service.observer.probe_start_ns = None

    def detach(self):
        if self.mode != 'off':
            self.record(result='lab_closed', reason='topology removed')
        self.mode = 'off'
        self.current = self.base = self.pending = None
        self.service.observer.probe_start_ns = None

    def register(self, value, uid, pid):
        intent = self.registry.validate(value)
        if self.service.lab and self.policy()['scheduler'] == 'htb':
            cls = self.policy()['classes'][intent['priority']]
            capacity = self.capacity()
            rate = capacity * cls['min_pct'] // 100
            ceil = capacity * cls['max_pct'] // 100
            if intent['min_kbit'] > rate or (intent['max_kbit'] and not rate <= intent['max_kbit'] <= ceil):
                raise ValueError('intent hard bounds outside operator baseline')
            if intent['background_min_pct'] > self.policy()['classes']['background']['min_pct']:
                raise ValueError('intent background floor outside operator baseline')
        elif intent['min_kbit'] or intent['max_kbit']:
            raise ValueError('an active HTB lab is required to admit hard bounds')
        return self.registry.register(intent, uid, pid)

    def plan(self, window, current):
        cfg, capacity = self.cfg, self.capacity()
        root = window['queue'].get('root', {})
        bg = window['queue'].get('background', {})
        backlog = window['backlog'].get('p95_bytes')
        pressure = (root.get('utilization') or 0) >= .75 and (backlog or 0) > 0
        floor_pct = max([cfg['background_floor_pct'], *(
            row['intent']['background_min_pct'] for row in self.registry.rows.values())])
        bg_floor = capacity * floor_pct // 100
        outcomes = {}
        attribution = {}
        bounds = dict(current)
        for name, row in sorted(self.registry.rows.items()):
            intent = row['intent']
            cls = intent['priority']
            rate, ceil = current[cls]
            cap = intent['max_kbit'] or self.base[cls][1]
            if cap < rate or cap > self.base[cls][1] or intent['min_kbit'] > self.base[cls][0] or bg_floor > self.base['background'][0]:
                raise ValueError('registered hard bounds conflict with current lab capacity')
            bounds[cls] = (rate, cap)
            measured = outcomes[name] = objective(window, name, intent, cfg['minimum_samples'])
            queue = window['queue'].get(cls, {})
            attribution[name] = dict(backlogged_samples=queue.get('backlogged_samples', 0),
                                     borrowed=queue.get('borrowed'), guaranteed_kbit=rate,
                                     eligible=queue.get('backlogged_samples', 0) >= 2
                                     and (measured['throughput_kbit'] or 0) >= .9*rate
                                     and not measured['failures']
                                     and measured['samples'] >= max(LATENCY_MIN_SAMPLES, cfg['minimum_samples']))
        detail = dict(previous=current, proposed=bounds, objectives=outcomes, attribution=attribution, pressure=pressure,
                      background_floor_kbit=bg_floor, background_throughput_kbit=(
                          bg['bytes_per_second']/125 if bg.get('bytes_per_second') is not None else None))
        if bounds != current:
            return dict(detail, action='enforce_class_ceiling')
        if current['background'][0] < bg_floor:
            return dict(detail, proposed=self.base, action='restore_background_floor')
        order = sorted(self.registry.rows, key=lambda name: (
            self.policy()['classes'][self.registry.rows[name]['intent']['priority']]['priority'], name))
        for name in order:
            intent = self.registry.rows[name]['intent']
            cls = intent['priority']
            rate, ceil = current[cls]
            if not attribution[name]['eligible'] or not STRATEGIES[self.strategy](outcomes[name], pressure, intent):
                continue
            step = min(capacity * cfg['step_pct']//100, current['background'][0]-bg_floor,
                       self.base[cls][0]+capacity*cfg['maximum_boost_pct']//100-rate, ceil-rate)
            if step <= 0:
                continue
            target = dict(current)
            target[cls] = (rate+step, ceil)
            target['background'] = (current['background'][0]-step, current['background'][1])
            return dict(detail, proposed=target, action='transfer_reservation', service=name,
                        prediction='more reserved service bandwidth may reduce queueing under continued demand')
        return dict(detail, action='hold')

    def fairness(self, window):
        rates = {name: row.get('bytes_per_second') for name, row in window['queue'].items()
                 if name in ('critical', 'interactive', 'standard', 'background')}
        positive = [v for v in rates.values() if v is not None and v > 0]
        jain = sum(positive)**2/(len(positive)*sum(v*v for v in positive)) if positive else None
        return dict(class_bytes_per_second=rates, jain_active_classes=jain,
                    background_starvation_observed_seconds=self.starvation,
                    utilization=window['queue'].get('root', {}).get('utilization'),
                    protected_kbit={key: val[0] for key, val in (self.current or {}).items()})

    def evaluate(self):
        if not self.service.lab or self.policy()['scheduler'] != 'htb':
            raise ValueError('evaluation requires an owned HTB lab')
        self.registry.expire()
        current = self.current
        original = self.base
        if current is None or self.mode == 'off':
            current = adaptive_tc.read(self.service.lab, self.capacity(), self.policy())
            self.base = current
        try:
            window = self.service.observer.window(time.monotonic_ns(), self.cfg['window_seconds'], complete=True)
            return dict(self.plan(window, current), dry_run=True, fairness=self.fairness(window))
        finally:
            self.base = original

    def tick(self):
        self.registry.expire()
        if self.mode == 'off':
            return
        now = time.monotonic()
        seconds = self.cfg['window_seconds']
        if self.registry.revision != self.revision:
            if self.mode == 'active' and self.current != self.base:
                self.write(self.base)
                self.record(result='restored_baseline', reason='intent set changed', allocation=self.base)
            self.revision = self.registry.revision
            self.pending = None
            self.service.observer.probe_start_ns = None
            self.streak.clear()
            self.next_action = now + seconds
        if now - self.last_tick < seconds:
            return
        self.last_tick = now
        observer = self.service.observer
        if (not observer.sampled_ns or observer.errors or
                (time.monotonic_ns()-observer.sampled_ns)/1e9 > max(seconds, 2*self.service.config['telemetry_seconds'])):
            self.stop('telemetry unavailable')
            self.error = 'telemetry unavailable'
            return
        expected = {f'1:{minor}': (self.current[name][0]*125, self.current[name][1]*125)
                    for name, minor in adaptive_tc.CLASSES.items()}
        expected['1:1'] = (self.capacity()*125, self.capacity()*125)
        actual = {row['handle']: (row.get('rate'), row.get('ceil'))
                  for row in self.service.telemetry.get('class', []) if row.get('class') == 'htb'}
        if actual != expected:
            raise RuntimeError('telemetry readback shows HTB capacity or allocation drift')
        began = time.monotonic_ns()
        window = observer.window(began, seconds, complete=True)
        window_end = time.monotonic_ns()
        plan = self.plan(window, self.current)
        self.timing = dict(time_ns=began, aggregation_ns=window_end-began,
                           policy_ns=time.monotonic_ns()-window_end,
                           telemetry_age_ns=max(0, began-observer.sampled_ns),
                           attribution=plan['attribution'])
        bg = plan['background_throughput_kbit']
        leaf = next((q for q in self.service.telemetry.get('qdisc', [])
                     if q.get('parent') == '1:20'), {})
        demanded = (leaf.get('backlog') or 0) > 0
        if demanded and bg == 0:
            self.starvation += seconds
            self.stop('background backlog without throughput')
            self.error = 'background starvation safeguard'
            return
        if self.pending:
            old = self.pending
            if began < old['end_ns'] + NANO//4:
                return
            post = observer.window(old['end_ns'], seconds, complete=True)
            intent = self.registry.rows[old['service']]['intent']
            measured = objective(post, old['service'], intent, self.cfg['minimum_samples'])
            before = old['before']
            post_bg = post['queue'].get('background', {})
            bg_rate = post_bg.get('bytes_per_second')
            bg = bg_rate/125 if bg_rate is not None else None
            demanded = post_bg.get('backlogged_samples', 0) > 0
            enough = measured['samples'] >= max(LATENCY_MIN_SAMPLES, self.cfg['minimum_samples'])
            comparable = (enough and not measured['probe_overlap']
                          and .8*before['reports'] <= measured['reports'] <= 1.2*before['reports'])
            degraded = (measured['reports'] >= self.cfg['minimum_samples'] and
                        ((measured['error_ratio'] or 0) > (before['error_ratio'] or 0)
                         or measured['deadline_miss']/measured['reports'] >
                            before['deadline_miss']/before['reports'] + .02))
            latency_improved = (measured.get('p99_us') is not None and before['p99_us'] is not None
                                and measured['p99_us'] < before['p99_us'] * .9)
            throughput_improved = (before['desired_kbit'] > 0 and measured.get('throughput_kbit') is not None
                                  and before['throughput_kbit'] is not None
                                  and measured['throughput_kbit'] > before['throughput_kbit'] * 1.1
                                  and measured.get('p99_us') is not None and before['p99_us'] is not None
                                  and measured['p99_us'] <= before['p99_us'] * 1.1)
            keep = (comparable and not degraded and (latency_improved or throughput_improved)
                    and (not demanded or (bg is not None and bg >= plan['background_floor_kbit']*.8)))
            verdict = 'kept' if keep else 'rolled_back' if comparable or degraded else 'inconclusive'
            rollback = None
            if not keep:
                rollback = self.write(old['allocation'])
                self.next_action = time.monotonic() + max(3*seconds, self.cfg['cooldown_seconds'])
            self.record(result=verdict, service=old['service'], comparable=comparable, degraded=degraded,
                        before=before, measured=measured, background_kbit=bg, allocation=self.current,
                        post_start_ns=old['start_ns'], post_end_ns=old['end_ns'],
                        time_to_evaluate_ns=began-old['verified_ns'], rollback=rollback)
            self.pending = None
            observer.probe_start_ns = None
            self.streak.clear()
            self.reversals = 0 if keep else self.reversals + int(verdict == 'rolled_back')
            if self.reversals >= self.cfg['maximum_reversals']:
                self.stop('repeated rejected probes')
                self.error = 'oscillation guard: consecutive transfer reversals'
                self.record(result='oscillation_guard', reason=self.error)
            return
        boosted = any(self.current[key][0] > self.base[key][0] for key in self.current)
        if boosted and self.mode == 'active':
            healthy = (not plan['pressure'] or all(
                item['throughput_kbit'] is not None
                and item['throughput_kbit'] < .1 * self.base[self.registry.rows[name]['intent']['priority']][0]
                for name, item in plan['objectives'].items()))
            self.recovery = self.recovery + 1 if healthy else 0
            if self.recovery >= self.cfg['recovery_windows'] and now >= self.next_action:
                target = {name: (self.base[name][0], ceil) for name, (_, ceil) in self.current.items()}
                previous = self.current
                self.write(target)
                self.record(result='released', reason='sustained recovery or idle service',
                            previous=previous, allocation=self.current, objectives=plan['objectives'])
                self.next_action = now + self.cfg['cooldown_seconds']
                self.recovery = 0
                self.streak.clear()
                return
        else:
            self.recovery = 0
        if plan['action'] == 'hold' or now < self.next_action:
            self.streak.clear()
            return
        name = plan.get('service', plan['action'])
        self.streak = {name: self.streak.get(name, 0) + 1}
        if plan['action'] == 'transfer_reservation' and self.streak[name] < self.cfg['enter_windows']:
            return
        if self.actions >= self.cfg['maximum_actions']:
            self.stop('adjustment budget exhausted')
            self.error = 'adjustment budget exhausted, possible oscillation'
            return
        self.actions += 1
        self.next_action = now + self.cfg['cooldown_seconds']
        self.streak.clear()
        if self.mode == 'shadow':
            self.record(result='hypothetical', **plan)
            return
        previous = self.current
        timing = self.write(plan['proposed'])
        self.record(result='pending' if 'service' in plan else 'verified', timing=timing, **plan)
        if 'service' in plan:
            verified = timing['verified_ns']
            start = ((verified + NANO - 1)//NANO + 1)*NANO
            observer.probe_start_ns = start
            self.pending = dict(service=name, allocation=previous, before=plan['objectives'][name],
                                start_ns=start, end_ns=start+seconds*NANO, verified_ns=verified)

    def safe_tick(self):
        try:
            self.tick()
        except Exception as error:
            self.error = str(error)
            self.record(result='controller_fault', reason=self.error)
            try:
                self.stop('controller fault')
            except Exception as rollback:
                self.error += f'; rollback refused or failed: {rollback}'
                self.mode = 'off'
                self.record(result='rollback_failed', reason=self.error)

    def status(self):
        window = self.service.observer.window(time.monotonic_ns(), self.cfg['window_seconds'])
        return dict(mode=self.mode, strategy=self.strategy, allocation=self.current,
                    actions=self.actions, pending=self.pending, error=self.error,
                    allocation_verified=self.error is None and self.mode != 'off', reversals=self.reversals,
                    fairness=self.fairness(window), timing=self.timing)
