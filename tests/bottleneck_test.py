"""Attribution, evidence quality, and directional action regression tests."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))

from adaptive import Controller
from bottleneck import Bottleneck, EGRESS, INGRESS, RECEIVER, REMOTE, UNKNOWN, direction


def observation(step, *, tx=0, rx=0, tx_queue=0, rx_queue=None, cap=4000):
    tx_counter = dict(kind='htb', bytes=step*125000, backlog=tx_queue, drops=0)
    rx_counter = dict(kind='htb', bytes=step*62500, backlog=rx_queue, drops=0) if rx_queue is not None else None
    return dict(egress=direction(tx_counter, {}, {'tx_bytes': tx}, 'tx_bytes', configured=cap, owned=True),
                ingress=direction(rx_counter, {}, {'rx_bytes': rx, 'rx_dropped': 0}, 'rx_bytes',
                                  configured=2000 if rx_counter else None, owned=True))


def socket_row(step, *, segments=100, rwnd=0, rtt=20000, port=9000):
    return dict(cookie='00abcd', state='established', netns=7, local_port=50000, remote_port=port,
                tcp_info=dict(bytes_acked=50000*step, busy_time_us=250000*step,
                              receive_window_limited_us=rwnd*step, data_segments_out=segments*step,
                              total_retrans_segments=0, rtt_us=rtt, min_rtt_us=500, notsent_bytes=4000))


class Engine(unittest.TestCase):
    def test_owned_egress_hysteresis_and_stale_gate(self):
        engine = Bottleneck()
        initial = engine.observe(0, observation(0))
        self.assertEqual(initial['state'], 'UNKNOWN')
        first = engine.observe(.25, observation(1, tx_queue=3000))
        self.assertEqual((first['state'], first['action']), ('BUILDING', 'OBSERVE'))
        duplicate = engine.observe(.27, observation(1, tx_queue=3000))
        self.assertEqual(duplicate['state'], 'BUILDING')
        live = engine.observe(.5, observation(2, tx_queue=3000))
        self.assertEqual((live['classification'], live['action']), (EGRESS, 'REVIEW_EGRESS_ALLOCATION'))
        self.assertTrue(live['directions']['egress']['saturated'])
        self.assertEqual(engine.view(20)['action'], 'OBSERVE')
        for index in range(3, 6):
            clear = engine.observe(index*.25, observation(index))
            self.assertEqual(clear['state'], 'RECOVERING' if index < 5 else 'NORMAL')

    def test_distinct_ingress_and_conflicting_local_evidence(self):
        engine = Bottleneck()
        for step in range(3):
            row = engine.observe(step*.25, observation(step, rx_queue=4000))
        self.assertEqual((row['classification'], row['action']), (INGRESS, 'REVIEW_INGRESS_IFB'))
        mixed = engine.observe(.75, observation(3, tx_queue=4000, rx_queue=4000))
        self.assertEqual((mixed['classification'], mixed['action']), (UNKNOWN, 'OBSERVE'))

    def test_rate_reset_and_capacity_provenance(self):
        engine = Bottleneck()
        for step in range(3):
            engine.observe(step*.25, observation(step, tx_queue=4000))
        changed = engine.observe(.75, observation(3, tx_queue=4000, cap=2000))
        self.assertEqual(changed['state'], 'UNKNOWN')
        self.assertEqual(changed['capacity_reductions']['egress'], dict(before=4000, after=2000))
        next_row = engine.observe(1, observation(4, tx_queue=4000, cap=2000))
        self.assertEqual(next_row['directions']['egress']['capacity_source'], 'qdisc_readback')
        self.assertIsNotNone(next_row['last_capacity_reduction'])
        reset = engine.observe(1.25, observation(0, tx_queue=4000, cap=2000))
        self.assertEqual(reset['state'], 'UNKNOWN')

    def test_tcp_suspicions_and_truncated_evidence(self):
        for window_limit, segments, coverage, expected in (
                (0, 100, True, REMOTE), (200000, 10, True, RECEIVER),
                (0, 10, True, UNKNOWN), (0, 100, False, UNKNOWN)):
            with self.subTest(expected=expected, coverage=coverage):
                engine = Bottleneck()
                for step in range(3):
                    row = engine.observe(step*.25, observation(step),
                                         [socket_row(step, rwnd=window_limit, segments=segments)],
                                         socket_coverage=coverage)
                self.assertEqual(row['classification'], expected)
                self.assertIn(row['action'], ('ADVISE', 'OBSERVE'))

    def test_correlates_latency_with_the_reported_service_class(self):
        engine = Bottleneck()
        for step in range(5):
            directions = observation(step, tx_queue=2000+step*1000)
            directions['egress']['classes'] = {'critical': {'rate': 150000,
                'stats': {'bytes': step*50000, 'drops': 0, 'backlog': 2000+step*1000}}}
            app = [{'service': 'demo', 'class': 'critical', 'port': 9000,
                    'p95_us': 5000+step*1000, 'samples': 32, 'target_us': 10000}]
            row = engine.observe(step*.25, directions, application=app)
        self.assertEqual(row['application'][0]['backlog_latency_pairs'], 4)
        self.assertAlmostEqual(row['application'][0]['backlog_latency_correlation'], 1)
        self.assertEqual(row['application'][0]['class_backlog_bytes'], 6000)

    def test_controller_admission_suppresses_candidate_allocation(self):
        controller = Controller.__new__(Controller)
        controller.egress_gate = lambda: False
        proposed = dict(action='transfer_reservation', proposed={'critical': [3000, 4000]})
        held = controller.qualify(proposed)
        self.assertEqual((held['action'], held['proposed']), ('hold', None))
        controller.egress_gate = lambda: True
        self.assertIs(controller.qualify(proposed), proposed)


if __name__ == '__main__':
    unittest.main()
