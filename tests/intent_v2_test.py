"""API v2 contracts and live non-root native/bridge/network validation."""
import array
import json
import os
import random
import signal
import shutil
import socket
import socketserver
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
BUILD=Path(os.environ.get('FLOWCORE_INTENT_BUILD',ROOT/'build'))
sys.path.insert(0,str(ROOT/'scripts'))
sys.path.insert(0,str(ROOT/'examples/python'))
from daemon import Service
from service_config import load
from flowcore_intent import Service as Bridge


def packet(**fields):
    return dict(op='intent_v2',version=2,**fields)


class API(unittest.TestCase):
    def setUp(self):
        self.service=Service(load(ROOT/'config/service.json'))
        self.api=self.service.intent_api
        self.uid,self.pid=os.getuid(),os.getpid()

    def call(self,**fields):
        return self.api.call(packet(**fields),self.uid,self.pid)

    def register(self,name='v2_demo',**options):
        return self.call(action='register',isolation='shared',intent=dict(name=name,priority='standard',**options))['lease']

    def test_identity_constraints_and_lifecycle(self):
        token=self.register()
        with self.assertRaises(PermissionError):
            self.api.call(packet(action='status',lease=token),self.uid,self.pid+1)
        with self.assertRaises(PermissionError):
            self.api.call(packet(action='status',lease=token),self.uid+1,self.pid)
        with self.assertRaises(PermissionError):
            self.call(action='register',isolation='shared',intent=dict(name='forged',priority='critical'))
        with self.assertRaises(ValueError):
            self.register('hard',min_kbit=100)
        now=time.monotonic_ns()
        start=dict(id=1,phase='start',start_ns=now,end_ns=0,deadline_us=1,cookie=None)
        self.assertEqual(self.call(action='batch',lease=token,events=[start])['accepted'],1)
        end=dict(start,phase='cancelled',end_ns=time.monotonic_ns())
        result=self.call(action='batch',lease=token,events=[end,end])
        self.assertEqual((result['accepted'],result['rejected']),(1,1))
        self.assertEqual(self.call(action='status',lease=token)['outcomes']['cancelled'],1)
        view=self.service.observer.window(time.monotonic_ns(),2)['application']['v2_demo']['application']
        self.assertEqual(view['deadline_miss'],1)
        self.api.leases[token]['expires_ns']=0
        with self.assertRaises(PermissionError):
            self.call(action='status',lease=token)
        self.assertNotIn('v2_demo',self.service.controller.registry.rows)

    def test_revocation_and_versions(self):
        token=self.register()
        self.api.revoke('v2_demo')
        with self.assertRaises(PermissionError):
            self.call(action='status',lease=token)
        with self.assertRaises(PermissionError):
            self.register()
        with self.assertRaises(ValueError):
            self.api.call(dict(op='intent_v2',version=3,action='register'),self.uid,self.pid)
        with self.assertRaises(ValueError):
            self.call(action='register',intent={'name':'bad','priority':'standard'},isolation='../path')

    def test_descriptor_association_and_quota(self):
        token=self.register()
        with socket.socket() as tcp:
            response=self.api.call(packet(action='associate',lease=token),self.uid,self.pid,tcp.fileno())
            cookie=int(response['cookie'])
            self.assertEqual(cookie,int.from_bytes(tcp.getsockopt(socket.SOL_SOCKET,57,8),sys.byteorder))
        count=len(list(Path('/proc/self/fd').iterdir()))
        with open('/dev/null','rb') as source:
            for _ in range(32):
                with self.assertRaises(OSError):
                    self.api.call(packet(action='associate',lease=token),self.uid,self.pid,source.fileno())
        self.assertEqual(len(list(Path('/proc/self/fd').iterdir())),count)
        with self.assertRaises(ValueError):
            self.call(action='batch',lease=token,events=[{}]*33)
        for i in range(300):
            stamp=time.monotonic_ns()
            event=dict(id=i+1,phase='start',start_ns=stamp,end_ns=0,deadline_us=0,cookie=None)
            self.call(action='batch',lease=token,events=[event])
        self.assertEqual(len(self.api.leases[token]['requests']),256)
        self.api.leases[token]['process_start']='incorrect'
        self.api.expire()
        self.assertNotIn(token,self.api.leases)


    def test_bounded_parser_fuzz(self):
        token=self.register()
        randomizer=random.Random(497)
        values=[None,True,False,[],{},'../wrong',-1,2**100,0,1]
        for _ in range(1000):
            event={key:randomizer.choice(values) for key in
                   ('id','phase','start_ns','end_ns','deadline_us','cookie')}
            result=self.call(action='batch',lease=token,events=[event])
            self.assertEqual(result['accepted'],0)
        self.assertLessEqual(len(self.api.leases[token]['requests']),256)


class Echo(socketserver.BaseRequestHandler):
    def handle(self):
        def read(size):
            output=b''
            while len(output)<size:
                part=self.request.recv(size-len(output))
                if not part:
                    return None
                output+=part
            return output
        self.request.settimeout(3)
        while True:
            prefix=read(4)
            if prefix is None:
                break
            size=struct.unpack('!I',prefix)[0]
            if size>65536 or read(size) is None:
                break
            time.sleep(.001)
            self.request.sendall(b'R'*64)



class Multiplex(socketserver.BaseRequestHandler):
    def handle(self):
        identifiers=[]
        self.request.settimeout(3)
        self.request.setsockopt(socket.IPPROTO_TCP,socket.TCP_NODELAY,1)
        def read(size):
            data=b''
            while len(data)<size:
                part=self.request.recv(size-len(data))
                if not part: raise EOFError('short multiplex request')
                data+=part
            return data
        for _ in range(8):
            identifier,size=struct.unpack('!II',read(8))
            if size>1024: raise ValueError('oversize demo payload')
            read(size)
            identifiers.append(identifier)
        for identifier in reversed(identifiers):
            time.sleep(.002)
            self.request.sendall(struct.pack('!I',identifier))


def live():
    with tempfile.TemporaryDirectory(dir=BUILD,prefix='v2-live-') as directory:
        directory=Path(directory)
        cfg=load(ROOT/'config/service.json')
        cfg['intent_api']['priorities']=['standard','interactive','critical']
        config=directory/'config.json'
        config.write_text(json.dumps(cfg))
        runtime=directory/'run'
        env=dict(os.environ,FLOWCORE_RUNTIME=str(runtime),FLOWCORE_STATE=str(directory/'state'))
        results=[]
        with (directory/'daemon.log').open('w') as log:
            proc=subprocess.Popen([BUILD/'flowcored','--config',config],env=env,stdout=log,stderr=log)
            try:
                path=runtime/'control.sock'
                for _ in range(100):
                    if path.exists(): break
                    if proc.poll() is not None: raise RuntimeError(log.name)
                    time.sleep(.05)
                with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as handed:
                    handed.settimeout(2)
                    handed.connect(str(path))
                    child=os.fork()
                    if child==0:
                        handed.sendall(b'{"op":"status"}\n')
                        os._exit(0)
                    os.waitpid(child,0)
                    assert json.loads(handed.recv(4096))['code']=='forbidden'
                # Unexpected SCM_RIGHTS must be rejected and every received FD closed.
                baseline=len(list(Path(f'/proc/{proc.pid}/fd').iterdir()))
                with open('/dev/null','rb') as source:
                    for count in (1,3)*8:
                        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as probe:
                            probe.settimeout(2)
                            probe.connect(str(path))
                            probe.sendmsg([b'{"op":"status"}\n'],
                                [(socket.SOL_SOCKET,socket.SCM_RIGHTS,array.array('i',[source.fileno()]*count))])
                            assert not json.loads(probe.recv(4096))['ok']
                time.sleep(.05)
                assert len(list(Path(f'/proc/{proc.pid}/fd').iterdir()))<=baseline
                session_output=subprocess.check_output([BUILD/'intent_session_native',path],text=True,timeout=12)
                assert json.loads(session_output)['connections']==1
                output=subprocess.check_output([BUILD/'intent_v2_native',path],text=True,timeout=8)
                assert int(output.split()[0])>0
                subprocess.run([BUILD/'flowcore-intent-c-demo',path],check=True,timeout=5)
                go=BUILD/'flowcore-intent-go-demo'
                if go.exists():
                    assert 'accepted=' in subprocess.check_output([go,path],text=True,timeout=5)
                with Bridge(BUILD/'libflowcore_intent.so',path,'python_demo') as client:
                    assert client.ready()
                    with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as probe:
                        probe.settimeout(2)
                        probe.connect(str(path))
                        probe.sendall(b'{"op":"intent_inspect","name":"python_demo"}\n')
                        assert json.loads(probe.recv(8192))['ok']
                    with client.request():
                        time.sleep(.001)
                    assert client.flush()
                # Legacy daemon interoperability: no sidecar means explicit stream fallback.
                endpoint=runtime/'intent.sock'
                hidden=runtime/'intent.compat-hidden'
                endpoint.rename(hidden)
                try:
                    with Bridge(BUILD/'libflowcore_intent.so',path,'legacy_compat') as client:
                        assert client.ready()
                        with client.request(): pass
                        assert client.flush()
                        stats=client.transport_statistics()
                        assert stats['session_connections']==0 and stats['legacy_requests']>=2,stats
                finally:
                    hidden.rename(endpoint)
                with socketserver.ThreadingTCPServer(('127.0.0.1',0),Multiplex) as multiplex_server:
                    worker=threading.Thread(target=multiplex_server.serve_forever,daemon=True)
                    worker.start()
                    try:
                        mux=json.loads(subprocess.check_output([sys.executable,'-B',
                            ROOT/'examples/python/multiplex_demo.py',BUILD/'libflowcore_intent.so',path,
                            str(multiplex_server.server_address[1])],text=True,timeout=10))
                        assert (mux['offered'],mux['completed'],mux['failed'],mux['tcp_connections'])==(8,8,0,1)
                        assert [row['wire_id'] for row in mux['results']]==list(reversed(range(8)))
                    finally:
                        multiplex_server.shutdown()
                        worker.join()
                with socketserver.ThreadingTCPServer(('127.0.0.1',0),Echo) as server:
                    thread=threading.Thread(target=server.serve_forever,daemon=True)
                    thread.start()
                    # Real application I/O still succeeds with an unavailable intent daemon.
                    with Bridge(BUILD/'libflowcore_intent.so',runtime/'absent.sock','offline_io') as offline:
                        with socket.create_connection(('127.0.0.1',server.server_address[1]),timeout=2) as network:
                            with offline.request():
                                network.sendall(struct.pack('!I',7)+b'offline')
                                reply=b''
                                while len(reply)<64:
                                    part=network.recv(64-len(reply))
                                    assert part
                                    reply+=part
                        assert offline.statistics()['accepted']==0 and offline.statistics()['dropped']>0
                    workers=[subprocess.Popen([BUILD/'flowcore-intent-v2-demo',path,
                        str(server.server_address[1]),name,str(size)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
                        for name,size in (('payment',4096),('database',16384),('background',65536))]
                    for child in workers:
                        out,err=child.communicate(timeout=12)
                        assert child.returncode==0,(out,err)
                        results.append(json.loads(out))
                    server.shutdown()
                    thread.join()
                # Keep the same native client alive through daemon termination and restart.
                with Bridge(BUILD/'libflowcore_intent.so',path,'recovery') as client:
                    assert client.ready()
                    with client.request(): pass
                    assert client.flush()
                    before=client.statistics()['accepted']
                    first_generation=client.generation()
                    # Ambiguous timeout must preserve admission rather than duplicate a live lease.
                    os.kill(proc.pid,signal.SIGSTOP)
                    try:
                        with client.request(): pass
                        client.flush()
                    finally:
                        os.kill(proc.pid,signal.SIGCONT)
                    for _ in range(30):
                        with client.request(): pass
                        client.flush()
                        if client.statistics()['accepted']>before: break
                        time.sleep(.05)
                    with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as probe:
                        probe.settimeout(2)
                        probe.connect(str(path))
                        probe.sendall(b'{"op":"intent_inspect","name":"recovery"}\n')
                        reply=b''
                        while not reply.endswith(b'\n'):
                            reply+=probe.recv(32768)
                        view=json.loads(reply)
                        assert view.get('ok'),view
                        assert len(view['result']['instances'])==1
                    assert client.generation()==first_generation
                    before=client.statistics()['accepted']
                    proc.terminate()
                    proc.wait(timeout=5)
                    with client.request(): pass
                    client.flush()
                    proc=subprocess.Popen([BUILD/'flowcored','--config',config],env=env,stdout=log,stderr=log)
                    for _ in range(100):
                        with client.request(): pass
                        client.flush()
                        if client.statistics()['accepted']>before: break
                        time.sleep(.05)
                    assert client.statistics()['accepted']>before,client.statistics()
                    assert client.generation()>first_generation
                    with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as udp:
                        cookie=client.associate_socket(udp.fileno())
                        with client.request(cookie=cookie): pass
                        assert client.flush()
                        assert client.dissociate_socket(cookie)==0
                # A SIGKILL must reclaim the process lease without graceful SDK close.
                code="import sys,time;sys.path.insert(0,sys.argv[1]);from flowcore_intent import Service;s=Service(sys.argv[2],sys.argv[3],'crashed');assert s.ready();print('ready',flush=True);time.sleep(60)"
                crashed=subprocess.Popen([sys.executable,'-c',code,str(ROOT/'examples/python'),
                    str(BUILD/'libflowcore_intent.so'),str(path)],stdout=subprocess.PIPE,text=True)
                try:
                    assert crashed.stdout.readline().strip()=='ready'
                finally:
                    crashed.kill()
                    crashed.wait(timeout=5)
                    crashed.stdout.close()
                time.sleep(.3)
                with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as probe:
                    probe.settimeout(2)
                    probe.connect(str(path))
                    probe.sendall(b'{"op":"intent_list"}\n')
                    reply=b''
                    while not reply.endswith(b'\n'):
                        reply+=probe.recv(32768)
                    assert b'crashed' not in reply,reply
                print(json.dumps(dict(native_concurrency=output.strip(),persistent_session=json.loads(session_output),legacy_transport=stats,offline_tcp_success=True,multiplex=mux,network=results)))
            finally:
                proc.terminate()
                proc.wait(timeout=5)
                log.flush()
                shutil.copyfile(log.name,ROOT/'results/intent-session-daemon.log')


if __name__=='__main__':
    if '--live' in sys.argv:
        live()
    else:
        unittest.main()
