"""The Python request driver uses the same native feasibility and pressure core."""
import sys
import tempfile
import time
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))

from overload_runtime import Backpressure, assess
from deadline_workload import AdmissionWorkload
from deadline_runtime import evidence as read_deadline_evidence
from service_config import CLASSES


class OverloadRuntime(unittest.TestCase):
    def test_assessment_preserves_both_payload_directions_and_processing_cost(self):
        now = time.monotonic_ns()
        work = dict(deadline_ns=now+100_000_000, request_bytes=4096, response_bytes=64,
                    processing_p50_us=2000, processing_p95_us=5000, processing_min_us=1000,
                    observed_request_rate_bytes_s=800_000)
        evidence = dict(observed_ns=now, scope_verified=True, available_bytes_s=1_000_000,
            backlog_bytes=0, rtt_p50_us=5000, rtt_p95_us=10000,
            response_available_bytes_s=10_000_000, response_backlog_bytes=0,
            verified_max_bytes_s=1_000_000, verified_response_max_bytes_s=10_000_000,
            verified_min_rtt_us=3000, active_concurrency=2, concurrency_limit=8,
            queue_depth=0, capacity_bytes_s=1_000_000, background_floor_bytes_s=200_000,
            background_goodput_bytes_s=200_000, response_path_verified=True,
            operator_fairness_verified=True)
        result = assess(work, evidence, now)
        self.assertEqual(result['feasibility'], 'FEASIBLE')
        self.assertEqual(result['recommendation'], 'ACCEPT')
        self.assertGreaterEqual(result['predicted_p95_us'], 15_000)
        self.assertEqual(result['fairness'], 'floor_observed')
        slower = assess(dict(work, observed_request_rate_bytes_s=100_000), evidence, now)
        self.assertGreater(slower['predicted_p95_us'], result['predicted_p95_us'])

    def test_unknown_and_hard_infeasible_are_distinct(self):
        now = time.monotonic_ns()
        work = dict(deadline_ns=now+10_000_000, request_bytes=1024*1024,
                    response_bytes=64, processing_p50_us=2000, processing_p95_us=5000,
                    processing_min_us=1000)
        evidence = dict(observed_ns=now, scope_verified=True, available_bytes_s=1_000_000,
            backlog_bytes=0, rtt_p50_us=5000, rtt_p95_us=10000,
            response_available_bytes_s=10_000_000, response_backlog_bytes=0,
            verified_max_bytes_s=100_000, verified_response_max_bytes_s=10_000_000,
            verified_min_rtt_us=3000, active_concurrency=2, concurrency_limit=8, queue_depth=0,
            capacity_bytes_s=1_000_000, background_floor_bytes_s=200_000,
            background_goodput_bytes_s=200_000, response_path_verified=True,
            operator_fairness_verified=True)
        result = assess(work, evidence, now)
        self.assertEqual(result['feasibility'], 'INFEASIBLE')
        self.assertEqual(result['recommendation'], 'REJECT_RECOMMENDED')
        evidence['observed_ns'] = now-3_000_000_000
        result = assess(work, evidence, now)
        self.assertEqual(result['recommendation'], 'UNKNOWN')
        self.assertTrue(result['stale_evidence'])

    def test_backpressure_hysteresis_budget_and_gradual_recovery(self):
        now = time.monotonic_ns()
        controller = Backpressure(2, 8, 16)
        try:
            healthy = dict(sampled_ns=now, capacity_bytes_s=12_000_000,
                delivered_bytes_s=4_000_000, backlog_bytes=0, active_concurrency=2, queue_depth=0,
                offered_requests=100, admitted_requests=96, rejected_requests=4,
                completed_requests=96, failed_requests=0, deadline_misses=0,
                background_goodput_bytes_s=200_000, background_floor_bytes_s=150_000,
                scope_verified=True, counters_complete=True, operator_fairness_verified=True)
            self.assertEqual(controller.observe(healthy, now)['state'], 'NORMAL')
            pressure = dict(healthy, delivered_bytes_s=11_500_000, backlog_bytes=32_000,
                            active_concurrency=8, queue_depth=4)
            pressure['sampled_ns'] += 250_000_000
            self.assertEqual(controller.observe(pressure, pressure['sampled_ns'])['state'],
                             'APPROACHING_CAPACITY')
            pressure['sampled_ns'] += 250_000_000
            update = controller.observe(pressure, pressure['sampled_ns'])
            self.assertEqual(update['action'], 'REDUCE_CONCURRENCY')
            self.assertEqual(update['recommended_concurrency'], 6)
            self.assertEqual(update['admission_budget_per_second'], 12)
            recovery = dict(healthy, sampled_ns=pressure['sampled_ns']+1_250_000_000,
                            active_concurrency=1)
            controller.observe(recovery, recovery['sampled_ns'])
            for _ in range(2):
                recovery['sampled_ns'] += 1_100_000_000
                update = controller.observe(recovery, recovery['sampled_ns'])
            self.assertEqual(update['action'], 'INCREASE_CONCURRENCY')
            self.assertEqual(update['recommended_concurrency'], 7)
            self.assertEqual(update['fairness'], 'floor_observed')
        finally:
            controller.close()

    def test_admission_rate_budget_and_closed_lifecycle_accounting(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = dict(minimum_concurrency=1, maximum_concurrency=8,
                           maximum_admissions_per_second=4, tight_every=10,
                           tight_deadline_ms=1)
            admission = AdmissionWorkload(Path(directory), profile, 'deadline-overload-v2',
                                          deadline_ms=50, payload=128)
            stamp = time.monotonic_ns()
            self.assertTrue(admission._consume_admission_budget(stamp, 4))
            self.assertFalse(admission._consume_admission_budget(stamp, 4))
            self.assertTrue(admission._consume_admission_budget(stamp+250_000_000, 4))

            old = time.monotonic()
            admission.offer(1, old-1.0)
            admission.admission_result(1, True, 'ACCEPT', 50)
            admission.terminal(1, 'TimeoutError', 80)
            closed = admission.recent_accounting(time.monotonic_ns())
            self.assertTrue(closed['counters_complete'])
            self.assertEqual((closed['offered_requests'], closed['admitted_requests'],
                              closed['rejected_requests'], closed['failed_requests'],
                              closed['deadline_misses']), (1, 1, 0, 1, 1))

    def test_lab_evidence_requires_owned_fresh_readback_and_reports_gap(self):
        now = time.monotonic_ns()

        class Observer:
            sampled_ns = now
            errors = []
            truncated = False

            @staticmethod
            def window(_now, _seconds):
                return dict(rtt=dict(p50_us=5000, p95_us=10_000), queue={
                    'critical': dict(bytes_per_second=700_000),
                    'interactive': dict(bytes_per_second=100_000),
                    'standard': dict(bytes_per_second=50_000),
                    'background': dict(bytes_per_second=150_000)})

        class Service:
            lab = object()
            observer = Observer()
            profile = dict(bandwidth_kbit=8000)
            telemetry = dict(qdisc=[dict(root=True, kind='htb', handle='1:')] + [
                dict(parent=f'1:{minor}', backlog=0) for minor in CLASSES.values()],
                **{'class': [{'class': 'htb', 'handle': '1:1', 'rate': 1_000_000,
                              'ceil': 1_000_000}] + [
                    {'class': 'htb', 'handle': f'1:{minor}', 'rate': 200_000,
                     'ceil': 800_000} for minor in CLASSES.values()],
                    'traffic_classes': {name: dict(handle=f'1:{minor}')
                                        for name, minor in CLASSES.items()}})

        service = Service()
        result = read_deadline_evidence(service, now)
        self.assertEqual(service.deadline_evidence_status['reason'], 'class_scoped_readback')
        self.assertTrue(result['critical']['class_scoped'])
        self.assertFalse(result['critical']['response_path_verified'])

        service.telemetry['qdisc'][0]['kind'] = 'fq_codel'
        self.assertEqual(read_deadline_evidence(service, now), {})
        self.assertEqual(service.deadline_evidence_status['reason'], 'unsupported_qdisc_root')


if __name__ == '__main__':
    unittest.main()
