"""Repeatable live comparisons for the managed controller."""
from paths import BINDIR, RESULTS
import argparse
import json
import os
import random
import subprocess
import time
from pathlib import Path

from benchmark import ROOT, TC, owned_directory, receiver_metrics, run
from cli import request
from daemon import SOCKET
from observer_perf import await_service, command, stop
from service_config import load

MODES = ('linux', 'fifo', 'fq_codel', 'cake', 'static', 'adaptive', 'shadow')


def configure_baseline(mode, sender, capacity):
    def tc(*args):
        return run(['nsenter', '-t', sender, '-n', TC, *args])
    if mode not in ('linux', 'fifo', 'fq_codel'):
        return
    tc('qdisc', 'del', 'dev', 'fc0', 'root')
    if mode == 'linux':
        return
    rate = f'{capacity}kbit'
    tc('qdisc', 'add', 'dev', 'fc0', 'root', 'handle', '1:', 'htb', 'default', '20')
    tc('class', 'add', 'dev', 'fc0', 'parent', '1:', 'classid', '1:20', 'htb', 'rate', rate, 'ceil', rate)
    tc('qdisc', 'add', 'dev', 'fc0', 'parent', '1:20', 'pfifo' if mode == 'fifo' else 'fq_codel', 'limit', '1000')


def await_ready(server, path):
    deadline = time.monotonic()+5
    while 'ready' not in path.read_text():
        if server.poll() is not None or time.monotonic() > deadline:
            raise RuntimeError('TCP server did not start')
        time.sleep(.05)


def evaluate(output, mode, duration, rate, capacity, static_share=40, rates=None):
    owned_directory(output)
    cfg = load(ROOT/'config/service.json')
    cfg['lab']['bandwidth_kbit'] = capacity
    cfg['telemetry_seconds'] = 1
    cfg['observability']['bpf'] = 'off'
    cfg['adaptive'].update(window_seconds=2, cooldown_seconds=2, minimum_samples=20)
    if mode == 'static':
        cfg['policies']['default']['classes']['critical']['min_pct'] = static_share
        cfg['policies']['default']['classes']['background']['min_pct'] = 60-static_share
    if mode == 'cake':
        cfg['policies']['default'] = cfg['policies']['fair']
    cfg_path = output/'config.json'
    cfg_path.write_text(json.dumps(cfg, indent=2))
    server = client = daemon = None
    started_unix = time.time()
    with (output/'daemon.log').open('w') as dl, (output/'server.log').open('w') as sl, (output/'client.log').open('w') as cl:
        try:
            daemon = subprocess.Popen([BINDIR/'flowcored', '--config', cfg_path], stdout=dl, stderr=dl)
            await_service(daemon)
            request('lab_up')
            topology = request('inspect')['namespaces']
            sender, receiver = topology
            configure_baseline(mode, sender, capacity)
            if mode != 'cake':
                request('intent_register', intent={'name': 'demo', 'target_p99_ms': 15})
            if mode in ('adaptive', 'shadow'):
                request('controller_start' if mode == 'adaptive' else 'controller_shadow')
            args = ['--duration', str(duration), '--rate', str(rate)]
            server = subprocess.Popen(command(receiver, 'server', output/'receiver.jsonl', args), stdout=sl, stderr=sl)
            await_ready(server, output/'server.log')
            if rates:
                args += ['--rates', *(str(rate) for rate in rates)]
            before = request('observe')
            client = subprocess.Popen(command(sender, 'client', output, [*args, '--report-service', 'demo']),
                                      stdout=cl, stderr=cl)
            rc = client.wait(timeout=duration+18)
            if rc not in (0, 2):
                raise RuntimeError(f'client failed with exit {rc}')
            time.sleep(.2)
            after = request('observe')
            state = request('controller_status')
            inspect = request('inspect')
            history = [row for row in request('policy_history') if row['time_unix'] >= started_unix]
            if inspect['namespaces'] != topology:
                raise RuntimeError('controller replaced the namespaces')
            data = json.loads((output/'client.json').read_text())
            data.update(receiver_metrics(output/'receiver.jsonl', data['start'], data['end']))
            cpu = sum(after['overhead'][field]-before['overhead'][field]
                      for field in ('daemon_cpu_seconds', 'child_cpu_seconds'))
            actions = [row for row in history if row['result'] in ('pending', 'verified', 'hypothetical', 'rolled_back', 'kept')]
            data.update(mode=mode, capacity_kbit=capacity, daemon_cpu_seconds=cpu,
                        active_adjustments=sum(row['result'] in ('pending', 'verified') for row in actions),
                        shadow_actions=sum(row['result']=='hypothetical' for row in actions),
                        retained=sum(row['result']=='kept' for row in actions),
                        rollbacks=sum(row['result']=='rolled_back' for row in actions),
                        controller_error=state['error'],
                        first_measurement_seconds=next((row['time_unix']-actions[0]['time_unix']
                            for row in actions if row['result'] in ('kept','rolled_back')), None))
            for name, obj in (('result', data), ('decisions', history), ('controller', state), ('network', inspect),
                              ('observation', after)):
                (output/(name+'.json')).write_text(json.dumps(obj, indent=2)+'\n')
            print(f"{output.name}: p99={data['p99_ms']} ms, bg={data['background_mbps']:.2f} Mbit/s, "
                  f"actions={data['active_adjustments']}, kept={data['retained']}, error={state['error']}", flush=True)
            return data
        finally:
            stop(client)
            stop(server)
            stop(daemon)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--duration', type=int, default=14)
    parser.add_argument('--rate', type=int, default=100)
    parser.add_argument('--capacity', type=int, default=10000)
    parser.add_argument('--repetitions', type=int, default=2)
    parser.add_argument('--modes', nargs='+', choices=MODES, default=MODES)
    parser.add_argument('--rates', type=int, nargs='+')
    parser.add_argument('--tune-static', action='store_true')
    args = parser.parse_args()
    if os.geteuid() != 0 or SOCKET.exists():
        parser.error('requires sudo and a stopped daemon')
    if not 8 <= args.duration <= 120 or not 10 <= args.rate <= 500 or not 1000 <= args.capacity <= 1000000 or not 1 <= args.repetitions <= 10:
        parser.error('invalid duration, rate, capacity or repetition count')
    output = RESULTS/f'adaptive-{time.time_ns()}'
    owned_directory(output)
    order = [(i, mode) for i in range(args.repetitions) for mode in args.modes]
    random.Random(20261105).shuffle(order)
    (output/'plan.json').write_text(json.dumps(dict(seed=20261105, order=order, **vars(args)), indent=2))
    print(f'Evidence: {output}', flush=True)
    static_share = 40
    if args.tune_static:
        candidates = [(share, evaluate(output/f'pilot-static{share}', 'static',
                                       args.duration, args.rate, args.capacity, share, args.rates))
                      for share in (20, 30, 40)]
        eligible = [(share, row) for share, row in candidates if row['failed'] == 0
                    and row['p99_ms'] is not None and row['background_mbps'] >= args.capacity*.0002
                    and row['jain_index'] >= .8]
        if not eligible:
            raise RuntimeError('no static pilot meets the experiment gates')
        static_share = min(eligible, key=lambda pair: pair[1]['p99_ms'])[0]
        (output/'selected-static.json').write_text(json.dumps(dict(critical_percent=static_share)))
    results = []
    for i, mode in order:
        results.append(evaluate(output/f'{i}-{mode}', mode, args.duration, args.rate, args.capacity, static_share, args.rates))
        (output/'summary.json').write_text(json.dumps(results, indent=2)+'\n')


if __name__ == '__main__':
    main()
