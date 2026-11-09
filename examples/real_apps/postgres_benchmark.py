"""Concurrent indexed PostgreSQL reads over the measured lab TCP path."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import math
import os
from pathlib import Path
import random
import socket
import struct
import statistics
import threading
import time

from flowcore_adapter import FlowcoreAdapter
from otel_client import OtlpMetrics


def percentile(values, fraction):
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)] if ordered else None


def transport_rtt(connection):
    try:
        with socket.fromfd(connection.pgconn.socket, socket.AF_INET, socket.SOCK_STREAM) as fd:
            raw = fd.getsockopt(socket.IPPROTO_TCP, socket.TCP_INFO, 104)
        return struct.unpack_from('=I', raw, 68)[0] if len(raw) >= 72 else None
    except (OSError, AttributeError):
        return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dsn', required=True)
    parser.add_argument('--rate', type=float, default=10)
    parser.add_argument('--duration', type=float, default=12)
    parser.add_argument('--concurrency', type=int, default=4)
    parser.add_argument('--deadline-ms', type=float, default=100)
    parser.add_argument('--seed', type=int, default=1)
    parser.add_argument('--start-ns', type=int, default=0)
    parser.add_argument('--service', default='database-client')
    parser.add_argument('--no-flowcore', action='store_true')
    parser.add_argument('--metrics-endpoint', default='')
    parser.add_argument('--metrics-token', default=os.environ.get('OTEL_EXPORTER_OTLP_HEADERS_TOKEN', ''))
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    if (not math.isfinite(args.rate) or not .1 <= args.rate <= 500 or
            not 1 <= args.duration <= 180 or not 1 <= args.concurrency <= 64 or
            args.concurrency > max(1, math.ceil(args.rate * args.duration)) or
            not 0 < args.deadline_ms <= 600000 or args.start_ns < 0):
        parser.error('workload parameter outside supported bounds')
    try:
        import psycopg
    except ImportError:
        parser.error('install examples/real_apps/requirements.txt for PostgreSQL workloads')
    adapter = FlowcoreAdapter(args.service, priority='standard',
                              target_p99_ms=args.deadline_ms, deadline_ms=args.deadline_ms,
                              enabled=not args.no_flowcore)
    rows, lock = [], threading.Lock()
    if adapter.sdk is not None:
        adapter.sdk.declare_transport('postgresql-tcp', 'tcp', multiplexed=False)
    count = math.ceil(args.rate * args.duration)
    start_ns = args.start_ns or time.monotonic_ns() + 300_000_000

    def worker(index):
        rng = random.Random(args.seed + index)
        ordinal = index
        connection = psycopg.connect(args.dsn, connect_timeout=3, autocommit=True)
        cookie = 0
        connection.execute("SELECT set_config('statement_timeout', %s, false)",
                           (f'{max(1, math.ceil(args.deadline_ms))}ms',))
        if adapter.sdk is not None:
            try:
                fd = connection.pgconn.socket
                cookie = adapter.sdk.associate_socket(fd if isinstance(fd, int) else fd.fileno())
            except (AttributeError, OSError, RuntimeError):
                pass
        try:
            while ordinal < count:
                scheduled = start_ns + int(ordinal * 1e9 / args.rate)
                wait = (scheduled - time.monotonic_ns()) / 1e9
                if wait > 0:
                    time.sleep(wait)
                began = time.monotonic_ns()
                status, error, value = 'ok', None, None
                try:
                    with adapter.request(deadline_ms=args.deadline_ms, cookie=cookie):
                        try:
                            value = connection.execute(
                                'SELECT value FROM flowcore_bench WHERE id = %s',
                                (rng.randrange(1, 4097),)).fetchone()[0]
                        except psycopg.errors.QueryCanceled as failure:
                            raise TimeoutError('PostgreSQL statement deadline exceeded') from failure
                except TimeoutError:
                    status, error = 'timeout', 'TimeoutError'
                except Exception as failure:
                    status, error = 'failure', type(failure).__name__
                ended = time.monotonic_ns()
                elapsed = (ended - began) / 1e6
                row = {'request': ordinal, 'scheduled_ns': scheduled, 'start_ns': began,
                       'end_ns': ended, 'latency_ms': elapsed, 'deadline_ms': args.deadline_ms,
                       'deadline_miss': status!='ok' or (ended-scheduled)/1e6 > args.deadline_ms,
                       'outcome': status, 'error': error, 'row_returned': value is not None,
                       'socket_cookie': cookie or None, 'tcp_srtt_us': transport_rtt(connection)}
                with lock:
                    rows.append(row)
                ordinal += args.concurrency
        finally:
            connection.close()

    try:
        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            futures = [pool.submit(worker, i) for i in range(args.concurrency)]
            for future in futures:
                future.result()
        adapter.flush()
        values = [row['latency_ms'] for row in rows if row['outcome'] == 'ok']
        failures = sum(row['outcome'] != 'ok' for row in rows)
        timeouts = sum(row['outcome'] == 'timeout' for row in rows)
        misses = sum(row['deadline_miss'] for row in rows)
        elapsed = args.duration
        metrics = OtlpMetrics(args.service)
        for row in rows:
            attrs = {'db.operation.name': 'SELECT', 'flowcore.traffic_class': 'standard'}
            metrics.observe('flowcore.database.query.duration', row['latency_ms'] / 1000, attrs)
            metrics.add_counter('flowcore.database.queries', 1,
                                {'flowcore.traffic_class': 'standard'})
            if row['outcome'] != 'ok':
                metrics.add_counter('flowcore.database.errors', 1,
                                    {'flowcore.traffic_class': 'standard'})
        export = metrics.export(args.metrics_endpoint, args.metrics_token)
        report = {'attempted': len(rows), 'completed': len(rows) - failures,
                  'errors': failures, 'timeouts': timeouts, 'failed': failures-timeouts,
                  'error_rate': failures / len(rows) if rows else 0,
                  'deadline_misses': misses,
                  'deadline_miss_rate': misses / len(rows) if rows else 0,
                  'completion_ratio': (len(rows) - failures) / len(rows) if rows else 0,
                  'tcp_srtt_p50_us': percentile([row['tcp_srtt_us'] for row in rows if row['tcp_srtt_us'] is not None],.50),
                  'query_rps': (len(rows) - failures) / elapsed,
                  'offered_rps': args.rate,
                  'p50_ms': percentile(values, .50), 'p95_ms': percentile(values, .95),
                  'p99_ms': percentile(values, .99),
                  'mean_ms': statistics.fmean(values) if values else None}
        result = {'schema_version': 1, 'service': args.service, 'sdk': adapter.status(), 'intent': {
                      'priority': 'standard', 'target_p99_ms': args.deadline_ms,
                      'deadline_ms': args.deadline_ms, 'socket_association': 'cookie where psycopg exposes fd'},
                  'workload': {'query': 'SELECT indexed row by primary key', 'random_seed': args.seed,
                               'start_monotonic_ns': start_ns,
                               'rate_rps': args.rate, 'duration_seconds': args.duration,
                               'concurrency': args.concurrency, 'connection_reuse': 'one persistent connection per worker'},
                  'summary': report, 'otel_export': export,
                  'requests': sorted(rows, key=lambda row: row['request'])}
        Path(args.output).write_text(json.dumps(result, indent=2, allow_nan=False) + '\n')
        print(json.dumps({'summary': report, 'otel_export': export}, allow_nan=False))
    finally:
        adapter.close()


if __name__ == '__main__':
    main()
