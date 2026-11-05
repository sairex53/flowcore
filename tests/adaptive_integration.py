"""Privileged checks of the adaptive daemon on disposable namespace links."""
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
from adaptive_experiment import await_ready
from benchmark import TC, owned_directory, receiver_metrics, run
from cli import request
from daemon import SOCKET
from observer_perf import await_service, command, stop
from service_config import load


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def eventually(check, timeout=10):
    deadline = time.monotonic()+timeout
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(.1)
    raise TimeoutError('adaptive condition did not converge')


def expect_error(operation, **fields):
    try:
        request(operation, **fields)
    except ValueError:
        return
    raise AssertionError(f'{operation} should have failed')


def live_client(sender, output, options):
    owned_directory(output)
    with (output/'stdout').open('w') as out:
        proc = subprocess.run(command(sender, 'client', output, options), stdout=out, stderr=out,
                              timeout=25)
    require(proc.returncode in (0, 2), 'workload infrastructure failed')
    return json.loads((output/'client.json').read_text())


def main():
    require(os.geteuid() == 0 and not SOCKET.exists(), 'requires sudo and a stopped daemon')
    output = ROOT/'results'/f'adaptive-integration-{time.time_ns()}'
    owned_directory(output)
    cfg = load(ROOT/'config/service.json')
    cfg['telemetry_seconds'] = 1
    cfg['observability']['bpf'] = 'off'
    cfg['adaptive'].update(window_seconds=2, cooldown_seconds=2)
    path = output/'config.json'
    path.write_text(json.dumps(cfg))
    server = daemon = load_client = None
    pids = []
    with (output/'daemon.log').open('w') as dl, (output/'server.log').open('w') as sl:
        try:
            daemon = subprocess.Popen([ROOT/'build/flowcored', '--config', path], stdout=dl, stderr=dl)
            await_service(daemon)
            request('lab_up')
            sender, receiver = request('inspect')['namespaces']
            pids += [sender, receiver]
            server = subprocess.Popen(command(receiver, 'server', output/'receiver.jsonl', []),
                                      stdout=sl, stderr=sl)
            await_ready(server, output/'server.log')
            subprocess.run(['nsenter', '-t', str(sender), '-n', ROOT/'build/flowcore-sdk-request', SOCKET],
                           check=True, timeout=8)
            require('sdk_demo' not in request('intent_list'), 'SDK instance was not released')

            base_intent = dict(name='demo', min_kbit=6000, max_kbit=9000, background_min_pct=25)
            first = request('intent_register', intent=base_intent)
            second = request('intent_register', intent=base_intent)
            require(first['instance_token'] != second['instance_token'], 'instance tokens collided')
            expect_error('intent_register', intent=dict(base_intent, name='conflict'))
            expect_error('intent_register', intent=dict(base_intent, max_kbit=8000))
            expect_error('intent_remove', name='demo', instance_token='wrong')
            request('intent_remove', name='demo', instance_token=first['instance_token'])
            require(len(request('intent_inspect', name='demo')['instances']) == 1, 'instance isolation failed')
            request('controller_shadow')
            proposed = request('policy_evaluate')
            require(proposed['proposed']['critical'] == [6000, 9000], 'dry run ceiling proposal missing')
            def ceil():
                return request('inspect')['tc']['traffic_classes']['critical']['ceil'] // 125
            eventually(lambda: request('policy_explain') and request('policy_explain')['result']=='hypothetical')
            require(ceil() == 20000, 'shadow changed kernel state')
            request('controller_start', strategy='static')
            eventually(lambda: ceil() == 9000)
            require(request('inspect')['namespaces'] == [sender, receiver], 'in-place update replaced namespaces')

            critical = live_client(sender, output/'critical-only', ['--duration','2','--rate','40','--streams','0',
                                                                    '--report-service','demo'])
            require(critical['completed']==80 and critical['application_reports']['accepted']==80,
                    'critical-only TCP test failed')
            background = live_client(sender, output/'background-only',
                                     ['--duration','2','--rates','0','--report-service','demo'])
            time.sleep(.2)
            bg = receiver_metrics(output/'receiver.jsonl', background['start'], background['end'])
            require(background['offered']==0 and not background['background_errors']
                    and bg['background_mbps']>0 and bg['jain_index']>.8, 'background-only test failed')
            mixed = live_client(sender, output/'mixed-switching', ['--duration','8','--rates','40','140','70','120',
                                                                  '--report-service','demo'])
            require(mixed['offered']==740 and mixed['completed']>0, 'mixed switching workload failed')
            require(ceil()==9000, 'hard class ceiling drifted under load')
            live_background = output/'remove-under-load'
            owned_directory(live_background)
            with (live_background/'stdout').open('w') as lb:
                load_client = subprocess.Popen(command(sender, 'client', live_background,
                    ['--duration','8','--rates','0']), stdout=lb, stderr=lb)
            time.sleep(3.2)
            require(load_client.poll() is None, 'background exited before removal')
            request('intent_remove', name='demo', instance_token=second['instance_token'])
            eventually(lambda: ceil()==20000)
            require(not request('intent_list'), 'last instance removal leaked its lease')
            request('controller_stop')

            request('controller_start')
            run(['nsenter','-t',sender,'-n',TC,'class','change','dev','fc0','parent','1:',
                 'classid','1:1','htb','rate','16000kbit','ceil','16000kbit'])
            drift = eventually(lambda: (row if (row:=request('controller_status'))['mode']=='off' and row['error'] else None))
            require('drift' in drift['error'], 'capacity drift was not detected')
            parent = next(row for row in request('inspect')['tc']['class'] if row['handle']=='1:1')
            require(parent['rate']==16000*125, 'controller overwrote externally changed capacity')
            require(load_client.wait(timeout=15)==0, 'bandwidth reduction disrupted background workload')
            stop(server)
            request('lab_configure', profile=dict(cfg['lab'], bandwidth_kbit=16000))
            sender, receiver = request('inspect')['namespaces']
            pids += [sender, receiver]
            request('controller_start')
            os.kill(sender, signal.SIGTERM)
            eventually(lambda: not request('status')['lab_up'])
            require(request('controller_status')['mode']=='off', 'controller survived lost namespace')

            request('intent_register', intent={'name':'short_lease','ttl_seconds':1})
            eventually(lambda: not request('intent_list'), timeout=4)
        finally:
            stop(load_client)
            stop(server)
            stop(daemon)
    require(all(not Path(f'/proc/{pid}/ns/net').exists() for pid in pids), 'namespace holder leaked')
    missing = subprocess.run([ROOT/'build/flowcore-sdk-request', SOCKET], capture_output=True, timeout=2)
    require(missing.returncode == 1, 'SDK did not fail cleanly without daemon')
    print(f'PASS: SDK, class readback, dry-run, shadow, mixed workloads, expiry, conflict, capacity drift and cleanup. {output}')


if __name__ == '__main__':
    main()
