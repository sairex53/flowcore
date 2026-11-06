"""Nonprivileged contract tests. Generated numbers here are fixtures, never measurements."""
import copy
import csv
import json
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
from benchmark import receiver_metrics
from benchmark_v2 import account, observed
from research_faults import apply_conditions
from research_manifest import load_manifest, validate
from research_report import report
from research_runner import compose_scenario_network, convergence
from research_minimize import candidates, minimize as run_minimizer, reduce_duration
from research_stats import aggregate, compare, median_ci


class Research(unittest.TestCase):
    def manifest(self):
        return load_manifest(ROOT/'scenarios/chaos-smoke.json')

    def test_receiver_sample_validation(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'build') as work:
            path = Path(work)/'receiver.jsonl'
            def measure(samples, start=0, end=1):
                path.write_text(''.join(json.dumps(dict(time=t, bytes=b))+'\n' for t,b in samples))
                return receiver_metrics(path, start, end)
            self.assertEqual(measure([(0,[0,0]),(1,[125000,125000])])['background_mbps'], 2)
            self.assertEqual(measure([(0,[]),(1,[])])['background_mbps'], 0)
            path.write_text(''.join(json.dumps(dict(time=t,bytes=[0,0],udp_bytes=values))+'\n'
                                         for t,values in ((0,[0]),(1,[1250]))))
            self.assertAlmostEqual(receiver_metrics(path,0,1)['udp_mbps'],.01)
            for invalid in ([(0,[0]),(0,[2])], [(0,[1]),(1,[0])],
                            [(0,[0,0]),(1,[1])], [(0,[0]),(float('nan'),[1])]):
                with self.assertRaises(ValueError):
                    measure(invalid)
            with self.assertRaises(ValueError):
                measure([(0,[0]),(1,[1])], 1, 1)

    def test_failed_fault_removes_lab(self):
        service = Mock(lab=Mock(namespace_fds=[10,11]), research_mode='fq_codel')
        network = self.manifest()["network"]
        with self.assertRaises(ValueError):
            apply_conditions(service, network, 'fifo', 1, True)
        service.close_lab.assert_not_called()
        with patch('research_faults.update_conditions', side_effect=OSError('injected kernel failure')):
            with self.assertRaisesRegex(RuntimeError, 'lab removed'):
                apply_conditions(service, network, 'fq_codel', 1, True)
        service.close_lab.assert_called_once()

    def test_v3_overlapping_events_and_duration_bounds(self):
        manifest = load_manifest(ROOT/'scenarios/chaos-overlap-v3.json')
        composed = compose_scenario_network(manifest['network'], manifest['events'][:5])
        self.assertEqual(composed['bandwidth_kbit'], 3000)
        self.assertEqual(composed['reverse_bandwidth_kbit'], 2000)
        self.assertAlmostEqual(composed['loss_pct'], 8)
        self.assertEqual(composed['loss_correlation_pct'], 55)
        self.assertEqual(composed['base_rtt_ms'], 80)
        shorter = reduce_duration(manifest)
        self.assertIsNotNone(shorter)
        self.assertEqual(sum(stage['seconds'] for stage in shorter['stages']),
                         shorter['duration_seconds'])
        self.assertEqual(sum(phase['seconds'] for phase in shorter['workload']['phases']),
                         shorter['duration_seconds'])
        reductions=list(candidates(manifest))
        operations={operation for _,operation in reductions}
        self.assertTrue({'remove_event','shorten_event','remove_effect','weaken_rate_change',
                         'weaken_impairment','reduce_workload','shorten_scenario'} <= operations)
        with tempfile.TemporaryDirectory(dir=ROOT/'build') as work:
            trials=[]
            def reproduced(candidate,label,parent):
                path=Path(parent)/f'attempt-{len(trials)}'
                path.mkdir()
                trials.append(candidate)
                return path,dict(runs=[dict(valid=True,failed=2,offered=10),
                                       dict(valid=True,failed=2,offered=10)])
            with patch('research_minimize.RESULTS',Path(work)), \
                 patch('research_minimize.owned_directory',side_effect=lambda path:path.mkdir()), \
                 patch('research_minimize.execute',side_effect=reproduced):
                minimized=run_minimizer(manifest,'fq_codel',.1,2)
            self.assertEqual(len(trials),2)
            self.assertLess(minimized['events'],len(manifest['events']))
        conflict = copy.deepcopy(manifest)
        conflict['events'][1].update(at_seconds=6.5,duration_seconds=2,
                                     effects={'loss_pct':2})
        with self.assertRaisesRegex(ValueError,'correlated-loss'):
            validate(conflict)

    def test_v3_report_records_transition_timing(self):
        manifest = load_manifest(ROOT/'scenarios/chaos-overlap-v3.json')
        with tempfile.TemporaryDirectory(dir=ROOT/'build') as work:
            root = Path(work)
            run = root/'r0-fq_codel'
            run.mkdir()
            (run/'timeline.jsonl').write_text(json.dumps(dict(time_ns=10**9,
                tc=dict(qdisc=[],receiver_qdisc=[])))+'\n')
            (run/'transitions.jsonl').write_text(json.dumps(dict(
                scheduled_ns=10**9,actual_application_ns=1001000000,
                kernel_confirmed_ns=1003000000,application_latency_ns=2000000,
                operations=[dict(kind='event_start',id='x')],active_event_ids=['x'],
                status='applied'))+'\n')
            (run/'decisions.json').write_text('[]')
            (run/'requests.csv').write_text('id,scheduled,completed,latency_ms,outcome,traffic_class,service,dispatch_lag_ms\n')
            (run/'receiver.jsonl').write_text('')
            summary = dict(version=3,status='complete',experiment_id='fixture',
                block_seeds=[1,2],manifest=manifest,
                environment=self.dataset(1,10)['environment'],
                runs=[dict(mode='fq_codel',repetition=0,artifact='r0-fq_codel',
                    valid=True,workload_seed=1,offered=10,completed=10,failed=0,
                    p99_ms=10,total_mbps=1,background_mbps=1,jain_index=1,
                    cpu_seconds=0,adjustments_per_minute=0,event_loss=0,
                    start=1,end=2)])
            path=root/'summary.json'
            path.write_text(json.dumps(summary))
            page=report(path)
            self.assertIn('Fault update application latency',page.read_text())
            self.assertIn('transitions.csv',page.read_text())
            self.assertIn('2000000',(run/'transitions.csv').read_text())

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

    def test_v2_paired_gates(self):
        manifest = load_manifest(ROOT/'scenarios/v2-heavy-paired.json')
        data = dict(version=2, status='complete', experiment_id='fixture', block_seeds=list(range(1,11)),
                    manifest=manifest, environment=self.dataset(1,10)['environment'], runs=[])
        for rep in range(10):
            for mode in manifest['modes']:
                data['runs'].append(dict(mode=mode, repetition=rep, workload_seed=rep+1,
                    valid=True, p99_ms=10 if mode=='static' else 8, total_mbps=10,
                    deadline_attainment=.99, offered=100, failed=0))
        def result(value):
            return compare(value,value,'static','slo')
        decision = result(data)
        self.assertEqual(decision['outcome'],'PASS')
        self.assertTrue(decision['lower_p99_supported'])
        self.assertAlmostEqual(decision['findings'][0]['median_regression'],-.2)
        self.assertGreaterEqual(decision['findings'][0]['simultaneous_median_ci']['coverage'],.9875)
        short = copy.deepcopy(data)
        short['runs'] = short['runs'][:-2]
        short['manifest']['repetitions'] = 9
        short['block_seeds'] = short['block_seeds'][:-1]
        self.assertEqual(result(short)['outcome'],'INCONCLUSIVE')
        self.assertEqual(result(dict(data,status='running'))['outcome'],'INVALID EXPERIMENT')
        failed = copy.deepcopy(data)
        for row in failed['runs']:
            if row['mode']=='slo':
                row['failed'] = 5
        outcome = result(failed)
        self.assertEqual(outcome['outcome'],'REGRESSION')
        self.assertEqual(outcome['findings'][0]['pairs'],0)
        failed['runs'][-1]['valid'] = False
        self.assertEqual(result(failed)['outcome'],'INVALID EXPERIMENT')
        other = dict(data,experiment_id='other-run')
        self.assertEqual(compare(data,other,'static','slo')['outcome'],'INVALID EXPERIMENT')

    def test_observed_rate_and_noqueue_counters(self):
        def snapshot(t, count, kind):
            return dict(begin_ns=t, end_ns=t, links=[
                [dict(name='fc0', counters=dict(tx_bytes=count, tx_packets=count//1000))],
                [dict(name='fc1', counters=dict(rx_bytes=count, rx_packets=count//1000))]],
                tc=dict(qdisc=[dict(root=True, kind=kind, bytes=count, overlimits=count//1000)],
                        traffic_classes={'critical': dict(stats=dict(bytes=count, packets=count//1000))}))
        first, last = (snapshot(0,0,'htb'),snapshot(10**9,1250000,'htb'))
        rate = observed(first,last,10000)
        self.assertEqual(rate['sender_egress_mbps'],10)
        self.assertTrue(rate['saturation_supported'])
        self.assertEqual(rate['tc_class']['critical']['bytes'],1250000)
        self.assertEqual(rate['forward_link_bytes_relative_gap'],0)
        first['tc']['qdisc'][0]['kind'] = 'noqueue'
        last['tc']['qdisc'][0]['kind'] = 'noqueue'
        rate = observed(first,last,None)
        self.assertIsNone(rate['sender_qdisc']['bytes'])
        self.assertIsNone(rate['qdisc_to_sender_bytes_relative_gap'])
        self.assertFalse(rate['saturation_supported'])

    def test_failure_aware_accounting(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'build') as work:
            path = Path(work)/'requests.csv'
            with path.open('w') as stream:
                writer = csv.writer(stream)
                writer.writerow(('id','scheduled','completed','latency_ms','outcome','traffic_class','service','dispatch_lag_ms'))
                writer.writerows(((0,0,.01,10,'ok','critical','critical',.1),
                                  (1,.99,1.02,30,'ok','critical','critical-2',.2),
                                  (2,.1,5.1,5000,'TimeoutError','critical','critical',.3),
                                  (3,.2,.21,10,'inflight_limit','standard','standard',.4)))
            metrics = account(path,4,0,1,25,1000)
            self.assertEqual(metrics['deadline_attainment'],.25)
            self.assertEqual(metrics['deadline_misses'],3)
            self.assertEqual(metrics['completion_ratio'],.5)
            self.assertEqual(metrics['timeout_requests'],1)
            self.assertEqual(metrics['request_payload_mbps'],.008)

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
