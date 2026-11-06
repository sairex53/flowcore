"""Unprivileged MPTCP policy and experiment-accounting unit tests."""
import math
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))

from multipath_engine import choose_path, summarize_probe, validate_plan
from multipath import _link_up, _request_metrics, _recovery_metrics


def qualities():
    return [
        dict(path_id=1, attempts=5, successes=5, availability=1.0,
             probe_failure_ratio=0.0, rtt_p95_ms=8.0, throughput_mbps=1.5),
        dict(path_id=2, attempts=5, successes=5, availability=1.0,
             probe_failure_ratio=0.0, rtt_p95_ms=20.0, throughput_mbps=10.0),
        dict(path_id=3, attempts=5, successes=4, availability=0.8,
             probe_failure_ratio=0.2, rtt_p95_ms=4.0, throughput_mbps=4.0),
    ]


class MultipathPolicy(unittest.TestCase):
    def test_each_policy_has_explicit_supported_endpoint_semantics(self):
        rows = qualities()
        self.assertEqual(choose_path('linux-default', rows)['backup_path_ids'], [])
        self.assertTrue(choose_path('linux-default', rows)['all_paths_active'])
        self.assertEqual(choose_path('static-endpoints', rows)['chosen_path_id'], 1)
        self.assertEqual(choose_path('latency-preferred', rows)['chosen_path_id'], 3)
        self.assertEqual(choose_path('reliability-preferred', rows)['chosen_path_id'], 1)
        throughput = choose_path('throughput-preferred', rows)
        self.assertEqual(throughput['chosen_path_id'], 2)
        self.assertEqual(set(throughput['backup_path_ids']), {1, 3})
        self.assertFalse(throughput['all_paths_active'])
        aware = choose_path('slo-aware', rows, target_latency_ms=10)
        self.assertEqual(aware['chosen_path_id'], 1)
        self.assertTrue(aware['probe_target_feasible'])
        self.assertIn('not an application-latency guarantee', aware['target_semantics'])

    def test_probe_target_does_not_hide_infeasibility(self):
        plan = choose_path('slo-aware', qualities(), target_latency_ms=3)
        self.assertFalse(plan['probe_target_feasible'])
        self.assertEqual(plan['chosen_path_id'], 3)

    def test_failed_paths_are_excluded_and_every_backup_is_owned(self):
        plan = choose_path('latency-preferred', qualities(), unavailable=[3])
        self.assertEqual(plan['chosen_path_id'], 1)
        self.assertEqual(set(plan['backup_path_ids']), {2, 3})
        with self.assertRaises(ValueError):
            choose_path('latency-preferred', qualities(), unavailable=[99])
        with self.assertRaises(ValueError):
            validate_plan(dict(chosen_path_id=999, backup_path_ids=[]), [1, 2])

    def test_invalid_metrics_and_no_usable_path_are_rejected(self):
        rows = qualities()
        rows[0] = dict(rows[0], throughput_mbps=math.inf)
        with self.assertRaises(ValueError):
            choose_path('throughput-preferred', rows)
        rows = [dict(row, successes=0, availability=0.0, probe_failure_ratio=1.0)
                for row in qualities()]
        with self.assertRaises(ValueError):
            choose_path('linux-default', rows)

    def test_probe_accounting_keeps_failures_and_latency_samples_distinct(self):
        path = dict(id=1, client_interface='fc0', client_address='10.1.0.1',
                    server_address='10.1.0.2', profile={})
        result = summarize_probe(path, [4.0, 7.0], 2, 5, 1000, 2.0)
        self.assertEqual(result['rtt_p95_ms'], 7.0)
        self.assertEqual(result['probe_failure_ratio'], .6)
        self.assertEqual(result['throughput_bytes'], 1000)
        with self.assertRaises(ValueError):
            summarize_probe(path, [1.0], 2, 5, 0, 1.0)

    def test_application_deadline_counts_failed_requests_as_misses(self):
        rows = [
            dict(outcome='ok', latency_ms=5.0),
            dict(outcome='ok', latency_ms=30.0),
            dict(outcome='timeout', latency_ms=100.0),
        ]
        summary = _request_metrics(rows, 20.0)
        self.assertEqual(summary['offered'], 3)
        self.assertEqual(summary['completed'], 2)
        self.assertEqual(summary['failed'], 1)
        self.assertEqual(summary['deadline_misses'], 2)
        self.assertAlmostEqual(summary['deadline_attainment'], 1 / 3)

    def test_link_state_uses_the_correct_namespace(self):
        class Lab:
            def network(self, index):
                interface = 'fc0' if index == 0 else 'fc1'
                return dict(interfaces=[dict(name=interface, up=True, operstate='up', carrier=True)])
        path = dict(client_interface='fc0', server_interface='fc1')
        self.assertTrue(_link_up(Lab(), path, 0))
        self.assertTrue(_link_up(Lab(), path, 1))
        self.assertFalse(_link_up(Lab(), path, 0) and
                         _link_up(Lab(), dict(client_interface='fc0', server_interface='missing'), 1))

    def test_administrative_up_without_carrier_is_not_available(self):
        class Lab:
            def network(self, index):
                return dict(interfaces=[dict(name='fc1', up=True, carrier=False)])
        self.assertFalse(_link_up(Lab(), dict(server_interface='fc1'), 1))

    def test_missing_recovery_events_are_explicit(self):
        result = _recovery_metrics([], [], dict(id=1))
        self.assertIsNone(result['primary_subflow_address_reobserved_ms'])
        self.assertIsNone(result['outage_goodput_mbps'])


if __name__ == '__main__':
    unittest.main()
