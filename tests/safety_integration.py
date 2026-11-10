"""Linux integration with deterministic pauses supplied only by the test daemon."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from benchmark import ENV, TC
from kernel_api_experiment import host_identity
from platform_policy import EMPTY


def wait(check, seconds=9):
    end = time.monotonic()+seconds
    while time.monotonic()<end:
        try:
            result = check()
            if result:
                return result
        except (ConnectionError,FileNotFoundError):
            pass
        time.sleep(.025)
    raise AssertionError('deadline waiting for '+str(check))


def test_daemon(work, config):
    # No fault-injection switches in the deployed service.
    from daemon import main
    from service_lab import ServiceLab
    tc, counters, close = ServiceLab.tc,ServiceLab.counters,ServiceLab.close

    def boundary(lab, name):
        flag = work/name
        if not flag.exists():
            return
        flag.unlink()
        marker = work/(name+'.pending')
        marker.write_text(json.dumps([p.pid for p in lab.holders]))
        marker.replace(work/(name+'.entered'))
        end = time.monotonic()+15
        while not (work/(name+'.release')).exists() and time.monotonic()<end:
            time.sleep(.02)

    def install(lab,*args):
        result = tc(lab,*args)
        if args[:2]==('class','add'):
            boundary(lab,'after-class')
        if args[:2]==('class','change'):
            boundary(lab,'after-actuation')
            lost_ack = work/'lose-actuation-ack'
            if lost_ack.exists():
                lost_ack.unlink()
                raise TimeoutError('injected timeout after kernel class update')
        return result

    def collect(lab):
        boundary(lab,'before-counters')
        return counters(lab)

    def shutdown(lab):
        close(lab)
        flag = work/'shutdown-error'
        if flag.exists():
            flag.unlink()
            raise OSError('injected failure during owned lab shutdown')

    ServiceLab.tc, ServiceLab.counters, ServiceLab.close = install, collect, shutdown
    sys.argv = [sys.argv[0],'--research','--config',config]
    return main()


def client(env,fields,disconnect=False):
    with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as s:
        s.settimeout(4)
        s.connect(str(Path(env['FLOWCORE_RUNTIME'])/'control.sock'))
        s.sendall(json.dumps(fields).encode()+b'\n')
        if not disconnect:
            with s.makefile('rb') as reply:
                return json.load(reply)


def main():
    assert os.geteuid()==0,'requires a disposable Linux namespace runner'
    identity = host_identity()
    with tempfile.TemporaryDirectory(dir=ROOT/'build',prefix='safety-') as tmp, ExitStack() as cleanup:
        work = Path(tmp)
        env = dict(ENV,FLOWCORE_RUNTIME=str(work/'run'),FLOWCORE_STATE=str(work/'state'))
        config = work/'config.json'
        cfg = json.loads((ROOT/'config/service.json').read_text())
        cfg['telemetry_seconds'] = .5
        cfg['observability']['bpf'] = 'off'
        cfg['adaptive']['window_seconds'] = 2
        network = dict(cfg['lab'])
        network['base_rtt_ms'] = network.pop('delay_ms')
        config.write_text(json.dumps(cfg))
        config.chmod(0o600)
        log = cleanup.enter_context((work/'daemon.log').open('w+'))
        argv = [sys.executable,'-B',__file__,'--daemon',work,config]

        def start():
            p = subprocess.Popen(argv,env=env,stdout=log,stderr=log)
            def stop():
                if p.poll() is None:
                    p.terminate()
                    try:
                        p.wait(timeout=6)
                    except subprocess.TimeoutExpired:
                        p.kill()
                        p.wait(timeout=3)
            cleanup.callback(stop)
            wait(lambda: get('status'))
            return p

        def get(op,**fields):
            reply = client(env,dict(op=op,**fields))
            assert reply['ok'],reply
            return reply['result']

        try:
            proc = start()
            contender = subprocess.run(argv,env=env,stdout=log,stderr=log,timeout=5)
            assert contender.returncode!=0
            assert get('ops_status')['checkpoint']['starts']==1

            with ThreadPoolExecutor(max_workers=2) as pool:
                updates = [dict(op='platform_commit',revision=0,policy={**EMPTY,'global':{'desired_kbit':rate}})
                           for rate in (100,200)]
                replies = list(pool.map(lambda req:client(env,req),updates))
            assert sum(r['ok'] for r in replies)==1
            assert 'revision conflict' in next(r['error'] for r in replies if not r['ok'])
            client(env,dict(op='platform_commit',revision=1,policy={**EMPTY,'global':{'desired_kbit':300}}),disconnect=True)
            wait(lambda:get('platform_policies')['revision']==2)

            (work/'after-class').touch()
            client(env,dict(op='lab_up'),disconnect=True)
            entered = work/'after-class.entered'
            wait(entered.exists)
            holders = json.loads(entered.read_text())
            proc.kill()
            assert proc.wait(timeout=4)==-9
            wait(lambda:all(not Path(f'/proc/{pid}/ns/net').exists() for pid in holders))
            proc = start()
            recovery = get('ops_status')
            assert recovery['previous_unclean'] and not recovery['owned_lab_active']
            assert get('controller_status')['mode']=='off'
            assert get('platform_policies')['revision']==2

            get('lab_up')
            from overload import Pressure, cooperative_poll
            wait(lambda: 'directions' in get('capacity_estimate'))
            capacity = get('capacity_estimate')
            assert capacity['scope'] == 'owned_lab_sender'
            assert len(capacity['namespace_identity']) == 2 and capacity['sample_monotonic_ns'] > 0
            advice = cooperative_poll(get, Pressure(), 8)
            assert advice == dict(state='NORMAL', action='NO_INTERVENTION', recommended_concurrency=8), advice
            sender, receiver = get('inspect')['namespaces']
            inodes = get('research_snapshot')['namespaces']
            get('controller_start',strategy='optimizer')
            assert get('controller_status')['allocation_verified']
            (work/'before-counters').touch()
            wait((work/'before-counters.entered').exists)
            with open(f'/proc/{sender}/ns/net','rb') as target:
                inode = os.fstat(target.fileno()).st_ino
                assert inode==inodes[0] and inode!=os.stat('/proc/self/ns/net').st_ino
                subprocess.run(['nsenter',f'--net=/proc/self/fd/{target.fileno()}','--','ip','link','del','fc0'],
                               pass_fds=(target.fileno(),),check=True,timeout=4)
            (work/'before-counters.release').touch()
            wait(lambda:not get('status')['lab_up'])
            state = get('controller_status')
            assert state['mode']=='off' and not state['allocation_verified']
            assert state['allocation'] is None and state['pending'] is None
            wait(lambda:all(not Path(f'/proc/{pid}/ns/net').exists() for pid in (sender,receiver)))
            get('lab_up')
            sender, receiver = get('inspect')['namespaces']
            inodes = get('research_snapshot')['namespaces']
            original = get('research_snapshot')['tc']['traffic_classes']
            get('controller_start',strategy='slo')
            with open(f'/proc/{sender}/ns/net','rb') as target:
                assert os.fstat(target.fileno()).st_ino==inodes[0]
                subprocess.run(['nsenter',f'--net=/proc/self/fd/{target.fileno()}','--','ip','link','set',
                                'dev','fc0','alias','foreign-safety-test'],pass_fds=(target.fileno(),),check=True,timeout=4)
            wait(lambda:get('controller_status')['mode']=='off')
            assert 'ownership drift' in get('controller_status')['error']
            after = get('research_snapshot')['tc']['traffic_classes']
            for name,row in original.items():
                assert (row['rate'],row['ceil'])==(after[name]['rate'],after[name]['ceil'])
            get('lab_down')
            get('lab_up')
            sender, receiver = get('inspect')['namespaces']
            inodes = get('research_snapshot')['namespaces']
            with open(f'/proc/{sender}/ns/net','rb') as target:
                assert os.fstat(target.fileno()).st_ino==inodes[0]
                cmd = ['nsenter',f'--net=/proc/self/fd/{target.fileno()}','--',TC]
                subprocess.run([*cmd,'filter','add','dev','fc0','parent','1:','protocol','ip',
                                'pref','999','flower','ip_proto','udp','dst_port','7899','classid','1:20'],
                               pass_fds=(target.fileno(),),check=True,timeout=4)
                denied = client(env,dict(op='controller_start'))
                assert not denied['ok'] and 'filter ownership drift' in denied['error'],denied
                rows = json.loads(subprocess.check_output([*cmd,'-j','filter','show','dev','fc0','parent','1:'],
                                  pass_fds=(target.fileno(),),timeout=4))
                assert any(row.get('pref')==999 for row in rows)
            assert get('ops_status')['runtime_safety']['phase']=='QUARANTINED'
            assert get('lab_up')['changed']
            wait(lambda:all(not Path(f'/proc/{pid}/ns/net').exists() for pid in (sender,receiver)))
            get('research_conditions', network=network, mode='static-step', seed=155, link_up=True)
            (work/'lose-actuation-ack').touch()
            rejected = client(env,dict(op='research_reservation_step'))
            assert not rejected['ok'] and 'rolled back' in rejected['error'],rejected
            safety = get('ops_status')['runtime_safety']
            assert safety['phase']=='REJECTED' and safety['recovery']=='VERIFIED_COMPENSATION',safety
            assert safety['kernel_verified_generation']==0 and safety['desired_generation']==1
            get('research_reservation_step')
            safety = get('ops_status')['runtime_safety']
            assert safety['phase']=='VERIFIED' and safety['kernel_verified_generation']==2
            get('lab_down')
            get('lab_up')
            get('research_conditions', network=network, mode='static-step', seed=155, link_up=True)
            (work/'after-actuation').touch()
            client(env,dict(op='research_reservation_step'),disconnect=True)
            entered = work/'after-actuation.entered'
            wait(entered.exists)
            holders = json.loads(entered.read_text())
            assert json.loads((work/'state/actuation.json').read_text())['phase']=='PROPOSED'
            proc.kill()
            assert proc.wait(timeout=4)==-9
            wait(lambda:all(not Path(f'/proc/{pid}/ns/net').exists() for pid in holders))
            proc = start()
            ops = get('ops_status')
            assert ops['previous_unclean'] and not ops['owned_lab_active']
            assert ops['previous_actuation']['evidence']['phase']=='PROPOSED'
            assert ops['previous_actuation']['reconciliation']=='HISTORICAL_NO_REPLAY'

            # Measure resources on the same PID before and after concurrent
            # stale policy proposals. No successful requests write state.
            def resources():
                fields = dict(line.split(':',1) for line in
                    Path(f'/proc/{proc.pid}/status').read_text().splitlines())
                return dict(fds=len(list(Path(f'/proc/{proc.pid}/fd').iterdir())),
                            threads=int(fields['Threads']),rss_kib=int(fields['VmRSS'].split()[0]))
            before = resources()
            request = dict(op='platform_commit',revision=0,policy={**EMPTY,'global':{'desired_kbit':200}})
            with ThreadPoolExecutor(max_workers=6) as pool:
                replies = list(pool.map(lambda _:client(env,request),range(2048)))
            assert all(not reply['ok'] and 'revision conflict' in reply['error'] for reply in replies)
            after = resources()
            assert after['fds']==before['fds'] and after['threads']==before['threads'],(before,after)
            assert after['rss_kib']-before['rss_kib']<16384,(before,after)
            print(json.dumps(dict(stale_proposals=len(replies),before=before,after=after)),flush=True)
            proc.terminate()
            assert proc.wait(timeout=6)==0
            assert json.loads((work/'state/recovery.json').read_text())['clean_shutdown']
            wait(lambda: not (work/'run/control.sock').exists())
            # A corrupt historical actuation journal grants no restart authority.
            (work/'state/actuation.json').write_text('{')
            proc = start()
            assert get('ops_status')['previous_actuation']['reconciliation']=='UNREADABLE_NO_REPLAY'
            assert get('controller_status')['mode']=='off'
            get('lab_up')
            (work/'shutdown-error').touch()
            proc.terminate()
            assert proc.wait(timeout=6)==1
            assert not (work/'run/control.sock').exists()
            assert not json.loads((work/'state/recovery.json').read_text())['clean_shutdown']
        except BaseException:
            log.seek(0)
            print(log.read(),flush=True)
            raise
    assert host_identity()==identity
    print('PASS: second daemon refused, concurrent CAS, disconnect after commit, SIGKILL during partial HTB install, clean restart, veth deletion during sampling, stale link alias and foreign filter rejected, ambiguous ACK restored, SIGKILL during verified-scope write, corrupt journal, shutdown error, concurrent stale proposals')


if __name__=='__main__':
    if len(sys.argv)>1 and sys.argv[1]=='--daemon':
        sys.exit(test_daemon(Path(sys.argv[2]),sys.argv[3]))
    main()
