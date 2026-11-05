"""Ordinary-user host telemetry, owned TCP_INFO and application reporting."""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BINARY = Path(os.environ.get('FLOWCORE_TEST_BINARY',ROOT/'build/flowcore'))
DAEMON = BINARY.parent/'flowcored'
sys.path.insert(0,str(ROOT/'scripts'))
from tcp_diag import socket_snapshot


def main():
    if os.geteuid()==0:
        raise SystemExit('run this test as an ordinary user')
    (ROOT/'results').mkdir(exist_ok=True)
    output=ROOT/'results'/f'deep-host-{time.time_ns()}'
    output.mkdir()
    with tempfile.TemporaryDirectory(dir=ROOT/'build',prefix='deep-host-') as work:
        runtime=Path(work)/'runtime'
        env=dict(os.environ,FLOWCORE_RUNTIME=str(runtime))
        def rpc(op,**fields):
            with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as peer:
                peer.settimeout(4)
                peer.connect(str(runtime/'control.sock'))
                peer.sendall(json.dumps(dict(op=op,**fields)).encode()+b'\n')
                with peer.makefile('rb') as stream:
                    data=json.loads(stream.readline(1024*1024))
                assert data['ok'],data
                return data['result']
        with (output/'daemon.log').open('w') as log, socket.socket() as server:
            proc=subprocess.Popen([DAEMON],env=env,stdout=log,stderr=log)
            try:
                deadline=time.monotonic()+5
                while not (runtime/'control.sock').exists():
                    assert proc.poll() is None
                    assert time.monotonic()<deadline
                    time.sleep(.02)
                cap=[line for line in Path(f'/proc/{proc.pid}/status').read_text().splitlines()
                     if line.startswith('CapEff:')][0].split()[1]
                assert cap=='0000000000000000'
                server.bind(('127.0.0.1',0))
                server.listen()
                with socket.create_connection(server.getsockname()) as client:
                    accepted,_=server.accept()
                    with accepted:
                        client.sendall(b'x'*4096)
                        assert len(accepted.recv(4096))==4096
                        direct=socket_snapshot(client)
                        deadline=time.monotonic()+6
                        while True:
                            rows=rpc('connections')['connections']
                            found=next((r for r in rows if r['local_port']==client.getsockname()[1]),None)
                            if found: break
                            assert time.monotonic()<deadline,rows
                            time.sleep(.1)
                        assert found['tcp_info']['bytes_sent']==direct['tcp_info']['bytes_sent']
                        assert found['process'] is None
                        assert found['memory']['send_buffer_bytes']>0
                        start=rpc('request_start',service='demo',deadline_ms=20)
                        time.sleep(.002)
                        rpc('request_complete',service='demo',token=start['token'],outcome='ok')
                        rpc('request_sample',service='demo',duration_us=1000,outcome='ok',deadline_ms=20)
                        rpc('request_sample',service='demo',duration_us=1,outcome='failure',deadline_ms=20,
                            failure_reason='connect_refused')
                        view=rpc('observe',interface='lo')
                        assert view['scope']=='current_network_namespace'
                        assert view['host_interfaces'][0]['name']=='lo'
                        assert view['windows']['60']['application']['demo']['application']['failure']==1
                        assert view['windows']['60']['application']['demo']['application']['deadline_miss']==1
                        assert view['windows']['60']['application']['demo']['ipc_boundary']['ok']==1
                        assert view['windows']['60']['rtt']['samples']>0
                        assert 'rtt_us' in view['metric_schema']['tcp']
                        assert not rpc('health')['backend']['active']
                        for name,data in [('observe',view),('owned-socket',direct),('connections',rows),
                                          ('metrics',rpc('metrics')),('events',rpc('events'))]:
                            (output/f'{name}.json').write_text(json.dumps(data,indent=2)+'\n')
                        prom=subprocess.check_output([BINARY,'metrics','--prometheus'],env=env,text=True)
                        assert 'flowcore_tcp_srtt_seconds' in prom
                        assert found['cookie'] not in prom
                        (output/'metrics.prom').write_text(prom)
            finally:
                proc.terminate()
                assert proc.wait(timeout=6)==0
                assert not (runtime/'control.sock').exists()
    print(f'PASS: unprivileged host TCP, direct socket, native queue, application provenance and exports. {output}')


if __name__=='__main__':
    main()
