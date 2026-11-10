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


@contextmanager
def loaded(namespaces, expected_inodes, ports):
    stop = Event()
    with ExitStack() as stack, ThreadPoolExecutor(max_workers=2*len(ports)) as pool:
        fds = [stack.enter_context(open(f'/proc/{pid}/ns/net','rb')).fileno() for pid in namespaces]
        assert [os.fstat(fd).st_ino for fd in fds] == expected_inodes
        assert os.stat('/proc/self/ns/net').st_ino not in expected_inodes
        lab = HeldNamespaces(fds)
        futures = []
        for port in ports:
            client,peer = tcp_pair(lab,stack,'10.203.0.2',port,False)
            futures.extend((pool.submit(send,stop,client),pool.submit(receive,stop,peer)))
        try:
            yield
        finally:
            stop.set()
            for task in futures:
                task.result(timeout=2)


def measured_rate(env, traffic_class):
    def sample():
        value = request(env, op='research_snapshot')
        return value['end_ns'], value['tc']['traffic_classes'][traffic_class]['stats']['bytes']
    start, before = sample()
    time.sleep(1.2)
    end, after = sample()
    return (after-before)*8e6/(end-start)


def live_ceiling(env, namespaces, inodes):
    with loaded(namespaces, inodes, (9000,)):
        time.sleep(.7)
        before = measured_rate(env, 'critical')
        call(env, 'controller', 'start', '--strategy', 'optimizer')
        wait(lambda: allocation(env)['critical']==(6000,9000))
        time.sleep(.6)
        after = measured_rate(env, 'critical')
        events = request(env, op='controller_trace')['events']
        applied = next(row for row in reversed(events) if row['result']=='verified')
        proposed = next(row for row in reversed(events) if row['result']=='proposed'
                        and row['proposal_id']==applied['proposal_id'])
        assert applied['trace']['reason_code']=='ACTION_APPLIED' and proposed['kernel_diff']
        assert before > 12000 and 7800 <= after <= 10300, (before, after)
        print(json.dumps(dict(fixture='authorized_ceiling', before_wire_kbit=before,
                              after_wire_kbit=after, proposal=proposed, applied=applied['timing'])), flush=True)


def live_transfer(env, namespaces, expected_inodes):
    with loaded(namespaces, expected_inodes, (9000,9001,9002,9003)):
        def probe():
            # Fixed SLO samples make policy eligibility deterministic. Real TCP
            # traffic and HTB counters determine whether the actuator has an effect.
            for _ in range(22):
                request(env, op='request_sample', service='demo', outcome='ok', duration_us=80000, deadline_ms=25)
            state = request(env, op='controller_status')
            if state['error']:
                raise RuntimeError(state['error'])
            return state

        pending = verdict = None
        end = time.monotonic()+24
        while time.monotonic() < end:
            state = probe()
            if pending is None and state['pending']:
                rows = request(env, op='research_snapshot')['tc']['traffic_classes']
                assert state['allocation']['critical'][0] == rows['critical']['rate']//125 > 6000
                assert state['allocation']['background'][0] == rows['background']['rate']//125 >= 4000
                assert all(rows[name]['stats']['bytes'] > 0 for name in rows)
                pending = next(row for row in reversed(request(env, op='policy_history')) if row['result']=='pending')
                assert pending['timing']['transaction']=='VERIFIED'
                assert len(pending['optimizer']['diff'])==2
                assert pending['optimizer']['alternatives']['adaptive']['disposition']=='selected'
            if pending and not state['pending']:
                verdict = next(row for row in reversed(request(env, op='policy_history'))
                               if row.get('proposal_id')==pending['proposal_id'] and 'outcome' in row)
                break
            time.sleep(.12)
        if not verdict:
            raise AssertionError('no verified reservation verdict: '+json.dumps(state['last_decision']))
        assert verdict['trace']['reason_code']=='ACTION_REVERTED' and verdict['outcome'] in ('harmful','inconclusive')
        assert verdict['effect_evidence']['traffic_effect_observed'], verdict
        assert allocation(env)['critical']==(6000,9000)
        print(json.dumps(dict(fixture='reservation_probe', allocation=pending['proposed'],
                              timing=pending['timing'], solve_ns=pending['optimizer']['solver_and_bridge_ns'],
                              verdict=verdict)), flush=True)


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
                live_ceiling(env,namespaces,inodes)
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
