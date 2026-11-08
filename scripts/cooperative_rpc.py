"""Experimental one-request-per-mTLS-stream RPC and cancellation."""
import asyncio
from collections import OrderedDict
import hashlib
import json
import secrets
import socket
import struct
import time

from cooperative_slo import MAX_BUDGET_US, MAX_BYTES, receive_budget, remaining_us
from fabric_protocol import fields, hexdigest, ident, integer
from ipc_security import decode

VERSION = 1
LIMIT = 32_768
TRANSPORT_SECONDS = 3
OUTCOMES = ('ok', 'partial', 'expired', 'deferred', 'cancelled', 'transport', 'failed')
ROLES = ('gateway', 'backend', 'database')
CANCEL = b'{"version":1,"op":"cancel"}'


async def read(reader):
    size, = struct.unpack('!I', await reader.readexactly(4))
    integer(size, 1, LIMIT)
    value = decode(await reader.readexactly(size))
    integer(value.get('version'), VERSION, VERSION)
    return value


async def send(writer, message):
    raw = json.dumps(dict(message, version=VERSION), separators=(',', ':'), allow_nan=False).encode()
    integer(len(raw), 1, LIMIT)
    writer.write(struct.pack('!I', len(raw)) + raw)
    await writer.drain()


def request(value, nonce, parent_role):
    fields(value, ('version', 'op', 'nonce', 'trace', 'span', 'parent', 'caller', 'budget_us', 'payload'))
    if value['op'] != 'invoke' or value['nonce'] != nonce or value['caller'] != parent_role:
        raise ValueError('unbound request')
    for key in ('trace', 'span', 'parent'):
        hexdigest(value[key], 32)
    if value['span'] == value['parent']:
        raise ValueError('self parent')
    integer(value['budget_us'], 0, MAX_BUDGET_US)
    if not isinstance(value['payload'], str) or len(value['payload']) > MAX_BYTES or not value['payload'].isascii():
        raise ValueError('invalid application payload')
    return value


def result(value, context, expected):
    fields(value, ('version', 'op', 'trace', 'span', 'status', 'spans'))
    if value['op'] != 'reply' or any(value[key] != context[key] for key in ('trace', 'span')):
        raise ValueError('unbound reply')
    if value['status'] not in OUTCOMES or not isinstance(value['spans'], list) or not 1 <= len(value['spans']) <= 8:
        raise ValueError('invalid reply')
    seen = set()
    for index, node in enumerate(value['spans']):
        fields(node, ('span', 'parent', 'service', 'status', 'duration_us', 'processing_us',
                      'budget_us', 'hops', 'forecast'))
        hexdigest(node['span'], 32)
        hexdigest(node['parent'], 32)
        if (node['span'] in seen or node['service'] not in ROLES or node['status'] not in OUTCOMES or
                (index and node['parent'] not in seen)):
            raise ValueError('invalid dependency tree')
        if not index and (node['span'] != context['span'] or node['parent'] != context['parent'] or
                          node['service'] != expected or node['status'] != value['status']):
            raise ValueError('unexpected service span')
        seen.add(node['span'])
        integer(node['duration_us'], 0, MAX_BUDGET_US)
        integer(node['processing_us'], 0, node['duration_us'])
        integer(node['budget_us'], 0, MAX_BUDGET_US)
        if node['forecast'] not in ('fits', 'at_risk', 'insufficient', 'expired', 'disabled'):
            raise ValueError('invalid forecast')
        if not isinstance(node['hops'], list) or len(node['hops']) > 3:
            raise ValueError('invalid hop count')
        for hop in node['hops']:
            fields(hop, ('child', 'service', 'elapsed_us', 'status', 'srtt_us'))
            hexdigest(hop['child'], 32)
            if hop['service'] not in ROLES or hop['status'] not in OUTCOMES:
                raise ValueError('invalid hop')
            integer(hop['elapsed_us'], 0, MAX_BUDGET_US)
            if hop['srtt_us'] is not None:
                integer(hop['srtt_us'], 1, MAX_BUDGET_US)
    return value


def srtt(writer):
    try:
        # Linux TCP_INFO tcpi_rtt, the same UAPI prefix used by FLOWCORE telemetry.
        raw = writer.get_extra_info('socket').getsockopt(socket.IPPROTO_TCP, socket.TCP_INFO, 72)
        value, = struct.unpack_from('=I', raw, 68)
        return value if 0 < value <= MAX_BUDGET_US else None
    except (OSError, struct.error):
        return None


async def invoke(endpoint, tls, caller, trace, parent, deadline_ns, payload, enforce):
    start = time.monotonic_ns()
    span = secrets.token_hex(16)
    context = dict(trace=trace, span=span, parent=parent)
    writer = None
    sent = False
    status, spans, rtt = 'transport', [], None
    timeout = min(TRANSPORT_SECONDS, remaining_us(deadline_ns)/1e6) if enforce else TRANSPORT_SECONDS
    try:
        async with asyncio.timeout(timeout):
            reader, writer = await asyncio.open_connection(endpoint['host'], endpoint['port'], ssl=tls,
                server_hostname=endpoint['server_name'], ssl_handshake_timeout=1.5, ssl_shutdown_timeout=.2,
                limit=LIMIT)
            challenge = await read(reader)
            fields(challenge, ('version', 'op', 'nonce'))
            if challenge['op'] != 'challenge':
                raise ValueError('missing RPC challenge')
            nonce = hexdigest(challenge['nonce'], 32)
            await send(writer, dict(op='invoke', nonce=nonce, caller=caller, **context,
                                    budget_us=min(MAX_BUDGET_US, remaining_us(deadline_ns)), payload=payload))
            sent = True
            response = result(await read(reader), context, endpoint['service'])
            status, spans = response['status'], response['spans']
            rtt = srtt(writer)
    except TimeoutError:
        status = 'expired' if enforce and not remaining_us(deadline_ns) else 'transport'
    except (OSError, EOFError, ValueError, KeyError, asyncio.IncompleteReadError):
        status = 'transport'
    finally:
        if writer is not None:
            if not writer.is_closing() and sent and (status in ('expired', 'transport') or asyncio.current_task().cancelling()):
                # Best effort on the authenticated in-flight stream. EOF also cancels.
                writer.write(struct.pack('!I', len(CANCEL)) + CANCEL)
            writer.close()
    return dict(child=span, service=endpoint['service'], elapsed_us=(time.monotonic_ns()-start)//1000,
                status=status, srtt_us=rtt), spans


class ReplayWindow:
    def __init__(self, limit=4096, ttl_seconds=20):
        self.entries = OrderedDict()
        self.limit, self.ttl = limit, ttl_seconds

    def admit(self, key, now):
        while self.entries and next(iter(self.entries.values())) <= now-self.ttl:
            self.entries.popitem(last=False)
        if key in self.entries or len(self.entries) >= self.limit:
            return False
        self.entries[key] = now
        return True


class Server:
    def __init__(self, pins, work, capacity=96):
        self.pins, self.work, self.capacity = pins, work, capacity
        self.active = 0
        self.replay = ReplayWindow()

    async def handle(self, reader, writer):
        if self.active >= self.capacity:
            writer.close()
            return
        self.active += 1
        jobs = []
        try:
            tls = writer.get_extra_info('ssl_object')
            pin = hashlib.sha256(tls.getpeercert(binary_form=True)).hexdigest()
            caller = self.pins.get(pin)
            if caller is None:
                return
            nonce = secrets.token_hex(16)
            started = time.monotonic_ns()
            async with asyncio.timeout(TRANSPORT_SECONDS):
                await send(writer, dict(op='challenge', nonce=nonce))
                value = request(await read(reader), nonce, caller)
                received = time.monotonic_ns()
                if not self.replay.admit((pin, value['trace'], value['span']), time.monotonic()):
                    return
                budget = receive_budget(value['budget_us'], started, received) if value['budget_us'] else 0
                deadline = received + budget*1000
                work = asyncio.create_task(self.work(value, budget, deadline))
                cancel = asyncio.create_task(read(reader))
                jobs.extend((work, cancel))
                done, _ = await asyncio.wait(jobs, return_when=asyncio.FIRST_COMPLETED)
                if cancel in done:
                    # A frame other than cancel also invalidates this single-use stream.
                    return
                status, spans = work.result()
                await send(writer, dict(op='reply', trace=value['trace'], span=value['span'],
                                        status=status, spans=spans))
        except (TimeoutError, OSError, EOFError, ValueError, KeyError, asyncio.IncompleteReadError):
            pass
        finally:
            for job in jobs:
                job.cancel()
            try:
                if jobs:
                    await asyncio.gather(*jobs, return_exceptions=True)
            finally:
                self.active -= 1
                writer.close()


async def serve(host, port, context, rpc, stop):
    loop = asyncio.get_running_loop()
    connections = set()

    async def handshake(raw):
        reader = asyncio.StreamReader(limit=LIMIT)
        protocol = asyncio.StreamReaderProtocol(reader)
        try:
            transport, _ = await loop.connect_accepted_socket(lambda: protocol, raw, ssl=context,
                ssl_handshake_timeout=1.5, ssl_shutdown_timeout=.2)
        except (OSError, TimeoutError):
            raw.close()
            return
        except asyncio.CancelledError:
            raw.close()
            raise
        writer = asyncio.StreamWriter(transport, protocol, reader, loop)
        await rpc.handle(reader, writer)

    async def accept(listener):
        while True:
            raw, _ = await loop.sock_accept(listener)
            if len(connections) >= rpc.capacity:
                raw.close()
                continue
            task = asyncio.create_task(handshake(raw))
            connections.add(task)
            task.add_done_callback(connections.discard)

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.setblocking(False)
        listener.bind((host, port))
        listener.listen(64)
        accepting = asyncio.create_task(accept(listener))
        print('ready', flush=True)
        try:
            await stop.wait()
        finally:
            accepting.cancel()
            await asyncio.gather(accepting, return_exceptions=True)
            active = tuple(connections)
            for task in active:
                task.cancel()
            await asyncio.gather(*active, return_exceptions=True)
