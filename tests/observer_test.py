"""Deterministic tests for the telemetry pipeline."""
import copy
import json
import os
import random
import socket
import struct
import sys
import time
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from bpf_source import BpfSource, validate_record
from connection_lifecycle import Lifecycles
from observability import NANO, Observer, delta
from observability_config import DEFAULT, validate
from tcp_diag import connections, tcp_info
from telemetry_windows import Histogram, Windows


class ObserverTests(unittest.TestCase):
    def engine(self):
        self.now = 10 * NANO
        cfg = copy.deepcopy(DEFAULT)
        cfg['thresholds'].update(window_seconds=1, min_samples=1)
        return Observer(cfg, lambda: self.now)

    def test_tcp_info_short_and_real(self):
        data = bytearray(248)
        struct.pack_into('=I', data, 68, 1234)
        struct.pack_into('=Q', data, 104, 345_000)
        struct.pack_into('=Q', data, 112, 500_000)
        data[7] = 1
        struct.pack_into('=H', data, 240, 2)
        self.assertEqual(tcp_info(data)['total_rto'], 2)
        self.assertEqual(tcp_info(data)['pacing_rate_bytes_per_second'], 345_000)
        self.assertEqual(tcp_info(data)['max_pacing_rate_bytes_per_second'], 500_000)
        self.assertTrue(tcp_info(data)['delivery_rate_app_limited'])
        self.assertNotIn('total_rto', tcp_info(data[:104]))
        self.assertNotIn('max_pacing_rate_bytes_per_second', tcp_info(data[:112]))
        self.assertEqual(tcp_info(data[:104])['rtt_us'], 1234)
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            listener.listen()
            with socket.create_connection(listener.getsockname()) as client:
                conn, _ = listener.accept()
                with conn:
                    client.sendall(b'x')
                    self.assertEqual(conn.recv(1), b'x')
                    rows, limited = connections(1024)
                    local = client.getsockname()[1]
                    row = next(r for r in rows if r['local_port'] == local and r['state'] == 'established')
                    direct = tcp_info(client.getsockopt(socket.IPPROTO_TCP, socket.TCP_INFO, 512))
                    self.assertEqual(row['tcp_info']['bytes_sent'], direct['bytes_sent'])
                    self.assertEqual(row['tcp_info']['rtt_us'], direct['rtt_us'])
                    self.assertTrue(row['memory']['send_buffer_bytes'])
                    self.assertIsNotNone(row['cookie'])
                    self.assertFalse(limited)

    def test_histogram_high_volume(self):
        hist = Histogram()
        hist.add(0)
        self.assertEqual(hist.quantile(.99), 0)
        for value in range(1, 100001):
            hist.add(value)
        self.assertEqual(hist.count, 100001)
        self.assertLessEqual(len(hist.counts), 257)
        for quantile in (.5, .95, .99):
            estimate = hist.quantile(quantile)
            exact = int(100000 * quantile)
            self.assertGreaterEqual(estimate, exact)
            self.assertLessEqual(estimate, exact * 1.091 + 1)

        overflow = Histogram()
        overflow.add(2**40)
        self.assertIsNone(overflow.quantile(.99))
        self.assertEqual(overflow.summary()['overflow_samples'], 1)

    def test_late_and_ordered_windows(self):
        windows = Windows(10)
        for second in range(100):
            windows.at(second * NANO).counters['event'] += 1
        self.assertEqual(len(windows.buckets), 10)
        self.assertIsNone(windows.at(80 * NANO))
        windows.at(95 * NANO).counters['event'] += 3
        self.assertEqual(windows.late_drops, 1)
        self.assertEqual(windows.summarize(99 * NANO, 10).counters['event'], 13)

    def test_counters_and_lifecycle(self):
        observer = self.engine()
        row = dict(namespace='sender', family=2, local_address='127.0.0.1', remote_address='127.0.0.1',
                   local_port=1200, remote_port=9000, cookie='0000000000001234', state='established',
                   tcp_info={'rtt_us': 1200, 'total_retrans_segments': 1, 'data_segments_out': 10})
        observer.track([dict(row)], False, self.now)
        self.now += NANO
        observer.track([dict(row, state='fin_wait_1', tcp_info={'total_retrans_segments': 2,
                                                                'data_segments_out': 20})], False, self.now)
        self.assertEqual(observer.last_rows[0]['observed_age_ms'], 1000)
        self.assertEqual(observer.events[-1]['kind'], 'sampled_state_change')
        self.assertEqual(observer.window(self.now, 1)['retransmit_ratio'], .1)
        self.now += NANO
        observer.track([dict(row, remote_port=9010)], False, self.now)
        self.assertEqual(observer.last_rows[0]['observed_age_ms'], 0)
        self.assertIsNone(delta({'counter': 1}, {'counter': 2}, 'counter'))

    def test_ingestion_hysteresis_and_correlation(self):
        observer = self.engine()
        started = observer.ingest({'op': 'request_start', 'service': 'demo', 'deadline_ms': 1}, 20)
        with self.assertRaises(ValueError):
            observer.ingest(dict(op='request_complete', service='demo', token=started['token'], outcome='ok'), 21)
        self.now += 2_000_000
        observer.ingest(dict(op='request_complete', service='demo', token=started['token'], outcome='ok'), 20)
        for _ in range(2):
            observer.ingest(dict(op='request_sample', service='demo', duration_us=40000, outcome='ok'), 20)
            observer.windows.at(self.now).observe('rtt', 40000)
            observer.detect(self.now)
            self.now += NANO
        self.assertTrue(observer.snapshot()['findings']['demo.slo']['active'])
        self.assertTrue(any(event['kind'] == 'correlated_observations' and not event['causal_claim']
                            for event in observer.events))
        for _ in range(3):
            observer.ingest(dict(op='request_sample', service='demo', duration_us=1000, outcome='ok'), 20)
            observer.detect(self.now)
            self.now += NANO
        self.assertFalse(observer.snapshot()['findings']['demo.slo']['active'])
        with self.assertRaises(ValueError):
            observer.ingest(dict(op='request_sample', service='fake', duration_us=1, outcome='ok'), 20)
        for _ in range(1024):
            observer.ingest({'op': 'request_start', 'service': 'demo'}, 20)
        with self.assertRaises(ValueError):
            observer.ingest({'op': 'request_start', 'service': 'demo'}, 20)

    def test_cooperative_byte_and_udp_message_accounting(self):
        observer = self.engine()
        row = dict(op='request_sample', service='demo', outcome='ok', duration_us=1000,
                   transport='udp', bytes_offered=1200, bytes_admitted=1200,
                   bytes_transmitted=1100, bytes_received=2400,
                   udp_messages_attempted=3, udp_messages_transmitted=2,
                   udp_send_failures=1, udp_urgent_messages=1, udp_background_messages=2)
        observer.ingest(row, 20)
        app = observer.window(self.now, 1)['application']['demo']['application']
        self.assertEqual(app['traffic_reports'], 1)
        self.assertEqual(app['traffic']['bytes_received'], 2400)
        self.assertEqual(app['transports']['udp']['reports'], 1)
        self.assertEqual(app['transports']['udp']['message_reports'], 1)
        self.assertEqual(app['transports']['udp']['udp_messages_transmitted'], 2)
        with self.assertRaises(ValueError):
            observer.ingest(dict(row, udp_messages_transmitted=4), 20)

    def test_intent_lifecycle_marks_window_incomplete_until_terminal_event(self):
        observer = self.engine()
        observer.intent_request_start('demo')
        self.assertFalse(observer.window(self.now, 1)['application']['demo']['application']
                         ['request_window_complete'])
        observer.ingest(dict(op='request_sample', service='demo', duration_us=1000, outcome='ok'), 20)
        observer.intent_request_complete('demo')
        window = observer.window(self.now, 1)['application']['demo']['application']
        self.assertTrue(window['request_window_complete'])
        self.assertEqual(window['requests_started'], 1)
        self.assertEqual(window['ok'], 1)

    def test_collector_failure_and_fallback(self):
        observer = self.engine()
        lab = Mock(enter=Mock(return_value=nullcontext()))
        with patch('observability.connections', side_effect=OSError('injected dump failure')):
            observer.sample(lab, {}, 20000)
        self.assertIn('inet_diag', observer.errors)
        self.assertIsNone(observer.snapshot()['sample_age_seconds'])
        with patch.object(Path, 'is_file', return_value=False):
            observer.bpf.start([], ROOT / 'build')
            self.assertEqual(observer.bpf.reason, 'kernel BTF unavailable')
        cfg = copy.deepcopy(DEFAULT)
        cfg['windows'] = [301]
        with self.assertRaises(ValueError):
            validate(cfg)

    def test_seeded_trace_mutations(self):
        rng = random.Random(4401)
        base = json.dumps(dict(version=1,type='loss',kernel=1,transport=2)).encode()
        for _ in range(2000):
            raw = bytearray(base)
            for _ in range(rng.randint(1,6)):
                raw[rng.randrange(len(raw))] = rng.randrange(256)
            try:
                row = validate_record(json.loads(raw))
            except (ValueError,KeyError,TypeError,RecursionError):
                continue
            self.assertEqual(row['version'],1)
            self.assertGreaterEqual(row['kernel'],0)

    def test_lifecycle_bounds_and_ordering(self):
        tracker = Lifecycles(32)
        event = dict(time_ns=10,flow_id=1,cookie='0000000000000001',netns=42,
                     sport=10,dport=20,family=2,kind=1,new=1,old=2,origin_state=2,lifetime_ns=0)
        for index in range(10000):
            tracker.ingest(dict(event,flow_id=index+1,time_ns=index+10))
        self.assertEqual(len(tracker.rows),32)
        self.assertEqual(len(tracker.cookies),1)
        self.assertEqual(tracker.evicted,9968)
        latest = dict(event,flow_id=10000,time_ns=10010,new=7,lifetime_ns=100)
        tracker.ingest(latest)
        tracker.ingest(dict(latest,time_ns=10000,new=1))
        self.assertEqual(tracker.out_of_order,1)
        self.assertEqual(next(reversed(tracker.rows.values()))['state'],'closed')
        joined = tracker.correlate(dict(cookie=event['cookie'],netns=42,cgroup_id=9))
        self.assertEqual(joined['flow_id'],10000)
        self.assertIsNone(tracker.correlate(dict(cookie=event['cookie'],netns=43)))
        tracker.ingest(dict(latest,time_ns=10011,cookie='0000000000000002'))
        self.assertEqual(tracker.identity_conflicts,1)
        self.assertEqual(next(reversed(tracker.rows.values()))['cookie'],event['cookie'])

    def test_trace_parser_bounds(self):
        for row in (dict(version=2),dict(version=1,type='ready',hooks=['arbitrary'],unavailable_hooks=[]),
                    dict(version=1,type='loss',kernel=-1,transport=0),
                    dict(version=1,type='tcp',time_ns='bad')):
            with self.assertRaises(ValueError):
                validate_record(row)
        observer = self.engine()
        observer.track([],False,self.now)
        observer.track([],False,self.now-1)
        self.assertEqual(observer.snapshot_order_drops,1)

    def test_bpf_loss_and_chunking(self):
        src = BpfSource('auto')
        read, write = os.pipe()
        with os.fdopen(read, 'rb') as stream:
            src.proc = Mock(stdout=stream)
            src.started = time.monotonic()
            try:
                ready = dict(version=1,type='ready',hooks=['tcp_state'],
                             unavailable_hooks=[],mechanism='raw_tracepoint')
                event = dict(version=1,type='tcp',time_ns=1,cookie='0000000000000001',
                             lifetime_ns=0,flow_id=1,origin_state=2,netns=42,sport=10,dport=20,
                             family=2,kind=1,old=2,new=1,local_address='127.0.0.1',remote_address='127.0.0.1')
                os.write(write,json.dumps(ready).encode()+b'\n{"version":1,"type":"loss","kernel":')
                self.assertEqual(src.drain(), [])
                os.write(write,b'12,"transport":4}\ninvalid\n'+json.dumps(event).encode()+b'\n')
                self.assertEqual(len(src.drain()), 1)
                self.assertEqual(src.status()['kernel_reserve_drops'], 12)
                self.assertEqual(src.status()['transport_drops'], 4)
                self.assertEqual(src.status()['parse_drops'], 1)
            finally:
                os.close(write)


if __name__ == '__main__':
    unittest.main()
