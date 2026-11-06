"""Installed-runtime recovery tests; no host package/service/network mutations."""
import argparse
import concurrent.futures
import json
import os
import signal
import socket
import subprocess
import tempfile
import time
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]

def check(prefix,configuration):
    prefix=Path(prefix);configuration=Path(configuration)
    with tempfile.TemporaryDirectory(dir=ROOT/'build',prefix='recovery-') as temp:
        work=Path(temp);runtime=work/'runtime';state=work/'state'
        config=work/'service.json';baseline=json.loads(configuration.read_text())
        config.write_text(json.dumps(baseline))
        env=dict(os.environ,FLOWCORE_RUNTIME=str(runtime),FLOWCORE_STATE=str(state))
        daemon=prefix/'bin/flowcored';exe=prefix/'bin/flowcore'
        def query(op):
            with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as peer:
                peer.settimeout(3);peer.connect(str(runtime/'control.sock'))
                peer.sendall(json.dumps(dict(op=op)).encode()+b'\n')
                with peer.makefile('rb') as stream:reply=json.loads(stream.readline(1048577))
                assert reply['ok'],reply
                return reply['result']
        with (work/'daemon.log').open('w') as log:
            def start():
                proc=subprocess.Popen([daemon,'--monitor-only','--config',config],env=env,stdout=log,stderr=log)
                deadline=time.monotonic()+6
                while True:
                    if proc.poll() is not None:raise RuntimeError((work/'daemon.log').read_text())
                    try:
                        query('status')
                        return proc
                    except (OSError,ValueError):
                        if time.monotonic()>deadline:raise RuntimeError('startup deadline')
                        time.sleep(.02)
            assert subprocess.run([daemon,'--check-config','--config',config],env=env,capture_output=True).returncode==0
            proc=start()
            try:
                assert query('ops_status')['checkpoint']['clean_shutdown'] is False
                with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                    assert all(row['healthy'] for row in pool.map(lambda _:query('status'),range(24)))
                candidate=dict(baseline,telemetry_seconds=3)
                config.write_text(json.dumps(candidate));proc.send_signal(signal.SIGHUP)
                deadline=time.monotonic()+5
                while query('ops_status')['checkpoint']['generation']!=1:
                    assert time.monotonic()<deadline;time.sleep(.03)
                config.write_text('{');proc.send_signal(signal.SIGHUP)
                deadline=time.monotonic()+5
                while not query('ops_status')['reload_errors']:
                    assert time.monotonic()<deadline;time.sleep(.03)
                assert query('ops_status')['checkpoint']['generation']==1
                assert query('ops_diagnostics')['operations']['host_mutations_enabled'] is False
                proc.kill();proc.wait(timeout=3)
                assert (runtime/'control.sock').exists()
                config.write_text(json.dumps(candidate))
                proc=start()
                assert query('ops_status')['previous_unclean'] is True
                assert query('ops_status')['owned_lab_active'] is False
                proc.terminate();assert proc.wait(timeout=5)==0
                assert not (runtime/'control.sock').exists()
                assert json.loads((state/'recovery.json').read_text())['clean_shutdown']
                for _ in range(3):
                    proc=start();proc.terminate();assert proc.wait(timeout=5)==0
                sentinel=work/'sentinel';sentinel.write_text('preserve')
                (runtime/'control.sock').symlink_to(sentinel)
                failed=subprocess.run([daemon,'--monitor-only','--config',config],env=env,stdout=log,stderr=log,timeout=5)
                assert failed.returncode!=0 and sentinel.read_text()=='preserve'
                (runtime/'control.sock').unlink()
                checkpoint=state/'recovery.json';checkpoint.write_text('{')
                failed=subprocess.run([daemon,'--monitor-only','--config',config],env=env,stdout=log,stderr=log,timeout=5)
                assert failed.returncode!=0 and checkpoint.read_text()=='{'
                config.chmod(0o666)
                assert subprocess.run([daemon,'--check-config','--config',config],env=env,stdout=log,stderr=log).returncode!=0
                print('PASS: installed reload, partial config rejection, SIGKILL/stale socket recovery, concurrent clients, rapid restart, symlink refusal, corrupt checkpoint refusal')
            finally:
                if proc.poll() is None:proc.terminate();proc.wait(timeout=5)

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--prefix',type=Path,required=True)
    parser.add_argument('--config',type=Path,required=True)
    args=parser.parse_args();check(args.prefix,args.config)
