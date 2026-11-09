"""A bounded supervisor for owned HTB class canaries and read-only shadows."""
from collections import deque
from copy import copy
import os
import subprocess
import time

import adaptive_tc
from predictive import Predictor
from service_config import keys, number
from strategy_registry import CLASS, STRATEGIES, guards, inventory, names

NANO = 10**9
RECOVERY_SECONDS = 5
FAULTS = (OSError, RuntimeError, ValueError, TypeError, KeyError, subprocess.SubprocessError)


def metrics(window, intents):
    rows = [window['application'].get(name, {}).get('application', {}) for name in intents]
    attempts = sum(row.get('ok', 0)+row.get('failure', 0)+row.get('timeout', 0) for row in rows)
    failures = sum(row.get('failure', 0)+row.get('timeout', 0) for row in rows)
    deadlines = sum(row.get('deadline_reported', 0) for row in rows)
    misses = sum(row.get('deadline_miss', 0) for row in rows)
    tails = [row['p99_us'] for row in rows if row.get('samples', 0) >= 10 and row.get('p99_us') is not None]
    root = window['queue'].get('root', {})
    background = window['queue'].get('background', {}).get('bytes_per_second')
    return dict(attempts=attempts, failures=failures, failure_ratio=failures/attempts if attempts else None,
                deadlines=deadlines, misses=misses, deadline_miss_ratio=misses/deadlines if deadlines else None,
                worst_service_p99_ms=max(tails)/1000 if tails else None, tail_services=len(tails),
                utilization=root.get('utilization'), backlog_bytes=window['backlog'].get('p95_bytes'),
                background_kbit=background/125 if background is not None else None)


def verdict(row, baseline, limits):
    if row['attempts'] < limits['minimum_samples']:
        return 'insufficient', 'application_samples'
    if row['failure_ratio'] > limits['max_failure_ratio']:
        return 'hard', 'failure_rate'
    if limits['max_deadline_miss_ratio'] < 1 and row['deadlines'] < limits['minimum_samples']:
        return 'insufficient', 'deadline_samples'
    if row['deadlines'] >= limits['minimum_samples'] and row['deadline_miss_ratio'] > limits['max_deadline_miss_ratio']:
        return 'hard', 'deadline_misses'
    if (row['background_kbit'] is None or row['worst_service_p99_ms'] is None
            or row['utilization'] is None):
        return 'insufficient', 'missing_guard_metrics'
    if row['background_kbit'] < limits['minimum_background_kbit']:
        return 'hard', 'background_goodput'
    if row['worst_service_p99_ms'] > limits['max_p99_ms']:
        return 'hard', 'absolute_p99'
    if baseline is not None:
        if row['tail_services'] != baseline['tail_services'] or abs(row['utilization']-baseline['utilization']) > .25:
            return 'insufficient', 'regime_changed'
        if row['worst_service_p99_ms'] > baseline['worst_service_p99_ms']*(1+limits['p99_regression']):
            return 'soft', 'relative_p99'
    return 'pass', None


class Engine:
    def __init__(self, service, clock=time.monotonic_ns):
        self.service, self.clock = service, clock
        self.shadow = {}
        self.shadow_until = 0
        self.shadow_generation = None
        self.last_shadow = None
        self.proposals = {}
        self.events = deque(maxlen=48)
        self.state = 'IDLE'
        self.experiment = None
        self.recovery_until = 0
        self.cached_inventory = None
        self.shadow_evaluations = self.shadow_cpu_ns = 0
        self.switches = self.regressions = 0

    def registry(self, refresh=False):
        if self.cached_inventory is None or refresh:
            self.cached_inventory = inventory(self.service)
        return self.cached_inventory

    def record(self, event, **detail):
        self.events.append(dict(time_ns=self.clock(), event=event, **detail))
        self.service.log('strategy_'+event, **detail)

    def subscribe(self, candidates, seconds):
        candidates = names(candidates, CLASS)
        number(seconds, 1, 60, True)
        if candidates and not self.owned():
            raise ValueError('class shadows require an owned HTB lab')
        self.shadow = {name: Predictor() if name=='predictive' else None for name in candidates}
        self.shadow_generation = self.service.observer.generation
        self.shadow_until = self.clock()+seconds*NANO
        self.proposals = {}
        self.last_shadow = None
        self.cached_inventory = None
        return self.status()

    def owned(self):
        s = self.service
        return bool(s.lab and s.controller.policy()['scheduler']=='htb' and s.observer.cfg['enabled'])

    def detach(self):
        if self.experiment is not None:
            self.record('topology_removed', previous=self.state, rollback_verified=False)
        self.state, self.experiment = 'IDLE', None
        self.shadow.clear()
        self.proposals = {}
        self.cached_inventory = None

    def scope(self):
        return (self.service.lab, self.service.observer.generation,
                self.service.controller.capacity(), self.service.controller.registry.revision)

    def arm(self, candidate, scope, seconds, limits, confirmed):
        limits = guards(limits)
        number(seconds, 8, 45, True)
        if (confirmed is not True or scope != 'owned-fc0' or not self.owned()
                or not self.service.research or os.geteuid()!=0):
            raise PermissionError('canary requires explicit privileged Research Lab owned-fc0 authority')
        if not isinstance(candidate,str) or candidate not in CLASS or candidate == 'static' or not self.registry(refresh=True)[candidate]['canary']:
            raise ValueError('strategy is unavailable for class canary')
        if self.experiment or self.state=='FAULTED' or self.clock() < self.recovery_until:
            raise ValueError('canary already active, faulted or cooling down')
        controller = self.service.controller
        if controller.mode != 'off':
            raise ValueError('canary cannot take over a running controller')
        seconds_window = controller.cfg['window_seconds']
        if seconds_window > 8 or seconds < 3*seconds_window+2:
            raise ValueError('lease must fit baseline, wash-in and candidate windows')
        self.service.lab.assert_owned_sender()
        controller.verify_lab_baseline()
        allocation = controller.policy()['classes']
        if (allocation['background']['min_pct'] < limits['background_floor_pct']
                or controller.cfg['background_floor_pct'] < limits['background_floor_pct']):
            raise ValueError('controller and baseline must protect the requested background floor')
        now = self.clock()
        self.state = 'BASELINE'
        self.experiment = dict(candidate=candidate, scope=self.scope(), guards=limits, until=now+seconds*NANO,
            next_end=((now+NANO-1)//NANO+seconds_window)*NANO, baseline=None, last=None,
            bad=0, missing=0, good=0, started=False)
        self.record('armed', candidate=candidate, lease_seconds=seconds, guards=limits)
        return self.status()

    def rollback(self, reason='operator rollback', regression=False):
        run = self.experiment
        if run is None:
            return self.status()
        controller = self.service.controller
        self.regressions += regression
        self.experiment = None
        try:
            if run['started']:
                if not self.owned() or self.scope()[:3] != run['scope'][:3]:
                    raise RuntimeError('owned topology or capacity changed before rollback')
                self.service.lab.assert_owned_sender()
                controller.stop('strategy canary: '+reason)
                controller.verify_lab_baseline()
                self.switches += 1
            self.state = 'RECOVERING'
            self.recovery_until = self.clock()+RECOVERY_SECONDS*NANO
            self.record('rollback', reason=reason, candidate=run['candidate'], attempted=run['started'],
                        verified=run['started'], clean_windows=run['good'], regression=regression)
        except FAULTS as error:
            # A scope mismatch makes restoring the old allocation unsafe. Freeze
            # controller bookkeeping so its periodic tick cannot write again.
            try:
                controller.detach()
            except FAULTS as freeze_error:
                error = RuntimeError(f'{error}; controller freeze failed: {freeze_error}')
            self.state = 'FAULTED'
            self.record('rollback_refused', reason=reason, candidate=run['candidate'],
                        detail=str(error), verified=False, controller_frozen=controller.mode == 'off')
        return self.status()

    def status(self):
        run = self.experiment
        active = self.service.controller.strategy if self.service.controller.mode == 'active' else 'static'
        return dict(state=self.state, active=None if self.state == 'FAULTED' else active,
            canary=(dict(candidate=run['candidate'], baseline=run['baseline'], last=run['last'],
                         expires_ns=run['until'], clean_windows=run['good']) if run else None),
            shadow=self.proposals, history=list(self.events), switches=self.switches,
            regressions=self.regressions, shadow_evaluations=self.shadow_evaluations,
            shadow_cpu_ns=self.shadow_cpu_ns)

    def observation(self, now):
        observer = self.service.observer
        sample = observer.sampled_ns
        maximum_age = max(2.5, self.service.config['telemetry_seconds']*2.5)*NANO
        return bool(sample and 0 <= now-sample <= maximum_age and not observer.errors and not observer.truncated
                    and not self.service.error and self.service.research_link_up)

    def shadow_tick(self, now):
        if not self.shadow:
            return
        observer = self.service.observer
        if now >= self.shadow_until or self.shadow_generation != observer.generation:
            self.shadow.clear()
            self.proposals = {}
            return
        if not self.owned():
            self.proposals = {}
            return
        fresh = self.observation(now)
        key = (observer.sampled_ns, fresh)
        if key == self.last_shadow:
            return
        self.last_shadow = key
        controller = self.service.controller
        since = time.process_time_ns()
        window = observer.window(now, controller.cfg['window_seconds'], complete=True)
        current = controller.current
        if current is None or controller.mode=='off':
            classes = self.service.telemetry.get('traffic_classes', {})
            current = {name: (row.get('rate', 0)//125,row.get('ceil', 0)//125)
                       for name,row in classes.items() if type(row.get('rate')) is int and type(row.get('ceil')) is int}
        capacity = controller.capacity()
        base = {name:(capacity*row['min_pct']//100,capacity*row['max_pct']//100)
                for name,row in controller.policy()['classes'].items()}
        observed = metrics(window, controller.registry.rows)
        output = {}
        for name, predictor in self.shadow.items():
            value = dict(strategy=name, domain='class', evidence='prediction', sample_ns=observer.sampled_ns,
                         observation=observed, action='hold', proposed=None, expected=None,
                         requirements=['owned-fc0', 'fresh_telemetry', 'background_floor'])
            try:
                if not fresh:
                    raise ValueError('telemetry_unavailable')
                adaptive_tc.validate(capacity,current)
                planner = copy(controller)
                planner.mode, planner.strategy, planner.base, planner.current = 'shadow',name,base,current
                if predictor is not None:
                    planner.predictor = predictor
                    planner.sample_prediction()
                plan = planner.qualify(planner.plan(window,current))
                value.update(action=plan['action'], proposed=plan.get('proposed'), expected=plan.get('prediction'), reason=plan.get('bottleneck_gate') or plan.get('reason'),
                             constraints=dict(capacity_kbit=capacity, background_floor_kbit=plan.get('background_floor_kbit')))
            except FAULTS as error:
                value['reason'] = str(error)
            output[name] = value
        self.proposals = output
        self.shadow_evaluations += len(output)
        self.shadow_cpu_ns += time.process_time_ns()-since

    def tick(self):
        now = self.clock()
        try:
            self.shadow_tick(now)
        except FAULTS as error:
            self.proposals = {}
            self.shadow.clear()
            self.record('shadow_unavailable', detail=str(error))
        run = self.experiment
        if run is None:
            if self.state=='RECOVERING' and now >= self.recovery_until:
                self.state = 'IDLE'
            return
        try:
            self.canary_tick(run, now)
        except FAULTS as error:
            self.record('fault', detail=str(error))
            self.rollback('strategy_fault')

    def canary_tick(self, run, now):
        if not self.owned() or self.scope()!=run['scope']:
            self.rollback('scope_changed')
            return
        controller, observer = self.service.controller, self.service.observer
        if run['started'] and (controller.mode!='active' or controller.strategy!=run['candidate'] or controller.error):
            self.rollback('controller_failed_or_stopped')
            return
        if now >= run['until']:
            self.rollback('lease_expired')
            return
        if not self.observation(now):
            if run['started']:
                self.rollback('telemetry_lost')
            return
        if now < run['next_end']+NANO//4:
            return
        seconds = controller.cfg['window_seconds']
        end = run['next_end']
        if now-end >= (seconds+1)*NANO:
            self.rollback('guard_window_missed')
            return
        run['next_end'] += seconds*NANO
        row = metrics(observer.window(end, seconds, complete=True),controller.registry.rows)
        run['last'] = row
        state, reason = verdict(row,run['baseline'],run['guards'])
        self.record('window', state=self.state, result=state, reason=reason, measurements=row)
        if self.state=='BASELINE':
            if state!='pass':
                if state=='hard':
                    self.rollback('unsafe_baseline_'+reason)
                return
            if now+((seconds*2+1)*NANO) >= run['until']:
                self.rollback('insufficient_lease_after_baseline')
                return
            if row['utilization'] < .75 or not row['backlog_bytes']:
                return
            controller.start('active', STRATEGIES[run['candidate']][1])
            run['started'] = True
            run['baseline'] = row
            self.switches += 1
            self.state = 'CANARY'
            run['next_end'] = ((self.clock()+NANO-1)//NANO+seconds)*NANO
            self.record('switch', active=run['candidate'], evidence='same_run_observation',
                        expected='bounded class reservation changes under observed local pressure')
        elif state=='hard':
            self.rollback(reason, regression=True)
        elif state=='insufficient':
            run['missing'] += 1
            run['bad'] = 0
            if run['missing'] >= 2:
                self.rollback(reason)
        else:
            run['missing'] = 0
            run['bad'] = run['bad']+1 if state=='soft' else 0
            run['good'] += state=='pass'
            if run['bad'] >= 2:
                self.rollback(reason, regression=True)

    def command(self, request):
        op = request['op'].removeprefix('strategy_')
        if op in ('list','inspect','rollback'):
            keys(request,('op',))
            if op=='list':
                return self.registry(refresh=True)
            return self.rollback() if op=='rollback' else self.status()
        if op=='shadow':
            keys(request,('op','names','seconds'))
            return self.subscribe(request['names'],request['seconds'])
        if op=='activate':
            keys(request,('op','name','scope','seconds','guards','confirm'))
            return self.arm(request['name'],request['scope'],request['seconds'],request['guards'],request['confirm'])
        raise ValueError('unsupported strategy command')
