#!/usr/bin/env python3
"""Application-level TCP workload, with receiver-side background accounting."""
import argparse
import asyncio
import csv
import json
import math
import random
import socket
import struct
import time
from pathlib import Path
from app_metrics import Reporter
from tcp_diag import tcp_info


def percentile(values, fraction):
    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)] if values else None


async def server(output, payload, request_port, background_port, streams, research=None):
    counters = [0] * streams

    async def critical(reader, writer):
        try:
            await reader.readexactly(payload)
            writer.write(b'R' * 64)
            await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

    async def background(reader, writer):
        try:
            flow = struct.unpack('!I', await reader.readexactly(4))[0]
            if flow >= len(counters):
                return
            while data := await reader.read(65536):
                counters[flow] += len(data)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

    ports = research['ports'].values() if research else [request_port]
    listeners = [await asyncio.start_server(critical, '10.203.0.2', port) for port in ports]
    listeners.append(await asyncio.start_server(background, '10.203.0.2', background_port))
    print('ready', flush=True)
    with output.open('w') as stream:
        while True:
            stream.write(json.dumps({'time': time.monotonic(), 'bytes': counters.copy()}) + '\n')
            stream.flush()
            await asyncio.sleep(0.1)


async def client(output, duration, rate, payload, request_port, background_port, streams,
                 service=None, deadline_ms=None, rates=None, research=None):
    request_data = b'Q' * payload
    reporters = ({name: Reporter(name, deadline_ms) for name in research['ports']} if research else
                 {'critical': Reporter(service, deadline_ms)} if service else {})
    weights = research['priority_weights'] if research else {'critical': 100}
    schedule_rng = random.Random(research['seed'] if research else 0)
    cap = research['max_inflight'] if research else 512
    own_tcp_info = {}
    rows = []
    window = []
    background_errors = []
    stopping = False

    async def background(flow):
        budget = research['bulk_bytes_per_stream'] if research else 0
        sent = 0
        while True:
            writer = None
            try:
                _, writer = await asyncio.open_connection('10.203.0.2', background_port)
                writer.get_extra_info('socket').setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 32768)
                writer.transport.set_write_buffer_limits(high=32768, low=8192)
                writer.write(struct.pack('!I', flow))
                while not budget or sent < budget:
                    count = min(16384, budget-sent) if budget else 16384
                    writer.write(b'B' * count)
                    await writer.drain()
                    sent += count
                writer.close()
                await writer.wait_closed()
                return
            except asyncio.CancelledError:
                return
            except (OSError, TimeoutError) as error:
                if not stopping and len(background_errors) < 256:
                    background_errors.append(type(error).__name__)
                if not research:
                    return
                await asyncio.sleep(.2)
            finally:
                if writer:
                    writer.transport.abort()

    async def request(index, scheduled, traffic_class):
        reporter = reporters.get(traffic_class)
        port = research['ports'][traffic_class] if research else request_port
        writer = None
        outcome = 'ok'
        direct_socket = None
        try:
            remaining = 5 - (time.monotonic() - scheduled)
            if remaining <= 0:
                raise TimeoutError('late request')
            async with asyncio.timeout(remaining):
                reader, writer = await asyncio.open_connection('10.203.0.2', port)
                direct_socket = writer.get_extra_info('socket')
                direct_socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                writer.write(request_data)
                await writer.drain()
                if await reader.readexactly(64) != b'R' * 64:
                    raise ValueError('invalid response')
        except Exception as error:
            outcome = type(error).__name__
        finally:
            ended = time.monotonic()
            if reporter and direct_socket and outcome == 'ok' and not own_tcp_info:
                try:
                    own_tcp_info.update(source='getsockopt(TCP_INFO)',
                        tcp_info=tcp_info(direct_socket.getsockopt(socket.IPPROTO_TCP, socket.TCP_INFO, 512)),
                        send_buffer_bytes=direct_socket.getsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF),
                        receive_buffer_bytes=direct_socket.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF))
                except OSError:
                    pass
            if writer:
                writer.transport.abort()
        latency = (ended - scheduled) * 1000
        rows.append((index, scheduled, ended, latency, outcome, traffic_class))
        window.append((latency, outcome))
        if reporter:
            reporter.submit(latency * 1000, outcome)

    async def feedback():
        while True:
            await asyncio.sleep(1)
            batch = window.copy()
            window.clear()
            if batch:
                tail = percentile([value for value, _ in batch], .99)
                if any(status != 'ok' for _, status in batch):
                    tail = max(tail, 30)
                print(f'{tail:.6f} {len(batch)}', flush=True)
            else:
                print('0 0', flush=True)

    backgrounds = {}
    retired = []
    def set_backgrounds(count):
        for flow in list(backgrounds):
            if flow >= count:
                task = backgrounds.pop(flow)
                task.cancel()
                retired.append(task)
        for flow in range(count):
            if flow not in backgrounds:
                backgrounds[flow] = asyncio.create_task(background(flow))

    phase_plan = research['phases'] if research else [
        dict(seconds=duration/len(rates or [rate]), requests_per_second=value, background_streams=streams)
        for value in (rates or [rate])]
    set_backgrounds(0 if research else phase_plan[0]['background_streams'])
    if research:
        started = research['start_monotonic']
        await asyncio.sleep(max(0, started-time.monotonic()))
    else:
        await asyncio.sleep(3)
        started = time.monotonic()
    report = asyncio.create_task(feedback())
    pending = set()
    async def background_schedule():
        offset = 0
        for phase in phase_plan:
            await asyncio.sleep(max(0, started+offset-time.monotonic()))
            set_backgrounds(phase['background_streams'])
            offset += phase['seconds']
    changes = asyncio.create_task(background_schedule())
    offered = sum(round(phase['seconds']*phase['requests_per_second']) for phase in phase_plan)

    def scheduled_offsets():
        offset = 0
        for phase in phase_plan:
            phase_rate = phase['requests_per_second']
            for index in range(round(phase['seconds']*phase_rate)):
                yield offset+index/phase_rate
            offset += phase['seconds']

    schedule = scheduled_offsets()
    for index, offset in enumerate(schedule):
        scheduled = started + offset
        await asyncio.sleep(max(0, scheduled - time.monotonic()))
        traffic_class = schedule_rng.choices(list(weights), weights=list(weights.values()))[0]
        reporter = reporters.get(traffic_class)
        if len(pending) >= cap:
            ended = time.monotonic()
            rows.append((index, scheduled, ended, (ended - scheduled) * 1000, 'inflight_limit', traffic_class))
            window.append((5000, 'inflight_limit'))
            if reporter:
                reporter.submit((ended - scheduled) * 1e6, 'inflight_limit')
            continue
        task = asyncio.create_task(request(index, scheduled, traffic_class))
        pending.add(task)
        task.add_done_callback(pending.discard)
    await asyncio.sleep(max(0, started + duration - time.monotonic()))
    measurement_end = started + duration
    if pending:
        await asyncio.gather(*pending)
    stopping = True
    report.cancel()
    changes.cancel()
    for task in backgrounds.values():
        task.cancel()
    await asyncio.gather(report, changes, *backgrounds.values(), *retired, return_exceptions=True)
    with (output / 'requests.csv').open('w') as stream:
        writer = csv.writer(stream)
        writer.writerow(['id', 'scheduled', 'completed', 'latency_ms', 'outcome', 'traffic_class'])
        writer.writerows(sorted(rows))
    successes = [row[3] for row in rows if row[4] == 'ok']
    summary = {'start': started, 'end': measurement_end, 'offered': offered,
               'completed': len(successes), 'failed': len(rows) - len(successes),
               'p50_ms': percentile(successes, .50), 'p95_ms': percentile(successes, .95),
               'p99_ms': percentile(successes, .99), 'max_ms': max(successes, default=None),
               'background_errors': background_errors}
    if reporters:
        per_class = {name: await reporter.close() for name, reporter in reporters.items()}
        summary['application_reports'] = {key: sum(row[key] for row in per_class.values())
                                          for key in ('accepted', 'dropped')}
        if research:
            summary['reports_by_class'] = per_class
        if own_tcp_info:
            (output / 'own-tcp-info.json').write_text(json.dumps(own_tcp_info, indent=2) + '\n')
    (output / 'client.json').write_text(json.dumps(summary, indent=2) + '\n')
    if background_errors and not research:
        raise RuntimeError('background workload failed; inspect client.json')
    if summary['failed']:
        raise SystemExit(2)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['server', 'client'])
    parser.add_argument('output', type=Path)
    parser.add_argument('--duration', type=float, default=12)
    parser.add_argument('--rate', type=int, default=100)
    parser.add_argument('--payload', type=int, default=4096)
    parser.add_argument('--request-port', type=int, default=9000)
    parser.add_argument('--background-port', type=int, default=9001)
    parser.add_argument('--streams', type=int, default=4)
    parser.add_argument('--rates', type=int, nargs='+', help='equal-duration phases in requests per second')
    parser.add_argument('--report-service', help='submit timings as a registered service to flowcored')
    parser.add_argument('--deadline-ms', type=float)
    parser.add_argument('--research-plan', type=Path)
    args = parser.parse_args()
    research = json.loads(args.research_plan.read_text()) if args.research_plan else None
    if args.deadline_ms is not None and (not args.report_service or not .001 <= args.deadline_ms <= 600000):
        parser.error('deadline requires a reporting service and range .001..600000 ms')
    if not 1 <= args.duration <= 600 or not 1 <= args.rate <= 1000:
        parser.error('duration must be 1..600 and rate 1..1000')
    if args.rates is not None and (len(args.rates) > 16 or any(not 0 <= rate <= 1000 for rate in args.rates)):
        parser.error('up to 16 phases with rates in 0..1000')
    if not 1 <= args.payload <= 65536 or not 0 <= args.streams <= 32:
        parser.error('payload must be 1..65536 and streams 0..32')
    if not all(1 <= port <= 65535 for port in (args.request_port, args.background_port)) or args.request_port == args.background_port:
        parser.error('request and background ports must be distinct and in 1..65535')
    workload = (args.payload, args.request_port, args.background_port, args.streams)
    asyncio.run(server(args.output, *workload, research=research) if args.mode == 'server'
                else client(args.output, args.duration, args.rate, *workload,
                            service=args.report_service, deadline_ms=args.deadline_ms, rates=args.rates, research=research))
