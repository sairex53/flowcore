"""Real kernel seqpacket framing/credentials/FD tests without root or networking changes."""
import array
import ctypes
import json
import os
import select
import socket
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from daemon import Service
from intent_session import Sessions
from intent_v2 import identity
from service_config import load


class Protocol(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory(dir=ROOT/'build',prefix='session-')
        self.service=Service(load(ROOT/'config/service.json'))
        self.server=Sessions(self.service,Path(os.path.relpath(self.directory.name))/'intent.sock')
        self.sockets=[]

    def tearDown(self):
        for sock in self.sockets:
            sock.close()
        self.server.__exit__()
        self.directory.cleanup()

    def connect(self):
        sock=socket.socket(socket.AF_UNIX,socket.SOCK_SEQPACKET)
        sock.settimeout(2)
        sock.connect(str(self.server.path))
        self.sockets.append(sock)
        self.server.poll([self.server.server])
        return sock

    def receive(self,sock):
        for _ in range(100):
            ready=select.select(self.server.inputs(),[],[],.01)[0]
            self.server.poll(ready)
            if select.select([sock],[],[],0)[0]:
                reply=sock.recv(32768)
                return json.loads(reply) if reply else None
        self.fail('session did not respond')

    def call(self,sock,**fields):
        sock.send(json.dumps(dict(op='intent_v2',version=2,**fields)).encode())
        return self.receive(sock)

    def test_non_utf8_process_name(self):
        libc=ctypes.CDLL(None,use_errno=True)
        name=ctypes.create_string_buffer(16)
        self.assertEqual(libc.prctl(16,ctypes.byref(name),0,0,0),0)
        before=identity(os.getpid())[0]
        try:
            self.assertEqual(libc.prctl(15,ctypes.c_char_p(b'bad\xff)name'),0,0,0),0)
            self.assertEqual(identity(os.getpid())[0],before)
            sock=self.connect()
            response=self.call(sock,action='register',isolation='shared',
                               intent=dict(name='binary_name',priority='standard'))
            self.assertTrue(response['ok'])
        finally:
            libc.prctl(15,ctypes.byref(name),0,0,0)

    def test_recycled_socket_quota_and_abandonment(self):
        api=self.service.intent_api
        packet=dict(op='intent_v2',version=2)
        token=api.call(dict(packet,action='register',isolation='shared',
                           intent=dict(name='churn',priority='standard')),os.getuid(),os.getpid())['lease']
        lease=api.leases[token]
        for identifier in range(80):
            with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as udp:
                response=api.call(dict(packet,action='associate',lease=token),os.getuid(),os.getpid(),udp.fileno())
                cookie=int(response['cookie'])
                if identifier==0:
                    api.call(dict(packet,action='batch',lease=token,events=[dict(id=1,phase='start',
                        start_ns=time.monotonic_ns(),end_ns=0,deadline_us=0,cookie=cookie)]),os.getuid(),os.getpid())
                detached=api.call(dict(packet,action='dissociate',lease=token,cookie=cookie),os.getuid(),os.getpid())
                self.assertEqual(detached['abandoned'],1 if identifier==0 else 0)
            self.assertFalse(lease['cookies'])
        self.assertEqual(lease['abandoned_requests'],1)
        self.assertFalse(lease['requests'])

    def test_unknown_clock_and_revocation_audit(self):
        api=self.service.intent_api
        with patch('intent_v2.os.stat',side_effect=[SimpleNamespace(st_ino=1),SimpleNamespace(st_ino=2)]):
            with self.assertRaises(PermissionError):
                api.call(dict(op='intent_v2',version=2,action='register',isolation='shared',
                    intent=dict(name='wrong_clock',priority='standard')),os.getuid(),os.getpid())
        api.revoke('future_service',os.getuid(),os.getpid())
        self.assertEqual(api.audit[-1]['event'],'revocation')
        self.assertEqual(api.audit[-1]['pid'],os.getpid())

    def test_reuse_renew_independent_expiry(self):
        sock=self.connect()
        first=self.call(sock,action='register',isolation='shared',
                        intent=dict(name='renewable',priority='standard',ttl_seconds=1))
        self.assertTrue(first['ok'])
        token=first['result']['lease']
        self.assertTrue(first['result']['renewable'])
        now=time.monotonic_ns()
        event=dict(id=1,phase='start',start_ns=now,end_ns=0,deadline_us=0,cookie=None)
        self.assertEqual(self.call(sock,action='batch',lease=token,events=[event])['result']['accepted'],1)
        previous=self.service.intent_api.leases[token]['expires_ns']
        self.assertTrue(self.call(sock,action='renew',lease=token)['ok'])
        self.assertGreater(self.service.intent_api.leases[token]['expires_ns'],previous)
        self.assertEqual(len(self.service.intent_api.leases[token]['requests']),1)
        second=self.call(sock,action='register',isolation='shared',
                         intent=dict(name='renewable',priority='standard',ttl_seconds=1))['result']['lease']
        row=self.service.controller.registry.rows['renewable']
        self.assertGreaterEqual(row['monotonic_expiry'],self.service.intent_api.leases[second]['expires_ns']/1e9)
        self.service.intent_api.leases[token]['expires_ns']=0
        self.service.intent_api.expire()
        self.assertTrue(self.call(sock,action='status',lease=second)['ok'])
        self.assertEqual(self.server.serial,1)
        self.assertEqual(len(self.server.clients),1)
        self.assertTrue(self.call(sock,action='close',lease=second)['ok'])

    def test_framing_descriptors_and_identity(self):
        sock=self.connect()
        sock.send(b'{"op":"status"}')
        self.assertEqual(self.receive(sock)['code'],'forbidden')
        sock.send(b'['*100)
        self.assertEqual(self.receive(sock)['code'],'invalid_request')
        # A delegated connected socket cannot inherit the parent's identity.
        child=os.fork()
        if child==0:
            try: sock.send(b'{"op":"intent_v2","version":2,"action":"status","lease":"wrong"}')
            finally: os._exit(0)
        os.waitpid(child,0)
        response=self.receive(sock)
        self.assertEqual(response['code'],'forbidden')
        self.assertFalse(response['retry_registration'])
        self.assertIsNone(self.receive(sock))
        # Truncation must be explicit and close the session.
        large=self.connect()
        large.send(b'x'*70000)
        self.assertEqual(self.receive(large)['code'],'invalid_request')
        self.assertIsNone(self.receive(large))
        baseline=len(list(Path('/proc/self/fd').iterdir()))
        with open('/dev/null','rb') as source:
            for payload,count in ((b'',1),(b'{"op":"status"}',9)):
                peer=self.connect()
                peer.sendmsg([payload],[(socket.SOL_SOCKET,socket.SCM_RIGHTS,array.array('i',[source.fileno()]*count))])
                self.receive(peer)
                peer.close()
        self.assertEqual(len(list(Path('/proc/self/fd').iterdir())),baseline)

    def test_connection_bounds_idle_and_round_robin(self):
        peers=[self.connect() for _ in range(17)]
        self.assertEqual(len(self.server.clients),16)
        self.assertIsNone(self.receive(peers[-1]))
        for peer in peers[:16]: peer.send(b'{"op":"status"}')
        ready=select.select(self.server.inputs(),[],[],.1)[0]
        self.server.poll(ready)
        # Eight frames per poll; serving moves a client to the end.
        self.server.poll(select.select(self.server.inputs(),[],[],.1)[0])
        for peer in peers[:16]:
            self.assertEqual(json.loads(peer.recv(4096))['code'],'forbidden')
        for state in self.server.clients.values():
            state['last']=time.monotonic()-31
        self.server.poll([])
        self.assertFalse(self.server.clients)

    def test_udp_and_all_request_outcomes(self):
        sock=self.connect()
        token=self.call(sock,action='register',isolation='shared',
                        intent=dict(name='outcomes',priority='standard'))['result']['lease']
        with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as udp:
            sock.sendmsg([json.dumps(dict(op='intent_v2',version=2,action='associate',lease=token)).encode()],
                [(socket.SOL_SOCKET,socket.SCM_RIGHTS,array.array('i',[udp.fileno()]))])
            response=self.receive(sock)
            self.assertEqual(response['result']['protocol'],17)
            cookie=int(response['result']['cookie'])
            for identifier,outcome in enumerate(('ok','timeout','failure','cancelled'),1):
                stamp=time.monotonic_ns()
                event=dict(id=identifier,phase='start',start_ns=stamp,end_ns=0,deadline_us=1,cookie=cookie)
                self.assertEqual(self.call(sock,action='batch',lease=token,events=[event])['result']['accepted'],1)
                event.update(phase=outcome,end_ns=time.monotonic_ns())
                result=self.call(sock,action='batch',lease=token,events=[event])['result']
                self.assertEqual(result['accepted'],1)
                self.assertTrue(result['notifications']['slo_risk'])
                self.assertFalse(result['network_observed'])
                self.assertGreater(result['observed_ns'],0)
        status=self.call(sock,action='status',lease=token)['result']
        self.assertEqual(status['inflight'],0)
        self.assertEqual(status['outcomes'],dict(ok=1,timeout=1,failure=1,cancelled=1))
        self.assertTrue(self.call(sock,action='close',lease=token)['ok'])


if __name__=='__main__':
    unittest.main()
