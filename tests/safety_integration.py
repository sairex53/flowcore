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
from benchmark import ENV
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
    tc, counters = ServiceLab.tc,ServiceLab.counters

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
        return result

    def collect(lab):
        boundary(lab,'before-counters')
        return counters(lab)

    ServiceLab.tc,ServiceLab.counters = install,collect
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
            proc.terminate()
            assert proc.wait(timeout=6)==0
            assert json.loads((work/'state/recovery.json').read_text())['clean_shutdown']
            wait(lambda:all(not Path(f'/proc/{pid}/ns/net').exists() for pid in (sender,receiver)))
        except BaseException:
            log.seek(0)
            print(log.read(),flush=True)
            raise
    assert host_identity()==identity
    print('PASS: second daemon refused, concurrent CAS, disconnect after commit, SIGKILL during partial HTB install, clean restart, veth deletion during sampling, stale link alias rejected')


if __name__=='__main__':
    if len(sys.argv)>1 and sys.argv[1]=='--daemon':
        sys.exit(test_daemon(Path(sys.argv[2]),sys.argv[3]))
    main()
