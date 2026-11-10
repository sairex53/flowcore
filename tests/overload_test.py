"""Deterministic overload diagnostics and cooperative backpressure fixtures."""
import csv
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))

from measurement import TIMING_FIELDS
from overload import NANO, Pressure, accounting, advise, compare_configuration, cooperative_poll


class OverloadTest(unittest.TestCase):
    def test_hysteresis_duplicate_stale_and_recovery(self):
        engine = Pressure()

        def step(t, rate, use, backlog, age=0):
            return engine.observe(rate, use, backlog, t*NANO, now_ns=(t+age)*NANO)

        self.assertEqual(step(1, 12000, .3, 0), 'NORMAL')
        self.assertEqual(step(2, 12000, .91, 7000), 'APPROACHING_CAPACITY')
        self.assertEqual(step(2, 12000, .91, 7000), 'APPROACHING_CAPACITY')
        self.assertEqual(step(3, 12000, .95, 9000), 'OVERLOADED')
        self.assertEqual(step(4, 7000, .94, 9000), 'CAPACITY_COLLAPSE')
        self.assertEqual(step(5, 6000, .95, 9000), 'CAPACITY_COLLAPSE')
        self.assertEqual(step(6, 12000, .6, 0), 'RECOVERING')
        self.assertEqual(step(7, 12000, .6, 0), 'NORMAL')
        self.assertEqual(step(8, 12000, .99, 9000, age=4), 'INSUFFICIENT_DATA')
        self.assertEqual(advise('OVERLOADED', 4*NANO, 8, now_ns=8*NANO)['action'], 'NO_INTERVENTION')

    def test_cooperative_advice_fails_safe(self):
        pressure = Pressure()

        def query(op):
            self.assertEqual(op, 'capacity_estimate')
            return dict(scope='owned_lab_sender', sample_age_seconds=0, namespace_identity=[1, 2],
                        sample_monotonic_ns=ns, directions=dict(egress=dict(
                            capacity_source='qdisc_readback', controllable=True, capacity_kbit=12000.0,
                            backlog_bytes=12000, utilization=.98)))

        ns = NANO
        self.assertEqual(cooperative_poll(query, pressure, 12, now_ns=ns)['action'], 'NO_INTERVENTION')
        ns = 2*NANO
        self.assertEqual(cooperative_poll(query, pressure, 12, now_ns=ns)['recommended_concurrency'], 9)

        def lost_reply(_):
            raise EOFError('fixture')

        self.assertEqual(cooperative_poll(lost_reply, pressure, 12)['recommended_concurrency'], 12)
        self.assertEqual(pressure.state, 'INSUFFICIENT_DATA')
        self.assertEqual(cooperative_poll(query, pressure, 12, now_ns=ns)['action'], 'NO_INTERVENTION')
        with self.assertRaises(ValueError):
            cooperative_poll(lost_reply, pressure, 0)

    def test_accounting_keeps_offer_and_failure_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            outcomes = ['inflight_limit', 'TimeoutError', 'ConnectionRefusedError', 'ok', 'ValueError']
            stages = [[0, 1], list(range(7)), list(range(4)), list(range(9)), list(range(5))]
            with (d/'request-timing.csv').open('w') as stream:
                writer = csv.writer(stream)
                writer.writerow(TIMING_FIELDS)
                for idx, (outcome, present) in enumerate(zip(outcomes, stages)):
                    stamps = [NANO+offset*1_000_000 if offset in present else '' for offset in range(9)]
                    stamps[-1] = NANO+20_000_000
                    writer.writerow([idx, outcome, *stamps])
            with (d/'requests.csv').open('w') as stream:
                writer = csv.writer(stream)
                writer.writerow(('id','outcome','latency_ms','service'))
                writer.writerows((idx, outcome, 20, 'api') for idx, outcome in enumerate(outcomes))
            result = dict(offered=5, completed=1, failed=4, start=1, end=2, deadline_ms=10)
            report = accounting(d, result, 1024)
            self.assertEqual(report['failure_reasons'], dict(CLIENT_INFLIGHT_LIMIT=1,
                TIMEOUT_AWAITING_RESPONSE=1, CONNECTION_FAILURE=1, UNKNOWN=1))
            cohort = report['cohort']
            self.assertEqual([cohort[key] for key in ('scheduled','released','admitted','rejected',
                'submission_attempted','completed','timed_out','connection_failure','deadline_missed')],
                [5, 5, 4, 1, 2, 1, 1, 1, 5])
            with self.assertRaises(ValueError):
                accounting(d, dict(result, offered=6), 1024)

    def test_classification_difference_is_explicit(self):
        baseline = dict(mode='baseline', kernel_configurations=[dict(label='setup', filters=[])])
        candidate = dict(mode='candidate', kernel_configurations=[dict(label='setup', filters=['udp'])])
        difference = compare_configuration(baseline, candidate)
        self.assertEqual(difference['status'], 'DIFFERENT')
        self.assertEqual(difference['differences'][0]['changed']['filters']['after'], ['udp'])
        self.assertEqual(compare_configuration(baseline, {})['status'], 'INSUFFICIENT_DATA')


if __name__ == '__main__':
    unittest.main()
