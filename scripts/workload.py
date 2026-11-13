#!/usr/bin/env python3
"""Application-level TCP workload, with receiver-side background accounting."""
import argparse
import asyncio
import csv
from contextlib import ExitStack
import json
import math
import os
import random
import socket
import struct
import time
from pathlib import Path
from app_metrics import Reporter
from tcp_diag import socket_snapshot


def percentile(values, fraction):
    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)] if values else None


def research_tcp_socket(host, algorithm):
    owned = socket.socket(socket.AF_INET6 if ':' in host else socket.AF_INET, socket.SOCK_STREAM)
    try:
        owned.setsockopt(socket.IPPROTO_TCP, socket.TCP_CONGESTION, algorithm.encode())
        check_algorithm(owned, algorithm)
        owned.setblocking(False)
        return owned
    except BaseException:
        owned.close()
        raise


def check_algorithm(sock, algorithm):
    actual = sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_CONGESTION, 16).split(b'\0', 1)[0].decode()
    if actual != algorithm:
        raise RuntimeError(f'TCP congestion control fell back from {algorithm} to {actual}')


async def research_connect(host, port, algorithm=None):
    if algorithm is None:
        return await asyncio.open_connection(host, port)
    owned = research_tcp_socket(host, algorithm)
    try:
        await asyncio.get_running_loop().sock_connect(owned, (host, port))
        check_algorithm(owned, algorithm)
        return await asyncio.open_connection(sock=owned)
    except BaseException:
        owned.close()
        raise


async def server(output, payload, request_port, background_port, streams, udp_port=9002, research=None):
    counters = [0] * streams
    algorithm = research.get('ecn_socket_algorithm') if research else None
    algorithm_fallbacks = 0

    def check_peer(writer):
        nonlocal algorithm_fallbacks
        if algorithm:
            try:
                check_algorithm(writer.get_extra_info('socket'), algorithm)
            except RuntimeError:
                algorithm_fallbacks += 1
                raise ConnectionError('server-side TCP congestion fallback')
    udp_count = max((phase.get('udp_streams',0) for phase in research['phases']),default=0) if research else 0
    udp_counters = [0] * udp_count

    async def critical(reader, writer):
        try:
            check_peer(writer)
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
            check_peer(writer)
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
    host = research.get('server_address','10.203.0.2') if research else '10.203.0.2'
    async def listen(handler, port):
        if algorithm is None:
            return await asyncio.start_server(handler, host, port)
        owned = research_tcp_socket(host, algorithm)
        try:
            owned.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            owned.bind((host, port))
            return await asyncio.start_server(handler, sock=owned)
        except BaseException:
            owned.close()
            raise

    listeners = [await listen(critical, port) for port in ports]
    listeners.append(await listen(background, background_port))
    udp_transport = None
    if udp_count:
        class DatagramCounters(asyncio.DatagramProtocol):
            def datagram_received(self, data, _address):
                if len(data) < 4:
                    return
                flow = struct.unpack('!I',data[:4])[0]
                if flow < len(udp_counters):
                    udp_counters[flow] += len(data)
        loop = asyncio.get_running_loop()
        family = socket.AF_INET6 if ':' in host else socket.AF_INET
        udp_transport, _ = await loop.create_datagram_endpoint(
            DatagramCounters, local_addr=(host,udp_port), family=family)
    print('ready', flush=True)
    with output.open('w') as stream:
        while True:
            stream.write(json.dumps({'time': time.monotonic(), 'bytes': counters.copy(),
                                     'udp_bytes': udp_counters.copy(), 'algorithm_fallbacks': algorithm_fallbacks}) + '\n')
            stream.flush()
            await asyncio.sleep(0.1)


async def client(output, duration, rate, payload, request_port, background_port, streams,
                 service=None, deadline_ms=None, rates=None, udp_port=9002, research=None, timing_stream=None):
    integrity = research.get('integrity') if research else None
    timing = None
    if integrity:
        from measurement import MAX_REQUESTS, TIMING_FIELDS, process_delta, process_sample
        timing = csv.writer(timing_stream)
        timing.writerow(TIMING_FIELDS)
    request_data = b'Q' * payload
    services = research.get('services', {}) if research else {}
    if research:
        deadline_ms = research.get('deadline_ms')
    reporters = ({name: Reporter(name, deadline_ms) for name in research['ports']} if research else
                 {'critical': Reporter(service, deadline_ms)} if service else {})
    weights = research['priority_weights'] if research else {'critical': 100}
    schedule_rng = random.Random(research['seed'] if research else 0)
    cap = research['max_inflight'] if research else 512
    own_tcp_info = {}
    rows = []
    window = []
    pacing_window = []
    background_errors = []
    udp_send_errors = []
    stopping = False
    socket_algorithm = research.get('ecn_socket_algorithm') if research else None
    paced_kbit = research.get('background_kbit_per_stream', 0) if research else 0
    pacing = None
    if research and 'socket_pacing_mode' in research:
        from pacing_workload import PacingWorkload, SLO_TARGET
        pacing = PacingWorkload(output, research['socket_pacing_mode'], research.get('deadline_pacing_floor'),
                                research.get('socket_shadows', ()),
                                target_us=research.get('pacing_target_us', SLO_TARGET),
                                trace=research.get('pacing_trace', False), hybrid_cap=research.get('hybrid_cap', 0),
                                control_directory=Path(research['hybrid_control_directory'])
                                if 'hybrid_control_directory' in research else None)
    admission = None
    if research and 'deadline_admission' in research:
        from deadline_workload import AdmissionWorkload
        admission = AdmissionWorkload(output, research['deadline_admission'], research['deadline_mode'], deadline_ms, payload)

    dscp = research.get('dscp') if research else None

    def mark(sock, traffic_class):
        if dscp is None:
            return
        level, option = ((socket.IPPROTO_IPV6, socket.IPV6_TCLASS) if sock.family == socket.AF_INET6
                         else (socket.IPPROTO_IP, socket.IP_TOS))
        value = dscp[traffic_class]<<2
        sock.setsockopt(level, option, (sock.getsockopt(level, option)&3)|value)
        if sock.getsockopt(level, option)&252 != value:
            raise RuntimeError('workload DSCP readback mismatch')

    async def background(flow):
        budget = research['bulk_bytes_per_stream'] if research else 0
        sent = 0
        while True:
            writer = None
            attached = False
            try:
                host = research.get('server_address','10.203.0.2') if research else '10.203.0.2'
                algorithm = socket_algorithm or (pacing.algorithm if pacing else None)
                _, writer = await research_connect(host, background_port, algorithm)
                sock = writer.get_extra_info('socket')
                mark(sock, 'background')
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 32768)
                writer.transport.set_write_buffer_limits(high=32768, low=8192)
                if pacing:
                    await pacing.membership(pacing.add, flow, writer.get_extra_info('socket'))
                    attached = True
                writer.write(struct.pack('!I', flow))
                while not budget or sent < budget:
                    chunk = 1200 if paced_kbit else 16384
                    count = min(chunk, budget-sent) if budget else chunk
                    if paced_kbit:
                        await asyncio.sleep(count * 8 / (paced_kbit * 1000))
                    writer.write(b'B' * count)
                    if pacing:
                        start_drain = time.monotonic_ns()
                        await writer.drain()
                        pacing.drain(time.monotonic_ns()-start_drain)
                    else:
                        await writer.drain()
                    sent += count
                    # drain() can complete synchronously on a fast veth.
                    # Give request timers and reporters a turn even then.
                    await asyncio.sleep(0)
                if attached:
                    await pacing.membership(pacing.remove, flow)
                    attached = False
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
                try:
                    if attached and flow in pacing.sockets:
                        await pacing.membership(pacing.remove, flow)
                finally:
                    if writer:
                        writer.transport.abort()

    async def request(index, scheduled, traffic_class, name, released_ns=None, scheduled_ns=None):
        nonlocal active_requests, peak_inflight
        active_requests += 1
        peak_inflight = max(peak_inflight, active_requests)
        try:
            if admission:
                admission.offer(index, scheduled)
            reporter = reporters.get(traffic_class)
            dispatch_ns = time.monotonic_ns()
            dispatch_lag_ms = (dispatch_ns/1e9 - scheduled) * 1000
            connect = connected = submitted = drained = received = None
            port = research['ports'][traffic_class] if research else request_port
            writer = None
            outcome = 'ok'
            direct_socket = None
            network_begin_ns = None
            budget = admission.budget_ms(index) if admission else deadline_ms
            advice = ''
            if admission:
                assessment = admission.admission(scheduled, index, traffic_class,
                                                 active_concurrency=max(0, active_requests-1))
                advice = assessment['decision']
                if admission.gating:
                    if advice == 'DEFER':
                        await asyncio.sleep(admission.profile['defer_us']/1e6)
                        assessment = admission.admission(
                            scheduled, index, traffic_class, first=False,
                            active_concurrency=max(0, active_requests-1))
                        advice = assessment['decision']
                    outcome = {'REJECT_INFEASIBLE': 'admission_rejected',
                               'REJECT_RECOMMENDED': 'admission_rejected',
                               'DEFER': 'admission_deferred'}.get(advice, 'ok')
                admission.admission_result(index, outcome == 'ok', advice, budget)
            try:
                if outcome == 'ok':
                    remaining = 5 - (time.monotonic() - scheduled)
                    if remaining <= 0:
                        raise TimeoutError('late request')
                    async with asyncio.timeout(remaining):
                        host = research.get('server_address','10.203.0.2') if research else '10.203.0.2'
                        if timing:
                            connect = time.monotonic_ns()
                        reader, writer = await research_connect(host, port, socket_algorithm)
                        if timing:
                            connected = time.monotonic_ns()
                        direct_socket = writer.get_extra_info('socket')
                        mark(direct_socket, traffic_class)
                        direct_socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                        network_begin_ns = time.monotonic_ns() if admission else None
                        if timing:
                            submitted = time.monotonic_ns()
                        writer.write(request_data)
                        await writer.drain()
                        if timing:
                            drained = time.monotonic_ns()
                        if await reader.readexactly(64) != b'R' * 64:
                            raise ValueError('invalid response')
                        if timing:
                            received = time.monotonic_ns()
            except Exception as error:
                outcome = type(error).__name__
            finally:
                completed_ns = time.monotonic_ns()
                ended = completed_ns/1e9
                if reporter and direct_socket and outcome == 'ok' and not own_tcp_info:
                    try:
                        own_tcp_info.update(socket_snapshot(direct_socket))
                    except OSError:
                        pass
                if writer:
                    writer.transport.abort()
            latency = (ended - scheduled) * 1000
            row = (index, scheduled, ended, latency, outcome, traffic_class, name, dispatch_lag_ms)
            rows.append(row + ((budget, advice) if admission else ()))
            if admission:
                admission.terminal(index, outcome, latency)
            if timing:
                timing.writerow((index, outcome, scheduled_ns, released_ns, dispatch_ns, connect, connected,
                                 submitted, drained, received, completed_ns))
            if outcome == 'ok' and network_begin_ns is not None:
                admission.completed(traffic_class, network_begin_ns, round(ended*1e9))
            if not outcome.startswith('admission_'):
                window.append((latency, outcome))
                if pacing and traffic_class == 'critical':
                    pacing_window.append((latency, outcome))
                if reporter:
                    reporter.submit(latency * 1000, outcome, budget)
        finally:
            active_requests -= 1

    async def feedback():
        while True:
            await asyncio.sleep(1)
            batch = window.copy()
            window.clear()
            if pacing:
                tail = percentile([value for value, _ in pacing_window], .99) or 0
                failures = sum(status != 'ok' for _, status in pacing_window)
                if failures:
                    tail = max(tail, 5000)
                pressure = admission.pressure() if admission else bool(
                    deadline_ms and len(pacing_window) >= 12 and
                    sum(value >= .8*deadline_ms for value, status in pacing_window if status == 'ok')
                    * 4 >= len(pacing_window))
                pacing.tick(tail, len(pacing_window), pressure, failures)
                pacing_window.clear()
            if batch:
                tail = percentile([value for value, _ in batch], .99)
                if any(status != 'ok' for _, status in batch):
                    tail = max(tail, 30)
                print(f'{tail:.6f} {len(batch)}', flush=True)
            else:
                print('0 0', flush=True)

    backgrounds = {}
    retired = []
    udp_backgrounds = {}
    retired_udp = []
    def set_backgrounds(count):
        for flow in list(backgrounds):
            if flow >= count:
                task = backgrounds.pop(flow)
                task.cancel()
                retired.append(task)
        for flow in range(count):
            if flow not in backgrounds:
                backgrounds[flow] = asyncio.create_task(background(flow))

    async def udp_background(flow):
        host = research.get('server_address','10.203.0.2')
        family = socket.AF_INET6 if ':' in host else socket.AF_INET
        address = (host,udp_port)
        rate_kbit = research['udp_kbit_per_stream']
        packet = struct.pack('!I',flow)+b'U'*1196
        interval = len(packet)*8/(rate_kbit*1000)
        sock = socket.socket(family,socket.SOCK_DGRAM)
        sock.setblocking(False)
        loop = asyncio.get_running_loop()
        try:
            while not stopping:
                try:
                    await loop.sock_sendto(sock,packet,address)
                except OSError as error:
                    if len(udp_send_errors)<256:
                        udp_send_errors.append(type(error).__name__)
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            return
        finally:
            sock.close()

    def set_udp_backgrounds(count):
        for flow in list(udp_backgrounds):
            if flow >= count:
                task=udp_backgrounds.pop(flow)
                task.cancel()
                retired_udp.append(task)
        for flow in range(count):
            if flow not in udp_backgrounds:
                udp_backgrounds[flow]=asyncio.create_task(udp_background(flow))

    phase_plan = research['phases'] if research else [
        dict(seconds=duration/len(rates or [rate]), requests_per_second=value, background_streams=streams)
        for value in (rates or [rate])]
    set_backgrounds(0 if research else phase_plan[0]['background_streams'])
    set_udp_backgrounds(0 if research else 0)
    if research:
        started = research['start_monotonic']
        warmup = research.get('warmup_seconds', 0)
        if warmup:
            await asyncio.sleep(max(0, started-warmup-time.monotonic()))
            set_backgrounds(phase_plan[0]['background_streams'])
            set_udp_backgrounds(phase_plan[0].get('udp_streams',0))
        await asyncio.sleep(max(0, started-time.monotonic()))
    else:
        await asyncio.sleep(3)
        started = time.monotonic()
    report = asyncio.create_task(feedback())
    pending = set()
    measurement_end = started+duration
    loop_lags = []
    skipped_ticks = 0
    ceiling = False
    peak_inflight = 0
    peak_outstanding = 0
    active_requests = 0
    process_begin = process_sample() if integrity else None

    async def clock_probe():
        nonlocal skipped_ticks
        period = 50_000_000
        scheduled = round(started*1e9)+period
        while scheduled < round(measurement_end*1e9):
            await asyncio.sleep(max(0, scheduled/1e9-time.monotonic()))
            lag = max(0, time.monotonic_ns()-scheduled)
            loop_lags.append(lag/1e6)
            skipped_ticks += lag//period
            scheduled += period*(1+lag//period)

    async def self_test():
        await asyncio.sleep(max(0, started+1-time.monotonic()))
        if integrity['self_test']=='client_exit':
            os._exit(75)
        time.sleep(.2)  # Explicit isolated generator-contamination fixture.

    clock_task = asyncio.create_task(clock_probe()) if integrity else None
    fault_task = (asyncio.create_task(self_test()) if integrity and integrity['self_test']!='none' else None)
    async def background_schedule():
        offset = 0
        for index, phase in enumerate(phase_plan):
            scheduled = started+offset
            await asyncio.sleep(max(0, scheduled-time.monotonic()))
            if research and research.get('pacing_trace'):
                observed = time.monotonic_ns()
                if pacing and pacing.guarded:
                    await pacing.membership(pacing.cohort_changed, 'phase', index)
                with (output/'workload-phases.jsonl').open('a') as log:
                    log.write(json.dumps(dict(index=index, scheduled_ns=round(scheduled*1e9),
                        actual_ns=observed, background_streams=phase['background_streams'],
                        requests_per_second=phase['requests_per_second']))+'\n')
            set_backgrounds(phase['background_streams'])
            set_udp_backgrounds(phase.get('udp_streams',0))
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

    closed = bool(integrity and integrity['load']=='closed')
    if closed:
        offered = 0

        async def lane(slot):
            nonlocal offered, ceiling
            rng = random.Random((research['seed']<<8)+slot)
            attempt = 0
            while (released := time.monotonic_ns())/1e9 < measurement_end:
                if offered >= MAX_REQUESTS:
                    ceiling = True
                    return
                index = offered
                offered += 1
                traffic_class = rng.choices(list(weights), weights=list(weights.values()))[0]
                names = services.get(traffic_class, [traffic_class])
                await request(index, released/1e9, traffic_class, names[(slot+attempt)%len(names)], released)
                attempt += 1

        await asyncio.gather(*(lane(slot) for slot in range(integrity['concurrency'])))
    else:
        for index, offset in enumerate(scheduled_offsets()):
            scheduled = started+offset
            await asyncio.sleep(max(0, scheduled-time.monotonic()))
            released = time.monotonic_ns()
            scheduled_ns = round(scheduled*1e9) if timing else None
            traffic_class = schedule_rng.choices(list(weights), weights=list(weights.values()))[0]
            names = services.get(traffic_class, [traffic_class])
            name = names[index % len(names)]
            reporter = reporters.get(traffic_class)
            if len(pending) >= cap:
                completed_ns = time.monotonic_ns()
                ended = completed_ns/1e9
                row = (index, scheduled, ended, (ended-scheduled)*1000, 'inflight_limit', traffic_class, name, (ended-scheduled)*1000)
                rows.append(row + ((admission.budget_ms(index), '') if admission else ()))
                if admission:
                    admission.offer(index, scheduled)
                    admission.admission_result(index, False, 'CLIENT_INFLIGHT_LIMIT',
                                               admission.budget_ms(index))
                    admission.terminal(index, 'inflight_limit', (ended-scheduled)*1000)
                if timing:
                    timing.writerow((index, 'inflight_limit', scheduled_ns, released, None, None, None,
                                     None, None, None, completed_ns))
                window.append((5000, 'inflight_limit'))
                if pacing and traffic_class == 'critical':
                    pacing_window.append((5000, 'inflight_limit'))
                if reporter:
                    reporter.submit((ended-scheduled)*1e6, 'inflight_limit',
                                    admission.budget_ms(index) if admission else deadline_ms)
                continue
            task = asyncio.create_task(request(index, scheduled, traffic_class, name, released, scheduled_ns))
            pending.add(task)
            peak_outstanding = max(peak_outstanding, len(pending))
            task.add_done_callback(pending.discard)
    await asyncio.sleep(max(0, measurement_end-time.monotonic()))
    if pending:
        await asyncio.gather(*pending)
    for task in (clock_task, fault_task):
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    stopping = True
    report.cancel()
    changes.cancel()
    for task in backgrounds.values():
        task.cancel()
    for task in udp_backgrounds.values():
        task.cancel()
    worker_results = await asyncio.gather(report, changes, *backgrounds.values(), *retired,
                         *udp_backgrounds.values(), *retired_udp, return_exceptions=True)
    worker_errors = sorted({type(item).__name__ for item in worker_results
                            if isinstance(item, BaseException) and not isinstance(item, asyncio.CancelledError)})
    with (output / 'requests.csv').open('w') as stream:
        writer = csv.writer(stream)
        columns = ['id', 'scheduled', 'completed', 'latency_ms', 'outcome', 'traffic_class', 'service', 'dispatch_lag_ms']
        writer.writerow(columns + (['deadline_ms', 'admission'] if admission else []))
        writer.writerows(sorted(rows))
    successes = [row[3] for row in rows if row[4] == 'ok']
    summary = {'start': started, 'end': measurement_end, 'offered': offered,
               'completed': len(successes), 'failed': len(rows) - len(successes),
               'p50_ms': percentile(successes, .50), 'p95_ms': percentile(successes, .95),
               'p99_ms': percentile(successes, .99), 'max_ms': max(successes, default=None),
               'background_errors': background_errors,'udp_send_errors':len(udp_send_errors),
               'worker_errors': worker_errors}
    if integrity:
        summary['measurement_client'] = dict(load=integrity['load'], configured_concurrency=integrity['concurrency'],
            request_cap=cap, peak_inflight=peak_inflight, peak_outstanding=peak_outstanding,
            loop_samples=len(loop_lags), loop_lag_max_ms=max(loop_lags, default=None),
            loop_lag_p99_ms=percentile(loop_lags,.99), missed_loop_ticks=skipped_ticks, request_ceiling_hit=ceiling,
            process=process_delta(process_begin, process_sample()))
    if admission:
        summary['deadline_admission'] = admission.summary()
    if pacing:
        summary['socket_pacing'] = pacing.finish()
    if reporters:
        per_class = {name: await reporter.close() for name, reporter in reporters.items()}
        summary['application_reports'] = {key: sum(row[key] for row in per_class.values())
                                          for key in ('accepted', 'dropped')}
        if research:
            summary['reports_by_class'] = per_class
        if own_tcp_info:
            (output / 'own-tcp-info.json').write_text(json.dumps(own_tcp_info, indent=2) + '\n')
    (output / 'client.json').write_text(json.dumps(summary, indent=2) + '\n')
    if worker_errors:
        raise RuntimeError('workload worker failed; inspect client.json')
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
    parser.add_argument('--udp-port', type=int, default=9002)
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
    if not all(1 <= port <= 65535 for port in (args.request_port, args.background_port,args.udp_port)) or args.request_port == args.background_port:
        parser.error('ports must be in 1..65535; TCP request/background ports must differ')
    workload = (args.payload, args.request_port, args.background_port, args.streams)
    with ExitStack() as resources:
        timing_stream = None
        if args.mode=='client' and research and research.get('integrity'):
            timing_stream = resources.enter_context((args.output/'request-timing.csv').open('w', buffering=1))
        asyncio.run(server(args.output, *workload, udp_port=args.udp_port,research=research) if args.mode == 'server'
                    else client(args.output, args.duration, args.rate, *workload,
                                service=args.report_service, deadline_ms=args.deadline_ms, rates=args.rates,
                                udp_port=args.udp_port,research=research, timing_stream=timing_stream))
