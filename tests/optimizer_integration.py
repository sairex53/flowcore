"""Readback-verified optimizer transactions on a disposable veth pair."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
import json
import os
from pathlib import Path
import subprocess
import tempfile
from threading import Event
import time

from bottleneck_integration import ROOT, call
from benchmark import BINDIR, ENV, TC
from bottleneck_experiment import receive, send, tcp_pair
from incident_integration import request
from kernel_api_experiment import host_identity


class HeldNamespaces:
    def __init__(self, fds):
        self.fds = fds

    @contextmanager
    def enter(self, index):
        with open('/proc/thread-self/ns/net','rb') as original:
            os.setns(self.fds[index],os.CLONE_NEWNET)
            try:
                yield
            finally:
                os.setns(original.fileno(),os.CLONE_NEWNET)


def allocation(env):
    rows = request(env,op='research_snapshot')['tc']['traffic_classes']
    return {name:(row['rate']//125,row['ceil']//125) for name,row in rows.items()}


def wait(check, timeout=12):
    end = time.monotonic()+timeout
    while time.monotonic() < end:
        result = check()
        if result:
            return result
        time.sleep(.1)
    raise AssertionError('expected optimizer state did not converge')


def live_transfer(env, namespaces, expected_inodes):
    stop = Event()
    with ExitStack() as stack, ThreadPoolExecutor(max_workers=4) as pool:
        fds = [stack.enter_context(open(f'/proc/{pid}/ns/net','rb')).fileno() for pid in namespaces]
        assert [os.fstat(fd).st_ino for fd in fds] == expected_inodes
        assert os.stat('/proc/self/ns/net').st_ino not in expected_inodes
        lab = HeldNamespaces(fds)
        futures = []
        for port in (9000,9001):
            client,peer = tcp_pair(lab,stack,'10.203.0.2',port,False)
            futures.extend((pool.submit(send,stop,client),pool.submit(receive,stop,peer)))
        try:
            def probe():
                # Fixed SLO trace to exercise the actuator deterministically.
                # The congestion, class counters and applied rates are real.
                for _ in range(22):
                    request(env,op='request_sample',service='demo',outcome='ok',duration_us=80000,deadline_ms=25)
                control = request(env,op='controller_status')
                if control['error']:
                    raise RuntimeError(control['error'])
                return control if control['pending'] else None
            # Spread fixed observations across complete independent windows.
            end = time.monotonic()+20
            while time.monotonic() < end:
                if control := probe():
                    rows = request(env,op='research_snapshot')['tc']['traffic_classes']
                    assert control['allocation']['critical'][0] == rows['critical']['rate']//125 > 6000
                    assert control['allocation']['background'][0] == rows['background']['rate']//125 >= 4000
                    assert all(rows[name]['stats']['bytes'] > 0 for name in ('critical','background'))
                    record = next(row for row in reversed(request(env,op='policy_history')) if row['result']=='pending')
                    assert record['timing']['transaction']=='VERIFIED'
                    assert len(record['optimizer']['diff'])==2
                    assert record['optimizer']['alternatives']['adaptive']['disposition']=='selected'
                    print(json.dumps(dict(allocation=control['allocation'],timing=record['timing'],
                                          solve_ns=record['optimizer']['solver_and_bridge_ns'])),flush=True)
                    return
                time.sleep(.12)
            last = request(env,op='controller_status')
            raise AssertionError('no verified reservation transfer: '+json.dumps(last['last_decision']))
        finally:
            stop.set()
            for task in futures:
                task.result(timeout=2)


def main():
    assert os.geteuid()==0,'run in a disposable privileged VM'
    identity = host_identity()
    with tempfile.TemporaryDirectory(dir=ROOT/'build',prefix='optimizer-ipc-') as tmp:
        output = Path(tmp)
        env = dict(ENV,FLOWCORE_RUNTIME=str(output/'runtime'),FLOWCORE_STATE=str(output/'state'))
        cfg = json.loads((ROOT/'config/service.json').read_text())
        cfg['observability']['bpf'] = 'off'
        cfg['telemetry_seconds'] = .5
        cfg['adaptive'].update(window_seconds=2,cooldown_seconds=2)
        config = output/'config.json'
        config.write_text(json.dumps(cfg))
        config.chmod(0o600)
        with (output/'daemon.log').open('w+') as log:
            daemon = subprocess.Popen([BINDIR/'flowcored','--research','--config',config],env=env,stdout=log,stderr=log)
            try:
                wait(lambda:(output/'runtime/control.sock').exists())
                unavailable = json.loads(call(env,'policy','optimize'))
                assert unavailable['optimizer']['conflicts'][0]['code']=='OWNED_HTB_REQUIRED'
                call(env,'lab','up')
                namespaces = request(env,op='inspect')['namespaces']
                inodes = request(env,op='research_snapshot')['namespaces']
                request(env,op='intent_register',intent=dict(name='demo',min_kbit=4500,max_kbit=9000))
                before = allocation(env)
                preview = json.loads(call(env,'policy','optimize'))
                assert preview['dry_run'] and preview['actual_verified']
                assert preview['optimizer']['diff'][0]['after']==[6000,9000]
                assert allocation(env)==before
                call(env,'controller','shadow','--strategy','optimizer')
                wait(lambda:any(row['result']=='hypothetical' for row in request(env,op='policy_history')))
                assert allocation(env)==before
                call(env,'controller','stop')
                call(env,'controller','start','--strategy','optimizer')
                wait(lambda:allocation(env)['critical']==(6000,9000))
                live_transfer(env,namespaces,inodes)
                call(env,'controller','stop')
                assert allocation(env)['critical']==(6000,9000)
                assert allocation(env)['background']==(6000,20000)
                call(env,'controller','start','--strategy','optimizer')
                with open(f'/proc/{namespaces[0]}/ns/net','rb') as fd:
                    assert os.fstat(fd.fileno()).st_ino==inodes[0]
                    subprocess.run(['nsenter',f'--net=/proc/self/fd/{fd.fileno()}','--',TC,'class','change',
                                    'dev','fc0','parent','1:','classid','1:1','htb','rate','16000kbit','ceil','16000kbit'],
                                   pass_fds=(fd.fileno(),),check=True,timeout=4)
                state = wait(lambda: (row if (row:=request(env,op='controller_status'))['mode']=='off' and row['error'] else None))
                assert 'capacity' in state['error'] and 'drift' in state['error']
                actual = request(env,op='research_snapshot')['tc']['class']
                assert next(row for row in actual if row['handle']=='1:1')['rate']==16000*125
            except BaseException:
                log.seek(0)
                print(log.read())
                raise
            finally:
                daemon.terminate()
                daemon.wait(timeout=10)
            assert daemon.returncode==0
        assert all(not Path(f'/proc/{pid}/ns/net').exists() for pid in namespaces)
    assert host_identity()==identity
    print('PASS: preview, shadow, hard cap, verified transfer, safe restoration, external capacity drift and cleanup')


if __name__ == '__main__':
    main()
