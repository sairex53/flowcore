"""Deterministic planner and evaluation regressions using the native bridge."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
import pacing_workload
from hybrid_runtime import NANO, Coordinator, inspect, plan
from research_manifest import validate

TEST_BUILD = Path(os.environ.get('FLOWCORE_HYBRID_TEST_BUILD', ROOT/'build'))
pacing_workload.BINDIR = TEST_BUILD

LIMITS = dict(background_floor_pct=20, max_goodput_regression_pct=10, cooldown_seconds=3,
              maximum_actions=2, initial_cap_bytes_s=400_000)
SLACK = dict(capacity=1_500_000, background_rate=450_000, background_ceiling=1_500_000,
             critical_rate=450_000, critical_ceiling=1_500_000, cap_sum=400_000, cap_min=400_000,
             background_floor=300_000, background_goodput=398_000, wire_rate=550_000,
             critical_wire_rate=120_000, critical_backlog=0, p99_us=30_000, target_us=50_000,
             sockets=1, fresh=True, demand=True)
PRESSURE = dict(SLACK, cap_min=1_500_000, cap_sum=9_000_000, sockets=6, wire_rate=1_480_000,
                critical_wire_rate=470_000, critical_backlog=20_000, p99_us=70_000)


class Hybrid(unittest.TestCase):
    def test_redundant_socket_cap_requires_demand_and_healthy_slo(self):
        proposal = plan(SLACK)
        self.assertEqual(proposal['limiting_actuator'], 'socket')
        self.assertEqual(proposal['background_upper_bytes_s'], 400_000)
        self.assertEqual(proposal['headroom_bytes_s'], 950_000)
        self.assertEqual(proposal['proposed'], {'per_socket_cap': 1_500_000})
        for overrides in (dict(demand=False), dict(background_goodput=100_000),
                          dict(p99_us=51_000), dict(p99_us=0), dict(fresh=False)):
            self.assertEqual(plan(dict(SLACK, **overrides))['action'], 'hold')

    def test_operator_ceiling_kept(self):
        proposal = plan(dict(SLACK, background_ceiling=480_000, cap_sum=500_000, cap_min=500_000,
                             background_goodput=460_000))
        self.assertEqual((proposal['limiting_actuator'], proposal['action']), ('class_ceiling', 'hold'))

    def test_class_probe_excludes_binding_socket_caps_and_respects_floor(self):
        self.assertEqual(plan(PRESSURE)['proposed'], dict(critical_rate=525_000, background_rate=375_000))
        for overrides in (dict(cap_min=400_000), dict(background_floor=400_000),
                          dict(critical_backlog=0), dict(wire_rate=900_000), dict(critical_ceiling=490_000)):
            self.assertEqual(plan(dict(PRESSURE, **overrides))['action'], 'hold')

    def test_capacity_scope_and_schema(self):
        self.assertEqual(plan(dict(PRESSURE, capacity=875_000))['limiting_actuator'], 'unknown')
        self.assertEqual(plan(dict(PRESSURE, capacity=875_000, background_rate=262_500,
                                   background_ceiling=875_000, critical_rate=262_500,
                                   critical_ceiling=875_000, background_floor=175_000))['proposed'],
                         dict(critical_rate=306_250, background_rate=218_750))
        with self.assertRaises(ValueError):
            plan(dict(SLACK, fresh=1))
        pressure = json.loads((ROOT/'scenarios/hybrid-v3-pressure.json').read_text())
        self.assertEqual(validate(pressure)['hybrid_v3']['initial_cap_bytes_s'], 0)
        for name in ('paired', 'smoke', 'baselines'):
            fixture = json.loads((ROOT/'scenarios'/f'hybrid-v3-{name}.json').read_text())
            self.assertEqual(validate(fixture)['hybrid_v3'], LIMITS)
            fixture['hybrid_v3']['background_floor_pct'] = 31
            with self.assertRaises(ValueError):
                validate(fixture)

    def test_tc_reads_nested_class_counter_and_refuses_missing(self):
        coordinator = Coordinator(None, LIMITS, 50_000, 2)
        auth = dict(revision=2, allocation=dict(background=[3600, 12000], critical=[3600, 12000]))
        cohort = dict(caps=[1_500_000], flows=[0], cohort=1, revision=2, demand=True)
        def sample(second, counter):
            timestamp = second*NANO
            value = dict(hybrid=auth, sample_ns=timestamp, control_bounds_ns=[timestamp-2*NANO, timestamp],
                         tc=dict(qdisc=[dict(root=True, bytes=1_470_000*second),
                                        dict(parent='1:10', backlog=2000)],
                                 traffic_classes={'critical': {'stats': {'bytes': counter}}}),
                         control_window={'critical': {'application':
                             {'p99_us': 60_000, 'samples': 30, 'deadline_reported': 30}}},
                         overhead=dict(daemon_cpu_seconds=.01*second, child_cpu_seconds=0))
            with patch('hybrid_runtime.time.monotonic_ns', return_value=timestamp):
                observation, _ = coordinator.observe(value, {'verified': True},
                    {'time_ns': timestamp, 'flows': [700_000*second]},
                    dict(cohort, time_ns=timestamp), 12000)
            return observation

        self.assertIsNone(sample(4, 1_900_000))
        observation = sample(5, 2_370_000)
        self.assertEqual(observation['critical_wire_rate'], 470_000)
        self.assertEqual(plan(observation)['action'], 'shift_class')
        self.assertFalse(sample(6, None)['fresh'])

    def test_intervention_lease_and_missing_telemetry(self):
        with tempfile.TemporaryDirectory(dir=TEST_BUILD) as work:
            directory = Path(work)
            coordinator = Coordinator(directory, LIMITS, 50_000, 2)
            auth = {'hybrid': {'revision': 1, 'boosted': False}}
            with patch.object(coordinator, 'observe', return_value=(SLACK, {'cohort': 2})):
                for _ in range(3):
                    self.assertIsNone(coordinator.tick(auth, {}, {}, {}, 12000, None, start_new=False))
            row = inspect(directory/'hybrid-v3.jsonl')['latest_plan']
            self.assertIn('evaluation_window_unavailable', row['blockers'])
            degraded = dict(SLACK, fresh=False)
            with patch.object(coordinator, 'observe', return_value=(degraded, {'cohort': 2})):
                self.assertIsNone(coordinator.tick(auth, {}, {}, {}, 12000, None))
            self.assertEqual(inspect(directory/'hybrid-v3.jsonl')['latest_plan']['proposal']['action'], 'hold')

    def test_outcome_guard_and_compensation(self):
        def outcome(**values):
            return dict(dict(p99_us=60_000, goodput=400_000, deadline_miss_ratio=.02,
                             fairness=.99, utilization=.98, cpu_seconds=.1), **values)

        with tempfile.TemporaryDirectory(dir=TEST_BUILD) as work:
            directory = Path(work)
            for after, useful in (([outcome(p99_us=55_000)]*2, True),
                                  ([outcome(p99_us=55_000)], False),
                                  ([outcome(p99_us=55_000, goodput=320_000)]*2, False)):
                coordinator = Coordinator(directory, LIMITS, 50_000, 2)
                coordinator.pending = dict(action='shift_class', before=outcome(), windows=after)
                calls = []
                def commit(*args):
                    calls.append(args)
                    return dict(revision=4)
                coordinator.evaluate_pending({'revision': 3, 'boosted': True},
                                             {'background_floor': 300_000}, commit)
                self.assertEqual(calls, [('restore', 3, 0, None)])
                journal = inspect(directory/'hybrid-v3.jsonl')
                self.assertEqual(journal['recent'][-2]['benefit_observed'], useful)
                self.assertEqual(coordinator.frozen, not useful)


if __name__ == '__main__':
    unittest.main()
