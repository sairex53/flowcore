"""Budget, forecast, schema and real TLS RPC regressions."""
import asyncio
import os
from pathlib import Path
import secrets
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from cooperative_app import config
from cooperative_rpc import LIMIT, ReplayWindow, Server, invoke, read, send
from cooperative_slo import aggregate, forecast, receive_budget
from fabric_lab import private_pki
from fabric_protocol import tls
from paths import BINDIR


class Model(unittest.TestCase):
    def test_clock_and_dependencies(self):
        self.assertEqual(receive_budget(100, 1_000, 11_001), 89)
        self.assertEqual(receive_budget(10, 1_000, 11_001), 0)
        self.assertEqual(receive_budget(10, 1_000, 999), 0)
        # Local work, two parallel branches, and a fan-in continuation.
        plan = [(0, 2000), (1, 14000), (1, 8000), (6, 1000)]
        self.assertEqual(forecast(plan, 20000), ('fits', 17000))
        self.assertEqual(forecast(plan, 16000), ('at_risk', 17000))
        self.assertEqual(forecast([(0, 2), (1, None)], 10), ('insufficient', 2))
        self.assertEqual(forecast([(2, 2)], 10), ('invalid', 0))
        self.assertEqual(forecast(plan, 0), ('expired', 17000))
        self.assertEqual(forecast([(0, 1)]*33, 10), ('invalid', 0))

    def test_application_config(self):
        cfg = dict(host='127.0.0.1', port=30000, start_ns=time.monotonic_ns(), duration=5)
        self.assertEqual(config('bulk_send', cfg), cfg)
        for patch in (dict(host=1), dict(duration=1000), dict(port=0), dict(secret='unexpected')):
            with self.assertRaises(ValueError):
                config('bulk_send', dict(cfg, **patch))

    def test_replay_capacity_and_summary(self):
        replay = ReplayWindow(limit=2, ttl_seconds=10)
        self.assertTrue(replay.admit('a', 3))
        self.assertFalse(replay.admit('a', 4))
        self.assertTrue(replay.admit('b', 5))
        self.assertFalse(replay.admit('c', 6))
        self.assertTrue(replay.admit('c', 13))
        row = dict(service='gateway', age_us=0, offered=5, risk=4, misses=2, errors=1, deferred=0, p95_us=None)
        self.assertEqual(aggregate(row), row)
        for update in (dict(risk=6), dict(priority='critical'), dict(offered=True), dict(age_us=2_500_001)):
            with self.assertRaises(ValueError):
                aggregate(dict(row, **update))


class Network(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        directory = tempfile.TemporaryDirectory(dir=BINDIR)
        cls.addClassCleanup(directory.cleanup)
        with patch('fabric_lab.UID', os.getuid()), patch('fabric_lab.GID', os.getgid()):
            cls.ids, cls.pins = private_pki(Path(directory.name), ('client','gateway'))

    async def asyncSetUp(self):
        self.entered, self.cancelled = asyncio.Event(), asyncio.Event()
        self.calls = []
        async def work(request, budget, deadline):
            self.calls.append((request, budget, deadline))
            self.entered.set()
            began = time.monotonic_ns()
            try:
                if request['payload'] == 'slow':
                    await asyncio.sleep(1)
                node = dict(span=request['span'], parent=request['parent'], service='gateway', status='ok',
                            duration_us=(time.monotonic_ns()-began)//1000, processing_us=0,
                            budget_us=budget, hops=[], forecast='fits')
                return 'ok', [node]
            except asyncio.CancelledError:
                self.cancelled.set()
                raise
        rpc = Server({self.pins['client']:'client'}, work)
        self.server = await asyncio.start_server(rpc.handle,'127.0.0.1',0,ssl=tls(self.ids['gateway'],server=True),
                                                 ssl_shutdown_timeout=.2, limit=LIMIT)
        self.port = self.server.sockets[0].getsockname()[1]
        self.endpoint = dict(host='127.0.0.1',port=self.port,server_name='gateway.test',service='gateway')
        self.context = tls(self.ids['client'])
        self.ids_for_request = dict(trace=secrets.token_hex(16),span=secrets.token_hex(16),parent=secrets.token_hex(16))

    async def asyncTearDown(self):
        self.server.close()
        await self.server.wait_closed()

    async def call(self, payload, context=None):
        return await invoke(self.endpoint, context or self.context, 'client',
            self.ids_for_request['trace'], self.ids_for_request['parent'],
            time.monotonic_ns()+140_000_000, payload, True)

    async def test_tls_budget_and_timeout_cancel(self):
        hop, nodes = await self.call('fast')
        self.assertEqual(hop['status'], 'ok')
        self.assertEqual(nodes[0]['parent'], self.ids_for_request['parent'])
        sent, received, _ = self.calls[0]
        self.assertLess(received, sent['budget_us'])
        self.assertLessEqual(sent['budget_us'], 140_000)
        hop, nodes = await self.call('slow')
        self.assertEqual((hop['status'], nodes), ('expired', []))
        await asyncio.wait_for(self.cancelled.wait(), .5)

    async def test_authenticated_role_and_replay(self):
        other = tls(self.ids['other'])
        self.assertEqual((await self.call('fast', other))[0]['status'], 'transport')
        self.assertFalse(self.calls)

        async def one(**override):
            reader, writer = await asyncio.open_connection('127.0.0.1', self.port, ssl=self.context,
                server_hostname='gateway.test', ssl_shutdown_timeout=.2, limit=LIMIT)
            try:
                challenge = await read(reader)
                msg = dict(op='invoke', nonce=challenge['nonce'], caller='client', payload='fast',
                           budget_us=100_000, **self.ids_for_request)
                msg.update(override)
                await send(writer,msg)
                return await asyncio.wait_for(read(reader),1)
            finally:
                writer.close()
                await writer.wait_closed()
        with self.assertRaises(asyncio.IncompleteReadError):
            await one(caller='backend')
        with self.assertRaises(asyncio.IncompleteReadError):
            await one(priority='critical')
        with self.assertRaises(asyncio.IncompleteReadError):
            await one(nonce='f'*32)
        self.assertEqual((await one())['status'],'ok')
        with self.assertRaises(asyncio.IncompleteReadError):
            await one()
        self.assertEqual(len(self.calls),1)


if __name__ == '__main__':
    unittest.main()
