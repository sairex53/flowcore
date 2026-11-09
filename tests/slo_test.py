"""Deterministic objective, evidence and budget regressions."""
import copy
import csv
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
import policy_optimizer
import slo_optimizer
from benchmark_v2 import account
from durable_state import read_json
from research_manifest import load_manifest
from service_config import load
from slo_optimizer import LIMITS, hybrid_tradeoff, profile, resource_conflicts, solve
from slo_report import choose

BUILD = Path(os.environ.get('FLOWCORE_SLO_TEST_BUILD', ROOT/'build'))
slo_optimizer.BINDIR = BUILD

POLICY = profile(read_json(ROOT/'config/slo-optimization.json'))
BASE = dict(critical_p99_ms=50, deadline_miss_ratio=.01, success_ratio=1, background_mbps=6,
            service_mbps=2, fairness=.95, utilization=.98, cpu_pct=8, rss_mib=32,
            updates_per_min=0, recovery_seconds=None)


def blocks(row):
    return {seed: dict(row) for seed in range(12)}


class SloOptimization(unittest.TestCase):
    def test_native_front_has_tradeoffs_and_excludes_dominated_policy(self):
        objectives = {name: POLICY['objectives'][name] for name in ('critical_p99_ms', 'background_mbps')}
        front, scores = solve(dict(static=BASE, fast=dict(BASE, critical_p99_ms=25, background_mbps=5),
                                  worse=dict(BASE, critical_p99_ms=80, background_mbps=4)), objectives)
        self.assertEqual(front, ['static', 'fast'])
        self.assertLess(scores['fast'], scores['worse'])
        with self.assertRaises(ValueError):
            solve({str(i): BASE for i in range(9)}, objectives)

    def test_guard_prevents_weighted_choice_from_sacrificing_background(self):
        rows = dict(static=blocks(BASE), fast=blocks(dict(BASE, critical_p99_ms=20, background_mbps=5)))
        result = choose(rows, {name: [] for name in rows}, POLICY)
        pair = result['comparisons'][0]
        self.assertEqual(pair['relation'], 'MEASURED_TRADEOFF')
        self.assertEqual(pair['baseline_guardrails_unproven'], ['background_mbps'])
        self.assertEqual(result['recommendation']['mode'], 'static')
        self.assertEqual(set(result['evidence_front']), set(rows))

    def test_confirmed_choice_and_noise(self):
        rows = dict(static=blocks(BASE), fast=blocks(dict(BASE, critical_p99_ms=38)))
        blockers = {name: [] for name in rows}
        self.assertEqual(choose(rows, blockers, POLICY)['recommendation']['mode'], 'fast')
        for index in range(6):
            rows['fast'][index]['critical_p99_ms'] = 65
        self.assertEqual(choose(rows, blockers, POLICY)['recommendation']['mode'], 'static')
        blockers['fast'] = [dict(code='INSUFFICIENT_MEASUREMENT')]
        self.assertEqual(choose(rows, blockers, POLICY)['eligible'], ['static'])

    def test_budget_fallback(self):
        ticks = iter((0, 200_000_000, 200_000_000))
        report = choose({'static': blocks(BASE)}, {'static': []}, POLICY, cpu_clock=lambda: next(ticks))
        self.assertEqual(report['recommendation']['status'], 'SOLVER_BUDGET_EXCEEDED')

    def test_capacity_bounds_and_many_conflicting_intents(self):
        resources = copy.deepcopy(POLICY['resources'])
        baseline = dict(critical=(3600, 12000), background=(3600, 12000),
                        interactive=(2400, 12000), standard=(2400, 12000))
        self.assertFalse(resource_conflicts(12000, resources, baseline))
        self.assertTrue(resource_conflicts(7000, resources, baseline))
        resources['classes']['background']['floor_pct'] = 60
        self.assertIn('PROTECTED_SHARES_OVERCOMMIT',
                      {item['code'] for item in resource_conflicts(7000, resources)})
        cfg = load(ROOT/'config/service.json')
        intents = {f'critical-{i}': dict(owner_uid=1000, monotonic_expiry=3,
                    intent=dict(priority='critical', min_kbit=40, max_kbit=0, background_min_pct=20))
                   for i in range(128)}
        controller = SimpleNamespace(capacity=lambda: 12000, cfg=cfg['adaptive'],
             policy=lambda: cfg['policies']['default'], registry=SimpleNamespace(rows=intents))
        model = policy_optimizer.contracts(controller, 2)
        self.assertIsNone(model['fallback'])
        self.assertIn('SERVICE_MINIMA_EXCEED_AUTHORIZED_RESERVATION',
                      {item['code'] for item in model['conflicts']})

    def test_hybrid_guard_keeps_slo_and_bulk_in_same_score(self):
        before = dict(p99_us=40_000, goodput=700_000)
        wins_latency = dict(before, p99_us=35_000, goodput=400_000)
        guard = dict(objectives={'critical_p99_ms': {'weight': 1, 'scale': 50},
                                 'background_mbps': {'weight': 5, 'scale': 4}}, min_score_gain=.01)
        # Live samples carry all fields even when weights select a subset.
        for row in (before, wins_latency):
            row.update(success_ratio=1, deadline_miss_ratio=0, fairness=1, utilization=.9,
                       cpu_seconds=.01, interval_seconds=1)
        self.assertFalse(hybrid_tradeoff(before, [wins_latency]*2, guard)['gain_met'])

    def test_recovery_requires_complete_healthy_windows(self):
        with tempfile.TemporaryDirectory(dir=BUILD) as tmp:
            path = Path(tmp)/'requests.csv'
            with path.open('w') as stream:
                writer = csv.writer(stream)
                writer.writerow(('id', 'scheduled', 'completed', 'latency_ms', 'outcome', 'traffic_class',
                                 'service', 'dispatch_lag_ms'))
                for index in range(20):
                    scheduled = 1+index//5+(index%5)*.1
                    writer.writerow((index, scheduled, scheduled+.01, 10, 'ok', 'critical', 'demo', 0))
            self.assertEqual(account(path, 20, 0, 5, 20, 100, recovery_start=1)['slo_recovery']['seconds'], 2)
            self.assertEqual(account(path, 20, 0, 2.99, 20, 100, recovery_start=1)['slo_recovery']['status'],
                             'right_censored')

    def test_profile_is_strict_and_scenarios_are_valid(self):
        with self.assertRaises(ValueError):
            profile(dict(POLICY, solver_cpu_ms=0))
        with self.assertRaises(ValueError):
            profile(dict(POLICY, objectives={'rtt_us': {'weight': 1, 'scale': 1}}))
        for scenario in ('paired', 'recovery', 'live'):
            self.assertTrue(load_manifest(ROOT/'scenarios'/f'slo-optimization-{scenario}.json')['hybrid_v3'])
        self.assertEqual(set(BASE), set(LIMITS))


if __name__ == '__main__':
    unittest.main()
