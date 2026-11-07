"""Version 1 authenticated network actuator for one isolated namespace."""
import fcntl
import json
import os
import re
import secrets
import signal
import socket
import stat
import struct
import sys
import time

from helper_netlink import Network
from ipc_security import decode
from service_config import keys, number, unique

VERSION = 1
LIMIT = 2048
CONFIG = '/etc/flowcore/helper.json'
CREDENTIALS = struct.Struct('=3i')
OPERATIONS = {
    'status': (),
    'lab.create': (),
    'lab.remove': (),
    'queue.set': ('limit_packets',),
    'queue.clear': (),
}
NS_GET_NSTYPE = 0xb703
CAP_NET_ADMIN = 1 << 12


class Rejected(Exception):
    def __init__(self, code):
        self.code = code


def request(raw):
    if not raw or len(raw) > LIMIT:
        raise Rejected('invalid_size')
    try:
        value = decode(raw)
        op = value['op']
        if type(value.get('version')) is not int or value['version'] != VERSION:
            raise Rejected('unsupported_version')
        if op not in OPERATIONS:
            raise Rejected('unsupported_operation')
        fields = ('version', 'op')
        if op != 'status':
            fields += ('epoch', 'revision')
        keys(value, fields + OPERATIONS[op])
        if op != 'status':
            if not isinstance(value['epoch'], str) or not re.fullmatch('[0-9a-f]{32}', value['epoch']):
                raise ValueError('invalid epoch')
            number(value['revision'], 0, 2**31-1, True)
        if op == 'queue.set':
            number(value['limit_packets'], 1, 4096, True)
        return value
    except ValueError as error:
        raise Rejected('invalid_request') from error


def config(raw):
    if not raw or len(raw) > LIMIT:
        raise ValueError('invalid helper config length')
    value = json.loads(raw, object_pairs_hook=unique)
    keys(value, ('version', 'operators', 'readers'))
    number(value['version'], VERSION, VERSION, True)
    for role in ('operators', 'readers'):
        ids = value[role]
        if not isinstance(ids, list) or len(ids) > 32:
            raise ValueError('invalid helper ACL')
        for uid in ids:
            number(uid, 1, 2**31-1, True)
        if len(set(ids)) != len(ids):
            raise ValueError('duplicate helper ACL entry')
    return value


def root_config(path=CONFIG):
    # Open each component relative to its already checked parent.
    parts = path.split('/')
    if parts[0] or any(part in ('', '.', '..') for part in parts[1:]):
        raise ValueError('invalid absolute configuration path')
    directory = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for name in parts[1:-1]:
            fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                         dir_fd=directory)
            os.close(directory)
            directory = fd
            info = os.fstat(directory)
            if info.st_uid or info.st_mode & 0o022:
                raise ValueError('untrusted configuration directory')
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                     dir_fd=directory)
        with os.fdopen(fd, 'rb') as stream:
            return config_from(stream)
    finally:
        os.close(directory)


def config_from(stream):
    info = os.fstat(stream.fileno())
    if not stat.S_ISREG(info.st_mode) or info.st_uid or info.st_mode & 0o022 or info.st_nlink != 1:
        raise ValueError('untrusted configuration file')
    return config(stream.read(LIMIT + 1))


def audit(event, **fields):
    print(json.dumps(dict(event=event, time_ns=time.time_ns(), **fields)), flush=True)


class Actuator:
    def __init__(self, net, acl):
        self.net = net
        self.epoch = net.tag.decode().removeprefix('flowcore:')
        self.revision = 0
        self.queue = None
        self.last_write = 0.0
        self.operators = {0, *acl['operators']}
        self.readers = self.operators | set(acl['readers'])
        self.failed = False

    def view(self):
        return dict(epoch=self.epoch, revision=self.revision, verified=not self.failed,
                    lab_up=None if self.failed else self.net.indices is not None,
                    limit_packets=self.queue)

    def run(self, uid, command):
        op = command['op']
        if uid not in (self.readers if op == 'status' else self.operators):
            raise Rejected('forbidden')
        if op == 'status':
            return self.view()
        if self.failed:
            raise Rejected('network_failed')
        if (command['epoch'], command['revision']) != (self.epoch, self.revision):
            raise Rejected('stale_revision')
        if self.revision == 2**31-1:
            raise Rejected('revision_exhausted')
        exists = self.net.indices is not None
        if (op == 'lab.create') == exists:
            raise Rejected('invalid_state')
        if op == 'queue.clear' and self.queue is None:
            raise Rejected('invalid_state')
        now = time.monotonic()
        if now - self.last_write < .05:
            raise Rejected('rate_limited')
        self.last_write = now
        target = command.get('limit_packets')
        audit('network_attempt', uid=uid, op=op, limit_packets=target,
              epoch=self.epoch, revision=self.revision)
        try:
            if op == 'lab.create':
                self.net.create()
            elif op == 'lab.remove':
                self.net.remove(self.queue)
                self.queue = None
            else:
                self.net.set_queue(self.queue, target)
                self.queue = target
        except (OSError, RuntimeError, ValueError, KeyError, struct.error) as error:
            self.failed = True
            self.net.indices = self.queue = None
            audit('network_failed', uid=uid, op=op, epoch=self.epoch, revision=self.revision,
                  failure=type(error).__name__, errno=getattr(error, 'errno', None),
                  detail=str(error) if type(error) is RuntimeError else None)
            raise Rejected('network_failed') from error
        self.revision += 1
        audit('network_committed', uid=uid, op=op, **self.view())
        return self.view()


def activation():
    names = os.environ.pop('LISTEN_FDNAMES', '').split(':')
    count = os.environ.pop('LISTEN_FDS', '')
    pid = os.environ.pop('LISTEN_PID', '')
    if (pid != str(os.getpid()) or count != str(len(names)) or len(set(names)) != len(names)
            or set(names) not in ({'control', 'host-netns'}, {'control', 'host-netns', 'test-config'})):
        raise ValueError('expected named activation descriptors')
    fds = dict(zip(names, range(3, 3 + len(names))))
    for fd in fds.values():
        os.set_inheritable(fd, False)
    return fds


def verify_privileges():
    with open('/proc/self/status') as stream:
        fields = dict(line.rstrip().split(':', 1) for line in stream if ':' in line)
    if (os.geteuid() == 0 or int(fields['NoNewPrivs']) != 1 or any(
            int(fields[key], 16) != CAP_NET_ADMIN for key in ('CapEff', 'CapPrm', 'CapBnd'))):
        raise ValueError('helper requires non-root UID, NoNewPrivileges and only CAP_NET_ADMIN')


def namespace_identity(host_fd):
    with os.fdopen(host_fd, 'rb') as witness, open('/proc/self/ns/net', 'rb') as local:
        host, current = (os.fstat(stream.fileno()) for stream in (witness, local))
        if (fcntl.ioctl(witness.fileno(), NS_GET_NSTYPE) != os.CLONE_NEWNET
                or (host.st_dev, host.st_ino) == (current.st_dev, current.st_ino)):
            raise ValueError('private network namespace required')
        return current.st_ino


def receive(conn, peer):
    raw, ancillary, flags, _ = conn.recvmsg(LIMIT, socket.CMSG_SPACE(CREDENTIALS.size),
                                            socket.MSG_CMSG_CLOEXEC)
    sender = None
    extra = False
    for level, kind, data in ancillary:
        if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
            for (fd,) in struct.iter_unpack('=i', data[:len(data)//4*4]):
                os.close(fd)
        if level == socket.SOL_SOCKET and kind == socket.SCM_CREDENTIALS and len(data) == CREDENTIALS.size:
            sender = CREDENTIALS.unpack(data)
        else:
            extra = True
    if flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC):
        raise Rejected('invalid_size')
    if extra or sender != peer:
        raise Rejected('forbidden')
    return request(raw)


def serve_one(listener, actuator, stopping):
    conn, _ = listener.accept()
    with conn:
        conn.settimeout(.25)
        peer = CREDENTIALS.unpack(conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, CREDENTIALS.size))
        try:
            command = receive(conn, peer)
            if stopping():
                raise Rejected('shutting_down')
            result = actuator.run(peer[1], command)
            reply = dict(version=VERSION, ok=True, result=result)
        except Rejected as error:
            reply = dict(version=VERSION, ok=False,
                         code=error.code if peer[1] in actuator.readers else 'forbidden')
        except (TimeoutError, ConnectionError):
            return
        try:
            conn.sendall(json.dumps(reply).encode())
        except OSError:
            # Commit may have happened. Epoch/revision lets the operator reconcile.
            pass


def main():
    try:
        verify_privileges()
        fds = activation()
        namespace = namespace_identity(fds['host-netns'])
        if 'test-config' in fds:
            with os.fdopen(fds['test-config'], 'rb') as stream:
                acl = config_from(stream)
        else:
            acl = root_config()
        with socket.socket(fileno=fds['control']) as listener, \
             socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as owner, \
             socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, socket.NETLINK_ROUTE) as route:
            if (listener.family != socket.AF_UNIX or listener.type != socket.SOCK_SEQPACKET
                    or not listener.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN)
                    or not listener.getsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED)):
                raise ValueError('invalid activation socket')
            owner.bind('\0flowcore-network-helper-v1')
            route.bind((0, 0))
            route.settimeout(.5)
            listener.settimeout(.25)
            net = Network(route, 'flowcore:' + secrets.token_hex(16))
            if set(net.links()) != {b'lo'}:
                raise ValueError('refusing a nonempty private namespace')
            actuator = Actuator(net, acl)
            stopping = False
            def stop(*_):
                nonlocal stopping
                stopping = True
            signal.signal(signal.SIGTERM, stop)
            signal.signal(signal.SIGINT, stop)
            audit('ready', epoch=actuator.epoch, namespace=namespace)
            while not stopping and not actuator.failed:
                try:
                    serve_one(listener, actuator, lambda: stopping)
                except (TimeoutError, ConnectionError):
                    continue
            audit('stopped', epoch=actuator.epoch, failed=actuator.failed)
            # The isolated namespace is released by process lifetime. A failed
            # ownership check must never cause a speculative delete.
            return int(actuator.failed)
    except (OSError, ValueError, RuntimeError, RecursionError, KeyError, struct.error) as error:
        audit('startup_failed', failure=type(error).__name__, errno=getattr(error, 'errno', None))
        return 1


if __name__ == '__main__':
    sys.exit(main())
