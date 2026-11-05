"""Nonprivileged contract tests. Generated numbers here are fixtures, never measurements."""
import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
from research_manifest import load_manifest, validate
from research_report import report
from research_runner import convergence
from research_stats import aggregate, compare, median_ci


class Research(unittest.TestCase):
    def manifest(self):
        return load_manifest(ROOT/'scenarios/chaos-smoke.json')

    def test_validation_and_replay(self):
        m = self.manifest()
        self.assertEqual(validate(json.loads(json.dumps(m))),m)
        for change in (
            lambda x:x.update(topology='host'),
            lambda x:x['network'].update(jitter_ms=100),
            lambda x:x['faults'][0].update(device='eth0'),
            lambda x:x['faults'][0].update(at_seconds=11),
            lambda x:x['workload']['phases'][0].update(seconds=1),
            lambda x:x['workload']['priority_weights'].update(critical=100),
            lambda x:x['faults'][2].update(link_up=1),
            lambda x:x['controller'].update(target_p99_ms=float('nan')),
        ):
            candidate = copy.deepcopy(m)
            change(candidate)
            with self.assertRaises(ValueError):
                validate(candidate)

    def dataset(self, n, tail, failure=0):
        m = self.manifest()
        m.update(repetitions=n,modes=['slo'])
        return dict(version=1,manifest=m,environment=dict(kernel='test',architecture='test',cpus=4,
                    tc='test',git_commit=None),runs=[
            dict(mode='slo',repetition=i,artifact=f'r{i}-slo',valid=True,p99_ms=tail,
                 total_mbps=10,jain_index=.99,cpu_seconds=.1,adjustments_per_minute=1,
                 event_loss=None,offered=100,failed=failure,completed=100-failure) for i in range(n)])

    def test_sign_tests_use_repetitions_and_exclude_censored_tails(self):
        baseline = self.dataset(12,10)
        regression = compare(baseline,self.dataset(12,13))
        tail = regression['findings'][0]
        self.assertEqual(tail['status'],'confirmed_regression')
        self.assertEqual(tail['pairs'],12)
        self.assertLess(tail['adjusted_p'],.05)
        self.assertEqual(compare(self.dataset(2,10),self.dataset(2,30))['findings'][0]['status'],'inconclusive')
        self.assertEqual(compare(baseline,self.dataset(12,13,1))['findings'][0]['pairs'],0)
        other = self.dataset(12,13)
        other['environment']['kernel'] = 'different'
        self.assertEqual(compare(baseline,other)['findings'][0]['status'],'inconclusive')
        self.assertIsNone(median_ci([1,2]))
        self.assertEqual(median_ci([1]*6)['low'],1)
        self.assertEqual(aggregate(baseline)['slo']['metrics']['p99_ms']['variance'],0)

    def test_report_files_and_invalid_run(self):
        data = self.dataset(1,10)
        data['runs'][0].update(valid=False,error='<script>alert(1)</script>')
        with tempfile.TemporaryDirectory(dir=ROOT/'build') as work:
            p = Path(work)/'summary.json'
            p.write_text(json.dumps(data))
            page = report(p)
            self.assertIn('&lt;script&gt;',page.read_text())
            self.assertNotIn('<script>alert',page.read_text())
            self.assertTrue((p.parent/'report.json').is_file())
            self.assertTrue((p.parent/'report.csv').is_file())

    def test_convergence_is_not_the_first_verdict(self):
        m = self.manifest()
        start = 20
        allocation = {'critical':[3000,10000]}
        def sample(seconds, pending=False):
            return dict(time_ns=int(seconds*1e9),state=dict(mode='active',pending=pending,error=None,allocation=allocation),
                        control_window={'critical':{'application':dict(samples=40,p99_us=10000)}})
        faults = [dict(applied_ns=22*10**9)]
        self.assertIsNone(convergence([sample(24)],[],faults,m,start,'slo'))
        self.assertIsNone(convergence([sample(24),sample(26,True)],[],faults,m,start,'slo'))
        self.assertEqual(convergence([sample(24),sample(26)],[],faults,m,start,'slo'),4)


if __name__ == '__main__':
    unittest.main()
