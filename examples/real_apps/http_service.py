"""Bounded threaded HTTP application used by the FLOWCORE integration lab."""
import argparse
import json
import hashlib
import math
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
import re
import stat
import threading
import time
from urllib.parse import parse_qs, urlsplit

from flowcore_adapter import FlowcoreAdapter

CHUNK = b'F' * 65536
MAX_TRANSFER = 64 * 1024 * 1024


class AppMetrics:
    def __init__(self):
        self.lock = threading.Lock()
        self.values = {'requests': 0, 'errors': 0, 'deadline_misses': 0, 'bytes_sent': 0, 'bytes_received': 0}
        self.durations = {'critical': [], 'background': [], 'database': []}

    def record(self, role, status, elapsed, deadline_ms, byte_count=0, received_count=0):
        with self.lock:
            self.values['requests'] += 1
            self.values['errors'] += int(status >= 400)
            self.values['deadline_misses'] += int(deadline_ms > 0 and elapsed * 1000 > deadline_ms)
            self.values['bytes_sent'] += byte_count
            self.values['bytes_received'] += received_count
            rows = self.durations.setdefault(role, [])
            rows.append(elapsed)
            if len(rows) > 8192:
                del rows[:4096]

    def prometheus(self):
        with self.lock:
            rows = ['# HELP app_http_requests_total Requests completed by this process.',
                    '# TYPE app_http_requests_total counter',
                    f'app_http_requests_total {self.values["requests"]}',
                    '# HELP app_http_errors_total HTTP 5xx responses.',
                    '# TYPE app_http_errors_total counter',
                    f'app_http_errors_total {self.values["errors"]}',
                    '# HELP app_http_deadline_misses_total Server-side duration exceeded declared deadline.',
                    '# TYPE app_http_deadline_misses_total counter',
                    f'app_http_deadline_misses_total {self.values["deadline_misses"]}',
                    '# HELP app_http_response_bytes_total Bytes returned by the service.',
                    '# TYPE app_http_response_bytes_total counter',
                    f'app_http_response_bytes_total {self.values["bytes_sent"]}',
                    '# HELP app_http_request_body_bytes_total Request body bytes consumed.',
                    '# TYPE app_http_request_body_bytes_total counter',
                    f'app_http_request_body_bytes_total {self.values["bytes_received"]}']
            for role, samples in sorted(self.durations.items()):
                values = sorted(samples)
                for percentile in (50, 95, 99):
                    index = max(0, math.ceil(len(values) * percentile / 100) - 1)
                    value = values[index] if values else 0
                    rows.append(f'app_http_duration_seconds{{role="{role}",quantile="{percentile/100:.2f}"}} {value}')
            return '\n'.join(rows) + '\n'


METRICS = AppMetrics()


class BoundedHTTPServer(ThreadingMixIn, HTTPServer):
    request_queue_size = 128
    daemon_threads = True

    def __init__(self, address, handler, max_workers):
        self.slots = threading.BoundedSemaphore(max_workers)
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
    protocol_version = 'HTTP/1.1'
    server_version = 'FlowcoreExample/1'

    def log_message(self, *_):
        return

    def _send(self, status, value, content_type='application/json'):
        data = value if isinstance(value, bytes) else json.dumps(value, separators=(',', ':')).encode()
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Connection', 'keep-alive')
        self.end_headers()
        self.wfile.write(data)
        return len(data)

    def _deadline(self):
        try:
            value = float(self.headers.get('X-Deadline-Ms', '0'))
            return value if math.isfinite(value) and 0 < value <= 600000 else 0
        except ValueError:
            return 0

    def do_GET(self):
        adapter = getattr(self.server, 'adapter', None)
        if adapter is None or urlsplit(self.path).path != '/api/critical':
            return self._handle_get()
        with adapter.request(sock=self.connection, deadline_ms=self._deadline()) as request:
            self._flowcore_request = request
            try:
                return self._handle_get()
            finally:
                self._flowcore_request = None

    def _handle_get(self):
        start = time.monotonic()
        route = urlsplit(self.path)
        status, sent, role = 200, 0, self.server.role
        deadline_ms = self._deadline()
        try:
            if route.path == '/health':
                adapter = getattr(self.server, 'adapter', None)
                if adapter is not None:
                    adapter.flush(3000)
                sent = self._send(200, {'status': 'ok', 'role': role,
                                        'flowcore': adapter.status() if adapter else None})
            elif route.path == '/metrics':
                sent = self._send(200, METRICS.prometheus().encode(),
                                  'text/plain; version=0.0.4; charset=utf-8')
            elif route.path == '/api/critical' and role == 'critical':
                query = parse_qs(route.query, strict_parsing=False)
                work_ms = min(1000, max(0, int(query.get('work_ms', ['2'])[0])))
                time.sleep(work_ms / 1000)
                sent = self._send(200, {'ok': True, 'role': 'critical', 'work_ms': work_ms})
            elif route.path == '/transfer' and role == 'background':
                query = parse_qs(route.query, strict_parsing=False)
                size = int(query.get('bytes', ['0'])[0])
                if not 0 <= size <= MAX_TRANSFER:
                    status = 400
                    sent = self._send(status, {'error': 'bytes must be within 64 MiB'})
                else:
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/octet-stream')
                    self.send_header('Content-Length', str(size))
                    self.send_header('Connection', 'keep-alive')
                    self.end_headers()
                    remaining = size
                    while remaining:
                        chunk = CHUNK[:min(remaining, len(CHUNK))]
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
                    sent = size
            elif route.path.startswith('/artifact/') and role == 'background':
                name = route.path.removeprefix('/artifact/')
                if not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', name) or name in ('.', '..'):
                    status = 400
                    sent = self._send(status, {'error': 'invalid artifact name'})
                else:
                    descriptor = os.open(name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                                         dir_fd=self.server.artifact_fd)
                    try:
                        metadata = os.fstat(descriptor)
                        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_TRANSFER:
                            raise ValueError('artifact is not a bounded regular file')
                        self.send_response(200)
                        self.send_header('Content-Type', 'application/octet-stream')
                        self.send_header('Content-Length', str(metadata.st_size))
                        self.send_header('Connection', 'keep-alive')
                        self.end_headers()
                        while chunk := os.read(descriptor, 65536):
                            self.wfile.write(chunk)
                        sent = metadata.st_size
                    finally:
                        os.close(descriptor)
            elif route.path == '/api/database' and role == 'database':
                dsn = self.server.db_dsn
                if not dsn:
                    status = 503
                    sent = self._send(status, {'error': 'set FLOWCORE_DATABASE_DSN to enable this endpoint'})
                else:
                    import psycopg
                    with psycopg.connect(dsn, connect_timeout=2) as connection:
                        row = connection.execute('SELECT value FROM flowcore_bench WHERE id = 1').fetchone()
                    sent = self._send(200, {'value': row[0]})
            else:
                status = 404
                sent = self._send(status, {'error': 'not found'})
        except (BrokenPipeError, ConnectionResetError):
            status = 499
        except Exception as error:
            status = 503 if type(error).__name__ in ('OperationalError', 'InterfaceError') else 500
            try:
                sent = self._send(status, {'error': type(error).__name__})
            except (BrokenPipeError, ConnectionResetError):
                status = 499
        finally:
            request = getattr(self, '_flowcore_request', None)
            if status >= 400 and request is not None:
                request.complete(2)
            METRICS.record(role, status, time.monotonic() - start, deadline_ms, sent)

    def do_POST(self):
        start = time.monotonic()
        route = urlsplit(self.path)
        role = self.server.role
        status, sent, received = 200, 0, 0
        try:
            if route.path != '/ingest' or role != 'background':
                status = 404
                sent = self._send(status, {'error': 'not found'})
            elif self.headers.get('Transfer-Encoding') is not None:
                status = 400
                self.close_connection = True
                sent = self._send(status, {'error': 'chunked uploads are not supported'})
            else:
                raw_length = self.headers.get('Content-Length')
                if not raw_length or not raw_length.isascii() or not raw_length.isdecimal():
                    raise ValueError('invalid content length')
                remaining = int(raw_length)
                if not 1 <= remaining <= MAX_TRANSFER:
                    raise ValueError('upload length outside 64 MiB limit')
                digest = hashlib.sha256()
                while remaining:
                    chunk = self.rfile.read(min(65536, remaining))
                    if not chunk:
                        raise ValueError('truncated upload')
                    received += len(chunk)
                    remaining -= len(chunk)
                    digest.update(chunk)
                sent = self._send(200, {'received_bytes': received, 'sha256': digest.hexdigest()})
        except (BrokenPipeError, ConnectionResetError):
            status = 499
        except (ValueError, OverflowError):
            status = 400
            self.close_connection = True
            try:
                sent = self._send(status, {'error': 'invalid bounded upload'})
            except (BrokenPipeError, ConnectionResetError):
                status = 499
        except Exception as error:
            status = 500
            try:
                sent = self._send(status, {'error': type(error).__name__})
            except (BrokenPipeError, ConnectionResetError):
                status = 499
        finally:
            METRICS.record(role, status, time.monotonic() - start, 0, sent, received)

    def do_HEAD(self):
        self._send(200, b'')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bind', default='0.0.0.0')
    parser.add_argument('--critical-port', type=int, default=9000)
    parser.add_argument('--background-port', type=int, default=9001)
    parser.add_argument('--database-port', type=int, default=9002)
    parser.add_argument('--max-workers', type=int, default=128)
    parser.add_argument('--database-dsn', default='')
    parser.add_argument('--artifact-dir', default='')
    parser.add_argument('--flowcore', action='store_true',
                        help='register the critical HTTP service and report server request scopes')
    args = parser.parse_args()
    ports = [args.critical_port, args.background_port, args.database_port]
    if len(set(ports)) != len(ports) or any(not 1 <= port <= 65535 for port in ports):
        parser.error('ports must be unique and in [1,65535]')
    if not 1 <= args.max_workers <= 512:
        parser.error('--max-workers must be in [1,512]')
    servers = []
    threads = []
    try:
        for role, port in zip(('critical', 'background', 'database'), ports):
            server = BoundedHTTPServer((args.bind, port), Handler, args.max_workers)
            server.role, server.db_dsn = role, args.database_dsn
            server.adapter = (FlowcoreAdapter('api-service', priority='critical',
                               target_p99_ms=30, deadline_ms=30)
                             if args.flowcore and role == 'critical' else None)
            server.artifact_fd = (os.open(args.artifact_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
                                  if args.artifact_dir and role == 'background' else -1)
            servers.append(server)
            thread = threading.Thread(target=server.serve_forever, name=f'http-{role}', daemon=True)
            thread.start()
            threads.append(thread)
        print(json.dumps({'ready': True, 'ports': ports,
                          'flowcore': next((server.adapter.status() for server in servers
                                            if server.adapter is not None), None)}), flush=True)
        for thread in threads:
            thread.join()
    except KeyboardInterrupt:
        pass
    finally:
        for server in servers:
            server.shutdown()
            if server.artifact_fd >= 0:
                os.close(server.artifact_fd)
            if getattr(server, 'adapter', None) is not None:
                server.adapter.close()
            server.server_close()


if __name__ == '__main__':
    main()
