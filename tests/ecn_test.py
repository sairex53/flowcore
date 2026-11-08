"""ECN parsing, source attribution, profile validation and bounded inference."""
import copy
import ctypes
import os
from pathlib import Path
import socket
import struct
import sys
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
from ecn_feedback import aqm_counters, estimate, mode, pairs
import ecn_policy
from ecn_probe import capture_fields
from research_manifest import load_manifest, validate
from tcp_diag import tcp_info


class Feedback(unittest.TestCase):
    def setUp(self):
        self.old = dict(ecn_negotiated=True, rtt_us=6000, delivered_segments=100,
                        delivered_ce_segments=4, total_retransmits=0)
        self.new = dict(self.old, rtt_us=8500, delivered_segments=150, delivered_ce_segments=10)
        self.options = dict(previous={'c': self.old}, current={'c': self.new},
                            before_aqm=dict(marks=0, drops=0, backlog=0),
                            after_aqm=dict(marks=5, drops=0, backlog=9000),
                            fresh=True, capacity=1_500_000, delivery_rate=500000,
                            rtt_us=8500, p99_us=17000, requests=100, elapsed_ns=1_000_000_000)

    def test_pre_loss_requires_independent_sources(self):
        fused = estimate(**self.options)
        self.assertTrue(fused['early'])
        self.assertEqual(fused['rtt_trend_us'], 2500)
        self.assertAlmostEqual(fused['ce_fraction'], .12)
        for update in (dict(fresh=False), dict(elapsed_ns=3_000_000_000),
                       dict(previous={}), dict(scope_matches=False), dict(after_aqm=dict(marks=5, backlog=9000)),
                       dict(after_aqm=dict(marks=0, drops=0, backlog=9000)),
                       dict(after_aqm=dict(marks=5, drops=0, backlog=0))):
            self.assertFalse(estimate(**dict(self.options, **update))['early'])

    def test_saturation_and_loss_preempt_early(self):
        self.new['delivered_ce_segments'] = 30
        self.assertEqual(estimate(**self.options)['classification'], 'mark_saturated')
        self.new['total_retransmits'] = 1
        self.assertEqual(estimate(**self.options)['classification'], 'loss_present')
        self.options['after_aqm']['drops'] = 1
        self.new['ecn_negotiated'] = False
        self.assertEqual(estimate(**self.options)['quality'], 'direct_loss')

    def test_flow_mix_and_invalid_counter_deltas(self):
        self.options['current']['unnegotiated'] = dict(self.new, ecn_negotiated=False)
        self.options['previous']['unnegotiated'] = dict(self.old, ecn_negotiated=False)
        report = estimate(**self.options)
        self.assertEqual(report['negotiated'], {'classic-or-accurate': 1, 'disabled': 1})
        self.assertEqual(report['paired_sockets'], 1)
        for invalid in (dict(self.new, delivered_segments=80), dict(self.new, delivered_ce_segments=90)):
            self.assertEqual(pairs({'c': self.old}, {'c': invalid}), {})
        self.assertEqual(mode({}), 'unknown')

    def test_unknown_aqm_does_not_manufacture_zero(self):
        self.assertIsNone(aqm_counters({'kind': 'pfifo', 'drops': 4})['marks'])
        self.assertEqual(aqm_counters(dict(kind='cake', tins=[{'ecn_mark': 5}, {'ecn_mark': 7}]))['marks'], 12)
        self.assertIsNone(aqm_counters(dict(kind='cake', tins=[{'ecn_mark': 5}, {}]))['marks'])


class Lab(unittest.TestCase):
    def test_strict_profile_and_queue_ownership(self):
        profile = load_manifest(ROOT/'scenarios/ecn-paired.json')
        for update in (dict(version=2), dict(interval_us=0), dict(tcp_ecn=False), dict(path='/tmp/wrong')):
            bad = copy.deepcopy(profile)
            bad['ecn'].update(update)
            with self.assertRaises(ValueError):
                validate(bad)
        lab = Mock(assert_owned_sender=Mock(), tc=Mock(return_value='[{"root":true,"kind":"pfifo","handle":"1:"}]'))
        with self.assertRaises(RuntimeError):
            ecn_policy.configure(lab, ecn_policy.DEFAULT, 'ecn-static')
        lab.command.assert_not_called()

    def test_bridge_preserves_legacy_buffer_length(self):
        path = os.environ.get('FLOWCORE_ECN_PACING_BRIDGE')
        if path is None:
            self.skipTest('native pacing bridge path unavailable')
        bridge = ctypes.CDLL(path)
        fields = ctypes.c_uint64
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            values = (fields*17)(*[12345]*17)
            for name, end in (('fc_pacing_inspect', 12), ('fc_pacing_inspect_v2', 16)):
                inspect = getattr(bridge, name)
                inspect.argtypes = [ctypes.c_int, ctypes.POINTER(fields)]
                inspect.restype = ctypes.c_int
                self.assertEqual(inspect(sock.fileno(), values), 0)
                self.assertNotEqual(values[0], 0)
                self.assertEqual(list(values)[end:], [12345]*(17-end))

    def test_optional_extended_tcp_info(self):
        info = bytearray(280)
        info[5] = 24
        self.assertEqual(mode(tcp_info(info[:104])), 'classic-or-accurate')
        self.assertNotIn('received_ce_segments', tcp_info(info[:248]))
        struct.pack_into('=I', info, 248, 31)
        struct.pack_into('=I', info, 276, 2 if sys.byteorder == 'little' else 2 << 30)
        self.assertEqual(tcp_info(info)['received_ce_segments'], 31)
        self.assertEqual(mode(tcp_info(info)), 'accurate')

    def test_ipv4_packet_marks_and_ace(self):
        packet = bytearray(54)
        packet[12:14] = b'\x08\x00'
        packet[14:16] = bytes((0x45, 3))
        struct.pack_into('!H', packet, 16, 1280)
        packet[23] = 6
        packet[26:30] = socket.inet_aton('10.203.0.1')
        struct.pack_into('!HH', packet, 34, 50000, 9000)
        packet[46:48] = bytes((0x51, 0xc2))
        self.assertEqual(capture_fields(packet), ('10.203.0.1', 50000, 9000, True, 3, 0xc2, 7))
        self.assertIsNone(capture_fields(packet[:53]))
        packet[20] = 0x20
        self.assertIsNone(capture_fields(packet))


if __name__ == '__main__':
    unittest.main()
