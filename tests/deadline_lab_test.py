"""Opt-in privileged smoke test using only a disposable FLOWCORE veth pair."""
import os
from pathlib import Path
import socket
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))


def run():
    if os.geteuid() != 0:
        raise SystemExit('Run this opt-in namespace test with sudo.')
    with tempfile.TemporaryDirectory(dir=ROOT/'results', prefix='deadline-lab-') as temp:
        os.environ['FLOWCORE_RUNTIME'] = temp
        os.environ['FLOWCORE_STATE'] = temp
        from daemon import Service
        from service_config import load
        service = Service(load(ROOT/'config/service.json'))
        try:
            service.replace('default', service.profile)
            with service.lab.enter(1):
                listener = socket.socket()
            with service.lab.enter(0):
                sender = socket.socket()
            with listener, sender:
                listener.settimeout(2)
                sender.settimeout(2)
                port = service.config['policies']['default']['classes']['standard']['port']
                listener.bind(('10.203.0.2', port))
                listener.listen(1)
                sender.connect(('10.203.0.2', port))
                accepted, _ = listener.accept()
                with accepted:
                    accepted.settimeout(2)
                    accepted.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    sender.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    def call(action, fd=None, **fields):
                        return service.intent_api.call(dict(op='intent_v2',version=2,action=action,**fields),
                                                       os.getuid(),os.getpid(),fd)

                    lease = call('register',isolation='shared',intent=dict(name='deadline_lab',priority='standard'))['lease']
                    cookie = int(call('associate',sender.fileno(),lease=lease)['cookie'])
                    data = b'x'*1024
                    for _ in range(6):
                        sender.sendall(data)
                        received = bytearray()
                        while len(received) < len(data):
                            chunk = accepted.recv(len(data)-len(received))
                            if not chunk:
                                raise EOFError('short owned veth transfer')
                            received.extend(chunk)
                        assert received == data
                        accepted.sendall(b'R')
                        assert sender.recv(1) == b'R'
                        service.poll_metrics()
                        time.sleep(.18)
                    work = dict(deadline_ns=time.monotonic_ns()+1_000_000_000,egress_bytes=128,
                                processing_us=0,connect_us=0,remote_us=None,reliability=0,expiration=0,cookie=cookie)
                    result = call('admission',lease=lease,work=work)
                    assert result['decision'] == 'ACCEPT_WITH_RISK', result
                    assert result['network_p95_us'] < 1_000_000, result
                    assert call('admission',lease=lease,work=dict(work,cookie=None))['decision'] == 'INSUFFICIENT_DATA'
                    print('Owned class forecast and missing-association fallback verified.')
        finally:
            service.close_lab()


if __name__ == '__main__':
    run()
