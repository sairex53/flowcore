#!/usr/bin/env python3
"""Authenticated, read-only HTTP API and static FLOWCORE dashboard."""
import argparse
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, HTTPServer
import hmac
import ipaddress
import io
import json
import math
import os
from pathlib import Path
import pwd
import re
import select
import shutil
import signal
import socket
from socketserver import ThreadingMixIn
import sqlite3
import stat
import struct
import subprocess
import sys
import threading
import time
from urllib.parse import parse_qsl, urlsplit

from dashboard_artifacts import Artifacts
from dashboard_history import History
from dashboard_model import abstract_address
from fabric_protocol import locked_state
from paths import ROOT, SOCKET

WEB = ROOT / 'monitoring' / 'dashboard'
REPLY_LIMIT = 1024 * 1024
HEADER_LIMIT = 8192
ID = re.compile(r'[a-zA-Z0-9_.:%@-]{1,96}', re.ASCII)
TOKEN = re.compile(rb'[A-Za-z0-9_-]{32,128}')
INCIDENT_KINDS = frozenset(('window', 'queue', 'tcp', 'request', 'interface', 'policy',
                           'controller', 'resource', 'mptcp_path', 'fault', 'quality'))
SERIES_KINDS = frozenset(('interface', 'service', 'traffic_class'))


def now_ms():
    return time.time_ns()//1_000_000


class APIError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def source_uid(value):
    if value is None:
        return os.geteuid()
    if value.isdecimal():
        return int(value)
    try:
        return pwd.getpwnam(value).pw_uid
    except KeyError as error:
        raise OSError('monitor source account unavailable') from error


class Source:
    def __init__(self, address, user):
        if address.startswith('@'):
            if user is None:
                raise ValueError('an abstract source requires an explicit --daemon-user')
            self.address = abstract_address(address[1:])
        elif Path(address).is_absolute() and len(os.fsencode(address)) <= 107:
            self.address = address
        else:
            raise ValueError('source must be an absolute pathname or @abstract-name')
        self.user = user

    def call(self, op, **values):
        deadline = time.monotonic() + 1.5
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(1)
            sock.connect(self.address)
            _, uid, _ = struct.unpack('=3i', sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if uid != source_uid(self.user):
                raise PermissionError('monitor source UID mismatch')
            sock.sendall(json.dumps(dict(op=op, **values)).encode() + b'\n')
            data = bytearray()
            while not data.endswith(b'\n'):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('monitor source deadline')
                sock.settimeout(remaining)
                chunk = sock.recv(min(32768, REPLY_LIMIT+1-len(data)))
                if not chunk:
                    raise OSError('monitor source returned no complete frame')
                data.extend(chunk)
                if len(data) > REPLY_LIMIT:
                    raise ValueError('monitor source frame exceeds limit')
        reply = json.loads(data)
        if not isinstance(reply, dict) or reply.get('ok') is not True:
            raise ValueError('monitor source rejected the request')
        return reply['result']

    def snapshot(self, window):
        value = self.call('monitor_snapshot', window=window)
        if (value['schema'] != 1 or type(value['captured_ms']) is not int or any(
                not isinstance(value[name], list) for name in
                ('interfaces', 'services', 'classes', 'bottlenecks', 'policy_history'))):
            raise ValueError('invalid monitor source schema')
        return value


def bounded_output(argv):
    deadline = time.monotonic()+.8
    with subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as proc:
        try:
            data = bytearray()
            limit = 16*1024
            while len(data) <= limit:
                remaining = deadline-time.monotonic()
                if remaining <= 0 or not select.select([proc.stdout], [], [], remaining)[0]:
                    raise TimeoutError('MPTCP probe timeout')
                block = os.read(proc.stdout.fileno(), min(4096, limit+1-len(data)))
                if not block:
                    if proc.wait(timeout=max(.01, deadline-time.monotonic())):
                        raise OSError('MPTCP probe unavailable')
                    return bytes(data), False
                data.extend(block)
            return bytes(data[:limit]), True
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()


def multipath_probe():
    try:
        enabled = Path('/proc/sys/net/mptcp/enabled').read_text().strip() == '1'
    except OSError:
        enabled = None
    result = dict(scope='dashboard network namespace', kernel_enabled=enabled, connections=None,
                  endpoints=None, sampled_ms=now_ms(), truncated=False)
    executable = shutil.which('ss')
    if executable:
        try:
            raw, truncated = bounded_output([executable, '-H', '-M', '-a', '-n', '-O'])
            lines = raw.decode('utf-8', 'replace').splitlines()
            if truncated:
                lines = lines[:-1]
            result['truncated'] = truncated or len(lines) > 128
            result['connections'] = []
            for line in lines[:128]:
                columns = line.split()
                if len(columns) >= 5:
                    state, receive, send, local, peer = columns[:5]
                    result['connections'].append(dict(state=state, receive_queue=receive,
                                                       send_queue=send, local=local, peer=peer))
        except (OSError, subprocess.SubprocessError):
            pass
    executable = shutil.which('ip')
    if executable:
        try:
            raw, truncated = bounded_output([executable, '-j', 'mptcp', 'endpoint', 'show'])
            endpoints = json.loads(raw) if not truncated else None
            if isinstance(endpoints, list):
                result['endpoints'] = endpoints[:64]
                result['truncated'] |= len(endpoints) > 64
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    return result


class Sampler:
    def __init__(self, source, history, *, window, interval, probe=multipath_probe):
        self.source, self.history, self.window, self.interval, self.probe = source, history, window, interval, probe
        self.lock = threading.Lock()
        self.snapshot, self.received, self.error = None, None, 'awaiting first sample'
        self.storage_error = None
        self.mptcp = None
        self.next_probe = 0
        self.stop = threading.Event()
        self.thread = None

    def collect(self):
        try:
            snapshot = self.source.snapshot(self.window)
        except (OSError, ValueError, KeyError, TypeError, RecursionError):
            snapshot = None
        stamp = now_ms()
        with self.lock:
            if snapshot is not None:
                self.snapshot, self.received, self.error = snapshot, time.monotonic(), None
            else:
                self.error = 'flowcored monitoring sample unavailable'
        try:
            self.history.append(stamp, snapshot)
            storage_error = None
        except (sqlite3.Error, ValueError, KeyError, TypeError):
            storage_error = 'historical sample could not be stored'
        mptcp = None
        if time.monotonic() >= self.next_probe:
            mptcp = self.probe()
            self.next_probe = time.monotonic()+15
        with self.lock:
            self.storage_error = storage_error
            if mptcp is not None:
                self.mptcp = mptcp

    def latest(self):
        with self.lock:
            elapsed = time.monotonic()-self.received if self.received is not None else None
            state = ('unavailable' if self.snapshot is None else 'stale' if
                     self.error or elapsed > self.interval*3 else 'ok')
            return self.snapshot, dict(status=state, received_age_seconds=elapsed, source_error=self.error,
                 history_error=self.storage_error, captured_ms=self.snapshot['captured_ms'] if self.snapshot else None,
                 poll_seconds=self.interval)

    def run(self):
        while not self.stop.is_set():
            start = time.monotonic()
            self.collect()
            self.stop.wait(max(.1, self.interval-(time.monotonic()-start)))

    def __enter__(self):
        self.thread = threading.Thread(target=self.run, name='dashboard-sampler')
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.stop.set()
        self.thread.join()


def checked_id(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise APIError(400, 'invalid identifier')
    return value


def integer(value, low, high):
    if not re.fullmatch(r'[0-9]{1,9}', value) or not low <= int(value) <= high:
        raise APIError(400, 'query integer outside supported range')
    return int(value)


def query(params, names):
    if params.keys()-set(names):
        raise APIError(400, 'unknown query parameter')


def pagination(rows, params, *, maximum=64):
    offset = integer(params.get('offset', '0'), 0, 10000)
    limit = integer(params.get('limit', str(min(32, maximum))), 1, maximum)
    return dict(items=rows[offset:offset+limit], total=len(rows), offset=offset, limit=limit)


def bounded(value, depth=0):
    """Limit arbitrary nested details; never render source content as markup."""
    if isinstance(value, str):
        return value[:1024]
    if type(value) is float:
        return value if math.isfinite(value) else None
    if value is None or type(value) in (int, bool):
        return value
    if depth >= 6:
        return None
    if isinstance(value, list):
        return [bounded(item, depth+1) for item in value[:64]]
    if isinstance(value, dict):
        return {str(key)[:80]: bounded(item, depth+1) for key, item in list(value.items())[:64]}
    return None


class Dashboard:
    def __init__(self, sampler, artifacts):
        self.sampler, self.artifacts = sampler, artifacts
        self.incident_cache = OrderedDict()
        self.incident_lock = threading.Lock()

    def incident(self, identifier, snapshot):
        if not any(row['id'] == identifier for row in snapshot['incidents']['incidents']):
            raise APIError(404, 'incident is outside the retained inventory')
        with self.incident_lock:
            current = self.incident_cache.get(identifier)
            if current is None:
                try:
                    current = self.sampler.source.call('monitor_incident', id=identifier)
                    if current['id'] != identifier or not isinstance(current['events'], list):
                        raise ValueError('invalid incident response')
                except (OSError, ValueError, KeyError, TypeError, RecursionError) as error:
                    raise APIError(503, 'incident source unavailable') from error
                self.incident_cache[identifier] = current
                if len(self.incident_cache) > 4:
                    self.incident_cache.popitem(last=False)
            return current

    def artifact(self, identifier):
        if identifier not in self.artifacts.paths:
            raise APIError(404, 'unknown artifact')
        try:
            return self.artifacts.read(identifier)
        except (OSError, ValueError, KeyError, TypeError, RecursionError, OverflowError) as error:
            raise APIError(422, 'artifact unreadable or unsupported') from error

    def route(self, route, params):
        snap, meta = self.sampler.latest()
        if route == 'history':
            query(params, ('kind', 'id', 'seconds', 'points'))
            kind, identifier = params.get('kind'), checked_id(params.get('id'))
            if kind not in SERIES_KINDS:
                raise APIError(400, 'unknown metric series kind')
            seconds = integer(params.get('seconds', '900'), 1, min(604800, self.sampler.history.seconds))
            points = integer(params.get('points', '180'), 1, 300)
            return meta, self.sampler.history.series(now_ms(), kind, identifier, seconds, points)
        if route == 'benchmarks':
            query(params, ())
            return {'status': 'ok'}, {'items': self.artifacts.catalogue()}
        if route in ('benchmark', 'benchmark-run'):
            query(params, ('id', 'offset', 'limit') if route == 'benchmark' else ('id', 'run'))
            artifact = self.artifact(checked_id(params.get('id')))
            rows = artifact['runs']
            header = {key: bounded(value) for key, value in artifact.items() if key != 'runs'}
            if route == 'benchmark-run':
                index = integer(params.get('run', '0'), 0, len(rows)-1)
                return {'status': 'ok'}, dict(header, run=bounded(rows[index]), index=index)
            summaries = [dict(index=index, **{key: bounded(row.get(key)) for key in (
                'mode', 'seed', 'repetition', 'status', 'measured', 'latency', 'throughput_mbps',
                'cpu_seconds', 'fairness', 'controller_actions', 'transport')}) for index, row in enumerate(rows)]
            return {'status': 'ok'}, dict(header, **pagination(summaries, params, maximum=64))
        if route == 'multipath':
            query(params, ())
            with self.sampler.lock:
                value = self.sampler.mptcp
            return {'status': 'ok' if value else 'unavailable'}, value
        if route == 'health':
            query(params, ())
            return meta, None if snap is None else {'daemon': snap['status'], 'quality': snap['quality']}
        known = ('overview', 'interfaces', 'services', 'traffic-classes', 'controller',
                 'bottlenecks', 'policy-history', 'incidents', 'incident')
        if route not in known:
            raise APIError(404, 'unknown API route')
        if snap is None:
            raise APIError(503, 'awaiting flowcored monitoring data')
        if route == 'overview':
            query(params, ())
            return meta, {key: snap[key] for key in ('scope', 'window_seconds', 'status', 'quality',
                                                     'interfaces', 'services', 'classes', 'controller', 'link_events')}
        if route == 'bottlenecks':
            query(params, ('interface', 'offset', 'limit'))
            rows = snap['bottlenecks']
            if 'interface' in params:
                identifier = checked_id(params['interface'])
                rows = [row for row in rows if row['id'] == identifier]
            return meta, pagination(rows, params)
        if route == 'controller':
            query(params, ())
            return meta, snap['controller']
        if route == 'incidents':
            query(params, ('offset', 'limit'))
            value = snap['incidents']
            return meta, dict(**pagination(value['incidents'], params), evicted=value['evicted'],
                              retained_limit=value['retention'])
        if route == 'incident':
            query(params, ('id', 'kind', 'offset', 'limit'))
            value = self.incident(checked_id(params.get('id')), snap)
            kind = params.get('kind')
            if kind and kind not in INCIDENT_KINDS:
                raise APIError(400, 'unknown incident event kind')
            events = [row for row in value['events'] if not kind or row['kind'] == kind]
            items = pagination(events, params)
            return meta, dict(id=value['id'], origin_ns=value['origin_ns'], report=value['report'],
                              kinds=sorted({row['kind'] for row in value['events']}), **items)
        query(params, ('offset', 'limit'))
        field = {'traffic-classes': 'classes', 'policy-history': 'policy_history'}.get(route, route)
        rows = snap[field]
        if route == 'policy-history':
            page = pagination(rows, params, maximum=32)
            page['items'] = [bounded(row) for row in page['items']]
            return meta, page
        return meta, pagination(rows, params)


ASSETS = {'/': ('index.html', 'text/html; charset=utf-8'), '/dashboard.css': ('dashboard.css', 'text/css; charset=utf-8'),
          '/dashboard.js': ('dashboard.js', 'text/javascript; charset=utf-8'),
          '/flowcore.svg': ('flowcore.svg', 'image/svg+xml')}


class Handler(BaseHTTPRequestHandler):
    server_version = 'FLOWCORE'
    sys_version = ''

    def handle(self):
        # Parse exactly one small HTTP header block with an absolute deadline.
        # BaseHTTPRequestHandler's normal socket reader has per-read timeouts.
        deadline = time.monotonic() + 3
        data = bytearray()
        while b'\r\n\r\n' not in data and b'\n\n' not in data:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            if len(data) == HEADER_LIMIT:
                self.connection.sendall(b'HTTP/1.0 431 Request Header Fields Too Large\r\n'
                                        b'Content-Length: 0\r\nConnection: close\r\n\r\n')
                return
            self.connection.settimeout(remaining)
            try:
                chunk = self.connection.recv(min(4096, HEADER_LIMIT-len(data)))
            except TimeoutError:
                return
            if not chunk:
                return
            data.extend(chunk)
        self.connection.settimeout(3)
        self.rfile.close()
        self.rfile = io.BytesIO(data)
        self.close_connection = True
        self.handle_one_request()

    def log_message(self, *_):
        # Requests can contain private service names. Do not log raw paths,
        # request headers, or credentials.
        return

    def send_payload(self, status, payload, kind):
        self.send_response(status)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(payload)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Security-Policy', "default-src 'none'; script-src 'self'; style-src 'self'; "
                         "connect-src 'self'; img-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Connection', 'close')
        if status == 401:
            self.send_header('WWW-Authenticate', 'Bearer')
        if status == 405:
            self.send_header('Allow', 'GET')
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(payload)
        self.close_connection = True

    def envelope(self, code, meta, data=None, error=None):
        payload = json.dumps(dict(schema=1, status=meta.get('status', 'error'), data=data,
                                  meta=meta, error=error), separators=(',', ':'), allow_nan=False).encode()
        if len(payload) > REPLY_LIMIT:
            raise APIError(503, 'bounded response limit exceeded')
        self.send_payload(code, payload, 'application/json; charset=utf-8')

    def do_GET(self):
        try:
            if len(self.path) > 1024 or not self.path.startswith('/') or self.path.startswith('//'):
                raise APIError(400, 'invalid request target')
            hosts = self.headers.get_all('Host', [])
            if len(hosts) != 1 or hosts[0].lower() not in self.server.hosts:
                raise APIError(421, 'unexpected host')
            if self.headers.get('Transfer-Encoding') or self.headers.get_all('Content-Length', []) not in ([], ['0']):
                raise APIError(400, 'GET body is forbidden')
            target = urlsplit(self.path)
            if target.fragment or target.netloc:
                raise APIError(400, 'invalid request target')
            if target.path in self.server.assets and not target.query:
                data, kind = self.server.assets[target.path]
                return self.send_payload(200, data, kind)
            if not target.path.startswith('/api/v1/'):
                raise APIError(404, 'unknown route')
            auth = self.headers.get_all('Authorization', [])
            if len(auth) != 1 or not hmac.compare_digest(auth[0].encode('latin-1'), self.server.authorization):
                raise APIError(401, 'bearer authorization required')
            pairs = parse_qsl(target.query, keep_blank_values=True, strict_parsing=True, max_num_fields=8)
            params = dict(pairs)
            if len(pairs) != len(params):
                raise APIError(400, 'duplicate query parameter')
            meta, data = self.server.dashboard.route(target.path.removeprefix('/api/v1/'), params)
            self.envelope(200, meta, data)
        except APIError as error:
            self.envelope(error.code, {'status': 'unavailable' if error.code == 503 else 'error'}, error=str(error))
        except (ValueError, UnicodeError):
            self.envelope(400, {'status': 'error'}, error='invalid request or data')
        except (sqlite3.Error, KeyError, TypeError, RecursionError, OverflowError):
            self.envelope(503, {'status': 'unavailable'}, error='monitoring data unavailable')

    def denied(self):
        self.envelope(405, {'status': 'error'}, error='read-only service')

    do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_HEAD = do_TRACE = do_CONNECT = denied


class Server(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    request_queue_size = 32

    def __init__(self, address, dashboard, token, public_host=None):
        self.dashboard = dashboard
        self.authorization = b'Bearer '+token
        self.slots = threading.BoundedSemaphore(16)
        self.assets = {path: ((WEB/name).read_bytes(), kind) for path, (name, kind) in ASSETS.items()}
        super().__init__(address, Handler)
        self.hosts = {f'{address[0]}:{self.server_port}', f'localhost:{self.server_port}'}
        if public_host:
            self.hosts.add(public_host.lower())

    def get_request(self):
        sock, address = super().get_request()
        sock.settimeout(3)
        return sock, address

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

    def handle_error(self, request, client_address):
        pass


def token_file(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(descriptor, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid not in (0, os.geteuid())
                or info.st_mode & 0o077 or info.st_size > 130):
            raise PermissionError('token file must be a private, owned regular file')
        token = stream.read(131).removesuffix(b'\n')
        if not TOKEN.fullmatch(token):
            raise ValueError('token must contain 32–128 URL-safe characters')
        return token


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bind', default='127.0.0.1', help='IPv4 loopback address only')
    parser.add_argument('--port', type=int, default=8844)
    parser.add_argument('--public-host', help='exact Host header accepted from an HTTPS reverse proxy')
    parser.add_argument('--socket', default=str(SOCKET))
    parser.add_argument('--daemon-user', help='expected flowcored account or UID')
    parser.add_argument('--token-file', type=Path, required=True)
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--interval', type=int, default=2)
    parser.add_argument('--window', type=int, default=10)
    parser.add_argument('--retention-hours', type=int, default=24)
    parser.add_argument('--max-samples', type=int, default=7200)
    parser.add_argument('--storage-mib', type=int, default=64)
    parser.add_argument('--artifact', action='append', default=[], metavar='ID=JSON_PATH')
    args = parser.parse_args()
    try:
        if (os.geteuid() == 0 or not ipaddress.IPv4Address(args.bind).is_loopback or
                not 0 <= args.port <= 65535 or not 1 <= args.interval <= 60 or
                not 1 <= args.window <= 3600 or not 1 <= args.retention_hours <= 168 or
                not 16 <= args.max_samples <= 20000 or not 8 <= args.storage_mib <= 256 or
                len(args.artifact) > 16 or (args.public_host and not re.fullmatch(
                    r'[A-Za-z0-9.-]{1,253}(?::[0-9]{1,5})?', args.public_host))):
            raise ValueError('run unprivileged with a loopback bind and bounded configuration')
        artifacts = {}
        for item in args.artifact:
            name, separator, path = item.partition('=')
            if not separator or not path or checked_id(name) in artifacts:
                raise ValueError('expected unique ID=JSON_PATH artifact')
            artifacts[name] = Path(path).absolute()
        source = Source(args.socket, args.daemon_user)
        secret = token_file(args.token_file)
        os.umask(0o077)
        stopped = False

        def stop(*_):
            nonlocal stopped
            stopped = True

        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, stop)
        with locked_state(args.state):
            history = History(args.state, hours=args.retention_hours, samples=args.max_samples,
                              storage_mib=args.storage_mib)
            try:
                sampler = Sampler(source, history, window=args.window, interval=args.interval)
                dashboard = Dashboard(sampler, Artifacts(artifacts))
                with Server((args.bind, args.port), dashboard, secret, args.public_host) as server, sampler:
                    server.timeout = .25
                    print(json.dumps(dict(event='dashboard_ready', bind=args.bind, port=server.server_port)), flush=True)
                    while not stopped:
                        server.handle_request()
            finally:
                history.close()
    except (OSError, ValueError, sqlite3.Error) as error:
        print(f'flowcore dashboard: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
