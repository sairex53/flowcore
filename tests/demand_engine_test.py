"""Native evidence model contracts: counters are bytes, rates are bits/s."""
import math
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from demand_intelligence import NativeDemand

MODEL = NativeDemand(os.environ['FLOWCORE_DEMAND_LIBRARY'])


def observation(evidence):
    return dict(evidence=evidence, interval_seconds=1.0, capacity_bps=10_000_000,
                configured_shaping_bps=10_000_000, observed_bps=2_000_000,
                app_offered_bytes=250_000, app_admitted_bytes=250_000,
                app_transmitted_bytes=250_000, app_received_bytes=500_000,
                delivered_bytes=250_000, root_backlog_before_bytes=0,
                root_backlog_after_bytes=0, root_drops=0, class_rate_bps=1_000_000,
                class_ceil_bps=1_000_000, class_served_bytes=250_000,
                class_backlog_before_bytes=0, class_backlog_after_bytes=0,
                class_drops=0, requests_attempted=10, requests_admitted=10,
                requests_completed=10)


class DemandEngine(unittest.TestCase):
    def test_distinguishes_offered_demand_from_delivered_goodput(self):
        row = MODEL.assess(observation(('app_traffic', 'app_counts', 'delivered', 'root_queue',
                                        'capacity', 'configured_cap')))
        self.assertEqual(row['classification'], 'DEMAND_LIMITED')
        self.assertEqual(row['offered_bps'], 2_000_000)
        self.assertEqual(row['delivered_bps'], 2_000_000)
        self.assertEqual(row['received_bps'], 4_000_000)
        self.assertEqual(row['selected_demand_bps'], 2_000_000)
        self.assertEqual(row['unused_capacity_bps'], 8_000_000)
        self.assertEqual(row['reclaimable_capacity_bps'], 8_000_000)

    def test_missing_demand_and_partial_reports_stay_unknown(self):
        row = MODEL.assess(observation(('root_queue', 'capacity', 'configured_cap')))
        self.assertEqual(row['classification'], 'UNKNOWN')
        self.assertIsNone(row['selected_demand_bps'])
        self.assertIsNone(row['demand_uncertainty_bps'])
        value = observation(('app_traffic', 'app_counts', 'app_incomplete', 'root_queue',
                             'capacity', 'configured_cap'))
        value.update(app_offered_bytes=1000, app_admitted_bytes=1000, app_transmitted_bytes=1000)
        row = MODEL.assess(value)
        self.assertEqual(row['classification'], 'UNKNOWN')
        self.assertIn('APPLICATION_REPORT_INCOMPLETE', row['flags'])
        self.assertIsNone(row['demand_uncertainty_bps'])

    def test_capacity_provenance_and_request_denominators(self):
        value = observation(('app_traffic', 'app_counts', 'delivered', 'root_queue',
                             'capacity', 'nominal_cap'))
        value.update(requests_attempted=20, requests_admitted=18, requests_completed=15,
                     requests_failed=3, requests_rejected=2, deadline_misses=4)
        row = MODEL.assess(value)
        self.assertIn('CAPACITY_UNCERTAIN', row['flags'])
        self.assertAlmostEqual(row['failure_ratio'], .25)
        self.assertAlmostEqual(row['deadline_miss_ratio'], 4/18)
        value['requests_completed'] = 16
        with self.assertRaises(ValueError):
            MODEL.assess(value)

    def test_local_class_and_socket_restrictions(self):
        local = observation(('app_traffic', 'delivered', 'root_queue', 'capacity',
                             'configured_cap', 'root_drops'))
        local.update(app_offered_bytes=1_187_500, app_admitted_bytes=1_187_500,
                     app_transmitted_bytes=1_187_500, delivered_bytes=1_187_500,
                     observed_bps=9_500_000, root_backlog_after_bytes=4096)
        self.assertEqual(MODEL.assess(local)['classification'], 'LOCAL_CAPACITY_LIMITED')

        restricted = observation(('app_traffic', 'app_incomplete', 'delivered', 'root_queue',
                                  'capacity', 'configured_cap', 'class', 'class_drops'))
        restricted.update(app_offered_bytes=375_000, app_admitted_bytes=250_000,
                          app_transmitted_bytes=250_000, class_backlog_after_bytes=4096,
                          class_served_bytes=118_750, observed_bps=2_000_000)
        self.assertEqual(MODEL.assess(restricted)['classification'], 'CLASS_RESTRICTED')

        paced = observation(('delivered', 'root_queue', 'capacity', 'configured_cap', 'tcp',
                             'tcp_limits', 'socket_pacing'))
        paced.update(observed_bps=900_000, socket_pacing_bps=1_000_000,
                     socket_send_queue_after_bytes=4096, socket_notsent_bytes=4096,
                     socket_delivery_bps=900_000)
        self.assertEqual(MODEL.assess(paced)['classification'], 'SOCKET_PACING_RESTRICTED')

    def test_receiver_remote_sender_and_udp_evidence(self):
        receiver = observation(('root_queue', 'capacity', 'configured_cap', 'tcp', 'tcp_limits'))
        receiver.update(socket_notsent_bytes=4096, tcp_rwnd_limited_fraction=.82)
        self.assertEqual(MODEL.assess(receiver)['classification'], 'RECEIVER_LIMITED_SUSPECTED')
        remote = observation(('root_queue', 'capacity', 'configured_cap', 'remote_pressure'))
        self.assertEqual(MODEL.assess(remote)['classification'], 'REMOTE_BOTTLENECK_SUSPECTED')
        sender = observation(('root_queue', 'capacity', 'configured_cap', 'tcp', 'tcp_limits'))
        sender.update(socket_notsent_bytes=4096, tcp_sendbuf_limited_fraction=.8)
        row = MODEL.assess(sender)
        self.assertEqual(row['classification'], 'UNKNOWN')
        self.assertIn('SENDER_BUFFER_LIMITED_EVIDENCE', row['flags'])
        udp = observation(('app_traffic', 'app_counts', 'udp_accounting'))
        udp.update(udp_messages_attempted=12, udp_messages_transmitted=10,
                   udp_send_failures=2, udp_urgent_messages=3, udp_background_messages=9)
        self.assertEqual(MODEL.assess(udp)['udp_send_failures'], 2)

    def test_queue_accounting_is_lower_bound_and_invalid_after_drops(self):
        value = observation(('root_queue', 'capacity', 'class', 'class_drops'))
        value.update(class_served_bytes=1000, class_backlog_before_bytes=100,
                     class_backlog_after_bytes=500)
        row = MODEL.assess(value)
        self.assertEqual(row['queue_accounted_demand_bps'], 11_200)
        self.assertIn('QUEUE_ACCOUNTED_LOWER_BOUND', row['flags'])
        value['class_drops'] = 1
        self.assertIsNone(MODEL.assess(value)['queue_accounted_demand_bps'])

    def test_forecast_bounded_and_detects_bursts(self):
        rows = [dict(time_ns=(i + 1) * 1_000_000_000,
                     demand_bps=(i + 1) * 100_000, capacity_bps=10_000_000) for i in range(8)]
        forecast = MODEL.forecast(rows, horizon_seconds=2, half_life_seconds=2)
        self.assertGreater(forecast['forecast_bps'], forecast['last_value_baseline_bps'])
        self.assertLessEqual(forecast['forecast_bps'], 40_000_000)
        self.assertGreater(forecast['confidence'], 0)
        self.assertTrue(math.isfinite(forecast['uncertainty_bps']))
        burst = [dict(row, demand_bps=100_000 if i != 7 else 5_000_000)
                 for i, row in enumerate(rows)]
        self.assertLess(MODEL.forecast(burst)['confidence'], forecast['confidence'])
        self.assertIsNone(MODEL.forecast(rows[:3]))
        rows[-1]['demand_bps'] = float('nan')
        with self.assertRaises(ValueError):
            MODEL.forecast(rows)


if __name__ == '__main__':
    unittest.main()
