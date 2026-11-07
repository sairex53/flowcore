"""Real unprivileged loopback coverage for the non-research workload path."""
import asyncio
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from workload import client


class WorkloadTest(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, inject_failure=False):
        received = [0, 0]
        handlers = set()

        async def serve(reader, writer, bulk=False):
            task = asyncio.current_task()
            handlers.add(task)
            try:
                if bulk:
                    flow = struct.unpack('!I', await reader.readexactly(4))[0]
                    while data := await reader.read(65536):
                        received[flow] += len(data)
                else:
                    await reader.readexactly(768)
                    writer.write(b'R' * 64)
                    await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()
                handlers.discard(task)

        requests = await asyncio.start_server(serve, '127.0.0.1', 0)
        bulk = await asyncio.start_server(lambda r, w: serve(r, w, True), '127.0.0.1', 0)
        request_port = requests.sockets[0].getsockname()[1]
        bulk_port = bulk.sockets[0].getsockname()[1]
        original = asyncio.open_connection

        async def connect(host, port):
            self.assertEqual(host, '10.203.0.2')
            if inject_failure and port == bulk_port:
                raise ValueError('fixture worker failure')
            return await original('127.0.0.1', port)

        try:
            with tempfile.TemporaryDirectory(dir=ROOT / 'build') as directory:
                path = Path(directory)
                with patch('workload.asyncio.open_connection', side_effect=connect):
                    if inject_failure:
                        with self.assertRaisesRegex(RuntimeError, 'workload worker failed'):
                            await client(path, .2, 10, 768, request_port, bulk_port, 2)
                    else:
                        await client(path, .2, 10, 768, request_port, bulk_port, 2)
                summary = json.loads((path / 'client.json').read_text())
                self.assertEqual(summary['completed'], 2)
                self.assertEqual(summary['failed'], 0)
                self.assertEqual(summary['worker_errors'], ['ValueError'] if inject_failure else [])
                if not inject_failure:
                    self.assertTrue(all(value > 0 for value in received))
        finally:
            requests.close()
            bulk.close()
            await requests.wait_closed()
            await bulk.wait_closed()
            if handlers:
                await asyncio.wait_for(asyncio.gather(*list(handlers)), 3)

    async def test_legacy_background_transfers(self):
        await self.exercise()

    async def test_worker_failure_is_not_silently_discarded(self):
        await self.exercise(True)


if __name__ == '__main__':
    unittest.main()
