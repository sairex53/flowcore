"""Exercise v2 clients against an older v2 feature reply and a bad version range."""
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path


def run_server(binary, mode):
    with tempfile.TemporaryDirectory(prefix='intent-compat-') as directory:
        endpoint=Path(directory)/'intent.sock'
        control=Path(directory)/'control.sock'  # SDK derives the seqpacket path from this name.
        server=socket.socket(socket.AF_UNIX,socket.SOCK_SEQPACKET)
        server.bind(str(endpoint))
        server.listen(4)
        server.settimeout(3)
        failures=[]

        def serve():
            try:
                connection,_=server.accept()
                with connection:
                    connection.settimeout(3)
                    while True:
                        packet=connection.recv(65537)
                        if not packet:
                            return
                        request=json.loads(packet.rstrip(b'\n'))
                        if request.get('action')=='register':
                            result={'lease':'0123456789abcdef0123456789abcdef','renewable':False}
                            if mode=='mismatch':
                                result.update(protocol_min=3,protocol_max=3)
                            # Legacy v2 shape intentionally omits the optional range and feature list.
                            reply={'ok':True,'version':2,'result':result}
                        elif request.get('action')=='batch':
                            count=len(request.get('events',[]))
                            reply={'ok':True,'version':2,'result':{
                                'accepted':count,'rejected':0,'slo_risk':False,'congestion':False,
                                'policy_fallback':False,'interface_degradation':False,
                                'insufficient_capacity':False,'network_observed':False}}
                        else:
                            reply={'ok':False,'version':2,'code':'unsupported'}
                        connection.sendall(json.dumps(reply).encode()+b'\n')
            except BaseException as error:
                failures.append(error)

        worker=threading.Thread(target=serve,daemon=True)
        worker.start()
        try:
            env=dict(os.environ)
            result=subprocess.run([binary,str(control),mode],env=env,text=True,
                                  capture_output=True,timeout=6)
            if result.returncode:
                raise AssertionError(f'{mode} compatibility probe failed: {result.stdout} {result.stderr} rc={result.returncode}')
        finally:
            server.close()
            worker.join(timeout=1)
        if failures:
            raise failures[0]


if __name__=='__main__':
    if len(sys.argv)!=2:
        raise SystemExit('usage: intent_protocol_compat.py <probe-binary>')
    run_server(sys.argv[1],'legacy-v2')
    run_server(sys.argv[1],'mismatch')
    print('PASS: older v2 feature reply and incompatible protocol range')
