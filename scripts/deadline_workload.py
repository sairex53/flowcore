"""Cooperative admission adapter for the existing offered-load workload."""
import collections
import time

from deadline_runtime import MISSING, estimate
from durable_state import read_json


class AdmissionWorkload:
    def __init__(self, output, profile, mode, deadline_ms, payload):
        self.path = output/'pacing-input.json'
        self.profile, self.mode, self.deadline_ms, self.payload = profile, mode, deadline_ms, payload
        self.gating = mode in ('deadline-admission', 'deadline-adaptive')
        self.history = collections.defaultdict(lambda: collections.deque(maxlen=64))
        self.last_read = 0
        self.inputs = {}
        self.counts = collections.Counter()
        self.risk, self.considered = 0, 0
        self.cpu_ns = 0

    def budget_ms(self, index):
        return self.profile['tight_deadline_ms'] if index % self.profile['tight_every'] == 0 else self.deadline_ms

    def admission(self, scheduled, index, traffic_class, *, first=True):
        began = time.process_time_ns()
        now = time.monotonic_ns()
        if now-self.last_read >= 100_000_000:
            self.last_read = now
            try:
                self.inputs = read_json(self.path, limit=8192).get('deadline_evidence', {})
            except (ValueError, OSError, AttributeError):
                self.inputs = {}
        observation = dict(self.inputs.get(traffic_class, {}))
        history = self.history[traffic_class]
        while history and now-history[0][0] > 2_500_000_000:
            history.popleft()
        if len(history) >= 8:
            samples = sorted(us for _, us in history)
            observation['transfer_p95_us'] = samples[(95*len(samples)+99)//100-1]
        budget_ns = round(self.budget_ms(index)*1_000_000)
        work = dict(deadline_ns=round(scheduled*10**9)+budget_ns, egress_bytes=self.payload,
                    processing_us=0, connect_us=self.profile['connect_reserve_us'], remote_us=0,
                    reliability=0, expiration=0)
        result = estimate(work, observation, now)
        if first:
            self.counts[result['decision']] += 1
            if traffic_class == 'critical' and result['queue_us'] != MISSING:
                self.considered += 1
                self.risk += result['decision'] in ('DEFER','ACCEPT_WITH_RISK') and result['queue_us'] >= 2000
        self.cpu_ns += time.process_time_ns()-began
        return result

    def completed(self, traffic_class, start_ns, finish_ns):
        self.history[traffic_class].append((finish_ns, max(1, (finish_ns-start_ns)//1000)))

    def pressure(self):
        active = self.considered >= 12 and self.risk*4 >= self.considered
        self.risk, self.considered = 0, 0
        return active

    def summary(self):
        return dict(mode=self.mode, decisions=dict(self.counts), estimator_cpu_ms=self.cpu_ns/1e6)
