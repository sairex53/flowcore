"""FLOWID storage, wire, mTLS and native client integration tests."""
import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import ssl
import struct
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
import flowid_server as protocol

from flowid_lab_support import SERVICE, Reader, client_args, endpoint_file, pki, settings, stop

CLIENT = None
SERVER = ROOT/'scripts/flowid_server.py'


def temporary():
    output = ROOT/'results'
    output.mkdir(exist_ok=True)
    return tempfile.TemporaryDirectory(prefix='flowid-test-', dir=output)


@contextmanager
def child(argv):
    with subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE) as proc:
        try:
            yield Reader(proc)
        finally:
            stop(proc)


class StoreTest(unittest.TestCase):
    def setUp(self):
        temp = temporary()
        self.addCleanup(temp.cleanup)
        self.state = Path(temp.name)
        self.cfg = protocol.config(dict(settings(self.state, 'a'*64, ttl=10), session_limit=2, peer_limit=1))
        self.clock = 100
        self.store = protocol.Store(self.state, self.cfg, clock=lambda: self.clock)
        self.addCleanup(lambda: self.store.close())

    def rejected(self, code, callback, *args):
        with self.assertRaises(protocol.Rejected) as rejected:
            callback(*args)
        self.assertEqual(rejected.exception.code, code)

    def test_durable_order_replay_and_fencing(self):
        ident, token, _, association, _ = self.store.open('a')
        self.rejected(protocol.CONFLICT, self.store.deliver, ident, 'a', association, 2, b'gap')
        self.assertEqual(self.store.deliver(ident, 'a', association, 1, b'msg'), (1, True))
        self.assertEqual(self.store.deliver(ident, 'a', association, 1, b'msg'), (1, False))
        self.rejected(protocol.CONFLICT, self.store.deliver, ident, 'a', association, 1, b'tampered')
        self.store.close()
        self.store = protocol.Store(self.state, self.cfg, clock=lambda: self.clock)
        self.rejected(protocol.AUTH, self.store.resume, token, 'b', 0)
        self.rejected(protocol.CONFLICT, self.store.resume, token, 'a', 2)
        _, committed, newer, _ = self.store.resume(token, 'a', 0)
        self.assertEqual((committed, newer), (1, 2))
        self.rejected(protocol.FENCED, self.store.deliver, ident, 'a', association, 2, b'msg')
        for seq in range(2, protocol.WINDOW+2):
            self.assertEqual(self.store.deliver(ident, 'a', newer, seq, b'msg'), (seq, True))
        self.rejected(protocol.CONFLICT, self.store.deliver, ident, 'a', newer, 1, b'msg')
        self.assertEqual(tuple(self.store.db.execute('SELECT app_messages,app_bytes FROM sessions').fetchone()),
                         (65, 195))
        self.assertEqual(self.store.end(ident, 'a', newer, 65), 65)
        self.rejected(protocol.CLOSED, self.store.resume, token, 'a', 65)

    def test_quotas_expiration_and_service_binding(self):
        _, token, _, _, _ = self.store.open('a')
        self.rejected(protocol.QUOTA, self.store.open, 'a')
        self.store.open('b')
        self.rejected(protocol.QUOTA, self.store.open, 'c')
        self.clock += 10
        self.rejected(protocol.EXPIRED, self.store.resume, token, 'a', 0)
        self.store.open('c')
        with self.assertRaises(ValueError):
            protocol.Store(self.state, dict(self.cfg, service='other.test'))
        with self.assertRaises(ValueError):
            protocol.config(dict(self.cfg, unexpected=True))

    def test_wire_rejects_oversize_version_and_flags(self):
        for header in (struct.pack('!I', protocol.MAX_FRAME+1), struct.pack('!IBBH', 4, 2, 1, 0),
                       struct.pack('!IBBH', 4, 1, 1, 1)):
            writer, reader = socket.socketpair()
            with writer, reader:
                writer.sendall(header)
                self.rejected(protocol.PROTOCOL, protocol.receive, reader, .1)


class LiveTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not CLIENT or not shutil.which('openssl') or os.geteuid() == 0:
            raise unittest.SkipTest('native client, OpenSSL CLI and unprivileged UID required')
        temp = temporary()
        cls.addClassCleanup(temp.cleanup)
        cls.path = Path(temp.name)
        cls.pin = pki(cls.path)

    @contextmanager
    def server(self, *, ttl=120, drop=None, address='127.0.0.1'):
        with tempfile.TemporaryDirectory(dir=self.path) as directory:
            directory = Path(directory)
            config = directory/'server.json'
            config.write_text(json.dumps(settings(self.path, self.pin, address=address, ttl=ttl)))
            args = [sys.executable, '-B', SERVER, config, '--state', directory/'state']
            if drop:
                args += ['--research-drop-ack', str(drop)]
            with child(args) as server:
                ready = server.get()
                self.assertEqual(ready['event'], 'flowid_ready')
                yield ready['port'], directory/'state'/'flowid-v1.sqlite'

    def test_ack_loss_mtls_and_durable_delivery(self):
        with self.server(drop=1) as (port, database):
            endpoint_file(self.path/'endpoints', port)
            with child(client_args(CLIENT, self.path)) as client:
                self.assertEqual(client.get()['event'], 'opened')
                rows = client.command('send once')
                self.assertEqual([row['event'] for row in rows], ['reconnected', 'send'])
                self.assertEqual((rows[0]['committed'], rows[-1]['acknowledged'], rows[-1]['pending']), (1, 1, 0))
                self.assertEqual(client.command('close')[-1]['status'], 'acknowledged')
            with sqlite3.connect(database) as store:
                self.assertEqual(store.execute('SELECT app_messages,app_bytes,closed FROM sessions').fetchall(),
                                 [(1, 4, 1)])
            with child(client_args(CLIENT, self.path, identity='foreign')) as client:
                self.assertEqual(client.get()['status'], 'authentication_failed')
            endpoint_file(self.path/'endpoints', port, service='other.test')
            with child(client_args(CLIENT, self.path, service='other.test')) as client:
                self.assertEqual(client.get()['status'], 'authentication_failed')

    def test_outbox_quota_and_endpoint_generation(self):
        with self.server() as (port, _):
            endpoint_file(self.path/'endpoints', port, generation=2)
            with child(client_args(CLIENT, self.path)) as client:
                self.assertEqual(client.get()['event'], 'opened')
                for i in range(64):
                    self.assertEqual(client.command('queue x')[-1]['pending'], i+1)
                self.assertEqual(client.command('queue x')[-1]['status'], 'quota')
                self.assertEqual(client.command('close')[-1]['status'], 'pending')
                self.assertEqual(client.command('flush')[-1]['acknowledged'], 64)
                endpoint_file(self.path/'endpoints', port, generation=1)
                self.assertEqual(client.command('recover')[-1]['status'], 'invalid_configuration')

    def test_endpoint_expiry_and_delayed_recovery(self):
        with self.server(ttl=2) as (port, _):
            endpoint_file(self.path/'endpoints', port, expires=int(time.time())-1)
            with child(client_args(CLIENT, self.path)) as client:
                self.assertEqual(client.get()['status'], 'unavailable')
            endpoint_file(self.path/'endpoints', port)
            with child(client_args(CLIENT, self.path)) as client:
                self.assertEqual(client.get()['event'], 'opened')
                time.sleep(2.1)
                self.assertEqual(client.command('recover')[-1]['status'], 'expired')

    def test_replacement_without_state_fails_closed(self):
        with self.server() as (port, _):
            endpoint_file(self.path/'endpoints', port)
            with child(client_args(CLIENT, self.path)) as client:
                self.assertEqual(client.get()['event'], 'opened')
                self.assertEqual(client.command('send initial')[-1]['acknowledged'], 1)
                with self.server() as (replacement, database):
                    endpoint_file(self.path/'endpoints', replacement, generation=2)
                    self.assertEqual(client.command('recover')[-1]['status'], 'expired')
                    self.assertEqual(client.command('send unsafe')[-1]['status'], 'expired')
                    with sqlite3.connect(database) as store:
                        self.assertEqual(store.execute('SELECT count(*) FROM sessions').fetchone(), (0,))

    def test_ipv6_and_untrusted_directory(self):
        try:
            with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as probe:
                probe.bind(('::1', 0))
        except OSError:
            self.skipTest('IPv6 loopback unavailable')
        with self.server(address='::1') as (port, _):
            endpoints = self.path/'endpoints'
            endpoint_file(endpoints, port, address='::1')
            with child(client_args(CLIENT, self.path)) as client:
                self.assertEqual(client.get()['event'], 'opened')
                self.assertEqual(client.command('send ipv6')[-1]['acknowledged'], 1)
                self.assertEqual(client.command('close')[-1]['status'], 'acknowledged')
            endpoints.chmod(0o666)
            with child(client_args(CLIENT, self.path)) as client:
                self.assertEqual(client.get()['status'], 'invalid_configuration')

    def test_live_replay_fencing_and_malformed_request(self):
        with self.server() as (port, database):
            context = ssl.create_default_context(cafile=str(self.path/'ca.pem'))
            context.minimum_version = ssl.TLSVersion.TLSv1_3
            context.set_alpn_protocols([protocol.ALPN])
            context.load_cert_chain(str(self.path/'client.pem'), str(self.path/'client.key'))
            def connect():
                return context.wrap_socket(socket.create_connection(('127.0.0.1', port), timeout=2),
                                           server_hostname=SERVICE)
            hello = struct.pack('!HB', protocol.CAPS, len(SERVICE))+SERVICE.encode()
            one = struct.pack('!Q', 1)+b'data'
            with connect() as old, connect() as new:
                protocol.send(old, protocol.OPEN, hello)
                op, body = protocol.receive(old, 2)
                self.assertEqual(op, protocol.OPENED)
                protocol.send(old, protocol.DATA, one)
                self.assertEqual(protocol.receive(old, 2)[0], protocol.ACK)
                protocol.send(new, protocol.RESUME, hello+body[2:34]+struct.pack('!Q', 0))
                self.assertEqual(protocol.receive(new, 2)[0], protocol.RESUMED)
                protocol.send(old, protocol.DATA, one)
                self.assertEqual(protocol.receive(old, 2), (protocol.ERROR, bytes((protocol.FENCED,))))
                protocol.send(new, protocol.DATA, one)
                self.assertEqual(protocol.receive(new, 2), (protocol.ACK, struct.pack('!Q', 1)))
                protocol.send(new, protocol.DATA, one+b'forgery')
                self.assertEqual(protocol.receive(new, 2), (protocol.ERROR, bytes((protocol.CONFLICT,))))
            with connect() as malformed:
                malformed.sendall(struct.pack('!I', protocol.MAX_FRAME+1))
                self.assertEqual(protocol.receive(malformed, 2), (protocol.ERROR, bytes((protocol.PROTOCOL,))))
            with sqlite3.connect(database) as store:
                self.assertEqual(store.execute('SELECT app_messages,app_bytes FROM sessions').fetchall(), [(1, 4)])


if __name__ == '__main__':
    cli = argparse.ArgumentParser()
    cli.add_argument('--client', type=Path)
    cli.add_argument('--server', type=Path, default=SERVER)
    args, remaining = cli.parse_known_args()
    CLIENT = args.client.resolve() if args.client else None
    SERVER = args.server.resolve()
    unittest.main(argv=[sys.argv[0], *remaining])
