"""Privileged local-only actuator for one explicitly owned research egress."""
import argparse
from contextlib import contextmanager
import os
from pathlib import Path
import signal
import socket
import struct
import subprocess
import sys
import time

from durable_state import read_json
from fabric_coordinator import MAX_GENERATION, MAX_RATE, LEASE_MS
from fabric_protocol import Rejected, audit, exchange, fields, hexdigest, ident, integer, response

PEER = struct.Struct('=3i')
ROOT_HANDLE = 'fc01:'
CLASS_HANDLE = 'fc01:20'


def uid(sock):
    return PEER.unpack(sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, PEER.size))[1]


def local(path, message):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(2)
        sock.connect(str(path))
        if uid(sock) != 0:
            raise Rejected('actuator_identity')
        return exchange(sock, message)


def desired(value):
    fields(value, ('cluster', 'host', 'epoch', 'generation', 'resource', 'interface',
                   'rate_kbit', 'lease_ms'))
    for key in ('cluster', 'host', 'resource'):
        ident(value[key])
    ident(value['interface'], 15)
    hexdigest(value['epoch'], 32)
    integer(value['generation'], 1, MAX_GENERATION)
    integer(value['rate_kbit'], 100, MAX_RATE)
    integer(value['lease_ms'], 250, LEASE_MS)
    return value


def config(value):
    fields(value, ('cluster', 'host', 'resource', 'interface', 'owner', 'floor_kbit',
                   'max_kbit', 'agent_uid', 'agent_gid'))
    for key in ('cluster', 'host', 'resource', 'owner'):
        ident(value[key])
    ident(value['interface'], 15)
    floor = integer(value['floor_kbit'], 100, MAX_RATE)
    integer(value['max_kbit'], floor, MAX_RATE)
    integer(value['agent_uid'], 1, 2**31-1)
    integer(value['agent_gid'], 1, 2**31-1)
    return value


class Actuator:
    def __init__(self, cfg, queue, clock=time.monotonic):
        self.cfg, self.queue, self.clock = cfg, queue, clock
        self.floor = self.rate = cfg['floor_kbit']
        self.epoch, self.generation, self.target = None, 0, self.floor
        self.expires = None
        self.state = 'fallback'
        self.faulted = False

    def initialize(self):
        self.queue.install(ROOT_HANDLE, 'htb', 'default', '20')
        self.write('add', self.floor)
        self.queue.tc('qdisc', 'add', 'dev', self.cfg['interface'], 'parent', CLASS_HANDLE,
                      'handle', 'fc20:', 'fq_codel')
        if self.read() != self.floor:
            raise RuntimeError('baseline readback mismatch')

    def write(self, operation, rate):
        self.queue.verify()
        self.queue.tc('class', operation, 'dev', self.cfg['interface'], 'parent', ROOT_HANDLE,
                      'classid', CLASS_HANDLE, 'htb', 'rate', f'{rate}kbit',
                      'ceil', f'{rate}kbit', 'quantum', '1514')

    def read(self):
        self.queue.assert_owned_sender()
        leaves = self.queue.rows('qdisc')
        if not any(row.get('root') and row.get('kind') == 'htb' and row.get('handle') == ROOT_HANDLE
                   for row in leaves):
            raise RuntimeError('owned HTB root missing')
        rows = self.queue.rows('class')
        classes = [row for row in rows if row.get('class') == 'htb']
        if len(classes) != 1 or classes[0].get('handle') != CLASS_HANDLE:
            raise RuntimeError('unexpected HTB class topology')
        if (len(leaves) != 2 or not any(row.get('handle') == 'fc20:' and row.get('kind') == 'fq_codel'
                                        and row.get('parent') == CLASS_HANDLE for row in leaves)):
            raise RuntimeError('unexpected HTB leaf topology')
        rate = classes[0].get('rate')
        if type(rate) is not int or rate % 125 or rate != classes[0].get('ceil'):
            raise RuntimeError('invalid class rate or ceil readback')
        return rate // 125

    def apply_rate(self, target):
        before = self.read()
        if before != self.rate:
            raise RuntimeError('external class drift')
        if target == before:
            return
        try:
            self.write('change', target)
            if self.read() != target:
                raise RuntimeError('class readback mismatch')
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            # A failed command may have mutated the class. Restore the previous
            # cap only when readback attributes the state to this transaction.
            observed = self.read()
            if observed == target:
                self.write('change', before)
            if self.read() != before:
                raise RuntimeError('cap compensation unverified') from error
            raise
        self.rate = target

    def fallback(self):
        self.expires = None
        try:
            changed = self.rate != self.floor or self.state != 'fallback'
            self.apply_rate(self.floor)
            self.state = 'fallback'
            if changed:
                audit('local_fallback', host=self.cfg['host'], rate_kbit=self.floor)
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            self.state, self.faulted = 'error', True
            audit('actuator_error', host=self.cfg['host'], error=type(error).__name__)

    def expire(self):
        if self.expires is not None and self.clock() >= self.expires:
            self.fallback()

    def status(self):
        try:
            verified = self.read() == self.rate and not self.faulted
        except (OSError, RuntimeError, subprocess.SubprocessError):
            verified = False
        return dict(epoch=self.epoch, generation=self.generation, resource=self.cfg['resource'],
                    rate_kbit=self.rate, state=self.state if verified else 'error', verified=verified)

    def apply(self, value):
        row = desired(value)
        if self.faulted:
            raise Rejected('actuator_faulted')
        if (any(row[key] != self.cfg[key] for key in ('cluster', 'host', 'resource', 'interface')) or
                not self.floor <= row['rate_kbit'] <= self.cfg['max_kbit']):
            raise Rejected('local_scope')
        if self.epoch not in (None, row['epoch']) or row['generation'] < self.generation:
            raise Rejected('stale_generation')
        if row['generation'] == self.generation and row['rate_kbit'] != self.target:
            raise Rejected('generation_conflict')
        try:
            changed = self.rate != row['rate_kbit'] or self.generation != row['generation'] or self.state != 'applied'
            self.apply_rate(row['rate_kbit'])
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            self.faulted = True
            self.fallback()
            raise Rejected('kernel_unverified') from error
        self.epoch, self.generation, self.target = row['epoch'], row['generation'], row['rate_kbit']
        self.state = 'applied'
        self.expires = self.clock() + row['lease_ms']/1000
        if changed:
            audit('local_verified', host=self.cfg['host'], generation=self.generation, rate_kbit=self.rate)
        return dict(epoch=self.epoch, generation=self.generation, resource=self.cfg['resource'],
                    rate_kbit=self.rate, state=self.state, verified=True)

    def dispatch(self, peer, request):
        if peer != self.cfg['agent_uid']:
            raise Rejected('local_peer')
        self.expire()
        if request['op'] == 'status':
            fields(request, ('op', 'version'))
            return self.status()
        if request['op'] == 'apply':
            fields(request, ('op', 'version', 'desired'))
            return self.apply(request['desired'])
        raise Rejected('local_operation')


@contextmanager
def socket_parent(path):
    if not path.is_absolute():
        raise ValueError('actuator socket must be an absolute path')
    ident(path.name)
    directory = os.open(path.parent, os.O_DIRECTORY | os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        metadata = os.fstat(directory)
        if metadata.st_uid != 0 or metadata.st_mode & 0o022:
            raise PermissionError('actuator socket parent must be an owned root directory')
        yield directory
    finally:
        os.close(directory)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('config', type=Path)
    parser.add_argument('--socket', type=Path, required=True)
    args = parser.parse_args()
    stop = False

    def stopping(*_):
        nonlocal stop
        stop = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, stopping)
    try:
        cfg = config(read_json(args.config))
        with socket_parent(args.socket) as directory, socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            # Pin the checked directory across bind, permission updates and cleanup.
            # An existing pathname is never unlinked at startup.
            name = args.socket.name
            listener.bind(f'/proc/self/fd/{directory}/{name}')
            try:
                os.chown(name, 0, cfg['agent_gid'], dir_fd=directory, follow_symlinks=False)
                os.chmod(name, 0o660, dir_fd=directory)
                from multi_host_agent import OwnedQueue
                queue = OwnedQueue(cfg['interface'], cfg['owner'])
                actuator = Actuator(cfg, queue)
                try:
                    actuator.initialize()
                except BaseException:
                    queue.close()
                    raise
                try:
                    listener.listen(8)
                    listener.settimeout(.15)
                    audit('actuator_ready', host=cfg['host'], rate_kbit=actuator.floor)
                    while not stop:
                        actuator.expire()
                        try:
                            connection, _ = listener.accept()
                        except TimeoutError:
                            continue
                        try:
                            with connection:
                                peer = uid(connection)
                                if peer == cfg['agent_uid']:
                                    response(connection, actuator.dispatch, peer)
                        except (OSError, EOFError):
                            pass
                finally:
                    # Keep the protected cap installed until the operator
                    # destroys the dedicated namespace. Removing it could
                    # exceed the cluster budget while other leases are live.
                    actuator.fallback()
                    queue.lease.close()
            finally:
                os.unlink(name, dir_fd=directory)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError) as error:
        print(f'fabric actuator: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
