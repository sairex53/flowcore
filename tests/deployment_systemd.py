"""Root-only transient sandbox validation of an extracted RPM payload."""
import argparse
import json
import os
import subprocess
import shutil
import time
import uuid
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]

def command(args,**kwargs):
    return subprocess.run([str(v) for v in args],capture_output=True,text=True,timeout=25,**kwargs)

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--payload',type=Path,required=True)
    args=parser.parse_args()
    if args.payload.is_symlink():raise ValueError('payload must not be a symlink')
    payload=args.payload.resolve(strict=True)
    if os.geteuid()!=0 or payload.is_symlink() or not payload.is_relative_to(ROOT/'build'):
        raise ValueError('root and a dedicated payload under project/build required')
    owner=payload.stat()
    config=payload/'etc/flowcore/service.json';original=config.read_bytes()
    unit='fc-rel-'+uuid.uuid4().hex[:8]
    runtime=Path('/run')/unit
    files=[payload,*payload.rglob('*')]
    result={}
    try:
        for file in files:os.chown(file,0,0,follow_symlinks=False)
        properties=dict(DynamicUser='yes',RuntimeDirectory=unit,RuntimeDirectoryMode='0700',
            StateDirectory=unit,StateDirectoryMode='0700',BindReadOnlyPaths=f'{payload}:/tmp/flowcore-release-payload',
            NoNewPrivileges='yes',CapabilityBoundingSet='',AmbientCapabilities='',
            ProtectSystem='strict',ProtectHome='yes',ProtectKernelTunables='yes',
            ProtectKernelModules='yes',ProtectKernelLogs='yes',ProtectControlGroups='yes',
            PrivateTmp='yes',PrivateDevices='yes',RestrictNamespaces='yes',
            RestrictAddressFamilies='AF_UNIX AF_NETLINK',MemoryMax='128M',TasksMax='32',
            CPUQuota='30%',WatchdogSec='15',Restart='on-failure',RestartSec='.3',
            TimeoutStopSec='10',KillMode='control-group',UMask='0077')
        argv=['systemd-run','--unit='+unit,'--collect','--service-type=notify']
        argv.extend('--property='+k+'='+v for k,v in properties.items())
        argv.extend(['--setenv=FLOWCORE_RUNTIME='+str(runtime/'daemon'),
                     '--setenv=FLOWCORE_STATE=/var/lib/'+unit+'/state',
                     '/usr/bin/python3','-E','-s','-B','/tmp/flowcore-release-payload/usr/share/flowcore/scripts/daemon.py','--monitor-only',
                     '--config','/tmp/flowcore-release-payload/etc/flowcore/service.json'])
        started=command(argv)
        if started.returncode:raise RuntimeError(started.stderr)
        def pid():
            return int(command(['systemctl','show',unit,'--property=MainPID','--value']).stdout.strip())
        def query():
            env=dict(os.environ,FLOWCORE_RUNTIME=str(runtime/'daemon'))
            answer=command([payload/'usr/bin/flowcore','ops','status'],env=env)
            if answer.returncode:raise RuntimeError(answer.stderr)
            return json.loads(answer.stdout)
        first=pid()
        assert first>0
        caps=next(line.split()[1] for line in Path(f'/proc/{first}/status').read_text().splitlines() if line.startswith('CapEff:'))
        assert int(caps,16)==0
        initial=query()
        config.write_text('{')
        assert command(['systemctl','kill','--signal=HUP',unit]).returncode==0
        deadline=time.monotonic()+5
        while not query()['reload_errors']:
            assert time.monotonic()<deadline;time.sleep(.03)
        config.write_bytes(original)
        assert command(['systemctl','kill','--signal=KILL','--kill-whom=main',unit]).returncode==0
        deadline=time.monotonic()+10
        while True:
            try:
                recovered=query()
                second=pid()
                if second!=first and recovered['previous_unclean']:break
            except (RuntimeError,ValueError):pass
            assert time.monotonic()<deadline
            time.sleep(.1)
        result=dict(status='PASS',unit=unit,cap_eff=caps,initial=initial,recovered=recovered,
                    old_pid=first,new_pid=second,properties=properties)
    finally:
        command(['systemctl','stop',unit])
        public=Path('/var/lib')/unit
        private=Path('/var/lib/private')/unit
        if public.is_symlink():
            if public.resolve()!=private:raise RuntimeError('unexpected test StateDirectory target')
            public.unlink()
        if private.is_dir() and not private.is_symlink():shutil.rmtree(private)
        elif public.is_dir() and not public.is_symlink():shutil.rmtree(public)
        config.write_bytes(original)
        for file in files:os.chown(file,owner.st_uid,owner.st_gid,follow_symlinks=False)
    assert not runtime.exists(),'runtime directory not removed'
    path=ROOT/'results'/('deployment-systemd-'+str(time.time_ns())+'.json')
    path.write_text(json.dumps(result,indent=2))
    print(json.dumps(dict(status='PASS',artifact=str(path),cap_eff=result['cap_eff'],
                         recovered_after_sigkill=True,runtime_removed=True)))

if __name__=='__main__':main()
