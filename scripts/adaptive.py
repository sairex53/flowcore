"""Deterministic, single-writer adaptive controller for the owned HTB lab."""
import json
import time
import uuid
from collections import deque

import adaptive_tc
from intents import Registry
from predictive import Predictor, HORIZON
from intelligence import Allocator, State, estimate, score

NANO = 10**9
LATENCY_MIN_SAMPLES = 100


def static(*_):
    return False


def threshold(slo, pressure, _intent):
    return pressure and slo['state'] != 'insufficient_data'


def slo_aware(slo, pressure, _intent):
    return pressure and slo['outside_deadband']


STRATEGIES = {'static': static, 'threshold': threshold, 'slo': slo_aware,
              'queue-aware': threshold, 'slo-aware': slo_aware, 'hybrid': slo_aware, 'predictive': slo_aware}
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
    def __init__(self, service, *, egress_gate=None):
        self.service = service
        self.egress_gate = egress_gate
        self.session = uuid.uuid4().hex
        self.registry = Registry(service.observer)
        self.allocator = Allocator()
        self.state = State()
        self.last_plan = None
        self.predictor = Predictor()
        self.cfg = service.config.get('adaptive', DEFAULT)
        self.history = deque(maxlen=128)
        self.freeze_until = 0
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
            try:
                from durable_state import read_json
                rows = read_json(self.path)
                if not isinstance(rows,list) or len(rows)>128 or any(not isinstance(row,dict) for row in rows):
                    raise ValueError('journal shape bound')
                self.history.extend(rows)
            except (OSError,ValueError,UnicodeError):
                # This audit trail never grants activation or ownership. Empty/corrupt
                # files after a power loss must not prevent safe read-only startup.
                self.service.log('error',detail='adaptive journal invalid; history unavailable; controller remains disabled')

    def record(self, **fields):
        if incident := getattr(self.service, 'incident', None):
            incident.decision(fields)
        self.history.append(dict(controller_session=self.session,time_unix=time.time(), time_ns=time.monotonic_ns(), generation=self.service.observer.generation, **fields))
        payload = json.dumps(list(self.history))
        while len(payload.encode())>262144 and self.history:
            self.history.popleft()
            payload = json.dumps(list(self.history))
        if self.path:
            try:
                from durable_state import atomic_json
                atomic_json(self.path,list(self.history))
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
        shared = strategy in ('queue-aware','slo-aware','hybrid','predictive')
        classes = [row['intent']['priority'] for row in self.registry.rows.values()]
        if not shared and len(classes) != len(set(classes)):
            raise ValueError('legacy strategy requires exclusive traffic classes')
        self.registry.shared_classes = shared
        self.stop('mode change')
        self.predictor = Predictor()
        self.state.move('OBSERVING','explicit activation')
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
        changed = self.current != target
        self.current = dict(target)
        if changed and (incident := getattr(self.service, 'incident', None)):
            incident.policy_generation += 1
            incident.recorder.record('controller', timing['verified_ns'], result='kernel_verified',
                                     generation=incident.policy_generation, changed=True)
        self.service.poll_metrics()
        return dict(timing, refresh_ns=time.monotonic_ns()-timing['verified_ns'])

    def stop(self, reason):
        self.freeze_until = 0
        if self.mode == 'active' and self.current != self.base and self.service.lab:
            self.write(self.base)
            self.record(result='restored_baseline', reason=reason, allocation=self.base)
        self.mode = 'off'
        self.pending = None
        self.state.move('DISABLED',reason)
        self.service.observer.probe_start_ns = None

    def detach(self):
        self.freeze_until = 0
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
        peers = [row['intent'] for name,row in self.registry.rows.items()
                 if name != intent['name'] and row['intent']['priority'] == intent['priority']]
        if self.service.lab and self.policy()['scheduler'] == 'htb':
            capacity = self.capacity()
            cls = self.policy()['classes'][intent['priority']]
            ceiling = min([capacity*cls['max_pct']//100,
                           *(item['max_kbit'] for item in [*peers,intent] if item['max_kbit'])])
            floor = sum(item['min_kbit'] for item in [*peers,intent])
            if floor > min(ceiling,capacity*cls['min_pct']//100):
                raise ValueError('aggregate service minimums exceed the class reservation')
        return self.registry.register(intent, uid, pid)

    def prediction_timing(self):
        confirmation = self.cfg['window_seconds']*(self.cfg['enter_windows']-1)
        sample_delay = max(.75,self.service.config.get('telemetry_seconds',1))
        return dict(horizon_seconds=HORIZON,confirmation_seconds=confirmation,
                    sample_interval_seconds=sample_delay,
                    proactive_feasible=confirmation+sample_delay<HORIZON)

    def sample_prediction(self):
        if self.mode == 'off' or self.strategy != 'predictive':
            return
        observer = self.service.observer
        stamp = observer.queue_time
        if stamp is None:
            return
        window = observer.window(stamp, self.cfg['window_seconds'], complete=True)
        application = {}
        for name,row in self.registry.rows.items():
            cls = row['intent']['priority']
            measured = objective(window,name,row['intent'],self.cfg['minimum_samples'])
            application[name] = dict(traffic_class=cls,**measured)
        allocation = tuple((cls,*self.current[cls]) for cls in sorted(self.current))
        try:
            self.predictor.observe(stamp,self.service.telemetry,self.capacity(),
                {cls:rate for cls,(rate,_) in self.current.items()},application,window.get('rtt'),allocation,
                rtt_variation=window.get('tcp_distributions',{}).get('rttvar_us'))
        except (OSError,ValueError,RuntimeError,AttributeError,TypeError,OverflowError) as error:
            self.predictor.invalidate(str(error))

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
            bounds[cls] = (rate, min(bounds[cls][1],cap))
            measured = outcomes[name] = objective(window, name, intent, cfg['minimum_samples'])
            queue = window['queue'].get(cls, {})
            attribution[name] = dict(backlogged_samples=queue.get('backlogged_samples', 0),
                                     borrowed=queue.get('borrowed'), guaranteed_kbit=rate,
                                     eligible=queue.get('backlogged_samples', 0) >= 2
                                     and (measured['throughput_kbit'] or 0) >= .9*rate
                                     and not measured['failures']
                                     and measured['samples'] >= max(LATENCY_MIN_SAMPLES, cfg['minimum_samples']))
        predictive = (dict(experimental=True,error=self.predictor.error,timing=self.prediction_timing(),
            forecasts={cls:{k:v for k,v in methods.get('hybrid',{}).items() if k!='input'}
                       for cls,methods in self.predictor.latest.items()})
                      if self.strategy == 'predictive' else None)
        detail = dict(previous=current, proposed=bounds, objectives=outcomes, attribution=attribution, pressure=pressure,
                      background_floor_kbit=bg_floor, predictive=predictive, background_throughput_kbit=(
                          bg['bytes_per_second']/125 if bg.get('bytes_per_second') is not None else None))
        if bounds != current:
            return dict(detail, action='enforce_class_ceiling')
        if current['background'][0] < bg_floor:
            return dict(detail, proposed=self.base, action='restore_background_floor')
        if self.strategy in ('queue-aware','slo-aware','hybrid','predictive'):
            observations, scores, owners = {}, {}, {}
            maxima = {cls:min(ceil,self.base[cls][0]+capacity*cfg['maximum_boost_pct']//100)
                      for cls,(_,ceil) in current.items()}
            for name,row in sorted(self.registry.rows.items()):
                intent = row['intent']
                cls = intent['priority']
                measured = outcomes[name]
                observation = estimate(window,name,intent,measured,current[cls][0],cfg['minimum_samples'])
                observations[name] = observation
                weight = 4 if cls == 'critical' else 2 if cls == 'interactive' else 1
                utility = score('hybrid' if self.strategy == 'predictive' else self.strategy,observation,measured,weight,
                                (current[cls][0]-self.base[cls][0])/max(1,capacity*cfg['step_pct']//100))
                if self.strategy == 'predictive':
                    forecast = self.predictor.latest.get(cls,{}).get('hybrid')
                    age = (time.monotonic_ns()-forecast['time_ns'])/NANO if forecast else None
                    if (self.prediction_timing()['proactive_feasible'] and
                            forecast and age is not None and 0 <= age < 1.5 and
                            forecast['actionable'] and forecast['lead_seconds']-age > .1 and
                            observation['eligible']):
                        utility = max(utility,weight*forecast['risk']/(1+max(0,
                            (current[cls][0]-self.base[cls][0])/max(1,capacity*cfg['step_pct']//100))))
                        observations[name]['proactive'] = True
                    observations[name]['prediction'] = ({k:v for k,v in forecast.items() if k!='input'}
                                                         if forecast else None)
                    observations[name]['effective_strategy'] = ('predictive' if observations[name].get('proactive')
                                                               else 'hybrid-reactive-fallback')
                if utility > scores.get(cls,0):
                    scores[cls], owners[cls] = utility, name
            desired = sum(row['intent']['desired_kbit'] for row in self.registry.rows.values())
            if desired > capacity:
                return dict(detail,action='hold',feasible=False,
                            conflict='aggregate desired throughput exceeds link capacity',estimator=observations)
            detail.update(estimator=observations, constraints=dict(capacity_kbit=capacity,
                          background_floor_kbit=bg_floor,maxima=maxima),
                          optimization='weighted deficit with diminishing boost utility')
            try:
                allocation = self.allocator.allocate(capacity,current,maxima,bg_floor,
                                                     capacity*cfg['step_pct']//100,scores)
            except ValueError as error:
                return dict(detail,action='hold',feasible=False,conflict=str(error))
            if allocation:
                cls,target = allocation
                return dict(detail,proposed=target,action='transfer_reservation',service=owners[cls],
                            feasible=True,prediction='reduce target class queueing while preserving background reservation')
            return dict(detail,action='hold',feasible=True)
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

    def qualify(self, plan):
        if plan['action'] == 'transfer_reservation' and self.egress_gate is not None and not self.egress_gate():
            return dict(plan, action='hold', proposed=None,
                        bottleneck_gate='fresh controllable egress pressure required')
        return plan

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
            return dict(self.qualify(self.plan(window, current)), dry_run=True, fairness=self.fairness(window))
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
        now_ns = time.monotonic_ns()
        verdict_due = self.pending is not None and now_ns >= self.pending['end_ns'] + NANO//4
        if now - self.last_tick < seconds and not verdict_due:
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
        plan = self.qualify(self.plan(window, self.current))
        self.last_plan = plan
        if self.strategy in ('queue-aware','slo-aware','hybrid','predictive') and plan['action'] == 'hold':
            self.record(result='hold', **plan)
        self.timing = dict(time_ns=began, aggregation_ns=window_end-began,
                           policy_ns=time.monotonic_ns()-window_end,
                           telemetry_age_ns=max(0, began-observer.sampled_ns),
                           attribution=plan['attribution'],feasible=plan.get('feasible',True))
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
            evaluation_start = time.monotonic_ns()
            post = observer.window(old['end_ns'], seconds, complete=True)
            aggregated = time.monotonic_ns()
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
            other_harm = False
            for peer,row in self.registry.rows.items():
                if peer == old['service']:
                    continue
                after = objective(post,peer,row['intent'],self.cfg['minimum_samples'])
                prior = old.get('peer_before',{}).get(peer,{})
                if after['failures'] or (after['samples'] >= LATENCY_MIN_SAMPLES
                        and prior.get('samples',0) >= LATENCY_MIN_SAMPLES
                        and after['p99_us'] > prior['p99_us']*1.1):
                    other_harm = True
            keep = (comparable and not degraded and not other_harm and (latency_improved or throughput_improved)
                    and (not demanded or (bg is not None and bg >= plan['background_floor_kbit']*.8)))
            verdict = 'kept' if keep else 'rolled_back' if comparable else 'inconclusive'
            assessed = time.monotonic_ns()
            rollback = None
            if not keep:
                rollback = self.write(old['allocation'])
                self.next_action = time.monotonic() + max((3+self.reversals)*seconds, self.cfg['cooldown_seconds'])
            self.record(result=verdict, service=old['service'], comparable=comparable, degraded=degraded,
                        before=before, measured=measured, background_kbit=bg, allocation=self.current,
                        post_start_ns=old['start_ns'], post_end_ns=old['end_ns'],
                        time_to_evaluate_ns=began-old['verified_ns'],
                        verdict_lag_ns=max(0,began-old['end_ns']-NANO//4),
                        post_aggregation_ns=aggregated-evaluation_start,
                        post_policy_ns=assessed-aggregated, rollback=rollback,
                        prediction=old.get('prediction'))
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
        if self.mode == 'observe':
            self.record(result='observed', **plan)
            return
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
            observer.probe_start_ns = verified
            self.pending = dict(service=name, allocation=previous, before=plan['objectives'][name],
                                start_ns=start, end_ns=start+seconds*NANO, verified_ns=verified,
                                peer_before=plan['objectives'], prediction=plan.get('predictive'))

    def verify_lab_baseline(self):
        capacity, policy = self.capacity(), self.policy()
        if not self.service.lab or policy['scheduler'] != 'htb':
            raise ValueError('restore verification requires owned HTB')
        expected = {name:(capacity*row['min_pct']//100,capacity*row['max_pct']//100)
                    for name,row in policy['classes'].items()}
        if adaptive_tc.read(self.service.lab,capacity,policy) != expected:
            raise RuntimeError('kernel allocation differs from configured baseline')

    def freeze_for_lab(self, seconds):
        if not self.service.research or self.mode != 'active' or self.pending or self.freeze_until or not self.service.lab:
            raise ValueError('freeze requires a settled active owned lab controller')
        if adaptive_tc.read(self.service.lab, self.capacity(), self.policy()) != self.current:
            raise RuntimeError('freeze refused after allocation drift')
        self.freeze_until = time.monotonic()+seconds
        self.record(result='lab_policy_frozen')

    def safe_tick(self):
        try:
            if self.freeze_until:
                self.registry.expire()
                if time.monotonic() < self.freeze_until and self.registry.revision == self.revision:
                    return
                self.stop('bounded diagnostic freeze ended')
            prior = self.pending
            self.tick()
            if self.mode == 'off':
                if self.error:
                    self.state.move('SAFE_FALLBACK',self.error)
                elif self.state.name != 'DISABLED':
                    self.state.move('DISABLED','controller off')
            elif self.pending:
                if self.state.name not in ('ADJUSTING','EVALUATING'):
                    if self.state.name != 'CONGESTED':
                        self.state.move('CONGESTED','eligible class pressure')
                    self.state.move('ADJUSTING','verified incremental update')
                self.state.move('EVALUATING','fixed post-change interval')
            elif prior:
                self.state.move('RECOVERING','probe verdict recorded')
            elif self.timing.get('feasible') is False:
                self.state.move('DEGRADED','infeasible operator objectives')
            elif self.state.name in ('OBSERVING','HEALTHY','CONGESTED','RECOVERING','DEGRADED'):
                if self.state.name == 'DEGRADED':
                    self.state.move('OBSERVING','reassessing feasibility')
                observations = self.timing.get('attribution', {})
                target = 'CONGESTED' if any(row.get('eligible') for row in observations.values()) else 'HEALTHY'
                self.state.move(target,'sampled queue and application evidence')
        except Exception as error:
            self.error = str(error)
            if self.state.name == 'DISABLED':
                self.state.move('OBSERVING','fault assessment')
            self.state.move('SAFE_FALLBACK',self.error)
            self.record(result='controller_fault', reason=self.error)
            try:
                self.stop('controller fault')
            except Exception as rollback:
                self.error += f'; rollback refused or failed: {rollback}'
                self.mode = 'off'
                self.record(result='rollback_failed', reason=self.error)

    def status(self):
        window = self.service.observer.window(time.monotonic_ns(), self.cfg['window_seconds'])
        return dict(controller_session=self.session,mode=self.mode, strategy=self.strategy, allocation=self.current,
                    actions=self.actions, pending=self.pending, error=self.error,
                    allocation_verified=self.error is None and self.mode != 'off',
                    prediction=self.predictor.status() if self.strategy == 'predictive' else None,
                    predictive_timing=self.prediction_timing() if self.strategy == 'predictive' else None,
                    engine_state=self.state.name, state_reason=self.state.reason,
                    last_decision=self.last_plan, reversals=self.reversals,
                    lab_frozen_until_ns=int(self.freeze_until*NANO) if self.freeze_until else None,
                    fairness=self.fairness(window), timing=self.timing)
