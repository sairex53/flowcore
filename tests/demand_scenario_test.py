"""Check that live demand profiles and atomic byte accounting stay reproducible."""
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))

from bottleneck_experiment import DEMAND_CASES, WindowTraffic


class DemandScenarioManifest(unittest.TestCase):
    def test_manifest_matches_live_harness_profiles(self):
        manifest = json.loads((ROOT/'scenarios/demand-network-v2.json').read_text())
        profiles = manifest['profiles']
        names = [row['name'] for row in profiles]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(set(names), DEMAND_CASES)
        for profile in profiles:
            self.assertIn('ground_truth', profile)
            self.assertIn('transport', profile)
            self.assertIn('flow_ports', profile)
        self.assertFalse(manifest['safety']['host_policy_writes'])
        self.assertFalse(manifest['safety']['host_qdisc_changes'])
        self.assertFalse(manifest['safety']['controller_policy_writes'])

    def test_traffic_updates_commit_as_one_window_event(self):
        meter = WindowTraffic()
        meter.record_tcp_send(1000, 700)
        meter.record_udp_send(1200, 1200, urgent=True, failed=False)
        meter.record_udp_send(1200, 0, urgent=False, failed=True)
        window = meter.snapshot()
        self.assertEqual(window['bytes_offered'], 3400)
        self.assertEqual(window['bytes_admitted'], 3400)
        self.assertEqual(window['bytes_transmitted'], 1900)
        self.assertEqual(window['udp_messages_attempted'], 2)
        self.assertEqual(window['udp_messages_transmitted'], 1)
        self.assertEqual(window['udp_send_failures'], 1)
        self.assertEqual(window['udp_urgent_messages'], 1)
        self.assertEqual(window['udp_background_messages'], 1)
        self.assertEqual(meter.snapshot()['bytes_offered'], 0)


if __name__ == '__main__':
    unittest.main()
