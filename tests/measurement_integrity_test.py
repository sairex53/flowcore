"""Deterministic contract tests for Measurement Integrity V3; fixtures are not measurements."""
import copy
import csv
import json
import random
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))

import measurement
from research_stats import compare_integrity, load_summary
from research_report import report
from research_manifest import load_manifest, validate


class MeasurementIntegrity(unittest.TestCase):
    def trace_fixture(self, directory):
        start_ns = 100_000_000_000
        complete_ns = start_ns + 100_000_000
        fields = measurement.TIMING_FIELDS
        stamps = dict(scheduled_ns=start_ns, released_ns=start_ns+1_000_000,
                      dispatch_ns=start_ns+1_500_000, connect_ns=start_ns+2_000_000,
                      connected_ns=start_ns+3_000_000, submitted_ns=start_ns+4_000_000,
                      drained_ns=start_ns+5_000_000, received_ns=start_ns+50_000_000,
                      completed_ns=complete_ns)
        with (directory/'request-timing.csv').open('w',newline='') as stream:
            writer = csv.DictWriter(stream,fieldnames=fields)
            writer.writeheader()
            writer.writerow(dict(id=0,outcome='ok',**stamps))
        with (directory/'requests.csv').open('w',newline='') as stream:
            writer = csv.DictWriter(stream,fieldnames=('id','scheduled','completed','latency_ms','outcome',
                                                       'traffic_class','service','dispatch_lag_ms'))
            writer.writeheader()
            writer.writerow(dict(id=0,scheduled='100.0',completed='100.1',latency_ms='100.0',
                                 outcome='ok',traffic_class='critical',service='api',dispatch_lag_ms='1.5'))
        manifest = dict(integrity={'load':'open'},
                        workload={'phases':[{'seconds':1,'requests_per_second':1}]})
        result = dict(start=100.0,end=101.0,offered=1,p99_ms=100.0)
        return manifest,result

    def test_timing_domains_and_known_percentile(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'build') as raw:
            directory = Path(raw)
            manifest,result = self.trace_fixture(directory)
            measured = measurement.timing_audit(directory,result,manifest)
            self.assertEqual(measured['clock'],'CLOCK_MONOTONIC')
            self.assertEqual(measured['records'],1)
            self.assertAlmostEqual(measured['release_lag_p99_ms'],1.0)
            self.assertAlmostEqual(measured['task_dispatch_p99_ms'],.5)
            self.assertAlmostEqual(measured['tcp_connect_p99_ms'],1.0)
            self.assertAlmostEqual(measured['write_drain_p99_ms'],1.0)
            self.assertAlmostEqual(measured['response_after_drain_p99_ms'],45.0)
            self.assertAlmostEqual(measured['submit_to_response_p99_ms'],46.0)
            self.assertAlmostEqual(measured['release_to_completion_p99_ms'],99.0)
            self.assertIsNone(measured['kernel_packet_timestamps'])

    def test_missing_duplicate_nonmonotonic_and_conversion_errors(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'build') as raw:
            directory = Path(raw)
            manifest,result = self.trace_fixture(directory)
            (directory/'request-timing.csv').write_text('id,outcome,scheduled_ns\n')
            with self.assertRaisesRegex(ValueError,'INVALID_TIMING_SCHEMA'):
                measurement.timing_audit(directory,result,manifest)
            self.trace_fixture(directory)
            with (directory/'request-timing.csv').open('a') as stream:
                stream.write((directory/'request-timing.csv').read_text().splitlines()[1]+'\n')
            with self.assertRaisesRegex(ValueError,'DUPLICATE_OR_OUT_OF_RANGE'):
                measurement.timing_audit(directory,result,manifest)
            self.trace_fixture(directory)
            with (directory/'request-timing.csv').open() as stream:
                rows = list(csv.DictReader(stream))
            rows[0]['submitted_ns'] = str(int(rows[0]['connected_ns'])-1)
            rows[0]['completed_ns'] = rows[0]['submitted_ns']
            with (directory/'request-timing.csv').open('w',newline='') as stream:
                writer=csv.DictWriter(stream,fieldnames=measurement.TIMING_FIELDS)
                writer.writeheader()
                writer.writerows(rows)
            with self.assertRaisesRegex(ValueError,'NONMONOTONIC'):
                measurement.timing_audit(directory,result,manifest)
            self.trace_fixture(directory)
            requests=(directory/'requests.csv').read_text().replace('100.1,100.0,ok','100.2,100.0,ok')
            (directory/'requests.csv').write_text(requests)
            with self.assertRaisesRegex(ValueError,'TIMESTAMP_CONVERSION'):
                measurement.timing_audit(directory,result,manifest)

    def test_open_loop_admission_failure_has_terminal_timestamp(self):
        import asyncio
        import io
        import time
        from workload import client

        async def exercise(directory):
            async def slow_echo(reader, writer):
                try:
                    await reader.readexactly(8)
                    await asyncio.sleep(.15)
                    writer.write(b'R'*64)
                    await writer.drain()
                finally:
                    writer.close()
                    await writer.wait_closed()

            async with await asyncio.start_server(slow_echo,'127.0.0.1',0) as server:
                port=server.sockets[0].getsockname()[1]
                trace=io.StringIO()
                profile=dict(integrity={'load':'open','self_test':'none','concurrency':0},
                             server_address='127.0.0.1', ports={'critical':port},
                             priority_weights={'critical':100}, seed=3,
                             phases=[dict(seconds=1,requests_per_second=70,background_streams=0)],
                             start_monotonic=time.monotonic()+.05,max_inflight=1)
                with self.assertRaises(SystemExit) as rejected:
                    await client(directory,1,70,8,port,port+1,0,research=profile,timing_stream=trace)
                self.assertEqual(rejected.exception.code,2)
                rows=list(csv.DictReader(io.StringIO(trace.getvalue())))
                limited=[row for row in rows if row['outcome']=='inflight_limit']
                self.assertTrue(limited)
                self.assertTrue(all(row['completed_ns'] and None not in row for row in limited))
                (directory/'request-timing.csv').write_text(trace.getvalue())
                summary=json.loads((directory/'client.json').read_text())
                manifest=dict(integrity=profile['integrity'],workload={'phases':profile['phases']})
                self.assertEqual(measurement.timing_audit(directory,summary,manifest)['records'],70)
                with (directory/'request-timing.csv').open('a') as stream:
                    stream.write(trace.getvalue().splitlines()[1]+',extra\n')
                with self.assertRaisesRegex(ValueError,'INVALID_TIMING_ROW_WIDTH'):
                    measurement.timing_audit(directory,summary,manifest)

        with tempfile.TemporaryDirectory(dir=ROOT/'build') as raw:
            asyncio.run(exercise(Path(raw)))

    def test_classifier_flags_inconsistent_wire_throughput(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'build') as raw:
            directory=Path(raw)
            manifest,result=self.trace_fixture(directory)
            manifest['integrity'].update(max_release_p99_ms=25,max_loop_lag_ms=110,max_cpu_psi_ratio=.15,
                max_steal_ratio=.04,max_counter_gap_ratio=.2,max_failure_ratio=.01,min_successful_requests=1)
            result.update(failure_ratio=0,completed=1,total_mbps=10,observed_rate_windows=[
                {'sender_link':{'tx_bytes':0},'forward_link_bytes_relative_gap':0}],outcomes={},
                measurement_client={'load':'open','configured_concurrency':0,'request_cap':256,
                    'peak_inflight':1,'peak_outstanding':1,'loop_lag_max_ms':0,'loop_samples':10,
                    'missed_loop_ticks':0,'request_ceiling_hit':False},
                start=100.0,end=101.0)
            host=dict(time_ns=1,cpu_ticks=[1,1,1,1,1,1,1,0],pressure_total_us={
                'cpu':{'some':0},'memory':{'full':0},'io':{'full':0}})
            end=dict(host,time_ns=1_000_000_001,cpu_ticks=[10,10,10,10,10,10,10,0],
                     pressure_total_us={'cpu':{'some':0},'memory':{'full':0},'io':{'full':0}})
            classified=measurement.classify(directory,result,manifest,dict(start=host,end=end),{})
            self.assertEqual(classified['state'],'INVALID_INFRASTRUCTURE')
            self.assertIn('PAYLOAD_EXCEEDS_WIRE_BYTES',
                          {item['code'] for item in classified['issues']})

    def test_partial_crash_ledger_reports_lower_bound_only(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'build') as raw:
            directory=Path(raw)
            self.trace_fixture(directory)
            ledger=measurement.partial_timing_ledger(directory)
            self.assertTrue(ledger['valid'])
            self.assertEqual(ledger['completed_timing_records'],1)
            self.assertTrue(ledger['offered_total_unknown'])
            (directory/'request-timing.csv').write_text('bad,header\n')
            self.assertEqual(measurement.partial_timing_ledger(directory)['error'],'INVALID_TIMING_SCHEMA')

    def test_checksum_seal_detects_changed_raw_artifact(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'build') as raw:
            directory=Path(raw)
            (directory/'payload.csv').write_text('a,b\n1,2\n')
            row={'artifact':'fixture','payload':'measured'}
            (directory/'result.json').write_text(json.dumps(row)+'\n')
            row['evidence_sha256']=measurement.seal(directory)
            self.assertEqual(measurement.verify(directory,row)['verified'],2)
            (directory/'payload.csv').write_text('a,b\n9,9\n')
            with self.assertRaisesRegex(ValueError,'digest mismatch'):
                measurement.verify(directory,row)

    def synthetic(self, candidate_p99=9.0, repetitions=12, invalid_candidate=None):
        manifest=load_manifest(ROOT/'scenarios/measurement-v3-paired.json')
        manifest['repetitions']=repetitions
        manifest['gates']['min_pairs']=10
        manifest['integrity']['bootstrap_resamples']=300
        seeds=list(range(1,repetitions+1))
        runs=[]
        for repetition,mode in ((i,m) for i in range(repetitions) for m in manifest['modes']):
            candidate=mode!='static'
            row=dict(mode=mode,repetition=repetition,artifact=f'r{repetition}-{mode}',
                     workload_seed=seeds[repetition],valid=True,offered=252,completed=252,failed=0,
                     p99_ms=candidate_p99 if candidate else 10.0,deadline_attainment=.99,
                     total_mbps=10.0,background_mbps=4.0,jain_index=.95,cpu_seconds=1.0,
                     measurement=dict(state='VALID',timing={'scheduled_workload_sha256':f'seed-{repetition}'}))
            if candidate and invalid_candidate==repetition:
                row['valid']=False
            runs.append(row)
        summary=dict(version=2,status='complete',manifest=manifest,environment=dict(kernel='k',architecture='aarch64',cpus=4,tc='tc',boot_fingerprint='boot',
            host={'cpu_topology':{}},build_configuration={},network_sysctls={}),block_seeds=seeds,
            experiment_id='fixture',runs=runs)
        return summary


    def test_summary_and_report_reject_tampered_artifact_without_dropping_run(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'build') as raw:
            root=Path(raw)
            manifest=load_manifest(ROOT/'scenarios/measurement-v3-paired.json')
            manifest['repetitions']=1
            manifest['modes']=['static']
            run_dir=root/'r0-static'
            run_dir.mkdir()
            (run_dir/'raw.csv').write_text('id,value\n0,1\n')
            row=dict(mode='static',repetition=0,artifact='r0-static',workload_seed=7,
                     valid=True,offered=1,completed=1,failed=0,p99_ms=1.0,
                     measurement=dict(version=3,state='VALID',issues=[]))
            (run_dir/'result.json').write_text(json.dumps(row)+'\n')
            row['evidence_sha256']=measurement.seal(run_dir)
            experiment=json.dumps(manifest,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
            (root/'experiment.json').write_bytes(experiment)
            environment=dict(kernel='k',architecture='aarch64',cpus=4,tc='tc',boot_fingerprint='b',
                host={},build_configuration={},network_sysctls={},git_commit=None,
                experiment_manifest_sha256=__import__('hashlib').sha256(experiment).hexdigest(),
                analysis_version=3)
            summary=dict(version=2,status='complete',manifest=manifest,environment=environment,
                         block_seeds=[7],experiment_id='fixture',runs=[row])
            summary_path=root/'summary.json'
            summary_path.write_text(json.dumps(summary)+'\n')
            self.assertTrue(load_summary(summary_path)['runs'][0]['valid'])
            (run_dir/'raw.csv').write_text('tampered\n')
            loaded=load_summary(summary_path)
            self.assertFalse(loaded['runs'][0]['valid'])
            self.assertEqual(loaded['runs'][0]['measurement']['state'],'INVALID_INFRASTRUCTURE')
            page=report(summary_path)
            output=json.loads((root/'report.json').read_text())
            self.assertEqual(output['state'],'INVALID_INFRASTRUCTURE')
            self.assertEqual(len(output['runs']),1)
            self.assertIn('ARTIFACT_INTEGRITY',Path(page).read_text())

    def test_bootstrap_paired_gates_and_power(self):
        data=self.synthetic()
        compared=compare_integrity(data,data,'static','fifo')
        self.assertEqual(compared['outcome'],'PASS')
        p99=next(row for row in compared['findings'] if row['metric']=='p99_ms')
        self.assertEqual(p99['pairs'],12)
        self.assertEqual(p99['simultaneous_median_ci']['method'],'paired bootstrap percentile interval')
        self.assertIsNotNone(p99['power']['power'])
        self.assertGreater(p99['power']['minimum_pairs_for_target'],0)
        regression=compare_integrity(self.synthetic(15),self.synthetic(15),'static','fifo')
        self.assertEqual(regression['outcome'],'REGRESSION')

    def test_open_paired_arms_must_share_the_same_offered_schedule(self):
        data=self.synthetic()
        next(row for row in data['runs'] if row['mode']=='fifo' and row['repetition']==2)['measurement']['timing']['scheduled_workload_sha256']='different'
        compared=compare_integrity(data,data,'static','fifo')
        self.assertEqual(compared['outcome'],'INVALID EXPERIMENT')
        self.assertEqual(compared['schedule_mismatches'],['r2-static<->r2-fifo'])
        row = next(row for row in data['runs'] if row['mode']=='fifo' and row['repetition']==2)
        row.update(completed=220, failed=32)
        row['measurement']['state'] = 'FAILED_WORKLOAD'
        compared = compare_integrity(data,data,'static','fifo')
        self.assertEqual(compared['outcome'], 'INVALID EXPERIMENT')
        self.assertEqual(compared['schedule_mismatches'], ['r2-static<->r2-fifo'])

    def test_overload_guard_preserves_complete_failure_denominator(self):
        from overload import guard
        data = self.synthetic()
        for row in data['runs']:
            if row['mode'] == 'fifo':
                row.update(completed=220, failed=32)
                row['measurement']['state'] = 'FAILED_WORKLOAD'
        verdict = guard(data, 'static', 'fifo')
        self.assertEqual(verdict['comparison']['outcome'], 'FAILED_WORKLOAD')
        self.assertEqual(verdict['observed_regressions'], ['failure_ratio'])
        self.assertEqual(verdict['action'], 'KEEP_BASELINE')

    def test_invalid_block_is_not_dropped_and_low_n_is_inconclusive(self):
        data=self.synthetic(invalid_candidate=3)
        compared=compare_integrity(data,data,'static','fifo')
        self.assertEqual(compared['outcome'],'INVALID EXPERIMENT')
        self.assertEqual(compared['invalid_blocks'],['r3-fifo'])
        data=self.synthetic(repetitions=5)
        self.assertEqual(compare_integrity(data,data,'static','fifo')['outcome'],'INCONCLUSIVE')

    def test_v3_manifest_comparison_accepts_v3_pairs(self):
        data=self.synthetic()
        data['version']=3
        data['manifest']['version']=3
        outcome=compare_integrity(data,data,'static','fifo')
        self.assertTrue(outcome['comparable_manifest_and_environment'])
        self.assertEqual(outcome['outcome'],'PASS')

    def test_client_exit_is_not_an_infrastructure_failure(self):
        data=self.synthetic()
        row=next(row for row in data['runs'] if row['artifact']=='r2-fifo')
        row.update(valid=False,measurement={'state':'FAILED_WORKLOAD','issues':[{'code':'CLIENT_EXIT'}]})
        outcome=compare_integrity(data,data,'static','fifo')
        self.assertEqual(outcome['outcome'],'FAILED_WORKLOAD')
        self.assertEqual(outcome['failed_workload_blocks'],['r2-fifo'])
        self.assertFalse(outcome['schedule_mismatches'])
        row['artifact_integrity']={'verified':False}
        self.assertEqual(compare_integrity(data,data,'static','fifo')['outcome'],'INVALID EXPERIMENT')

    def test_verify_rejects_incomplete_campaign_even_if_every_available_digest_matches(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from research_cli import dispatch
        args=SimpleNamespace(command='bench',action='verify',summary=Path('test.json'))
        data=self.synthetic(repetitions=1)
        with patch('research_cli.load_summary',return_value=data):
            for row in data['runs']:
                row['artifact_integrity']={'verified':True}
            report, status=dispatch(args)
            self.assertTrue(report['verified'])
            self.assertEqual(status,0)
            data['runs'].pop()
            report, status=dispatch(args)
            self.assertFalse(report['complete'])
            self.assertFalse(report['verified'])
            self.assertEqual(status,2)

    def test_williams_order_balances_positions_and_carryover(self):
        modes=['a','b','c','d']
        orders=measurement.block_order(modes,4,random.Random(13),'counterbalanced')
        self.assertEqual(len(orders),4)
        for column in zip(*orders):
            self.assertEqual(set(column),set(modes))
        transitions=[(row[i],row[i+1]) for row in orders for i in range(len(row)-1)]
        self.assertEqual(len(set(transitions)),len(modes)*(len(modes)-1))
        self.assertEqual(len(transitions),len(set(transitions)))

    def test_manifest_rejects_ambiguous_closed_loop_load(self):
        manifest=load_manifest(ROOT/'scenarios/measurement-v3-closed.json')
        self.assertEqual(validate(manifest)['integrity']['load'],'closed')
        invalid=copy.deepcopy(manifest)
        invalid['workload']['phases'][0]['requests_per_second']=1
        with self.assertRaisesRegex(ValueError,'closed loop'):
            validate(invalid)
        invalid=copy.deepcopy(manifest)
        invalid['integrity']['concurrency']=512
        with self.assertRaises(ValueError):
            validate(invalid)


if __name__ == '__main__':
    unittest.main()
