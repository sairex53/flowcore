"""Cooperative admission adapter for the existing offered-load workload."""
import collections
import time

from deadline_runtime import MISSING, estimate
from durable_state import read_json
from overload_runtime import Backpressure, assess as assess_v2


class AdmissionWorkload:
    def __init__(self, output, profile, mode, deadline_ms, payload):
        self.path = output/'pacing-input.json'
        self.profile, self.mode, self.deadline_ms, self.payload = profile, mode, deadline_ms, payload
        self.gating = mode in ('deadline-admission', 'deadline-adaptive',
                               'deadline-concurrency-static', 'deadline-overload-v2')
        self.history = collections.defaultdict(lambda: collections.deque(maxlen=64))
        self.last_read = 0
        self.inputs = {}
        self.counts = collections.Counter()
        self.risk, self.considered = 0, 0
        self.cpu_ns = 0
        self.backpressure = (Backpressure(profile['minimum_concurrency'], profile['maximum_concurrency'],
                                          profile['maximum_admissions_per_second'])
                             if mode == 'deadline-overload-v2' else None)
        self.backpressure_states = collections.Counter()
        self.backpressure_actions = collections.Counter()
        self.last_recommended_concurrency = profile.get('maximum_concurrency')
        self.last_admission_budget = profile.get('maximum_admissions_per_second', 1)
        self.lifecycle = {}
        self.admission_tokens = 0.0
        self.admission_token_stamp_ns = 0
        self.controller_input_errors = 0
        self.assessment_input_errors = 0
        self.controller_input_error_reasons = collections.Counter()
        self.assessment_input_error_reasons = collections.Counter()

    def offer(self, request_id, scheduled):
        self.lifecycle[request_id] = dict(scheduled_ns=round(scheduled*1e9), budget_ms=None,
                                          admission=None, outcome=None, latency_ms=None)

    def admission_result(self, request_id, admitted, recommendation, budget_ms):
        row = self.lifecycle.get(request_id)
        if row is not None:
            row.update(admission=bool(admitted), recommendation=recommendation,
                       budget_ms=float(budget_ms))

    def terminal(self, request_id, outcome, latency_ms):
        row = self.lifecycle.get(request_id)
        if row is not None:
            row.update(outcome=outcome, latency_ms=float(latency_ms))

    def _consume_admission_budget(self, now_ns, rate_per_second):
        """Apply a bounded burst token bucket to cooperative admission."""
        rate = max(1, int(rate_per_second))
        burst = max(1.0, min(float(rate), rate * 0.25))
        if not self.admission_token_stamp_ns:
            self.admission_tokens = burst
            self.admission_token_stamp_ns = now_ns
        else:
            elapsed = max(0, now_ns-self.admission_token_stamp_ns)/1e9
            self.admission_tokens = min(burst, self.admission_tokens+elapsed*rate)
            self.admission_token_stamp_ns = max(self.admission_token_stamp_ns, now_ns)
        if self.admission_tokens < 1.0:
            return False
        self.admission_tokens -= 1.0
        return True

    def recent_accounting(self, now):
        # Do not feed an open request window to the controller as if it were a
        # complete denominator. A half-second close delay leaves room for the
        # ordinary request deadline before the window can affect pressure.
        rows = [row for row in self.lifecycle.values()
                if now-2_500_000_000 <= row['scheduled_ns'] <= now-500_000_000]
        complete = bool(rows) and all(row['admission'] is not None and row['outcome'] is not None for row in rows)
        if not complete:
            return dict(offered_requests=0, admitted_requests=0, rejected_requests=0,
                        completed_requests=0, failed_requests=0, deadline_misses=0,
                        counters_complete=False)
        admitted = [row for row in rows if row['admission']]
        completed = [row for row in admitted if row['outcome'] == 'ok']
        misses = [row for row in admitted if row['outcome'] != 'ok' or
                  row['latency_ms'] > row['budget_ms']]
        return dict(offered_requests=len(rows), admitted_requests=len(admitted),
                    rejected_requests=len(rows)-len(admitted), completed_requests=len(completed),
                    failed_requests=len(admitted)-len(completed), deadline_misses=len(misses),
                    counters_complete=True)

    def budget_ms(self, index):
        return self.profile['tight_deadline_ms'] if index % self.profile['tight_every'] == 0 else self.deadline_ms

    def admission(self, scheduled, index, traffic_class, *, first=True, active_concurrency=0):
        began = time.process_time_ns()
        now = time.monotonic_ns()
        if now-self.last_read >= 100_000_000:
            self.last_read = now
            try:
                pacing_input = read_json(self.path, limit=8192)
                self.inputs = pacing_input.get('deadline_evidence', {})
                self.evidence_status = pacing_input.get('deadline_evidence_status', {})
            except (ValueError, OSError, AttributeError):
                self.inputs = {}
                self.evidence_status = dict(available=False, reason='invalid_pacing_input')
        observation = dict(self.inputs.get(traffic_class, {}))
        history = self.history[traffic_class]
        while history and now-history[0][0] > 2_500_000_000:
            history.popleft()
        budget_ns = round(self.budget_ms(index)*1_000_000)
        if self.mode == 'deadline-concurrency-static':
            rejected = active_concurrency >= self.profile['static_concurrency_limit']
            result = dict(decision='REJECT_RECOMMENDED' if rejected else 'ACCEPT',
                          reason='static_concurrency_limit' if rejected else 'within_static_limit',
                          feasibility='AT_RISK' if rejected else 'FEASIBLE',
                          active_concurrency=active_concurrency,
                          recommended_concurrency=self.profile['static_concurrency_limit'])
            if first:
                self.counts[result['decision']] += 1
            self.cpu_ns += time.process_time_ns()-began
            return result
        if self.mode == 'deadline-overload-v2':
            observation = dict(self.inputs.get(traffic_class, {}))
            limit = self.last_recommended_concurrency or self.profile['maximum_concurrency']
            queue_depth = max(0, active_concurrency-limit+1)
            accounting = self.recent_accounting(now)
            sample = dict(sampled_ns=observation.get('observed_ns', 0),
                          capacity_bytes_s=observation.get('capacity_bytes_s', 0),
                          delivered_bytes_s=observation.get('observed_rate_bytes_s') or 0,
                          backlog_bytes=observation.get('backlog_bytes') or 0,
                          active_concurrency=active_concurrency, queue_depth=queue_depth,
                          background_goodput_bytes_s=observation.get('background_goodput_bytes_s') or 0,
                          background_floor_bytes_s=observation.get('background_floor_bytes_s') or 0,
                          scope_verified=bool(observation.get('owned_egress') and
                                              observation.get('class_scoped') and
                                              observation.get('observed_rate_bytes_s') is not None),
                          operator_fairness_verified=bool(observation.get('operator_fairness_verified')))
            sample.update(accounting)
            try:
                pressure = self.backpressure.observe(sample, now)
            except ValueError as error:
                # Telemetry is advisory. A malformed sample must disable control
                # for this observation rather than abort application work.
                self.controller_input_errors += 1
                self.controller_input_error_reasons[str(error)] += 1
                pressure = dict(state='INSUFFICIENT_DATA', action='HOLD',
                    recommended_concurrency=limit, admission_budget_per_second=self.last_admission_budget,
                    available_admission_slots=0, changed=False)
            self.last_recommended_concurrency = pressure['recommended_concurrency']
            self.last_admission_budget = pressure['admission_budget_per_second']
            self.backpressure_states[pressure['state']] += 1
            self.backpressure_actions[pressure['action']] += 1
            request = dict(deadline_ns=round(scheduled*1e9)+budget_ns,
                           request_bytes=self.payload,
                           response_bytes=self.profile['response_bytes'],
                           processing_p50_us=self.profile['processing_p50_us'],
                           processing_p95_us=self.profile['processing_p95_us'],
                           processing_min_us=None, prefer_reliable=False)
            path = dict(observation, active_concurrency=active_concurrency,
                        concurrency_limit=pressure['recommended_concurrency'],
                        queue_depth=queue_depth, scope_verified=sample['scope_verified'])
            try:
                result = assess_v2(request, path, now)
            except ValueError as error:
                self.assessment_input_errors += 1
                self.assessment_input_error_reasons[str(error)] += 1
                result = dict(feasibility='INSUFFICIENT_EVIDENCE', recommendation='UNKNOWN',
                              decision='UNKNOWN', reason='invalid_assessment_evidence')
            if not self._consume_admission_budget(now, pressure['admission_budget_per_second']):
                result.update(decision='DEFER', recommendation='DEFER',
                              reason='adaptive_admission_rate_budget',
                              recommended_concurrency=pressure['recommended_concurrency'])
            elif (pressure['state'] in ('OVERLOADED', 'CAPACITY_COLLAPSE') and
                    active_concurrency >= pressure['recommended_concurrency']):
                result.update(decision='DEFER', recommendation='DEFER',
                              reason='cooperative_backpressure_budget',
                              recommended_concurrency=pressure['recommended_concurrency'])
            else:
                result['decision'] = result['recommendation']
                result['recommended_concurrency'] = pressure['recommended_concurrency']
            result['backpressure_state'] = pressure['state']
            result['admission_budget_per_second'] = pressure['admission_budget_per_second']
            result['active_concurrency'] = active_concurrency
            if first:
                self.counts[result['decision']] += 1
                self.considered += int(result.get('predicted_p95_us') is not None)
                self.risk += int(result['decision'] in ('DEFER', 'ACCEPT_WITH_RISK'))
            self.cpu_ns += time.process_time_ns()-began
            return result

        if len(history) >= 8:
            samples = sorted(us for _, us in history)
            observation['transfer_p95_us'] = samples[(95*len(samples)+99)//100-1]
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
        result = dict(mode=self.mode, decisions=dict(self.counts), estimator_cpu_ms=self.cpu_ns/1e6)
        offered = list(self.lifecycle.values())
        admitted = [row for row in offered if row['admission'] is True]
        completed = [row for row in admitted if row['outcome'] == 'ok']
        result['request_accounting'] = dict(offered=len(offered), admitted=len(admitted),
            rejected=sum(row['admission'] is False for row in offered),
            completed=len(completed), failed=sum(row['outcome'] not in (None, 'ok') for row in admitted),
            deadline_misses=sum(row['outcome'] != 'ok' or row['latency_ms'] > row['budget_ms']
                                for row in admitted if row['outcome'] is not None),
            terminal=sum(row['outcome'] is not None for row in offered),
            complete=all(row['admission'] is not None and row['outcome'] is not None for row in offered))
        if self.backpressure:
            result.update(backpressure_states=dict(self.backpressure_states),
                          backpressure_actions=dict(self.backpressure_actions),
                          final_recommended_concurrency=self.last_recommended_concurrency,
                          final_admission_budget_per_second=self.last_admission_budget,
                          controller_input_errors=self.controller_input_errors,
                          assessment_input_errors=self.assessment_input_errors,
                          controller_input_error_reasons=dict(self.controller_input_error_reasons),
                          assessment_input_error_reasons=dict(self.assessment_input_error_reasons),
                          deadline_evidence_status=getattr(self, 'evidence_status',
                                                           dict(available=False, reason='not_observed')))
            self.backpressure.close()
            self.backpressure = None
        return result
