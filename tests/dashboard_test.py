"""Dashboard authentication, bounded retention, and operational API regression cases."""
from concurrent.futures import ThreadPoolExecutor
import copy
import http.client
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from dashboard import Dashboard, Sampler, Server, now_ms, token_file
from dashboard_artifacts import Artifacts, latency, normalize
from dashboard_history import History
from dashboard_model import service_row

TOKEN = b'dashboard-test-authorization-fixture-12345'


def sample():
    return dict(schema=1, captured_ms=now_ms(), monotonic_ns=1, scope='owned_lab_namespaces',
                window_seconds=10, status={'healthy': True}, quality={'truncated': False},
                interfaces=[dict(id='lab:fc0', name='fc0', directions={
                    'egress': {'throughput_kbit': 1200, 'backlog_bytes': 77, 'dropped_packets': 0},
                    'ingress': {'throughput_kbit': None}})],
                classes=[], services=[], bottlenecks=[], controller={'state': 'STABLE'},
                policy_history=[dict(time_ns=i, result='hold') for i in range(128, 0, -1)],
                incidents=dict(retention=16, evicted=4, incidents=[dict(id='sample', event_count=512)]),
                link_events=[])


class Source:
    def __init__(self):
        self.available = True
        self.calls = 0

    def snapshot(self, window):
        if not self.available:
            raise OSError('fixture failure')
        return sample()

    def call(self, op, **args):
        assert op == 'monitor_incident' and args == {'id': 'sample'}
        self.calls += 1
        return dict(id='sample', origin_ns=1000, report={'assessment': 'CANDIDATES_ONLY'},
                    events=[dict(seq=index, time_ns=index+1000,
                                 kind='fault' if index % 2 else 'queue', data={}) for index in range(512)])


class DashboardTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name)
        self.history = History(self.path, hours=1, samples=16, storage_mib=8)
        self.addCleanup(self.history.close)
        (self.path/'history-v1.sqlite').chmod(0o600)
        self.source = Source()
        self.sampler = Sampler(self.source, self.history, window=10, interval=1, probe=lambda: None)
        self.artifacts = Artifacts({'actual': self.path/'actual.json', 'symlink': self.path/'link.json'})
        self.server = Server(('127.0.0.1', 0), Dashboard(self.sampler, self.artifacts), TOKEN)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def get(self, path, *, token=TOKEN, method='GET', host=None, body=None):
        headers = {} if token is None else {'Authorization': 'Bearer '+token.decode()}
        if host is not None:
            headers['Host'] = host
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        try:
            connection.request(method, path, headers=headers, body=body)
            reply = connection.getresponse()
            payload = reply.read()
            if reply.headers.get_content_type() == 'application/json':
                payload = json.loads(payload)
            return reply.status, payload, reply.headers
        finally:
            connection.close()

    def test_authentication_origin_and_read_only(self):
        for token in (None, b'wrong'):
            self.assertEqual(self.get('/api/v1/overview', token=token)[0], 401)
        self.assertEqual(self.get('/api/v1/health', host='untrusted.example')[0], 421)
        self.assertEqual(self.get('/api/v1/overview', method='POST')[0], 405)
        self.assertEqual(self.get('/api/v1/lab_up')[0], 404)
        self.assertEqual(self.get('/api/v1/health', body=b'body')[0], 400)
        self.assertEqual(self.get('/api/v1/health', host='a'*8500)[0], 431)
        code, _, headers = self.get('/', token=None)
        self.assertEqual(code, 200)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertIn("frame-ancestors 'none'", headers['Content-Security-Policy'])
        self.assertIsNone(headers['Access-Control-Allow-Origin'])

    def test_missing_stale_and_recovers(self):
        self.assertEqual(self.get('/api/v1/overview')[0], 503)
        self.assertEqual(self.get('/api/v1/health')[1]['status'], 'unavailable')
        self.sampler.collect()
        code, live, _ = self.get('/api/v1/overview')
        self.assertEqual(code, 200)
        self.assertEqual(live['status'], 'ok')
        self.assertEqual(live['data']['interfaces'][0]['directions']['egress']['backlog_bytes'], 77)
        self.source.available = False
        self.sampler.collect()
        _, stale, _ = self.get('/api/v1/overview')
        self.assertEqual(stale['status'], 'stale')
        self.assertEqual(stale['meta']['captured_ms'], live['meta']['captured_ms'])
        self.source.available = True
        self.sampler.collect()
        self.assertEqual(self.get('/api/v1/health')[1]['status'], 'ok')

    def test_history_retention_limits_and_gaps(self):
        stamp = now_ms()-60_000
        self.history.append(stamp-3601_000, sample())
        for i in range(40):
            self.history.append(stamp+i*1000, sample() if i % 2 else None)
        self.assertEqual(self.history.db.execute('SELECT count(*) FROM samples').fetchone()[0], 16)
        self.assertLessEqual(self.history.db.execute('PRAGMA max_page_count').fetchone()[0] * 4096, 8*1024*1024)
        base = '/api/v1/history?kind=interface&id=lab%3Afc0&seconds=60'
        self.assertEqual(self.get(base+'&points=301')[0], 400)
        self.assertEqual(self.get(base+'&points=2&points=1')[0], 400)
        self.assertEqual(self.get(base+'&points=300&extra=1')[0], 400)
        values = self.get(base+'&points=300')[1]['data']['points']
        self.assertEqual(len(values), 16)
        self.assertTrue(any(point['values'] is None for point in values))
        self.assertEqual(values[-1]['values']['egress_kbit'], 1200)
        self.assertLessEqual(len(self.get(base+'&points=3')[1]['data']['points']), 3)

    def test_storage_cap_keeps_newer_samples(self):
        directory = self.path/'small'
        directory.mkdir(mode=0o700)
        history = History(directory, hours=1, samples=500, storage_mib=1)
        try:
            snapshot = sample()
            snapshot['services'] = [dict(id=f'api-{i}', p99_ms=4000.25) for i in range(64)]
            stamp = now_ms()-10_000
            for i in range(160):
                history.append(stamp+i, snapshot)
            rows = history.db.execute('SELECT count(*), max(ts) FROM samples').fetchone()
            self.assertLess(rows[0], 160)
            self.assertEqual(rows[1], stamp+159)
            self.assertLessEqual((directory/'history-v1.sqlite').stat().st_size, 1024*1024)
        finally:
            history.close()

    def test_paged_decisions_and_high_volume_incident(self):
        self.sampler.collect()
        page = self.get('/api/v1/policy-history?offset=32&limit=32')[1]['data']
        self.assertEqual((page['total'], page['items'][0]['time_ns']), (128, 96))
        self.assertEqual(self.get('/api/v1/policy-history?limit=33')[0], 400)
        endpoint = '/api/v1/incident?id=sample&kind=fault&offset=250&limit=6'
        with ThreadPoolExecutor(max_workers=8) as workers:
            pages = list(workers.map(lambda _: self.get(endpoint)[1]['data'], range(8)))
        self.assertEqual(self.source.calls, 1)
        self.assertTrue(all(page == pages[0] for page in pages))
        self.assertEqual(pages[0]['total'], 256)
        self.assertEqual([row['seq'] for row in pages[0]['items']], list(range(501, 512, 2)))
        self.assertEqual(self.get('/api/v1/incident?id=..%2Fprivate')[0], 400)
        self.assertEqual(self.get('/api/v1/incident?id=unknown')[0], 404)

    def test_artifact_provenance_and_scoped_paths(self):
        rows = [dict(mode='static', status='MEASURED', host={'kernel': 'Linux'},
                     http={'p99_ms': 12}, controller_state={})]
        self.assertEqual(normalize(rows)['measured_runs'], 1)
        self.assertEqual(latency({'failed': 2, 'offered': 10, 'failure_ratio': .2})['errors'], 2)
        simulated = copy.deepcopy(rows)
        simulated[0]['simulated'] = True
        result = normalize(simulated)
        self.assertEqual((result['provenance'], result['measured_runs']), ('simulated', 0))
        (self.path/'actual.json').write_text(json.dumps(rows))
        (self.path/'link.json').symlink_to('actual.json')
        self.assertEqual(self.get('/api/v1/benchmark?id=actual')[1]['data']['items'][0]['latency']['p99_ms'], 12)
        self.assertEqual(self.get('/api/v1/benchmark?id=symlink')[0], 422)
        self.assertEqual(self.get('/api/v1/benchmark?id=..%2Factual')[0], 400)
        self.assertEqual(self.get('/api/v1/benchmark?id=missing')[0], 404)

    def test_token_permissions_and_sample_denominators(self):
        path = self.path/'secret'
        path.write_bytes(TOKEN)
        path.chmod(0o644)
        with self.assertRaises(PermissionError):
            token_file(path)
        path.chmod(0o600)
        self.assertEqual(token_file(path), TOKEN)
        source = dict(ok=80, timeout=3, failure=2, samples=80, deadline_reported=20,
                      deadline_miss=4, p50_us=1000, p95_us=2000, p99_us=6000)
        row = service_row('demo', dict(traffic_class='critical', application=source), 10, {'slo_ms': 5})
        self.assertEqual(row['error_rate'], 5/85)
        self.assertEqual(row['deadline_miss_rate'], 4/20)
        self.assertEqual(row['requests_per_second'], 8.5)
        self.assertTrue(row['above_target'])


if __name__ == '__main__':
    os.umask(0o077)
    unittest.main()
