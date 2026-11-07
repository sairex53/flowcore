"""Fabric v1 framing, mutual TLS and bounded request dispatch."""
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import socket
import ssl
import struct
import stat
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from ipc_security import decode

VERSION = 1
LIMIT = 128 * 1024
MAX_AGENTS = 16
CLASSES = ('critical', 'interactive', 'standard', 'background')


class Rejected(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def fields(value, required, optional=()):
    if not isinstance(value, dict) or set(required) - value.keys() or value.keys() - set(required) - set(optional):
        raise ValueError('unexpected or missing fields')
    return value


def ident(value, limit=48):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,%d}' % (limit-1), value):
        raise ValueError('invalid identifier')
    return value


def integer(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f'expected integer in [{low}, {high}]')
    return value


def hexdigest(value, size=64):
    if not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{%d}' % size, value):
        raise ValueError('invalid hexadecimal identifier')
    return value


def boolean(value):
    if type(value) is not bool:
        raise ValueError('expected boolean')
    return value


def bounded_list(value, limit):
    if not isinstance(value, list) or len(value) > limit:
        raise ValueError('expected bounded list')
    return value


def tls(settings, server=False):
    fields(settings, ('ca', 'cert', 'key'))
    for value in settings.values():
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise ValueError('TLS paths must be absolute')
    key = Path(settings['key']).stat()
    if not stat.S_ISREG(key.st_mode) or key.st_mode & 0o077:
        raise PermissionError('Fabric TLS key must be a private regular file')
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER if server else ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.verify_mode = ssl.CERT_REQUIRED
    context.load_verify_locations(cafile=settings['ca'])
    context.load_cert_chain(settings['cert'], settings['key'])
    if server:
        context.num_tickets = 0
    else:
        context.hostname_checks_common_name = False
    return context


def exact(sock, count, deadline):
    data = bytearray()
    while len(data) < count:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('frame deadline')
        sock.settimeout(remaining)
        chunk = sock.recv(count - len(data))
        if not chunk:
            raise EOFError('short frame')
        data.extend(chunk)
    return data


def receive(sock):
    deadline = time.monotonic() + 3
    size, = struct.unpack('!I', exact(sock, 4, deadline))
    if not 1 <= size <= LIMIT:
        raise Rejected('frame_size')
    value = decode(exact(sock, size, deadline))
    if type(value.get('version')) is not int or value['version'] != VERSION:
        raise Rejected('protocol_version')
    return value


def send(sock, message):
    payload = json.dumps(dict(message, version=VERSION), separators=(',', ':'), allow_nan=False).encode()
    if not 1 <= len(payload) <= LIMIT:
        raise Rejected('frame_size')
    sock.settimeout(3)
    sock.sendall(struct.pack('!I', len(payload)) + payload)


def exchange(sock, message):
    send(sock, message)
    response = receive(sock)
    if response['op'] == 'error':
        raise Rejected(response['code'])
    if response['op'] != 'result':
        raise Rejected('unexpected_response')
    return response['result']


def remote(context, endpoint, message):
    with socket.create_connection((endpoint['host'], endpoint['port']), timeout=3) as raw:
        with context.wrap_socket(raw, server_hostname=endpoint['server_name']) as sock:
            return exchange(sock, message)


def endpoint(value):
    fields(value, ('host', 'port', 'server_name'))
    for key in ('host', 'server_name'):
        if not isinstance(value[key], str) or not 1 <= len(value[key]) <= 253 or any(
                ord(char) < 33 or ord(char) > 126 for char in value[key]):
            raise ValueError('invalid endpoint address or name')
    integer(value['port'], 1, 65535)
    return value


def audit(event, **values):
    print(json.dumps(dict(event=event, time_ns=time.time_ns(), **values), separators=(',', ':')),
          flush=True)


def response(sock, dispatch, identity):
    try:
        result = dispatch(identity, receive(sock))
        send(sock, {'op': 'result', 'result': result})
    except Rejected as error:
        send(sock, {'op': 'error', 'code': error.code})
    except (ValueError, KeyError, TypeError, OverflowError):
        send(sock, {'op': 'error', 'code': 'invalid_request'})


def serve_tls(listener, context, dispatch):
    """Bound accepted TLS handshakes and use one short request per connection."""
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    slots = threading.BoundedSemaphore(16)

    def serve(raw):
        try:
            with raw:
                raw.settimeout(3)
                with context.wrap_socket(raw, server_side=True) as sock:
                    pin = hashlib.sha256(sock.getpeercert(binary_form=True)).hexdigest()
                    response(sock, dispatch, pin)
        except (OSError, EOFError):
            pass
        finally:
            slots.release()

    listener.settimeout(.2)
    with ThreadPoolExecutor(max_workers=16) as workers:
        while not stop.is_set():
            try:
                raw, _ = listener.accept()
            except TimeoutError:
                continue
            if slots.acquire(False):
                workers.submit(serve, raw)
            else:
                raw.close()


def locked_state(directory):
    import fcntl
    from ipc_security import private_directory, private_lock
    private_directory(directory)
    lock = private_lock(directory / 'lock')
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock.close()
        raise
    return lock
