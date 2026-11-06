import argparse
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

root=Path(__file__).resolve().parents[1]
parser=argparse.ArgumentParser(description='Check an unsigned local RPM digest and staged runtime')
parser.add_argument('rpm',type=Path)
rpm=parser.parse_args().rpm.resolve()
(root/'build').mkdir(exist_ok=True)
subprocess.run(['rpm','-K','--nosignature',rpm],check=True)
requires=subprocess.check_output(['rpm','-qp','--requires',rpm],text=True)
assert 'libstdc++.so.6' in requires and 'libc.so.6' in requires and 'GLIBCXX_' in requires, 'ELF runtime dependencies missing'
provides=subprocess.check_output(['rpm','-qp','--provides',rpm],text=True)
assert 'libflowcore_telemetry.so.1' in provides, 'native telemetry SONAME missing'

with tempfile.TemporaryDirectory(dir=root/'build',prefix='rpm-stage-') as temp:
    temp=Path(temp)
    stage=temp/'payload'
    stage.mkdir()
    source=subprocess.Popen(['rpm2cpio',rpm],stdout=subprocess.PIPE)
    try:
        subprocess.run(['cpio','-idm','--quiet'],cwd=stage,stdin=source.stdout,check=True)
    finally:
        source.stdout.close()
        assert source.wait()==0
    exe=stage/'usr/bin/flowcore'
    daemon=stage/'usr/bin/flowcored'
    assert subprocess.check_output([exe,'--version'],text=True).strip()=='FLOWCORE 0.2.0'
    assert (stage/'etc/flowcore/service.json').is_file()
    assert (stage/'usr/lib/systemd/system/flowcored.service').is_file()
    env=dict(os.environ,FLOWCORE_RUNTIME=str(temp/'runtime'),FLOWCORE_STATE=str(temp/'state'))
    with (temp/'daemon.log').open('w') as log:
        proc=subprocess.Popen([daemon,'--monitor-only','--config',stage/'etc/flowcore/service.json'],
                              env=env,stdout=log,stderr=log)
        try:
            for _ in range(50):
                if (temp/'runtime/control.sock').exists():
                    break
                if proc.poll() is not None:
                    raise RuntimeError((temp/'daemon.log').read_text())
                time.sleep(.1)
            def sample():
                return json.loads(subprocess.check_output([exe,'metrics','--json'],text=True,env=env))
            before=sample()
            time.sleep(10)
            after=sample()
            a,b=(row['observability']['overhead'] for row in (before,after))
            assert after['state']['healthy'] and after['state']['monitor_only']
            print(json.dumps(dict(cpu_seconds_10s=b['daemon_cpu_seconds']-a['daemon_cpu_seconds'],
                rss_bytes=b['daemon_rss_bytes'],interfaces=len(after['platform']['interfaces']))))
        finally:
            proc.terminate()
            proc.wait(timeout=8)
    assert not (temp/'runtime/control.sock').exists()
    subprocess.run(['/usr/bin/python3','-B',root/'tests/deployment_lifecycle.py',
                    '--prefix',stage/'usr','--config',stage/'etc/flowcore/service.json'],check=True)
print('PASS: RPM digest, payload layout, extracted monitor and cleanup')
