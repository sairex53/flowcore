"""Concurrent unary client; requests share a reusable multiplexed HTTP/2 channel."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import math
import os
from pathlib import Path
import statistics
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
if os.environ.get('FLOWCORE_GRPC_PROTO_DIR'):
    sys.path.insert(0, os.environ['FLOWCORE_GRPC_PROTO_DIR'])
import grpc
import grpc_app_pb2
import grpc_app_pb2_grpc
from flowcore_adapter import FlowcoreAdapter
from otel_client import OtlpMetrics


def percentile(values, fraction):
    values = sorted(values)
    return values[max(0, math.ceil(len(values) * fraction) - 1)] if values else None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', default='10.203.0.2:9002')
    parser.add_argument('--rate', type=float, default=20)
    parser.add_argument('--duration', type=float, default=10)
    parser.add_argument('--concurrency', type=int, default=8)
    parser.add_argument('--deadline-ms', type=float, default=50)
    parser.add_argument('--work-ms', type=int, default=2)
    parser.add_argument('--response-bytes', type=int, default=128)
    parser.add_argument('--seed', type=int, default=1)
    parser.add_argument('--service', default='grpc-critical')
    parser.add_argument('--no-flowcore', action='store_true')
    parser.add_argument('--metrics-endpoint', default='')
    parser.add_argument('--metrics-token', default=os.environ.get('OTEL_EXPORTER_OTLP_HEADERS_TOKEN', ''))
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    if (not math.isfinite(args.rate) or not .1 <= args.rate <= 500 or
            not 1 <= args.duration <= 180 or not 1 <= args.concurrency <= 64 or
            args.concurrency > max(1, math.ceil(args.rate * args.duration)) or
            not 0 < args.deadline_ms <= 600000 or not 0 <= args.work_ms <= 1000 or
            not 0 <= args.response_bytes <= 65536):
        parser.error('workload parameter outside supported bounds')
    adapter = FlowcoreAdapter(args.service, priority='interactive',
                              target_p99_ms=args.deadline_ms, deadline_ms=args.deadline_ms,
                              enabled=not args.no_flowcore)
    if adapter.sdk is not None:
        adapter.sdk.declare_transport('grpc-http2', 'logical', multiplexed=True)
    channel = grpc.insecure_channel(args.target, options=[
        ('grpc.max_concurrent_streams', 128),
        ('grpc.max_receive_message_length', 65536),
        ('grpc.max_send_message_length', 65536)])
    setup_started = time.monotonic_ns()
    grpc.channel_ready_future(channel).result(timeout=5)
    connection_setup_ms = (time.monotonic_ns() - setup_started) / 1e6
    stub = grpc_app_pb2_grpc.ApplicationStub(channel)
    count = math.ceil(args.rate * args.duration)
    start_ns = time.monotonic_ns() + 300_000_000
    rows, lock = [], threading.Lock()

    def worker(index):
        local = []
        ordinal = index
        while ordinal < count:
            scheduled = start_ns + int(ordinal * 1e9 / args.rate)
            delay = (scheduled - time.monotonic_ns()) / 1e9
            if delay > 0:
                time.sleep(delay)
            began = time.monotonic_ns()
            outcome, error, status = 'ok', None, 'OK'
            request = grpc_app_pb2.CriticalRequest(
                request_id=ordinal, work_ms=args.work_ms, response_bytes=args.response_bytes)
            try:
                scope = adapter.sdk.stream_request(
                    ordinal, 0, int(args.deadline_ms * 1000), 0) if adapter.sdk is not None else None
                if scope:
                    with scope:
                        try:
                            reply = stub.Critical(request, timeout=args.deadline_ms / 1000)
                        except grpc.RpcError as failure:
                            if failure.code() == grpc.StatusCode.DEADLINE_EXCEEDED:
                                status = failure.code().name
                                error = failure.details() or 'deadline exceeded'
                                raise TimeoutError('gRPC deadline exceeded') from failure
                            raise
                else:
                    reply = stub.Critical(request, timeout=args.deadline_ms / 1000)
                if reply.request_id != ordinal or len(reply.payload) != args.response_bytes:
                    outcome, status = 'failure', 'invalid_response'
            except TimeoutError as failure:
                outcome = 'timeout'
                error = error or str(failure)
            except grpc.RpcError as failure:
                status = failure.code().name
                error = failure.details() or str(failure.code())
                outcome = 'timeout' if failure.code() == grpc.StatusCode.DEADLINE_EXCEEDED else 'failure'
            ended = time.monotonic_ns()
            elapsed_ms = (ended - began) / 1e6
            local.append({'request': ordinal, 'scheduled_ns': scheduled, 'start_ns': began,
                          'end_ns': ended, 'latency_ms': elapsed_ms,
                          'deadline_ms': args.deadline_ms, 'deadline_miss': elapsed_ms > args.deadline_ms,
                          'outcome': outcome, 'grpc_status': status, 'error': error})
            ordinal += args.concurrency
        with lock:
            rows.extend(local)

    try:
        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            futures = [pool.submit(worker, i) for i in range(args.concurrency)]
            for future in futures:
                future.result()
        adapter.flush()
        good = [row['latency_ms'] for row in rows if row['outcome'] == 'ok']
        failures = sum(row['outcome'] != 'ok' for row in rows)
        timeouts = sum(row['outcome'] == 'timeout' for row in rows)
        misses = sum(row['deadline_miss'] for row in rows)
        metrics = OtlpMetrics(args.service)
        for row in rows:
            metrics.observe('flowcore.grpc.request.duration', row['latency_ms'] / 1000,
                            {'flowcore.traffic_class': 'interactive'})
            metrics.add_counter('flowcore.grpc.requests', 1,
                                {'flowcore.traffic_class': 'interactive'})
            if row['outcome'] != 'ok':
                metrics.add_counter('flowcore.grpc.errors', 1,
                                    {'flowcore.traffic_class': 'interactive'})
            if row['deadline_miss']:
                metrics.add_counter('flowcore.grpc.deadline_misses', 1,
                                    {'flowcore.traffic_class': 'interactive'})
        export = metrics.export(args.metrics_endpoint, args.metrics_token)
        report = {'attempted': len(rows), 'completed': len(rows) - failures,
                  'errors': failures, 'timeouts': timeouts, 'failed': failures-timeouts,
                  'error_rate': failures / len(rows) if rows else 0,
                  'deadline_misses': misses, 'deadline_miss_rate': misses / len(rows) if rows else 0,
                  'completion_ratio': (len(rows)-failures) / len(rows) if rows else 0,
                  'p50_ms': percentile(good, .50), 'p95_ms': percentile(good, .95),
                  'p99_ms': percentile(good, .99),
                  'mean_ms': statistics.fmean(good) if good else None,
                  'offered_rps': args.rate,
                  'completion_rps': (len(rows)-failures) / args.duration}
        result = {'schema_version': 1, 'service': args.service, 'sdk': adapter.status(),
                  'transport': {'protocol': 'gRPC unary over HTTP/2', 'connection_reuse': True,
                                'socket_cookie': 'unavailable through public grpcio API',
                                'request_association': 'logical SDK stream/request ID'},
                  'intent': {'priority': 'interactive', 'target_p99_ms': args.deadline_ms,
                             'deadline_ms': args.deadline_ms},
                  'connection_setup_ms': connection_setup_ms,
                  'workload': {'rate_rps': args.rate, 'duration_seconds': args.duration,
                               'concurrency': args.concurrency, 'random_seed': args.seed},
                  'summary': report, 'otel_export': export,
                  'requests': sorted(rows, key=lambda row: row['request'])}
        Path(args.output).write_text(json.dumps(result, indent=2, allow_nan=False) + '\n')
        print(json.dumps({'summary': report, 'otel_export': export}, allow_nan=False))
    finally:
        channel.close()
        adapter.close()


if __name__ == '__main__':
    main()
