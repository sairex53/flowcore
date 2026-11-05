"""Optional bounded application reporter for the TCP workload."""
import asyncio
import json
from paths import SOCKET


class Reporter:
    def __init__(self, service, deadline_ms):
        self.service = service
        self.deadline_ms = deadline_ms
        self.queue = asyncio.Queue(256)
        self.accepted = self.dropped = 0
        self.task = asyncio.create_task(self.run())

    def submit(self, duration_us, status):
        outcome = 'ok' if status == 'ok' else 'timeout' if status == 'TimeoutError' else 'failure'
        try:
            self.queue.put_nowait(dict(op='request_sample', service=self.service, outcome=outcome,
                                       duration_us=duration_us, deadline_ms=self.deadline_ms))
        except asyncio.QueueFull:
            self.dropped += 1

    async def run(self):
        while True:
            report = await self.queue.get()
            writer = None
            try:
                async with asyncio.timeout(1):
                    reader, writer = await asyncio.open_unix_connection(str(SOCKET), limit=4096)
                    writer.write(json.dumps(report).encode() + b'\n')
                    await writer.drain()
                    if json.loads(await reader.readline())['ok']:
                        self.accepted += 1
                    else:
                        self.dropped += 1
            except (OSError, ValueError, KeyError):
                self.dropped += 1
            except asyncio.CancelledError:
                self.dropped += 1
                raise
            finally:
                if writer:
                    writer.close()
                self.queue.task_done()

    async def close(self):
        try:
            await asyncio.wait_for(self.queue.join(), 3)
        except TimeoutError:
            self.dropped += self.queue.qsize()
        finally:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        return dict(accepted=self.accepted, dropped=self.dropped)
