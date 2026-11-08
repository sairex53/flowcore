#!/usr/bin/env python3
"""Reference FLOWID v1 server with transactional, durable demo application state."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
import hmac
import ipaddress
import os
from pathlib import Path
import re
import secrets
import signal
import socket
import sqlite3
import ssl
import stat
import struct
import sys
import threading
import time

from durable_state import read_json
from fabric_protocol import audit, fields, hexdigest, integer
from ipc_security import private_directory

VERSION = 1
ALPN = 'flowid/1'
CAPS = 7
TOKEN = 32
MAX_MESSAGE = 16 * 1024
MAX_FRAME = 4 + 8 + MAX_MESSAGE
MAX_CURSOR = 2**63 - 1
WINDOW = 64
OPEN, OPENED, RESUME, RESUMED, DATA, ACK, END, ENDED, ERROR = range(1, 10)
PROTOCOL, AUTH, EXPIRED, CONFLICT, QUOTA, CLOSED, FENCED, BUSY = range(1, 9)
IPPROTO_MPTCP = 262
TCP_IS_MPTCP = 43


class Rejected(ValueError):
    def __init__(self, code):
        super().__init__('FLOWID request rejected')
        self.code = code


def service_name(value):
    if (not isinstance(value, str) or not 1 <= len(value) <= 127 or any(
            not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label)
            for label in value.split('.'))):
        raise ValueError('invalid logical DNS service name')
    return value


def config(value):
    fields(value, ('service', 'listen', 'tls', 'clients'), ('ttl_seconds', 'session_limit', 'peer_limit', 'idle_seconds'))
    service_name(value['service'])
    listen = fields(value['listen'], ('address', 'port', 'transport'))
    if not isinstance(listen['address'], str) or '%' in listen['address']:
        raise ValueError('listen address must be numeric')
    ipaddress.ip_address(listen['address'])
    integer(listen['port'], 0, 65535)
    if listen['transport'] not in ('tcp', 'mptcp'):
        raise ValueError('unknown transport')
    fields(value['tls'], ('ca', 'certificate', 'key'))
    if any(not isinstance(path, str) or not Path(path).is_absolute() for path in value['tls'].values()):
        raise ValueError('TLS paths must be absolute')
    peers = value['clients']
    if not isinstance(peers, list) or not 1 <= len(peers) <= 32 or len(set(peers)) != len(peers):
        raise ValueError('invalid client pin set')
    for peer in peers:
        hexdigest(peer)
    integer(value.setdefault('ttl_seconds', 300), 1, 3600)
    integer(value.setdefault('session_limit', 128), 1, 256)
    integer(value.setdefault('peer_limit', min(16, value['session_limit'])), 1, value['session_limit'])
    integer(value.setdefault('idle_seconds', 8), 1, 30)
    return value


def ssl_context(value):
    fd = os.open(value['key'], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        key = os.fstat(fd)
        if (not stat.S_ISREG(key.st_mode) or key.st_nlink != 1 or not 1 <= key.st_size <= 65536
                or key.st_uid not in (0, os.geteuid()) or key.st_mode & 0o077):
            raise PermissionError('TLS key must be an owned private regular file')
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_3
        context.verify_mode = ssl.CERT_REQUIRED
        context.num_tickets = 0
        context.set_alpn_protocols([ALPN])
        context.load_verify_locations(cafile=value['ca'])
        context.load_cert_chain(value['certificate'], f'/proc/self/fd/{fd}', password=lambda: '')
    finally:
        os.close(fd)
    return context


def exact(sock, size, deadline):
    value = bytearray()
    while len(value) < size:
        remaining = deadline-time.monotonic()
        if remaining <= 0:
            raise TimeoutError('FLOWID frame deadline')
        sock.settimeout(remaining)
        chunk = sock.recv(size-len(value))
        if not chunk:
            raise EOFError('FLOWID transport closed')
        value.extend(chunk)
    return value


def receive(sock, seconds):
    deadline = time.monotonic()+seconds
    size, = struct.unpack('!I', exact(sock, 4, deadline))
    if not 4 <= size <= MAX_FRAME:
        raise Rejected(PROTOCOL)
    frame = exact(sock, size, deadline)
    if frame[0] != VERSION or frame[2] or frame[3]:
        raise Rejected(PROTOCOL)
    return frame[1], bytes(frame[4:])


def send(sock, op, body=b''):
    sock.settimeout(2)
    sock.sendall(struct.pack('!IBBH', 4+len(body), VERSION, op, 0)+body)


def cursor(raw):
    if len(raw) != 8:
        raise Rejected(PROTOCOL)
    value, = struct.unpack('!Q', raw)
    if value > MAX_CURSOR:
        raise Rejected(PROTOCOL)
    return value


def opening(operation, body, service):
    if operation not in (OPEN, RESUME) or len(body) < 3:
        raise Rejected(PROTOCOL)
    offered, size = struct.unpack('!HB', body[:3])
    if offered & CAPS != CAPS or len(body) != 3+size+(TOKEN+8 if operation == RESUME else 0):
        raise Rejected(PROTOCOL)
    if body[3:3+size] != service.encode('ascii'):
        raise Rejected(AUTH)
    return (body[3+size:3+size+TOKEN], cursor(body[-8:])) if operation == RESUME else (None, 0)


class Store:
    """The demo commit counter and its deduplication record share one transaction."""

    def __init__(self, directory, cfg, clock=time.time):
        private_directory(directory)
        path = directory/'flowid-v1.sqlite'
        fd = os.open(path, os.O_CREAT | os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, 0o600)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or info.st_nlink != 1 or info.st_mode & 0o077):
                raise PermissionError('state database must be an owned private regular file')
        finally:
            os.close(fd)
        self.db = sqlite3.connect(path, timeout=1, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock, self.cfg, self.clock = threading.Lock(), cfg, clock
        try:
            self.db.execute('PRAGMA journal_mode=DELETE')
            self.db.execute('PRAGMA synchronous=FULL')
            self.db.execute('PRAGMA foreign_keys=ON')
            pages = 16*1024*1024//self.db.execute('PRAGMA page_size').fetchone()[0]
            if self.db.execute(f'PRAGMA max_page_count={pages}').fetchone()[0] > pages:
                raise ValueError('FLOWID database exceeds configured cap')
            with self.transaction():
                self.db.execute('CREATE TABLE IF NOT EXISTS identity (id INTEGER PRIMARY KEY CHECK(id=1), service TEXT NOT NULL)')
                self.db.execute('INSERT OR IGNORE INTO identity VALUES (1, ?)', (cfg['service'],))
                if self.db.execute('SELECT service FROM identity WHERE id=1').fetchone()[0] != cfg['service']:
                    raise ValueError('FLOWID database belongs to another service')
                self.db.execute('''CREATE TABLE IF NOT EXISTS sessions (
                    id BLOB PRIMARY KEY, peer TEXT NOT NULL, expires INTEGER NOT NULL,
                    association INTEGER NOT NULL, committed INTEGER NOT NULL,
                    closed INTEGER NOT NULL, app_messages INTEGER NOT NULL, app_bytes INTEGER NOT NULL
                    ) WITHOUT ROWID''')
                self.db.execute('''CREATE TABLE IF NOT EXISTS replay (
                    id BLOB NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    seq INTEGER NOT NULL, digest BLOB NOT NULL, PRIMARY KEY(id,seq)
                    ) WITHOUT ROWID''')
        except BaseException:
            self.db.close()
            raise

    @contextmanager
    def transaction(self):
        with self.lock:
            self.db.execute('BEGIN IMMEDIATE')
            try:
                yield
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise

    def now(self):
        return int(self.clock())

    def state(self, identifier, peer, association=None):
        row = self.db.execute('SELECT * FROM sessions WHERE id=?', (identifier,)).fetchone()
        if row is None or row['expires'] <= self.now():
            raise Rejected(EXPIRED)
        if not hmac.compare_digest(row['peer'], peer):
            raise Rejected(AUTH)
        if row['closed']:
            raise Rejected(CLOSED)
        if association is not None and association != row['association']:
            raise Rejected(FENCED)
        return row

    def open(self, peer):
        token = secrets.token_bytes(TOKEN)
        identifier = hashlib.sha256(token).digest()
        ttl = self.cfg['ttl_seconds']
        with self.transaction():
            now = self.now()
            self.db.execute('DELETE FROM sessions WHERE expires <= ?', (now,))
            total, own = self.db.execute('SELECT count(*), count(CASE WHEN peer=? THEN 1 END) FROM sessions',
                                         (peer,)).fetchone()
            if total >= self.cfg['session_limit'] or own >= self.cfg['peer_limit']:
                raise Rejected(QUOTA)
            self.db.execute('INSERT INTO sessions VALUES (?,?,?,1,0,0,0,0)', (identifier, peer, now+ttl))
        return identifier, token, 0, 1, ttl

    def resume(self, token, peer, acknowledged):
        identifier = hashlib.sha256(token).digest()
        with self.transaction():
            row = self.state(identifier, peer)
            if acknowledged > row['committed'] or row['association'] == MAX_CURSOR:
                raise Rejected(CONFLICT)
            association = row['association']+1
            self.db.execute('UPDATE sessions SET association=? WHERE id=?', (association, identifier))
            return identifier, row['committed'], association, max(1, row['expires']-self.now())

    def deliver(self, identifier, peer, association, sequence, payload):
        if not 1 <= sequence <= MAX_CURSOR or not 1 <= len(payload) <= MAX_MESSAGE:
            raise Rejected(PROTOCOL)
        digest = hashlib.sha256(payload).digest()
        with self.transaction():
            row = self.state(identifier, peer, association)
            if sequence <= row['committed']:
                replay = self.db.execute('SELECT digest FROM replay WHERE id=? AND seq=?',
                                         (identifier, sequence)).fetchone()
                if replay is None or not hmac.compare_digest(replay['digest'], digest):
                    raise Rejected(CONFLICT)
                return row['committed'], False
            if sequence != row['committed']+1 or row['app_bytes'] > MAX_CURSOR-len(payload):
                raise Rejected(CONFLICT)
            self.db.execute('INSERT INTO replay VALUES (?,?,?)', (identifier, sequence, digest))
            self.db.execute('DELETE FROM replay WHERE id=? AND seq<=?', (identifier, sequence-WINDOW))
            self.db.execute('''UPDATE sessions SET committed=?, app_messages=app_messages+1,
                               app_bytes=app_bytes+? WHERE id=?''', (sequence, len(payload), identifier))
            return sequence, True

    def end(self, identifier, peer, association, acknowledged):
        with self.transaction():
            row = self.state(identifier, peer, association)
            if acknowledged != row['committed']:
                raise Rejected(CONFLICT)
            self.db.execute('UPDATE sessions SET closed=1 WHERE id=?', (identifier,))
        return acknowledged

    def close(self):
        self.db.close()


class Server:
    def __init__(self, cfg, store, context, drop_ack):
        self.cfg, self.store, self.context = cfg, store, context
        self.slots = threading.BoundedSemaphore(16)
        self.stopped = threading.Event()
        self.fault_lock = threading.Lock()
        self.drop_ack = drop_ack

    def inject(self, committed):
        with self.fault_lock:
            if committed == self.drop_ack:
                self.drop_ack = None
                audit('flowid_research_ack_dropped', sequence=committed)
                return True
            return False

    def protocol(self, sock, peer):
        try:
            operation, body = receive(sock, self.cfg['idle_seconds'])
            token, acknowledged = opening(operation, body, self.cfg['service'])
            if operation == OPEN:
                identifier, token, committed, association, ttl = self.store.open(peer)
                reply = struct.pack('!H', CAPS)+token
                code = OPENED
            else:
                identifier, committed, association, ttl = self.store.resume(token, peer, acknowledged)
                reply = struct.pack('!H', CAPS)
                code = RESUMED
            send(sock, code, reply+struct.pack('!QQI', committed, association, ttl))
            while not self.stopped.is_set():
                operation, body = receive(sock, self.cfg['idle_seconds'])
                if operation == DATA and len(body) > 8:
                    committed, applied = self.store.deliver(identifier, peer, association, cursor(body[:8]), body[8:])
                    if applied and self.inject(committed):
                        return
                    send(sock, ACK, struct.pack('!Q', committed))
                elif operation == END:
                    committed = self.store.end(identifier, peer, association, cursor(body))
                    send(sock, ENDED, struct.pack('!Q', committed))
                    return
                else:
                    raise Rejected(PROTOCOL)
        except Rejected as error:
            send(sock, ERROR, bytes((error.code,)))
        except sqlite3.Error:
            audit('flowid_store_unavailable')
            send(sock, ERROR, bytes((BUSY,)))

    def worker(self, raw):
        try:
            with raw:
                raw.settimeout(2)
                if self.cfg['listen']['transport'] == 'mptcp' and raw.getsockopt(socket.IPPROTO_TCP, TCP_IS_MPTCP) != 1:
                    return
                raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                with self.context.wrap_socket(raw, server_side=True) as sock:
                    if sock.selected_alpn_protocol() != ALPN:
                        return
                    peer = hashlib.sha256(sock.getpeercert(binary_form=True)).hexdigest()
                    if peer not in self.cfg['clients']:
                        send(sock, ERROR, bytes((AUTH,)))
                        return
                    self.protocol(sock, peer)
        except (OSError, EOFError):
            pass
        finally:
            self.slots.release()

    def serve(self):
        listen = self.cfg['listen']
        address = ipaddress.ip_address(listen['address'])
        family = socket.AF_INET if address.version == 4 else socket.AF_INET6
        protocol = socket.IPPROTO_TCP if listen['transport'] == 'tcp' else IPPROTO_MPTCP
        with socket.socket(family, socket.SOCK_STREAM, protocol) as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if family == socket.AF_INET6:
                listener.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            listener.bind((str(address), listen['port']))
            listener.listen(16)
            listener.settimeout(.2)
            audit('flowid_ready', service=self.cfg['service'], port=listener.getsockname()[1],
                  transport=listen['transport'])
            with ThreadPoolExecutor(max_workers=16) as pool:
                while not self.stopped.is_set():
                    try:
                        raw, _ = listener.accept()
                    except TimeoutError:
                        continue
                    if self.slots.acquire(False):
                        pool.submit(self.worker, raw)
                    else:
                        raw.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('config', type=Path)
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--research-drop-ack', type=int,
                        help='drop one acknowledgement after its durable commit in a lab')
    args = parser.parse_args()
    try:
        if os.geteuid() == 0:
            raise PermissionError('FLOWID server must run unprivileged')
        cfg = config(read_json(args.config))
        if args.research_drop_ack is not None:
            integer(args.research_drop_ack, 1, MAX_CURSOR)
            if not ipaddress.ip_address(cfg['listen']['address']).is_loopback:
                raise PermissionError('research acknowledgement fault requires loopback')
        context = ssl_context(cfg['tls'])
        os.umask(0o077)
        store = Store(args.state, cfg)
        try:
            server = Server(cfg, store, context, args.research_drop_ack)
            for sig in (signal.SIGINT, signal.SIGTERM):
                signal.signal(sig, lambda *_: server.stopped.set())
            server.serve()
        finally:
            store.close()
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error) as error:
        print(f'flowid server: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
