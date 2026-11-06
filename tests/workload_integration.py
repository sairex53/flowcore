"""Privileged two-cgroup classification test; only owned namespaces and cgroups."""
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from daemon import Service
from benchmark import owned_directory
from containers import attach, attachment_sample
from research_runner import exclusive
from service_lab import ServiceLab
from service_config import load
from observer_perf import stop

def main():
    if os.geteuid()!=0: raise PermissionError('requires isolated privileged test runner')
    with exclusive():
        output=ROOT/'results'/('workloads-'+str(time.time_ns()))
        owned_directory(output)
        cfg=load(ROOT/'config/service.json')
        lab=ServiceLab(output)
        processes=[];groups=[]
        try:
            lab.start();lab.install_policy(cfg['policies']['default'],cfg['lab'])
            service=Service(cfg)
            service.research=True
            service.lab=lab
            server="import socket; s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); s.bind(('0.0.0.0',9009));\nwhile True: s.recvfrom(65536)"
            processes.append(subprocess.Popen(lab.ns(1,[sys.executable,'-c',server])))
            for index,traffic_class in enumerate(('critical','background')):
                group=Path('/sys/fs/cgroup')/('flowcore-test-'+uuid.uuid4().hex)
                group.mkdir();groups.append(group)
                ready=output/('ready'+str(index));gate=output/('gate'+str(index))
                code="""import os,socket,time
from pathlib import Path
Path(%r).write_text(str(os.getpid()))
Path(%r).touch()
while not Path(%r).exists(): time.sleep(.01)
s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
for _ in range(100):
 s.sendto(b'W'*1024,('10.203.0.2',9009));time.sleep(.01)
time.sleep(30)
"""%(str(group/'cgroup.procs'),str(ready),str(gate))
                proc=subprocess.Popen(lab.ns(0,[sys.executable,'-c',code]));processes.append(proc)
                deadline=time.monotonic()+5
                while not ready.exists():
                    if proc.poll() is not None or time.monotonic()>deadline: raise RuntimeError('client startup failed')
                    time.sleep(.02)
                if traffic_class=='critical':
                    registration=service.workloads.register(0,proc.pid,'api')
                    service.command(dict(op='platform_workload_attach',id=registration['id']),uid=0,pid=os.getpid())
                else:
                    attach(service,proc.pid,traffic_class)
                try:
                    attach(service,proc.pid,'standard')
                except ValueError: pass
                else: raise AssertionError('shared cgroup conflict accepted')
            for index in range(2): (output/('gate'+str(index))).touch()
            time.sleep(2)
            attachment_sample(service)
            counters=lab.counters()['traffic_classes']
            for row in service.container_attachments.values():
                assert row['matched_egress']['packets']>=100,row
                assert counters[row['traffic_class']]['stats']['bytes']>=102400,counters
            snapshots=json.loads(json.dumps(list(service.container_attachments.values())))
            for proc in processes[1:]: stop(proc)
            attachment_sample(service)
            assert all(not row['active'] for row in service.container_attachments.values())
            tables=json.loads(lab.command(0,['nft','-j','list','tables']))
            assert not any(item.get('table',{}).get('name','').startswith('fcw_') for item in tables['nftables'])
            result=dict(status='PASS',path=str(output),classifiers=snapshots,counters=counters,
                        stale_cleanup=True,shared_cgroup_conflict_rejected=True)
            (output/'summary.json').write_text(json.dumps(result,indent=2))
            print(json.dumps(result))
        finally:
            for proc in processes: stop(proc)
            lab.close()
            for group in groups: group.rmdir()

if __name__=='__main__':main()
