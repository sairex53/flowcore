"""Real veth flows/export/NAT and separately labelled kernel packet fixtures."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from benchmark import owned_directory
from flow_engine_integration import setup,serve,RULES


def main():
    if os.geteuid()!=0:
        raise SystemExit('dedicated privileged namespace runner required')
    output=ROOT/'results'/f'flow-v2-{time.time_ns()}'
    owned_directory(output)
    host=Path('/proc/self/ns/net').stat().st_ino
    lab=setup(output)
    try:
        serve(lab)
        kernel=lab.spawn(0,[ROOT/'build/flow_parser_kernel',ROOT/'build/bpf/flow_engine.bpf.o'],
                         'kernel-fixtures',privileged=True)
        kernel.wait(timeout=15)
        assert kernel.returncode==0,(output/'kernel-fixtures.stderr').read_text()
        fixtures=json.loads((output/'kernel-fixtures.stdout').read_text())
        assert lab.start_flow_engine(),lab.flow_engine.reason
        engine=lab.flow_engine
        engine.set_policy(2,RULES)
        lab.command(0,['ping','-c','1','-W','1','10.203.0.2'])
        lab.command(0,['ping','-6','-c','1','-W','1','fd42:203::2'])
        producer="""import json,socket
cookies=[]
for i in range(1200):
    family=socket.AF_INET if i%2==0 else socket.AF_INET6
    target='10.203.0.2' if i%2==0 else 'fd42:203::2'
    with socket.socket(family,socket.SOCK_DGRAM) as sock:
        sock.connect((target,9100))
        cookies.append(int.from_bytes(sock.getsockopt(socket.SOL_SOCKET,57,8),'little'))
        sock.send(b'flow-export')
print(json.dumps(cookies),flush=True)
"""
        child=lab.spawn(0,[sys.executable,'-B','-c',producer],'many-flows')
        child.wait(timeout=5)
        assert child.returncode==0,(output/'many-flows.stderr').read_text()
        cookies=set(json.loads((output/'many-flows.stdout').read_text()))
        started=time.monotonic()
        rows=engine.dump()
        export_seconds=time.monotonic()-started
        quality=engine.last_snapshot.copy()
        assert quality['complete'] and len(rows)==quality['total'] and len(rows)>512,quality
        retained={int(row['cookie']) for row in rows}
        assert cookies<=retained,(len(cookies),len(retained))
        assert len({row['netns'] for row in rows})==1 and rows[0]['netns']!=host
        assert all(row['classid']==0x10010 and row['protocol']==17 for row in rows)
        (output/'many-flows.json').write_text(json.dumps(dict(rows=rows,quality=quality),indent=2))
        # A frozen snapshot must not change when live policy/flows change between pages.
        identifier=quality['snapshot']
        engine.set_policy(3,[])
        engine.records.clear()
        engine.send(f'page {identifier} 0')
        engine.wait(lambda:any(row['type']=='page' for row in engine.records))
        old=next(row for row in engine.records if row['type']=='page')
        assert old['snapshot']==identifier and all(row['generation']==2 for row in old['flows'])
        engine.records.clear();engine.send(f'page {identifier+1} 0')
        engine.wait(lambda:any(row['type']=='error' for row in engine.records))
        engine.set_policy(4,RULES)
        # SNAT is configured only in this new owned namespace; host rules are untouched.
        lab.command(0,['ip','addr','add','10.203.0.9/32','dev','fc0'])
        lab.command(1,['ip','route','add','10.203.0.9/32','dev','fc1'])
        nft=output/'nat.nft'
        nft.write_text('table ip flow_v2_nat { chain outgoing { type nat hook postrouting priority srcnat; '
                       'oifname "fc0" ip daddr 10.203.0.2 udp dport 9101 snat to 10.203.0.9:45000; }; }\n')
        try:
            lab.command(0,['/usr/bin/nft','-f',nft])
        except subprocess.CalledProcessError as error:
            raise RuntimeError(error.stderr) from error
        code="import socket,sys;s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.settimeout(2);s.connect(('10.203.0.2',9101));print(int.from_bytes(s.getsockopt(socket.SOL_SOCKET,57,8),'little'),flush=True);s.send(b'nat');assert s.recv(64)==b'nat';print('done',flush=True);sys.stdin.readline()"
        nat=lab.spawn(0,[sys.executable,'-B','-c',code],'nat-socket',stdin=subprocess.PIPE,stdout=subprocess.PIPE)
        cookie=int(nat.stdout.readline())
        assert nat.stdout.readline().strip()==b'done'
        combined=lab.flow_intelligence()
        translated=[row for row in combined['flows'] if int(row['cookie'])==cookie]
        assert len(translated)==1,translated
        row=translated[0]
        assert row['source']=='10.203.0.9' and row['sport']==45000,row
        assert row['socket_association']=='exact_cookie' and row['tuple_relation']=='different',row
        assert row['socket_snapshot']['local_address']=='10.203.0.1',row
        (output/'nat.json').write_text(json.dumps(row,indent=2))
        nat.stdin.write(b'close\n');nat.stdin.flush()
        nat.wait(timeout=3);nat.stdin.close();nat.stdout.close()
        lab.command(0,['/usr/bin/nft','delete','table','ip','flow_v2_nat'])
        # Failure after page one must never be presented as a partial success.
        original=engine.send
        def fail_after_first(message):
            if message.startswith('page '):
                engine.proc.kill();engine.proc.wait(timeout=3)
                raise RuntimeError('injected exporter crash')
            original(message)
        engine.send=fail_after_first
        try:
            engine.dump()
            raise AssertionError('partial snapshot accepted')
        except RuntimeError:
            assert not engine.last_snapshot['complete']
        engine.send=original
        engine.close()
        assert lab.start_flow_engine(),lab.flow_engine.reason
        print(json.dumps(dict(result='PASS',output=str(output),wire_flow_cookies=len(cookies),
                              exported=len(rows),export_seconds=export_seconds,quality=quality,
                              kernel_fixtures=fixtures,nat='exact cookie; differing tuples')),flush=True)
    finally:
        lab.close()
    assert Path('/proc/self/ns/net').stat().st_ino==host


if __name__=='__main__':
    main()
