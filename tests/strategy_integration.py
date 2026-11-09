"""Verify a real canary class ceiling and its compensation on owned HTB."""
import json
import os
from pathlib import Path
import subprocess
import time

from adaptive_integration import await_ready, await_service, command, eventually, require, stop
from cli import request
from daemon import SOCKET
from service_config import load
from benchmark import owned_directory

ROOT = Path(__file__).resolve().parents[1]


def main():
    require(os.geteuid() == 0 and not SOCKET.exists(), 'requires sudo and a stopped daemon')
    output = ROOT/'results'/f'strategy-integration-{time.time_ns()}'
    owned_directory(output)
    config = load(ROOT/'config/service.json')
    config['lab']['bandwidth_kbit'] = 12000
    config['telemetry_seconds'] = 1
    config['observability'].update(bpf='off', max_sockets=1024)
    config['adaptive']['window_seconds'] = 2
    config_path = output/'config.json'
    config_path.write_text(json.dumps(config))
    daemon = server = client = None
    holders = []
    with (output/'daemon.log').open('w') as dl, (output/'server.log').open('w') as sl, (output/'client.log').open('w') as cl:
        try:
            daemon = subprocess.Popen([ROOT/'build/flowcored', '--research', '--config', config_path], stdout=dl, stderr=dl)
            await_service(daemon)
            request('lab_up')
            holders = request('inspect')['namespaces']
            sender, receiver = holders
            server = subprocess.Popen(command(receiver, 'server', output/'receiver.jsonl', []), stdout=sl, stderr=sl)
            await_ready(server, output/'server.log')
            request('intent_register', intent=dict(name='demo', priority='critical', max_kbit=6000, ttl_seconds=45))

            def ceiling():
                return request('inspect')['tc']['traffic_classes']['critical']['ceil']//125

            require(ceiling() == 12000, 'unexpected baseline ceiling')
            request('strategy_shadow', names=['static', 'queue-aware'], seconds=25)
            owned_directory(output/'client')
            client = subprocess.Popen(command(sender, 'client', output/'client', [
                '--duration','18','--rate','34','--streams','6','--payload','4096',
                '--deadline-ms','200','--report-service','demo']), stdout=cl, stderr=cl)
            def shadow_proposal():
                proposal = request('strategy_inspect')['shadow'].get('queue-aware', {})
                return proposal if proposal.get('action') == 'enforce_class_ceiling' else None

            eventually(shadow_proposal, timeout=5)
            require(ceiling() == 12000, 'shadow wrote the proposed ceiling')
            limits = dict(minimum_samples=24, max_failure_ratio=.05, max_p99_ms=500,
                          p99_regression=.5, max_deadline_miss_ratio=.5,
                          minimum_background_kbit=1000, background_floor_pct=20)
            request('strategy_activate', name='queue-aware', scope='owned-fc0',
                    seconds=13, guards=limits, confirm=True)
            eventually(lambda: ceiling() == 6000, timeout=10)
            require(request('inspect')['namespaces'] == holders, 'canary replaced namespaces')
            require(request('strategy_inspect')['state'] == 'CANARY', 'ceiling changed outside canary')
            outcome = request('strategy_rollback')
            (output/'strategy.json').write_text(json.dumps(outcome, indent=2)+'\n')
            require(outcome['state'] == 'RECOVERING' and outcome['history'][-1]['verified'],
                    'canary rollback was not verified')
            require(ceiling() == 12000, 'rollback did not restore kernel ceiling')
        finally:
            stop(client)
            stop(server)
            stop(daemon)
    require(all(not Path(f'/proc/{pid}/ns/net').exists() for pid in holders), 'namespace holder leaked')
    print(f'PASS: shadow did not write, canary ceiling 12000 -> 6000 -> 12000 kbit/s, same namespaces, cleanup. {output}')


if __name__ == '__main__':
    main()
