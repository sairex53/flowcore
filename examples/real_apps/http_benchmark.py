"""Rate-controlled persistent HTTP client with full success/failure accounting."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import http.client
import hashlib
import json
import math
import os
from pathlib import Path
import random
import socket
import statistics
import sys
import threading
import time

from flowcore_adapter import FlowcoreAdapter
from otel_client import OtlpMetrics

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))


def percentile(values, fraction):
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)] if ordered else None


def summarize(rows, offered_rps, duration):
    successful = [row['latency_ms'] for row in rows if row['outcome'] == 'ok']
    attempted = len(rows)
    errors = sum(row['outcome'] != 'ok' for row in rows)
    timeouts = sum(row['outcome'] == 'timeout' for row in rows)
    failures = sum(row['outcome'] == 'failure' for row in rows)
    misses = sum(row['deadline_miss'] for row in rows)
    return {'attempted': attempted, 'completed': attempted - errors,
            'errors': errors, 'timeouts': timeouts, 'failed': failures,
            'error_rate': errors / attempted if attempted else 0,
            'deadline_misses': misses,
            'deadline_miss_rate': misses / attempted if attempted else 0,
            'completion_ratio': (attempted - errors) / attempted if attempted else 0,
            'p50_ms': percentile(successful, .50), 'p95_ms': percentile(successful, .95),
            'p99_ms': percentile(successful, .99),
            'mean_ms': statistics.fmean(successful) if successful else None,
            'offered_rps': offered_rps,
            'completion_rps': (attempted - errors) / duration if duration else 0}


def request_worker(index, args, start_ns, count, adapter, rows, lock):
    source = getattr(args, 'source_address', None)
    connection = http.client.HTTPConnection(args.host, args.port, timeout=args.timeout,
                                            source_address=(source, 0) if source else None)
    rng = random.Random(args.seed + index)
    initial_setup_ms = 0
    cookie = 0
    try:
        setup_started = time.monotonic_ns()
        try:
            connection.connect()
        except OSError:
            connection.close()
        else:
            if adapter.sdk is not None:
                try:
                    cookie = adapter._cookie(connection.sock)
                except (OSError, RuntimeError):
                    cookie = 0
        initial_setup_ms = (time.monotonic_ns() - setup_started) / 1e6
        for local in range(count):
            ordinal = index + local * args.concurrency
            scheduled = start_ns + int(ordinal * 1e9 / args.rate)
            delay = (scheduled - time.monotonic_ns()) / 1e9
            if delay > 0:
                time.sleep(delay)
            dispatch = time.monotonic_ns()
            outcome, status, body_size = 'ok', None, 0
            connection_setup_ms = initial_setup_ms if local == 0 else 0
            try:
                if connection.sock is None:
                    setup_started = time.monotonic_ns()
                    connection.connect()
                    if adapter.sdk is not None:
                        try:
                            cookie = adapter._cookie(connection.sock)
                        except (OSError, RuntimeError):
                            cookie = 0
                    connection_setup_ms = (time.monotonic_ns() - setup_started) / 1e6
                    dispatch = time.monotonic_ns()
                # Socket association is completed in connection setup, outside the request scope.
                with adapter.request(deadline_ms=args.deadline_ms, cookie=cookie) as sdk_request:
                    if connection.sock is not None:
                        connection.sock.settimeout(min(args.timeout, max(.001, args.deadline_ms / 1000)))
                    connection.request('GET', f'/api/critical?work_ms={args.work_ms}',
                                       headers={'X-Deadline-Ms': str(args.deadline_ms),
                                                'Connection': 'keep-alive'})
                    response = connection.getresponse()
                    status = response.status
                    data = response.read(4096)
                    body_size = len(data)
                    if connection.sock is not None:
                        connection.sock.settimeout(args.timeout)
                    if status >= 500 or status < 200:
                        outcome = 'failure'
                    elif status != 200:
                        outcome = 'failure'
                        if sdk_request is not None:
                            sdk_request.complete(2)
            except (TimeoutError, socket.timeout):
                outcome = 'timeout'
                connection.close()
            except (OSError, http.client.HTTPException):
                outcome = 'failure'
                connection.close()
            ended = time.monotonic_ns()
            latency = (ended - dispatch) / 1e6
            miss = latency > args.deadline_ms
            row = {'request': ordinal, 'scheduled_ns': scheduled, 'dispatch_ns': dispatch,
                   'end_ns': ended, 'latency_ms': latency, 'dispatch_lag_ms': max(0, (dispatch-scheduled)/1e6),
                   'deadline_ms': args.deadline_ms, 'deadline_miss': miss,
                   'connection_setup_ms': connection_setup_ms,
                   'outcome': outcome, 'status': status, 'response_bytes': body_size}
            with lock:
                rows.append(row)
    finally:
        connection.close()


def background_transfer(args, start_ns):
    if args.background_bytes <= 0:
        return {'enabled': False}
    delay = (start_ns - time.monotonic_ns()) / 1e9
    if delay > 0:
        time.sleep(delay)
    source = getattr(args, 'source_address', None)
    connection = http.client.HTTPConnection(args.host, args.background_port, timeout=args.background_timeout,
                                            source_address=(source, 0) if source else None)
    started = time.monotonic_ns()
    status, received, error = None, 0, None
    try:
        connection.connect()
        if args.background_upload_file:
            file_path = Path(args.background_upload_file)
            expected = file_path.stat().st_size
            if not 1 <= expected <= 64 * 1024 * 1024:
                raise ValueError('upload file must contain 1..64 MiB')
            connection.putrequest('POST', '/ingest')
            connection.putheader('Content-Length', str(expected))
            connection.putheader('Content-Type', 'application/octet-stream')
            connection.putheader('Connection', 'close')
            connection.endheaders()
            digest = hashlib.sha256()
            with file_path.open('rb') as source:
                while chunk := source.read(65536):
                    connection.send(chunk)
                    digest.update(chunk)
                    received += len(chunk)
            response = connection.getresponse()
            status = response.status
            body = response.read(4096)
            receipt = json.loads(body) if status == 200 else {}
            if (status != 200 or receipt.get('received_bytes') != received or
                    receipt.get('sha256') != digest.hexdigest()):
                error = 'upload_not_confirmed'
        else:
            path = args.background_path or f'/transfer?bytes={args.background_bytes}'
            connection.request('GET', path, headers={'Connection': 'keep-alive'})
            response = connection.getresponse()
            status = response.status
            if status == 200:
                while True:
                    chunk = response.read(65536)
                    if not chunk:
                        break
                    received += len(chunk)
            else:
                response.read(4096)
                error = 'http_status'
    except (OSError, TimeoutError, http.client.HTTPException, ValueError) as failure:
        error = type(failure).__name__
    finally:
        connection.close()
    ended = time.monotonic_ns()
    seconds = (ended - started) / 1e9
    expected = Path(args.background_upload_file).stat().st_size if args.background_upload_file else args.background_bytes
    direction = 'upload' if args.background_upload_file else 'download'
    return {'enabled': True, 'direction': direction, 'status': status, 'bytes_expected': expected,
            'bytes_transferred': received, 'bytes_sent': received if direction == 'upload' else 0,
            'bytes_received': received if direction == 'download' else 0,
            'completion_seconds': seconds,
            'goodput_mbps': received * 8 / seconds / 1e6 if seconds else 0, 'error': error}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='10.203.0.2')
    parser.add_argument('--port', type=int, default=9000)
    parser.add_argument('--background-port', type=int, default=9001)
    parser.add_argument('--rate', type=float, default=30)
    parser.add_argument('--duration', type=float, default=12)
    parser.add_argument('--concurrency', type=int, default=8)
    parser.add_argument('--deadline-ms', type=float, default=50)
    parser.add_argument('--work-ms', type=int, default=2)
    parser.add_argument('--background-bytes', type=int, default=8 * 1024 * 1024)
    parser.add_argument('--background-path', default='')
    parser.add_argument('--background-upload-file', default='')
    parser.add_argument('--timeout', type=float, default=3)
    parser.add_argument('--start-ns', type=int, default=0)
    parser.add_argument('--background-timeout', type=float, default=60)
    parser.add_argument('--seed', type=int, default=1)
    parser.add_argument('--service', default='api-critical')
    parser.add_argument('--priority', choices=('critical', 'interactive', 'standard'), default='critical')
    parser.add_argument('--no-flowcore', action='store_true')
    parser.add_argument('--metrics-endpoint', default='')
    parser.add_argument('--metrics-token', default=os.environ.get('OTEL_EXPORTER_OTLP_HEADERS_TOKEN', ''))
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    if (not math.isfinite(args.rate) or not .1 <= args.rate <= 500 or
            not 1 <= args.duration <= 180 or not 1 <= args.concurrency <= 64 or
            args.concurrency > max(1, math.ceil(args.rate * args.duration)) or
            not 0 < args.deadline_ms <= 600000 or not 0 <= args.work_ms <= 1000 or
            not 0 <= args.background_bytes <= 64 * 1024 * 1024 or
            not math.isfinite(args.background_timeout) or not 1 <= args.background_timeout <= 600 or
            args.start_ns < 0):
        parser.error('workload parameter outside supported bounds')
    adapter = FlowcoreAdapter(args.service, priority=args.priority,
                              target_p99_ms=args.deadline_ms, deadline_ms=args.deadline_ms,
                              enabled=not args.no_flowcore)
    if adapter.sdk is not None:
        adapter.sdk.declare_transport('api-http1', 'tcp', multiplexed=False)
    rows, lock = [], threading.Lock()
    number = math.ceil(args.rate * args.duration)
    start_ns = args.start_ns or time.monotonic_ns() + 300_000_000
    try:
        with ThreadPoolExecutor(max_workers=args.concurrency + 1) as pool:
            futures = [pool.submit(request_worker, i, args, start_ns,
                                   max(0, math.ceil((number-i)/args.concurrency)),
                                   adapter, rows, lock)
                       for i in range(args.concurrency)]
            bulk = pool.submit(background_transfer, args, start_ns)
            for future in futures:
                future.result()
            transfer = bulk.result()
        adapter.flush()
        report = summarize(rows, args.rate, args.duration)
        metrics = OtlpMetrics(args.service)
        for row in rows:
            attrs = {'http.request.method': 'GET', 'flowcore.traffic_class': args.priority}
            metrics.observe('flowcore.http.request.duration',
                            row['latency_ms'] / 1000, attrs)
            metrics.add_counter('flowcore.http.requests', 1,
                                {'flowcore.traffic_class': args.priority})
            if row['outcome'] != 'ok':
                metrics.add_counter('flowcore.http.errors', 1,
                                    {'flowcore.traffic_class': args.priority})
            if row['deadline_miss']:
                metrics.add_counter('flowcore.http.deadline_misses', 1,
                                    {'flowcore.traffic_class': args.priority})
        if transfer.get('bytes_transferred'):
            metrics.add_counter('flowcore.background.transfer.bytes',
                                transfer['bytes_transferred'],
                                {'flowcore.traffic_class': 'background'})
        export = metrics.export(args.metrics_endpoint, args.metrics_token)
        result = {'schema_version': 1, 'service': args.service,
                  'intent': {'priority': args.priority, 'target_p99_ms': args.deadline_ms,
                             'deadline_ms': args.deadline_ms,
                             'flow_association': 'TCP socket cookie when SDK and daemon are available'},
                  'sdk': adapter.status(), 'workload': {
                      'rate_rps': args.rate, 'duration_seconds': args.duration,
                      'concurrency': args.concurrency, 'connection_reuse': 'persistent per worker',
                      'random_seed': args.seed, 'background_bytes': args.background_bytes,
                      'start_monotonic_ns': start_ns,
                      'background_mode': 'upload' if args.background_upload_file else 'download',
                      'background_path': args.background_upload_file or args.background_path or 'generated bounded stream'},
                  'summary': report, 'background_transfer': transfer,
                  'otel_export': export, 'requests': sorted(rows, key=lambda row: row['request'])}
        Path(args.output).write_text(json.dumps(result, indent=2, allow_nan=False) + '\n')
        print(json.dumps({'summary': report, 'background_transfer': transfer,
                          'sdk': adapter.status(), 'otel_export': export}, allow_nan=False))
    finally:
        adapter.close()


if __name__ == '__main__':
    main()
