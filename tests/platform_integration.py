"""Privileged live platform tests; all mutated links belong to fresh namespaces."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import cli
from benchmark import TC, owned_directory
from observer_perf import await_service, stop
from platform_network import Inventory
from platform_policy import PolicyStore, load_json
from service_lab import ServiceLab


def check(condition,message):
    if not condition:
        raise AssertionError(message)


def test_interfaces(path):
    lab = ServiceLab(path)
    try:
        lab.start()
        lab.command(0,['ip','link','add','testa','type','veth','peer','name','testb','netns',lab.namespace_path(1)])
        for index,name,addr in ((0,'testa','10.205.0.1/30'),(1,'testb','10.205.0.2/30')):
            lab.command(index,['ip','addr','add',addr,'dev',name])
            lab.command(index,['ip','link','set',name,'up'])
        assert len(lab.namespace_fds) == 2
        for index, fd in enumerate(lab.namespace_fds):
            assert os.stat(lab.namespace_path(index)).st_ino == os.fstat(fd).st_ino
            assert os.fstat(fd).st_ino != os.stat('/proc/self/ns/net').st_ino
        lab.command(0,[TC,'qdisc','add','dev','testa','root','pfifo','limit','64'])
        inventory = Inventory(PolicyStore())
        def snapshot():
            with lab.enter(0):
                inventory.refresh()
            view = inventory.status()
            check(view['error'] is None,'inventory read failed')
            return view
        first = snapshot()
        check({row['name'] for row in first['interfaces'] if row['kind']=='veth' and row['available']}
              == {'fc0','testa'},'multiple active veth not discovered')
        check(next(row for row in first['interfaces'] if row['name']=='testa')['queue'] is not None,
              'RTM_GETQDISC did not return queue counters')
        lab.command(0,['ping','-c','2','-W','1','10.205.0.2'])
        traffic = snapshot()
        check(next(row for row in traffic['interfaces'] if row['name']=='testa')['path_quality']['tx_mbps']>0,
              'no observed traffic on second path')
        lab.command(0,['ip','link','set','testa','down'])
        snapshot()
        lab.command(0,['ip','link','set','testa','up'])
        recovered = snapshot()
        events = {event['event'] for event in recovered['events']}
        check({'link_unavailable','recovered','routes_changed'}<=events,'missing lifecycle notifications')
        (path/'interfaces.json').write_text(json.dumps(recovered,indent=2))
        lab.holders[1].terminate()
        lab.holders[1].wait(timeout=3)
        try:
            lab.namespace_path(1)
        except RuntimeError:
            pass
        else:
            raise AssertionError('dead namespace holder still accepted for commands')
    finally:
        lab.close()


def test_policy_restart(path):
    runtime = path/'runtime'
    state = path/'state'
    env = dict(os.environ,FLOWCORE_RUNTIME=str(runtime),FLOWCORE_STATE=str(state))
    cli.SOCKET = runtime/'control.sock'
    pids = []
    def daemon(log):
        proc = subprocess.Popen([ROOT/'build/flowcored'],env=env,stdout=log,stderr=log)
        await_service(proc)
        return proc
    with (path/'daemon.log').open('w') as log:
        proc = daemon(log)
        try:
            policy = load_json(ROOT/'config/platform-examples.json')
            result = cli.request('platform_commit',revision=0,policy=policy)
            check(result['changed'] and result['revision']==1,'profile commit failed')
            check(not cli.request('platform_commit',revision=1,policy=policy)['changed'],'commit not idempotent')
            cli.request('lab_up')
            pids = cli.request('inspect')['namespaces']
            result = cli.request('platform_apply_intent',service='api')
            check(result['provenance']['target_p99_ms']=='service','policy precedence lost')
            cli.request('controller_start',strategy='static')
            deadline = time.monotonic()+7
            while time.monotonic()<deadline:
                if cli.request('inspect')['tc']['traffic_classes']['critical']['ceil']==10000*125:
                    break
                time.sleep(.1)
            else:
                raise AssertionError('verified inherited ceiling not applied')
        finally:
            proc.kill()
            proc.wait(timeout=8)
        deadline = time.monotonic()+5
        while time.monotonic()<deadline and any(Path(f'/proc/{pid}/ns/net').exists() for pid in pids):
            time.sleep(.1)
        check(all(not Path(f'/proc/{pid}/ns/net').exists() for pid in pids),'crash leaked owned namespace')
        proc = daemon(log)
        try:
            check(cli.request('platform_policies')['revision']==1,'profile revision lost after restart')
            check(not cli.request('status')['lab_up'],'restart recreated stale lab')
            check(cli.request('controller_status')['mode']=='off','restart resumed stale actuation')
            check(not cli.request('intent_list'),'temporary registration survived restart')
            (path/'recovered.json').write_text(json.dumps(cli.request('platform_policies'),indent=2))
        finally:
            stop(proc)


if __name__=='__main__':
    if os.geteuid()!=0:
        raise SystemExit('run platform integration with sudo')
    path = ROOT/'results'/f'platform-integration-{time.time_ns()}'
    owned_directory(path)
    test_interfaces(path)
    test_policy_restart(path)
    print(f'PASS: two live interfaces, route/link events, verified inherited HTB limit, crash recovery. {path}')
