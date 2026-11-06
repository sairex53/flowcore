"""Unprivileged real daemon workload registration and privilege-boundary smoke test."""
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory(prefix='workload-ipc-',dir=ROOT/'build') as work:
    env=dict(os.environ,FLOWCORE_RUNTIME=work+'/runtime',FLOWCORE_STATE=work+'/state')
    with open(work+'/daemon.log','w') as log:
        daemon=subprocess.Popen([ROOT/'build/flowcored'],env=env,stdout=log,stderr=log)
        try:
            deadline=time.monotonic()+8
            while not Path(work+'/runtime/control.sock').exists():
                if daemon.poll() is not None or time.monotonic()>deadline:
                    raise RuntimeError('daemon startup failed: '+Path(work+'/daemon.log').read_text())
                time.sleep(.02)
            def call(*args,success=True):
                result=subprocess.run([ROOT/'build/flowcore','platform',*args],env=env,
                                      capture_output=True,text=True,timeout=12)
                assert (result.returncode==0)==success,(result.stdout,result.stderr)
                return json.loads(result.stdout) if success else None
            item=call('workload-register','--pid',str(os.getpid()),'--service','api')
            assert item['identity']['uid']==os.getuid()
            assert any(row['id']==item['id'] for row in call('workloads')['workloads'])
            if os.getuid()!=0: call('workload-attach',item['id'],success=False)
            call('workload-forget',item['id'])
            assert not call('workloads')['workloads']
            print('PASS: real peer-authenticated registration, list, forget and privilege rejection')
        finally:
            daemon.terminate();daemon.wait(timeout=5)
