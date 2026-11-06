"""Privileged, disposable namespaces only. No global BPF pins or host TC changes."""
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from benchmark import owned_directory
from flow_engine import FlowEngine
from service_config import load
from service_lab import ServiceLab

RULES=[dict(protocol=protocol,dport=port,service=service,classid=classid)
       for protocol,port,service,classid in ((6,9000,1,0x10010),(6,9002,2,0x10011),
        (6,9003,3,0x10012),(6,9001,4,0x10020),(17,9100,1,0x10010),(17,9101,4,0x10020))]


def setup(output):
    lab=ServiceLab(output)
    try:
        lab.start()
        cfg=load(ROOT/'config/service.json')
        lab.install_policy(cfg['policies']['default'],cfg['lab'])
        lab.tc('filter','del','dev','fc0','parent','1:')
        return lab
    except BaseException:
        lab.close()
        raise


def serve(lab):
    server=lab.spawn(1,[sys.executable,'-B',ROOT/'tests/flow_traffic.py','server'],'flow-server')
    deadline=time.monotonic()+3
    while 'ready' not in (lab.output/'flow-server.stdout').read_text():
        assert server.poll() is None and time.monotonic()<deadline
        time.sleep(.02)
    return server


def traffic(lab,name,duration=.4,churn=False,bulk=False):
    child=lab.spawn(0,[sys.executable,'-B',ROOT/'tests/flow_traffic.py','client',
        '--duration',str(duration),*(['--churn'] if churn else []),*(['--bulk'] if bulk else [])],name)
    child.wait(timeout=duration+8)
    assert child.returncode==0,(lab.output/f'{name}.stderr').read_text()
    return json.loads((lab.output/f'{name}.stdout').read_text())


def main():
    if os.geteuid()!=0: raise SystemExit('dedicated privileged runner required')
    output=ROOT/'results'/f'flow-engine-{time.time_ns()}'
    owned_directory(output)
    host=Path('/proc/self/ns/net').stat().st_ino
    lab=setup(output)
    try:
        serve(lab)
        assert lab.start_flow_engine(),lab.flow_engine.reason
        engine=lab.flow_engine
        engine.set_policy(2,RULES)
        security=dict(line.split(':',1) for line in Path(f'/proc/{engine.proc.pid}/status').read_text().splitlines() if ':' in line)
        allowed=(1<<12)|(1<<39)
        assert all(int(security[key],16)==allowed for key in ('CapEff','CapPrm','CapBnd')),security
        assert int(security['CapInh'],16)==0 and int(security['NoNewPrivs'])==1
        memory=[]
        for descriptor in Path(f'/proc/{engine.proc.pid}/fdinfo').iterdir():
            values=dict(line.split(':',1) for line in descriptor.read_text().splitlines() if ':' in line)
            if 'map_type' in values:
                memory.append({key:int(values[key]) for key in
                    ('map_type','key_size','value_size','max_entries','memlock') if key in values})
        (output/'map-memory.json').write_text(json.dumps(memory,indent=2))
        for bad in ('set 2 0','set -1 0','set 3 17','set 3 1 6 9000 1 65536 0 0 0'):
            engine.records.clear(); engine.send(bad)
            engine.wait(lambda:any(x['type']=='error' for x in engine.records))
        assert engine.ready
        conflict=FlowEngine()
        assert not conflict.start(lab),'concurrent ownership accepted'
        client=traffic(lab,'dual-stack',duration=.6,bulk=True)
        assert client['failed']==0,client
        rows=engine.dump()
        assert rows and {row['family'] for row in rows}=={2,10}
        assert {row['protocol'] for row in rows}=={6,17}
        for row in rows:
            expected=next(rule for rule in RULES if rule['protocol']==row['protocol'] and rule['dport']==row['dport'])
            assert (row['service'],row['classid'])==(expected['service'],expected['classid']),row
            assert row['netns']!=host and row['ifindex']>0
            assert row['first_ns']<=row['last_ns'] and row['packets'] and row['bytes']
        cookies={cookie for item in client['flows'] for cookie in item['cookies']}
        assert {int(row['cookie']) for row in rows}<=cookies
        counters=lab.counters()['traffic_classes']
        assert all(counters[name]['stats']['bytes']>0 for name in ('critical','interactive','standard','background')),counters
        assert engine.stats['classified']>0 and not engine.stats['map_full_packets'],engine.stats
        (output/'flows.json').write_text(json.dumps(rows,indent=2))
        # Exact cookie rule, then unmatched rules preserve default and support new generation.
        code="import socket,json,sys;s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.connect(('10.203.0.2',9101));print(int.from_bytes(s.getsockopt(socket.SOL_SOCKET,57,8),'little'),flush=True);sys.stdin.readline();s.send(b'cookie');assert s.recv(64)==b'cookie';print('done',flush=True);sys.stdin.readline()"
        probe=lab.spawn(0,[sys.executable,'-B','-c',code],'cookie-probe',
                        stdin=subprocess.PIPE,stdout=subprocess.PIPE)
        cookie=int(probe.stdout.readline())
        from flow_snapshot import snapshot
        with lab.enter(0):
            fallback=snapshot(4096)
        assert any(row['protocol']==17 and int(row['cookie'],16)==cookie
                   for row in fallback['flows'] if row['cookie']),fallback
        exact=dict(protocol=17,dport=0,service=4,classid=0x10020,cookie=cookie,cgroup=rows[0]['cgroup'])
        engine.set_policy(3,[exact])
        probe.stdin.write(b'go\n');probe.stdin.flush()
        assert probe.stdout.readline().strip()==b'done'
        combined=lab.flow_intelligence()
        associated=[row for row in combined['flows'] if int(row['cookie'])==cookie]
        assert len(associated)==1 and associated[0]['socket_association']=='exact_cookie',associated
        probe.stdin.write(b'close\n');probe.stdin.flush()
        probe.wait(timeout=3)
        probe.stdin.close();probe.stdout.close()
        assert probe.returncode==0
        matched=[row for row in engine.dump() if int(row['cookie'])==cookie]
        assert len(matched)==1 and matched[0]['classid']==0x10020 and matched[0]['generation']==3
        engine.set_policy(4,[])
        marked="import socket,sys;s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.setsockopt(socket.SOL_SOCKET,socket.SO_MARK,66);s.connect(('10.203.0.2',9101));print(int.from_bytes(s.getsockopt(socket.SOL_SOCKET,57,8),'little'),flush=True);sys.stdin.readline();s.send(b'mark');assert s.recv(64)==b'mark'"
        mark_probe=lab.spawn(0,[sys.executable,'-B','-c',marked],'mark-probe',privileged=True,
                             stdin=subprocess.PIPE,stdout=subprocess.PIPE)
        marked_cookie=int(mark_probe.stdout.readline())
        engine.set_policy(5,[dict(protocol=17,dport=9101,service=1,classid=0x10010,mark=66,
                                  cgroup=rows[0]['cgroup'])])
        mark_probe.stdin.write(b'go\n');mark_probe.stdin.flush()
        mark_probe.wait(timeout=3);mark_probe.stdin.close();mark_probe.stdout.close()
        assert mark_probe.returncode==0
        marked_rows=[row for row in engine.dump() if int(row['cookie'])==marked_cookie]
        assert len(marked_rows)==1 and marked_rows[0]['classid']==0x10010,marked_rows
        # While packets run, verify generation/service/class decisions stay coherent.
        moving=lab.spawn(0,[sys.executable,'-B',ROOT/'tests/flow_traffic.py','client',
                          '--duration','1.2','--bulk'],'updating')
        for generation in range(6,14):
            rules=[dict(rule) for rule in RULES]
            if generation%2:
                rules[0].update(service=4,classid=0x10020)
            engine.set_policy(generation,rules)
            for row in engine.dump():
                if row['dport']==9000 and row['generation']>=6:
                    expected=(4,0x10020) if row['generation']%2 else (1,0x10010)
                    assert (row['service'],row['classid'])==expected,row
        moving.wait(timeout=5)
        assert moving.returncode==0
        assert engine.stats.get('output_lost',0)==0
        # Process loss, forced close and reload detach link-FD ownership.
        engine.proc.kill();engine.proc.wait(timeout=3);engine.drain()
        assert not engine.ready
        assert lab.start_flow_engine(capacity=16),lab.flow_engine.reason
        engine=lab.flow_engine
        engine.set_policy(2,RULES)
        churn=traffic(lab,'churn',duration=2,churn=True)
        engine.dump()
        assert engine.stats['classified']==engine.stats['parsed']
        assert engine.stats['map_full_packets']>0 and engine.stats['entries_observed']<=16,engine.stats
        time.sleep(3.2)
        engine.dump()
        assert engine.stats['expired']>0,engine.stats
        engine.close()
        assert lab.start_flow_engine(capacity=16),lab.flow_engine.reason
        # Malformed oversized protocol kills the helper; no orphan link blocks reload.
        engine=lab.flow_engine
        os.write(engine.proc.stdin.fileno(),b'x'*4097)
        engine.proc.wait(timeout=3)
        assert engine.proc.returncode!=0
        engine.close()
        assert lab.start_flow_engine(capacity=16),lab.flow_engine.reason
        print(json.dumps(dict(result='PASS',dual_stack=client['offered'],churn=churn['offered'],
            retained_flows=len(rows),output=str(output))),flush=True)
        lab.flow_engine.close()
        assert lab.start_flow_engine(observe=True),lab.flow_engine.reason
        engine=lab.flow_engine
        engine.set_policy(2,RULES)
        before=lab.counters()['traffic_classes']['critical']['stats']['bytes']
        traffic(lab,'observe',duration=.3)
        engine.dump()
        assert engine.stats['classified']>0 and engine.stats['applied']==0
        assert lab.counters()['traffic_classes']['critical']['stats']['bytes']==before
        unsupported=engine.stats['unsupported']
        raw=r"import socket;s=socket.socket(socket.AF_PACKET,socket.SOCK_RAW);s.bind(('fc0',0));eth=b'\xff'*6+s.getsockname()[4];s.send(eth+b'\x08\x00'+b'\x41'*8);s.send(eth+b'\x86\xdd'+b'\x60'+b'\0'*5+b'\x3c'+b'\0'*33)"
        invalid=lab.spawn(0,[sys.executable,'-B','-c',raw],'unsupported-headers',privileged=True)
        invalid.wait(timeout=3)
        assert invalid.returncode==0
        engine.dump()
        assert engine.stats['unsupported']>unsupported
        lab.command(0,['ip','link','del','fc0'])
        engine.proc.wait(timeout=3)
        assert engine.proc.returncode!=0
        engine.drain()
        assert not engine.ready
        # Namespace holder loss triggers namespace teardown after all FD owners close.
        for holder in lab.holders: os.killpg(holder.pid,signal.SIGKILL)
    finally:
        lab.close()
    assert Path('/proc/self/ns/net').stat().st_ino==host


if __name__=='__main__':
    main()
