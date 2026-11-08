"""Runnable gateway, backend, database-like service and offered-load client."""
import argparse
import asyncio
from collections import Counter, deque
import json
import ipaddress
from pathlib import Path
import random
import secrets
import signal
import sys
import time

from cooperative_rpc import Server, invoke, serve as serve_rpc
from cooperative_slo import History, MAX_BUDGET_US, MAX_BYTES, STALE_US, forecast, remaining_us
from durable_state import atomic_json, read_json
from fabric_protocol import boolean, endpoint, fields, hexdigest, integer, tls
from workload import percentile

ROLES = ('gateway', 'backend', 'database')
WORK_US = {'gateway': 400, 'backend': 1200, 'database': 2500}
MAX_ACTIVE = 80


class Application:
    def __init__(self, cfg):
        self.cfg = cfg
        self.role = cfg['role']
        self.enforce = cfg['cooperative']
        self.client = tls(cfg['tls'])
        self.history = {key: History() for key in cfg['peers']}
        self.recent = deque(maxlen=2048)
        self.active = 0
        self.advice = None

    def plan(self, deadline, payload_size):
        now = time.monotonic_ns()
        duration = WORK_US[self.role]+1000
        if self.role == 'database':
            nodes = [(0, duration)]
        elif self.role == 'backend':
            nodes = [(0, duration), (1, self.history['database'].high(now))]
        else:
            # Each peer's entire RPC duration includes its own downstream work.
            nodes = [(0, duration), (1, self.history['backend'].high(now)),
                     (1, self.history['database'].high(now))]
        # A verified operator cap and its utilization supply a conservative
        # serialization hint. They are neither a reservation nor path bandwidth.
        if self.advice and 0 <= now-self.advice['sample_ns'] <= STALE_US*1000:
            cap, used = self.advice['cap_kbit'], self.advice['observed_kbit']
            if used is not None and used*5 >= cap*4:
                transfer_hint = (payload_size*8000 + cap-1)//cap
                nodes = [(mask, value if not mask or value is None else max(value, transfer_hint))
                         for mask, value in nodes]
        return forecast(nodes, remaining_us(deadline))[0]

    async def work(self, request, budget, deadline):
        started = time.monotonic_ns()
        advice = self.plan(deadline, len(request['payload']))
        status, processing = 'cancelled', 0
        hops, child_spans = [], []
        self.active += 1
        try:
            if self.enforce and advice == 'expired':
                status = 'expired'
            elif self.enforce and self.active > (6 if advice == 'at_risk' else 24):
                status = 'deferred'
            else:
                limit = remaining_us(deadline)/1e6 if self.enforce else 2.8
                async with asyncio.timeout(limit):
                    begin = time.monotonic_ns()
                    await asyncio.sleep(WORK_US[self.role]/1e6)
                    processing = (time.monotonic_ns()-begin)//1000
                    peers = list(self.cfg['peers'])
                    async with asyncio.TaskGroup() as group:
                        children = [group.create_task(invoke(self.cfg['peers'][name], self.client, self.role,
                            request['trace'], request['span'], deadline, request['payload'], self.enforce))
                            for name in peers]
                    for name, child in zip(peers, children, strict=True):
                        hop, spans = child.result()
                        hops.append(hop)
                        child_spans.extend(spans)
                        if hop['status'] == 'ok':
                            self.history[name].record(hop['elapsed_us'], time.monotonic_ns())
                    errors = [hop['status'] for hop in hops if hop['status'] != 'ok']
                    status = ('ok' if not errors else errors[0] if len(errors) == len(hops) and
                              len(set(errors)) == 1 else 'partial')
        except TimeoutError:
            status = 'expired' if self.enforce else 'transport'
        finally:
            self.active -= 1
            finished = time.monotonic_ns()
            duration = (finished-started)//1000
            self.recent.append((finished, duration, status, advice, finished > deadline))
        span = dict(span=request['span'], parent=request['parent'], service=self.role, status=status,
                    duration_us=duration, processing_us=processing, budget_us=budget, hops=hops,
                    forecast=advice)
        return status, [span, *child_spans]

    async def report(self):
        path = Path(self.cfg['summary'])
        while True:
            await asyncio.sleep(.4)
            now = time.monotonic_ns()
            while self.recent and now-self.recent[0][0] > 1_500_000_000:
                self.recent.popleft()
            if 'advice' in self.cfg:
                try:
                    value = await asyncio.to_thread(read_json, Path(self.cfg['advice']), 1024)
                    fields(value, ('sample_ns', 'cap_kbit', 'observed_kbit'))
                    integer(value['sample_ns'], 1, 2**63-1)
                    integer(value['cap_kbit'], 100, 1_000_000)
                    if value['observed_kbit'] is not None:
                        integer(value['observed_kbit'], 0, 100_000_000)
                    self.advice = value
                except (OSError, ValueError, KeyError, TypeError):
                    self.advice = None
            if not self.recent:
                continue
            rows = list(self.recent)
            good = [row[1] for row in rows if row[2]=='ok']
            status = Counter(row[2] for row in rows)
            summary = dict(service=self.role, offered=len(rows), risk=sum(row[3]=='at_risk' for row in rows),
                           misses=sum(row[4] or row[2]!='ok' for row in rows), errors=len(rows)-status['ok'],
                           deferred=status['deferred'], p95_us=percentile(good, .95))
            if summary['p95_us'] is not None:
                summary['p95_us'] = round(summary['p95_us'])
            try:
                await asyncio.to_thread(atomic_json, path, dict(sample_ns=now, summary=summary))
            except OSError:
                pass  # The agent ages out the last sample; request handling continues.


async def serve(cfg):
    app = Application(cfg)
    rpc = Server(cfg['pins'], app.work)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    reporting = asyncio.create_task(app.report())
    listening = asyncio.create_task(serve_rpc(cfg['host'], cfg['port'], tls(cfg['tls'], server=True), rpc, stop))
    try:
        done, _ = await asyncio.wait((listening, reporting), return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        reporting.cancel()
        stop.set()
        await asyncio.gather(reporting, listening, return_exceptions=True)


async def load(cfg):
    context = tls(cfg['tls'])
    records, pending = [], set()
    rng = random.Random(cfg['seed'])
    # Same offered schedule and payload sizes in each paired arm.
    payloads = ['a'*cfg['payload_bytes'], 'b'*(cfg['payload_bytes']//2)]
    start, duration, rate = (cfg[key] for key in ('start_ns', 'duration', 'rate'))
    deadline_us = cfg['deadline_us']
    async def one(index, scheduled, payload):
        trace, root = secrets.token_hex(16), secrets.token_hex(16)
        deadline = scheduled + deadline_us*1000
        lag_us = (time.monotonic_ns()-scheduled)//1000
        hop, spans = await invoke(cfg['gateway'], context, 'client', trace, root, deadline, payload, cfg['cooperative'])
        finish = time.monotonic_ns()
        records.append(dict(id=index, trace=trace, parent=root, scheduled_ns=scheduled,
            elapsed_us=(finish-scheduled)//1000, met=hop['status']=='ok' and finish<=deadline,
            status=hop['status'], bytes=len(payload), hop=hop, spans=spans, dispatch_lag_us=lag_us))
    for index in range(rate*duration):
        scheduled = start + index*1_000_000_000//rate
        await asyncio.sleep(max(0, (scheduled-time.monotonic_ns())/1e9))
        if len(pending) >= MAX_ACTIVE:
            records.append(dict(id=index, scheduled_ns=scheduled, status='client_limit', met=False,
                                bytes=0, elapsed_us=0, spans=[]))
            continue
        task = asyncio.create_task(one(index, scheduled, rng.choice(payloads)))
        pending.add(task)
        task.add_done_callback(pending.discard)
    if pending:
        await asyncio.gather(*pending)
    records.sort(key=lambda row: row['id'])
    if [row['id'] for row in records] != list(range(rate*duration)):
        raise RuntimeError('offered RPC ledger incomplete')
    path = Path(cfg['output'])
    with path.open('x') as stream:
        for row in records:
            stream.write(json.dumps(row, separators=(',', ':'))+'\n')


async def bulk_sink(cfg):
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, stop.set)
    received, active = 0, 0
    async def receive(reader, writer):
        nonlocal received, active
        if active >= 4:
            writer.close()
            return
        active += 1
        try:
            while chunk := await reader.read(65_536):
                received += len(chunk)
        finally:
            active -= 1
            writer.close()
    async with await asyncio.start_server(receive, cfg['host'], cfg['port'], backlog=8):
        print('ready', flush=True)
        with Path(cfg['output']).open('x', buffering=1) as stream:
            while not stop.is_set():
                stream.write(json.dumps(dict(time_ns=time.monotonic_ns(), bytes=received))+'\n')
                await asyncio.sleep(.1)


async def bulk_send(cfg):
    await asyncio.sleep(max(0, (cfg['start_ns']-time.monotonic_ns())/1e9))
    _, writer = await asyncio.open_connection(cfg['host'], cfg['port'])
    writer.transport.set_write_buffer_limits(high=32768)
    payload = bytes(8192)
    try:
        end = cfg['start_ns'] + cfg['duration']*1_000_000_000
        async with asyncio.timeout(max(0, (end-time.monotonic_ns())/1e9)):
            while True:
                writer.write(payload)
                await writer.drain()
                await asyncio.sleep(0)
    except TimeoutError:
        pass
    finally:
        writer.transport.abort()


def config(command, value):
    def target(row, service):
        fields(row, ('host','port','server_name','service'))
        endpoint({key: row[key] for key in ('host','port','server_name')})
        if row['service'] != service:
            raise ValueError('RPC endpoint role mismatch')

    def absolute(key):
        path = value[key]
        if not isinstance(path, str) or not Path(path).is_absolute():
            raise ValueError(f'{key} must be an absolute path')

    if command == 'serve':
        fields(value, ('role','host','port','cooperative','tls','summary','peers','pins'), ('advice',))
        role = value['role']
        allowed = {'gateway': ('client',), 'backend': ('gateway',), 'database': ('gateway','backend')}
        children = {'gateway': ('backend','database'), 'backend': ('database',), 'database': ()}
        if not isinstance(role, str) or role not in allowed:
            raise ValueError('invalid service role')
        fields(value['peers'], children[role])
        for name, peer in value['peers'].items():
            target(peer, name)
        pins = value['pins']
        if not isinstance(pins, dict) or not 1 <= len(pins) <= 8 or any(role_name not in allowed[role] for role_name in pins.values()):
            raise ValueError('invalid RPC peer allowlist')
        for pin in pins:
            hexdigest(pin)
        boolean(value['cooperative'])
        absolute('summary')
        if 'advice' in value:
            absolute('advice')
    elif command == 'load':
        fields(value, ('gateway','tls','cooperative','seed','start_ns','duration','rate','deadline_us','payload_bytes','output'))
        target(value['gateway'], 'gateway')
        boolean(value['cooperative'])
        integer(value['seed'], 0, 2**32-1)
        integer(value['rate'], 1, 80)
        integer(value['deadline_us'], 1, MAX_BUDGET_US)
        integer(value['payload_bytes'], 1, MAX_BYTES)
        absolute('output')
    elif command == 'bulk_sink':
        fields(value, ('host','port','output'))
        absolute('output')
    else:
        fields(value, ('host','port','start_ns','duration'))
    if command != 'load':
        if not isinstance(value['host'], str):
            raise ValueError('expected IPv4 address')
        ipaddress.IPv4Address(value['host'])
        integer(value['port'], 1, 65535)
    if command in ('load','bulk_send'):
        integer(value['duration'], 1, 20)
        integer(value['start_ns'], 1, time.monotonic_ns()+60_000_000_000)
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = dict(serve=serve, load=load, bulk_sink=bulk_sink, bulk_send=bulk_send)
    parser.add_argument('command', choices=commands)
    parser.add_argument('config', type=Path)
    args = parser.parse_args()
    asyncio.run(commands[args.command](config(args.command, read_json(args.config, limit=8192))))


if __name__ == '__main__':
    sys.exit(main())
