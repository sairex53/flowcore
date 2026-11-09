"""Hybrid V3's shared planner, bounded observation and outcome coordinator."""
import ctypes as c
from functools import cache
import json
from pathlib import Path
import tempfile
import time

from durable_state import read_json
from service_config import keys, number

NANO = 10**9
LIMITS = ('unknown', 'headroom', 'socket', 'class_ceiling', 'link')
ACTIONS = ('hold', 'release_socket', 'shift_class')
FIELDS = ('capacity', 'background_rate', 'background_ceiling', 'critical_rate', 'critical_ceiling',
          'cap_sum', 'cap_min', 'background_floor', 'background_goodput', 'wire_rate',
          'critical_wire_rate', 'critical_backlog', 'p99_us', 'target_us', 'sockets', 'fresh', 'demand')


def config(value):
    keys(value, ('background_floor_pct', 'max_goodput_regression_pct', 'cooldown_seconds',
                 'maximum_actions', 'initial_cap_bytes_s'))
    for key, low, high in (('background_floor_pct', 20, 60), ('max_goodput_regression_pct', 1, 30),
                           ('cooldown_seconds', 2, 10), ('maximum_actions', 1, 3)):
        number(value[key], low, high, True)
    rate = value['initial_cap_bytes_s']
    number(rate, 0, 1_500_000, True)
    if 0 < rate < 50_000:
        raise ValueError('Hybrid V3 startup cap is below the socket SDK floor')
    return value


@cache
def bridge():
    from pacing_workload import library, U64
    fn = library().fc_hybrid_plan_v1
    fn.argtypes = [c.POINTER(U64), c.POINTER(U64)]
    fn.restype = None
    return fn


def plan(frame):
    from pacing_workload import U64
    keys(frame, FIELDS)
    for key in FIELDS[:-2]:
        number(frame[key], 0, 10**12, True)
    if any(type(frame[key]) is not bool for key in FIELDS[-2:]):
        raise ValueError('fresh and demand require booleans')
    out = (U64*5)()
    bridge()((U64*len(FIELDS))(*(int(frame[key]) for key in FIELDS)), out)
    action = ACTIONS[out[1]]
    change = {}
    if action == 'shift_class':
        change = dict(critical_rate=frame['critical_rate']+out[4],
                      background_rate=frame['background_rate']-out[4])
    elif action == 'release_socket':
        change = dict(per_socket_cap=1_500_000)
    return dict(action=action, limiting_actuator=LIMITS[out[0]], headroom_bytes_s=out[2],
                background_upper_bytes_s=out[3], current=dict(frame), proposed=change,
                expectation={'hold': 'retain verified policy', 'release_socket': 'permit more bulk progress',
                             'shift_class': 'test more critical reservation under sustained pressure'}[action],
                risks=['request latency', 'deadline attainment', 'background goodput', 'fairness',
                       'utilization', 'CPU and policy transition cost'] if change else [],
                evidence='local measurement and configured limits; dry-run')


def publish(path, payload):
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, prefix='.hybrid-', delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(payload, stream, allow_nan=False)
            stream.close()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def sockets(directory):
    try:
        return read_json(directory/'hybrid-sockets.json', limit=8192)
    except (OSError, ValueError):
        return {}


class Coordinator:
    def __init__(self, directory, limits, target_us, window_seconds):
        self.directory, self.limits = directory, config(limits)
        self.target_us = target_us
        self.evaluation_seconds = max(6, 2*window_seconds+2)
        self.previous = self.requested = self.pending = None
        self.last_scope = self.last_sample = None
        self.streak = self.actions = self.rollbacks = 0
        self.cooldown = 0
        self.frozen = False

    def record(self, event, **value):
        with (self.directory/'hybrid-v3.jsonl').open('a') as stream:
            stream.write(json.dumps(dict(event=event, time_ns=time.monotonic_ns(), **value), allow_nan=False)+'\n')

    def observe(self, value, scope, receiver, socket_state, capacity):
        now = time.monotonic_ns()
        auth = value.get('hybrid', {})
        alloc = auth.get('allocation', {})
        if not alloc:
            return None, None
        caps = socket_state.get('caps', [])
        cohort = socket_state.get('cohort')
        scope_key = auth['revision'], cohort
        stamp = value.get('sample_ns')
        if scope_key != self.last_scope:
            self.streak, self.previous, self.last_sample = 0, None, None
            self.last_scope = scope_key
        tc = value['tc']
        qdiscs = tc.get('qdisc', [])
        root = next((q for q in qdiscs if q.get('root')), {})
        critical = next((q for q in qdiscs if q.get('parent')=='1:10'), {})
        critical_class = tc.get('traffic_classes', {}).get('critical', {})
        critical_bytes = critical_class.get('stats', critical_class).get('bytes')
        rows = [row['application'] for name, row in value['control_window'].items()
                if name.startswith('critical') and row.get('application')]
        tails = [r['p99_us'] for r in rows if r.get('p99_us') is not None and r.get('samples', 0) >= 10]
        requests = sum(row.get('samples', 0) for row in rows)
        failures = sum(row.get('failure', 0)+row.get('timeout', 0) for row in rows)
        deadlines = sum(row.get('deadline_reported', 0) for row in rows)
        misses = sum(row.get('deadline_miss', 0) for row in rows)
        current = (stamp, root.get('bytes'), critical_bytes, receiver, value['overhead'])
        previous = self.previous
        # Keep endpoints on real sample boundaries, even when runner polls faster.
        if not stamp or stamp == self.last_sample:
            return None, None
        self.previous, self.last_sample = current, stamp
        if not previous or not receiver or not previous[3] or not previous[0]:
            return None, None
        elapsed = stamp-previous[0]
        rx_elapsed = receiver['time_ns']-previous[3]['time_ns']
        if not (.65*NANO <= elapsed <= 1.7*NANO and .65*NANO <= rx_elapsed <= 1.7*NANO):
            return None, None
        rates = [(b-a)*NANO//rx_elapsed for a, b in zip(previous[3]['flows'], receiver['flows'])]
        selected = [rates[i] for i in socket_state.get('flows', []) if 0 <= i < len(rates)]
        bg = sum(selected)
        jain = bg*bg/(len(selected)*sum(r*r for r in selected)) if bg and selected else None
        wire, critical_rate = (
            (end-start)*NANO//elapsed if type(start) is int and type(end) is int else -1
            for start, end in zip(previous[1:3], current[1:3]))
        fresh = bool(scope['verified'] and caps and len(caps)==len(selected) and min(rates, default=-1)>=0
                     and min(wire, critical_rate, bg)>=0 and requests >= 12 and not failures and deadlines >= 12
                     and socket_state.get('revision') == auth['revision']
                     and 0 <= now-socket_state.get('time_ns', 0) <= 2*NANO)
        obs = dict(capacity=capacity*125, background_rate=alloc['background'][0]*125,
                   background_ceiling=alloc['background'][1]*125, critical_rate=alloc['critical'][0]*125,
                   critical_ceiling=alloc['critical'][1]*125, cap_sum=sum(caps), cap_min=min(caps, default=0),
                   background_floor=capacity*self.limits['background_floor_pct']//100*125,
                   background_goodput=max(0,bg), wire_rate=max(0,wire), critical_wire_rate=max(0,critical_rate),
                   critical_backlog=critical.get('backlog', 0), p99_us=max(tails, default=0),
                   target_us=self.target_us, sockets=len(caps), fresh=fresh, demand=bool(socket_state.get('demand')))
        app_begin, app_end = value['control_bounds_ns']
        outcome = dict(p99_us=obs['p99_us'], goodput=bg, fairness=jain, utilization=wire/obs['capacity'],
                       failure_count=failures, deadline_miss_ratio=misses/deadlines if deadlines else None,
                       receiver_begin_ns=previous[3]['time_ns'],
                       app_begin_ns=app_begin, app_end_ns=app_end,
                       cohort=cohort, revision=auth['revision'],
                       cpu_seconds=sum(current[4][key]-previous[4][key]
                                       for key in ('daemon_cpu_seconds', 'child_cpu_seconds')))
        return obs, outcome

    def tick(self, value, scope, receiver, socket_state, capacity, commit, *, allow=True, start_new=True):
        now = time.monotonic_ns()
        auth = value.get('hybrid', {})
        obs, outcome = self.observe(value, scope, receiver, socket_state, capacity)
        if self.requested:
            if (release := socket_state.get('release')) and release['id'] == self.requested['id']:
                self.pending = dict(action='release_socket', verified=release['verified_ns'],
                                    before=self.requested['before'], windows=[])
                self.actions += 1
                self.record('verified', action='release_socket', **release)
                self.requested = None
            elif not allow or now-self.requested['id'] >= 3*NANO:
                self.record('socket_command_expired', id=self.requested['id'])
                self.requested = None
                self.cooldown = now+3*NANO
            else:
                return self.requested['command']
        if self.pending:
            before = self.pending['before']
            verified = self.pending['verified']
            if obs and obs['fresh'] and outcome['cohort'] == before['cohort'] and min(
                    outcome['receiver_begin_ns'], outcome['app_begin_ns']) >= verified and (
                    not self.pending['windows'] or outcome['app_begin_ns'] >= self.pending['windows'][-1]['app_end_ns']):
                self.pending['windows'].append(outcome)
            if (len(self.pending['windows']) < 2 and now-verified < self.evaluation_seconds*NANO and allow
                    and (not outcome or not outcome['failure_count'])):
                return None
            self.evaluate_pending(auth, obs, commit)
            return None
        if not obs:
            self.streak = 0
            return None
        began = time.process_time_ns()
        proposal = plan(obs)
        decision_cpu = time.process_time_ns()-began
        candidate = proposal['action']
        blockers = [reason for blocked, reason in (
            (candidate == 'hold', 'no_supported_adjustment'),
            (self.frozen, 'safe_fallback'),
            (not allow or not start_new, 'evaluation_window_unavailable'),
            (now < self.cooldown, 'cooldown'),
            (self.actions >= self.limits['maximum_actions'], 'action_budget_exhausted'),
            (auth.get('boosted'), 'class_probe_active')) if blocked]
        if blockers:
            self.streak = 0
        else:
            self.streak = self.streak+1 if getattr(self, 'candidate', None) == candidate else 1
            self.candidate = candidate
            if self.streak < 2:
                blockers.append('awaiting_second_observation')
        self.record('plan', proposal=proposal, outcome=outcome, blockers=blockers,
                    decision_cpu_ns=decision_cpu)
        if blockers:
            return None
        self.streak = 0
        if candidate == 'release_socket':
            command = dict(id=now, revision=auth['revision'], cohort=outcome['cohort'],
                           caps=socket_state['caps'])
            self.requested = dict(id=now, command=command, before=outcome)
            return command
        if min(socket_state['caps']) < obs['capacity']:
            return None
        result = commit('shift', auth['revision'], value['sample_ns'], socket_state)
        if result:
            self.actions += 1
            self.pending = dict(action='shift_class', verified=result['timing']['verified_ns'],
                                before=outcome, windows=[])
            self.record('verified', action='shift_class', **result)
        return None

    def evaluate_pending(self, auth, obs, commit):
        pending = self.pending
        self.pending = None
        before, after = pending['before'], pending['windows']
        floor = (obs or {}).get('background_floor', 0)
        minimum = max(floor, before['goodput']*(100-self.limits['max_goodput_regression_pct'])/100)
        safe = len(after) == 2 and all(
            row['goodput'] >= minimum and row['p99_us'] <= before['p99_us']*1.10 and
            row['deadline_miss_ratio'] <= before['deadline_miss_ratio']+.02 and
            row['fairness'] is not None and before['fairness'] is not None and row['fairness'] >= before['fairness']-.05
            and row['utilization'] >= before['utilization']-.05
            and row['cpu_seconds'] <= before['cpu_seconds']+.05 for row in after)
        better = safe and all(row['p99_us'] <= before['p99_us']*.95 if pending['action']=='shift_class'
                              else row['goodput'] >= before['goodput']*1.05 for row in after)
        verdict = ('inconclusive' if len(after) != 2 else 'adverse' if not safe else
                   'observed_benefit' if better else 'no_observed_benefit')
        self.record('outcome', action=pending['action'], guard_passed=safe, benefit_observed=better,
                    verdict=verdict, before=before, after=after)
        if pending['action'] == 'shift_class' and auth.get('boosted'):
            result = commit('restore', auth['revision'], 0, None)
            self.rollbacks += 1
            self.record('restored_static', reason='bounded trial expired' if better else 'unproven or adverse', **result)
        self.frozen = not better
        self.cooldown = time.monotonic_ns()+self.limits['cooldown_seconds']*NANO

    def reset(self):
        self.previous = self.requested = self.pending = None
        self.last_scope = self.last_sample = None
        self.streak = 0
        self.cooldown = time.monotonic_ns()+self.limits['cooldown_seconds']*NANO


def plan_file(path):
    return plan(read_json(path, limit=8192))


def inspect(path):
    from collections import Counter
    from nextgen_report import read_records

    records = read_records(path)
    if not records:
        raise ValueError('empty or missing Hybrid V3 journal')
    events = Counter()
    latest = None
    for row in records:
        if not isinstance(row, dict) or type(row.get('event')) is not str:
            raise ValueError('invalid Hybrid V3 journal row')
        events[row['event']] += 1
        if row['event'] == 'plan':
            latest = row
    return dict(schema=1, records=len(records), events=dict(events), latest_plan=latest,
                recent=records[-16:])
