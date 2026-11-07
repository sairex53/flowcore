"""Exercise live daemon diagnostics, isolation, retention, export and offline replay."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time

from bottleneck_integration import ROOT, call
from benchmark import BINDIR, ENV
from causal_diagnostics import NS, WINDOW
from kernel_api_experiment import host_identity


def request(env, **fields):
    with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as conn:
        conn.settimeout(2)
        conn.connect(str(Path(env['FLOWCORE_RUNTIME'])/'control.sock'))
        conn.sendall(json.dumps(fields).encode()+b'\n')
        with conn.makefile('rb') as stream:
            result = json.load(stream)
    assert result['ok'], result
    return result['result']


def wait_until(stamp):
    time.sleep(max(0,(stamp-time.monotonic_ns())/NS))


def main():
    assert os.geteuid()==0, 'run on a disposable privileged lab runner'
    before = host_identity()
    with tempfile.TemporaryDirectory(dir=ROOT/'build',prefix='causal-ipc-') as tmp:
        directory = Path(tmp)
        env = dict(ENV,FLOWCORE_RUNTIME=str(directory/'runtime'),FLOWCORE_STATE=str(directory/'state'))
        config = json.loads((ROOT/'config/service.json').read_text())
        config['observability']['bpf'] = 'off'
        config['telemetry_seconds'] = .5
        cfg = directory/'service.json'
        cfg.write_text(json.dumps(config))
        cfg.chmod(0o600)
        bundle_path = directory/'bundle.json'
        with (directory/'daemon.log').open('w+') as log:
            proc = subprocess.Popen([BINDIR/'flowcored','--research','--config',cfg],env=env,stdout=log,stderr=log)
            try:
                timeout = time.monotonic()+5
                while not (directory/'runtime/control.sock').exists():
                    if proc.poll() is not None or time.monotonic() >= timeout:
                        log.seek(0)
                        raise RuntimeError(log.read())
                    time.sleep(.05)
                assert 'owned research lab' in call(env,'diagnose','--lab-intervention','freeze',expected=1)
                call(env,'lab','up')
                call(env,'controller','start','--strategy','hybrid')
                original = request(env,op='research_snapshot')['tc']['class']
                freeze = json.loads(call(env,'diagnose','--lab-intervention','freeze'))
                assert freeze['freeze_seconds']==15
                assert request(env,op='controller_status')['lab_frozen_until_ns']
                assert 'settled active' in call(env,'diagnose','--lab-intervention','freeze',expected=1)
                json.loads(call(env,'diagnose','--lab-intervention','restore'))
                assert request(env,op='controller_status')['lab_frozen_until_ns'] is None
                after = request(env,op='research_snapshot')['tc']['class']
                for left,right in zip(original,after):
                    assert (left['handle'],left['rate'],left['ceil'])==(right['handle'],right['rate'],right['ceil'])
                start = (time.monotonic_ns()//WINDOW+1)*WINDOW
                for offset,duration in ((0,1000),(WINDOW,22000)):
                    wait_until(start+offset+100_000_000)
                    for _ in range(110):
                        request(env,op='request_sample',service='demo',outcome='ok',duration_us=duration,deadline_ms=15)
                wait_until(start+2*WINDOW+2*NS+500_000_000)
                service = json.loads(call(env,'diagnose','--service','demo'))
                assert 'P99_REGRESSION' in service['signals'], service
                incidents = json.loads(call(env,'incident','list'))['incidents']
                incident = next(row for row in incidents if row['service']=='demo')
                live = json.loads(call(env,'incident','inspect',incident['id']))
                exported = call(env,'incident','export',incident['id'])
                bundle_path.write_text(exported)
                assert live == service
                if Path('/proc/pressure/cpu').is_file():
                    assert any(row['data'].get('source')=='system_psi_avg10' for row in live['timeline'])
                assert 'unknown or expired' in call(env,'incident','inspect','../state',expected=1)
            finally:
                proc.terminate()
                proc.wait(timeout=10)
            assert proc.returncode == 0
        assert json.loads(call(env,'incident','replay',str(bundle_path))) == live
        link = directory/'linked-bundle'
        link.symlink_to(bundle_path)
        assert call(env,'incident','replay',str(link),expected=3)
    assert host_identity()==before
    print('PASS: owned freeze/restore, live incident, bounded export, offline replay, host identity')


if __name__ == '__main__':
    main()
