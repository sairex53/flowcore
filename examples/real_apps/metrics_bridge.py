"""Bounded OTLP/HTTP JSON receiver with Prometheus scrape output."""
import argparse
import base64
import binascii
import hmac
import ipaddress
import os
import json
import math
import re
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

MAX_BODY = 262144
MAX_SERIES = 128
MAX_POINTS = 1024
SERVICE_RE = re.compile(r'[a-z][a-z0-9_-]{0,31}\Z')
METRIC_TYPES = {
    'flowcore.http.request.duration': 'histogram',
    'flowcore.http.requests': 'sum',
    'flowcore.http.errors': 'sum',
    'flowcore.http.deadline_misses': 'sum',
    'flowcore.background.transfer.bytes': 'sum',
    'flowcore.database.query.duration': 'histogram',
    'flowcore.database.queries': 'sum',
    'flowcore.database.errors': 'sum',
    'flowcore.grpc.request.duration': 'histogram',
    'flowcore.grpc.requests': 'sum',
    'flowcore.grpc.errors': 'sum',
    'flowcore.grpc.deadline_misses': 'sum',
}
PROM_NAMES = {name: name.replace('.', '_').replace('flowcore_', 'flowcore_app_', 1)
              for name in METRIC_TYPES}
LABELS = {
    'http.request.method': {'GET', 'POST', 'PUT', 'DELETE'},
    'http.response.status_code': {str(code) for code in range(100, 600)},
    'db.operation.name': {'SELECT', 'INSERT', 'UPDATE', 'DELETE'},
    'flowcore.traffic_class': {'critical', 'interactive', 'standard', 'background'},
}


def attrs(rows, resource=False):
    if not isinstance(rows, list) or len(rows) > 32:
        raise ValueError('invalid OTLP attributes')
    out = {}
    for row in rows:
        if not isinstance(row, dict) or set(row) != {'key', 'value'}:
            raise ValueError('invalid OTLP attribute')
        key, value = row['key'], row['value']
        if (not isinstance(key, str) or not key or len(key) > 128 or key in out or
                not isinstance(value, dict) or len(value) != 1):
            raise ValueError('invalid or duplicate OTLP attribute value')
        if resource and key == 'service.name':
            service = value.get('stringValue')
            if not isinstance(service, str) or not SERVICE_RE.fullmatch(service):
                raise ValueError('invalid service.name')
            out[key] = service
        elif not resource and key in LABELS:
            label = value.get('stringValue', value.get('intValue'))
            if str(label) not in LABELS[key]:
                raise ValueError('attribute value is not allowed')
            out[key] = str(label)
    return out


def timestamp(value):
    if isinstance(value, bool) or not (isinstance(value, (int, str))):
        raise ValueError('invalid OTLP timestamp')
    try:
        result = int(value)
    except ValueError as error:
        raise ValueError('invalid OTLP timestamp') from error
    if result < 0 or result > 2**64 - 1:
        raise ValueError('OTLP timestamp outside bounds')
    return result


def number(value):
    if type(value) not in (int, float) and not isinstance(value, str):
        raise ValueError('invalid OTLP number')
    try:
        result = float(value)
    except ValueError as error:
        raise ValueError('invalid OTLP number') from error
    if not math.isfinite(result) or result < 0 or result > 1e18:
        raise ValueError('OTLP number outside bounds')
    return result


class Store:
    def __init__(self, allowed_services=(), allow_trace_ids=False):
        self.allowed = frozenset(allowed_services)
        self.allow_trace_ids = allow_trace_ids
        self.lock = threading.Lock()
        self.previous = {}
        self.totals = {}
        self.exemplars = deque(maxlen=128)
        self.accepted = self.rejected = 0

    def ingest(self, doc, trace_authorized=False):
        if not isinstance(doc, dict) or set(doc) != {'resourceMetrics'}:
            raise ValueError('invalid OTLP request')
        resources = doc['resourceMetrics']
        if not isinstance(resources, list) or not 1 <= len(resources) <= 8:
            raise ValueError('invalid resourceMetrics')
        updates, traces = [], []
        point_count = 0
        for resource in resources:
            if not isinstance(resource, dict) or set(resource) - {'resource', 'scopeMetrics', 'schemaUrl'}:
                raise ValueError('invalid resource')
            raw_resource = resource.get('resource', {})
            if not isinstance(raw_resource, dict):
                raise ValueError('invalid resource identity')
            identity = attrs(raw_resource.get('attributes', []), True)
            service = identity.get('service.name')
            if service is None or self.allowed and service not in self.allowed:
                raise PermissionError('service.name not authorized')
            scopes = resource.get('scopeMetrics')
            if not isinstance(scopes, list) or len(scopes) > 8:
                raise ValueError('invalid scopeMetrics')
            for scope in scopes:
                metrics = scope.get('metrics') if isinstance(scope, dict) else None
                if not isinstance(metrics, list) or len(metrics) > 16:
                    raise ValueError('invalid metrics')
                for metric in metrics:
                    if not isinstance(metric, dict) or metric.get('name') not in METRIC_TYPES:
                        raise ValueError('metric not allowlisted')
                    name = metric['name']
                    kind = METRIC_TYPES[name]
                    body = metric.get(kind)
                    if not isinstance(body, dict):
                        raise ValueError('metric aggregation type mismatch')
                    if kind == 'sum' and (body.get('aggregationTemporality') != 2 or
                                          body.get('isMonotonic') is not True):
                        raise ValueError('sum must be monotonic cumulative')
                    points = body.get('dataPoints')
                    if not isinstance(points, list) or len(points) > MAX_POINTS:
                        raise ValueError('invalid data points')
                    for point in points:
                        point_count += 1
                        if point_count > MAX_POINTS:
                            raise ValueError('request point quota exceeded')
                        fields = ({'attributes', 'startTimeUnixNano', 'timeUnixNano',
                                   'asInt', 'asDouble', 'exemplars'} if kind == 'sum' else
                                  {'attributes', 'startTimeUnixNano', 'timeUnixNano',
                                   'count', 'sum', 'explicitBounds', 'bucketCounts', 'exemplars'})
                        if (not isinstance(point, dict) or set(point) - fields or
                                'startTimeUnixNano' not in point or 'timeUnixNano' not in point):
                            raise ValueError('invalid data point')
                        labels = attrs(point.get('attributes', []))
                        if len(labels) > 4:
                            raise ValueError('too many attributes')
                        start = timestamp(point['startTimeUnixNano'])
                        end = timestamp(point['timeUnixNano'])
                        if end < start:
                            raise ValueError('data point ends before it starts')
                        key = (service, name, tuple(sorted(labels.items())))
                        if any(existing[0] == key for existing in updates):
                            raise ValueError('duplicate time series in OTLP request')
                        if kind == 'sum':
                            if ('asInt' in point) == ('asDouble' in point):
                                raise ValueError('sum point requires exactly one numeric value')
                            amount = number(point.get('asInt', point.get('asDouble')))
                            update = (key, kind, start, amount, None)
                        else:
                            bounds, counts = point.get('explicitBounds'), point.get('bucketCounts')
                            if (not isinstance(bounds, list) or len(bounds) > 32 or
                                    not isinstance(counts, list) or len(counts) != len(bounds) + 1):
                                raise ValueError('invalid histogram buckets')
                            normalized_bounds = [number(item) for item in bounds]
                            if any(a >= b for a, b in zip(normalized_bounds, normalized_bounds[1:])):
                                raise ValueError('histogram bounds must be strictly increasing')
                            bucket_values = [number(item) for item in counts]
                            count_value = number(point.get('count'))
                            if (any(not item.is_integer() for item in bucket_values) or
                                    not count_value.is_integer()):
                                raise ValueError('histogram counts must be integral')
                            buckets = tuple(map(int, bucket_values))
                            count, total = int(count_value), number(point.get('sum'))
                            if sum(buckets) != count:
                                raise ValueError('histogram buckets do not sum to count')
                            update = (key, kind, start, total,
                                      (count, tuple(normalized_bounds), buckets))
                        updates.append(update)
                        exemplars = point.get('exemplars', [])
                        if exemplars and (not isinstance(exemplars, list) or len(exemplars) > 4):
                            raise ValueError('too many exemplars')
                        if self.allow_trace_ids and trace_authorized:
                            for exemplar in exemplars:
                                encoded = exemplar.get('traceId') if isinstance(exemplar, dict) else None
                                if not isinstance(encoded, str):
                                    continue
                                try:
                                    trace = base64.b64decode(encoded, validate=True)
                                except (ValueError, binascii.Error):
                                    continue
                                if len(trace) == 16:
                                    traces.append((service, trace.hex(), time.time_ns()))
        if len(updates) > MAX_POINTS:
            raise ValueError('request point limit exceeded')
        with self.lock:
            new_keys = {update[0] for update in updates if update[0] not in self.previous}
            if len(self.previous) + len(new_keys) > MAX_SERIES:
                raise ValueError('time-series quota exhausted')
            for key, kind, start, amount, histogram in updates:
                old = self.previous.get(key)
                if old and old['kind'] != kind:
                    raise ValueError('metric type changed')
                if kind == 'histogram' and old and old['bounds'] != histogram[1]:
                    raise ValueError('histogram bounds changed')
            for key, kind, start, amount, histogram in updates:
                old = self.previous.get(key)
                if kind == 'sum':
                    prior = old['value'] if old and old['start'] == start else 0
                    delta = amount - prior if amount >= prior else amount
                    self.totals[key] = self.totals.get(key, 0) + delta
                    self.previous[key] = {'kind': kind, 'start': start, 'value': amount}
                else:
                    count, bounds, buckets = histogram
                    prior = old if old and old['start'] == start else None
                    valid = (prior and count >= prior['count'] and amount >= prior['sum'] and
                             all(a >= b for a, b in zip(buckets, prior['buckets'])))
                    if valid:
                        dc, ds = count - prior['count'], amount - prior['sum']
                        db = tuple(a - b for a, b in zip(buckets, prior['buckets']))
                    else:
                        dc, ds, db = count, amount, buckets
                    total = self.totals.get(key, {'count': 0, 'sum': 0, 'bounds': bounds,
                                                  'buckets': (0,) * len(buckets)})
                    self.totals[key] = {'count': total['count'] + dc, 'sum': total['sum'] + ds,
                                        'bounds': bounds,
                                        'buckets': tuple(a + b for a, b in zip(total['buckets'], db))}
                    self.previous[key] = {'kind': kind, 'start': start, 'count': count,
                                          'sum': amount, 'bounds': bounds, 'buckets': buckets}
            self.exemplars.extend(traces)
            self.accepted += len(updates)
        return {'accepted': len(updates)}

    def note_rejected(self):
        with self.lock:
            self.rejected += 1

    def prometheus(self):
        with self.lock:
            lines, declared = [], set()
            for (service, name, label_items), value in self.totals.items():
                metric = PROM_NAMES[name]
                if isinstance(value, dict) and name.endswith('.duration'):
                    metric += '_seconds'
                elif not isinstance(value, dict):
                    metric += '_total'
                labels = dict(label_items)
                labels['service'] = service
                fmt = lambda mapping: '{' + ','.join(
                    f'{key}="{str(item).replace(chr(92), chr(92) + chr(92)).replace(chr(34), chr(92) + chr(34))}"'
                    for key, item in sorted(mapping.items())) + '}'
                if name not in declared:
                    kind = 'histogram' if isinstance(value, dict) else 'counter'
                    lines.extend((f'# HELP {metric} Application reported OTLP measurement.',
                                  f'# TYPE {metric} {kind}'))
                    declared.add(name)
                if isinstance(value, dict):
                    cumulative = 0
                    for bound, count in zip(value['bounds'], value['buckets']):
                        cumulative += count
                        lines.append(f'{metric}_bucket{fmt(dict(labels, le=bound))} {cumulative}')
                    cumulative += value['buckets'][-1]
                    lines.append(f'{metric}_bucket{fmt(dict(labels, le="+Inf"))} {cumulative}')
                    lines.append(f'{metric}_sum{fmt(labels)} {value["sum"]}')
                    lines.append(f'{metric}_count{fmt(labels)} {value["count"]}')
                else:
                    lines.append(f'{metric}{fmt(labels)} {value}')
            lines.extend(('# HELP flowcore_otel_ingest_accepted_total Accepted OTLP data points.',
                          '# TYPE flowcore_otel_ingest_accepted_total counter',
                          f'flowcore_otel_ingest_accepted_total {self.accepted}',
                          '# HELP flowcore_otel_ingest_rejected_total Rejected OTLP requests.',
                          '# TYPE flowcore_otel_ingest_rejected_total counter',
                          f'flowcore_otel_ingest_rejected_total {self.rejected}'))
            return '\n'.join(lines) + '\n'

    def trace_snapshot(self):
        with self.lock:
            return [{'service': service, 'trace_id': trace, 'received_unix_ns': stamp}
                    for service, trace, stamp in self.exemplars]


class BoundedHTTPServer(ThreadingMixIn, HTTPServer):
    request_queue_size = 32

    def __init__(self, address, handler, store, token=None, workers=16):
        self.store, self.token = store, token
        self.slots = threading.BoundedSemaphore(workers)
        super().__init__(address, handler)

    def get_request(self):
        request, client_address = super().get_request()
        request.settimeout(5.0)
        return request, client_address

    def process_request(self, request, client_address):
        if not self.slots.acquire(False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


class Handler(BaseHTTPRequestHandler):
    server_version = 'FlowcoreOTLP/1'

    def log_message(self, *_):
        return

    def authorized(self):
        return self.server.token is None or hmac.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + self.server.token)

    def do_GET(self):
        if self.path == '/metrics' and self.authorized():
            data = self.server.store.prometheus().encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain; version=0.0.4; charset=utf-8')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif self.path == '/debug/exemplars' and self.server.token and self.authorized():
            data = json.dumps(self.server.store.trace_snapshot(), separators=(',', ':')).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path != '/v1/metrics':
            self.send_error(404)
            return
        if not self.authorized():
            self.send_error(401)
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 1 <= length <= MAX_BODY:
                raise ValueError('request size outside bounds')
            body = json.loads(self.rfile.read(length))
            answer = self.server.store.ingest(body, trace_authorized=bool(self.server.token))
        except PermissionError:
            self.server.store.note_rejected()
            self.send_error(403)
            return
        except (ValueError, json.JSONDecodeError, RecursionError, TypeError, OverflowError,
                AttributeError, KeyError, IndexError):
            self.server.store.note_rejected()
            self.send_error(400)
            return
        data = json.dumps({'partialSuccess': {'rejectedDataPoints': 0},
                           'accepted': answer['accepted']}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bind', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=9464)
    parser.add_argument('--token')
    parser.add_argument('--token-fd', type=int)
    parser.add_argument('--allow-service', action='append', default=[])
    parser.add_argument('--allow-trace-ids', action='store_true')
    args = parser.parse_args()
    if args.token is not None and args.token_fd is not None:
        parser.error('use only one token source')
    if args.token_fd is not None:
        if args.token_fd < 0:
            parser.error('--token-fd must be nonnegative')
        try:
            token_bytes = os.read(args.token_fd, 256)
        finally:
            os.close(args.token_fd)
        try:
            args.token = token_bytes.decode('ascii')
        except UnicodeDecodeError:
            parser.error('token must be ASCII')
    if args.token is not None and not 32 <= len(args.token) <= 128:
        parser.error('token length must be between 32 and 128 characters')
    try:
        address = ipaddress.ip_address(args.bind)
    except ValueError:
        parser.error('--bind must be an IP address')
    if not address.is_loopback and not args.token:
        parser.error('non-loopback bind requires a bearer token')
    if args.allow_trace_ids and not args.token:
        parser.error('trace-ID retention requires a bearer token')
    if not 1 <= args.port <= 65535 or any(not SERVICE_RE.fullmatch(name) for name in args.allow_service):
        parser.error('invalid bind, port or allowlist')
    store = Store(args.allow_service, args.allow_trace_ids)
    server = BoundedHTTPServer((args.bind, args.port), Handler, store, args.token)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
