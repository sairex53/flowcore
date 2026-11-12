"""Privileged integration. Uses disposable lab interfaces only."""
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
BUILD = Path(os.environ.get('FLOWCORE_BUILD_DIR', ROOT / 'build')).resolve()
sys.path.insert(0, str(ROOT / 'scripts'))
from benchmark import owned_directory, run
from cli import request
from daemon import Service
from service_config import load
from service_lab import ServiceLab


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def await_status(predicate, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            status = request('status')
            if predicate(status):
                return status
        except OSError:
            pass
        time.sleep(.1)
    raise TimeoutError('expected daemon state was not reached')


def cli(*args, code=0):
    result = subprocess.run([BUILD / 'flowcore', *args], text=True, capture_output=True, timeout=30)
    check(result.returncode == code, f'{args}: {result.returncode} {result.stderr}')
    return json.loads(result.stdout) if code == 0 else result.stderr


def workload(output, sender, receiver, port):
    directory = output / str(port)
    owned_directory(directory)
    def command(pid, mode, target):
        user = os.environ.get('SUDO_UID', '0')
        group = os.environ.get('SUDO_GID', '0')
        return ['nsenter', '-t', str(pid), '-n', 'setpriv', '--reuid', user, '--regid', group,
                '--clear-groups', sys.executable, str(ROOT / 'scripts/workload.py'), mode, str(target),
                '--request-port', str(port), '--payload', '768', '--streams', '2', '--duration', '2', '--rate', '30']
    with (directory / 'server.log').open('w+') as log:
        server = subprocess.Popen(command(receiver, 'server', directory / 'receiver.jsonl'), stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + 5
            while 'ready' not in (directory / 'server.log').read_text():
                check(server.poll() is None and time.monotonic() < deadline, 'server not ready')
                time.sleep(.05)
            with (directory / 'client.log').open('w') as client_log:
                subprocess.run(command(sender, 'client', directory), stdout=client_log,
                               stderr=client_log, check=True, timeout=15)
            data = json.loads((directory / 'client.json').read_text())
            check(data['completed'] == 60 and data['failed'] == 0, 'request accounting')
            rows = [json.loads(line) for line in (directory / 'receiver.jsonl').read_text().splitlines()]
            check(all(n > 0 for n in rows[-1]['bytes']), 'background streams made no progress')
        finally:
            if server.poll() is None:
                server.terminate()
            server.wait(timeout=5)


def main():
    check(os.geteuid() == 0, 'run privileged integration with sudo')
    output = ROOT / 'results' / f'service-test-{time.time_ns()}'
    owned_directory(output)
    log = (output / 'daemon.jsonl').open('w')
    proc = subprocess.Popen([BUILD / 'flowcored'], stdout=log, stderr=log)
    pids = []
    try:
        await_status(lambda status: not status['lab_up'])
        check(cli('lab', 'up')['changed'], 'lab startup did not apply policy')
        state = cli('lab', 'inspect')
        pids.extend(state['namespaces'])
        check(not cli('lab', 'up')['changed'], 'identical config replaced the topology')
        check(cli('lab', 'inspect')['namespaces'] == state['namespaces'], 'idempotence changed PIDs')
        check('unknown policy' in cli('policy', 'apply', 'missing', code=1), 'invalid policy error')
        net = cli('interfaces')
        check(len(net['lab']) == 2, 'missing topology')
        for index, name in enumerate(('fc0', 'fc1')):
            link = next(link for link in net['lab'][index]['interfaces'] if link['name'] == name)
            check(any(addr['address'].startswith('fd42:') for addr in link['addresses']), 'missing IPv6')
            check(net['lab'][index]['routes'], 'missing routes')
        for port in (9000, 9002, 9003):
            workload(output, *state['namespaces'], port)
        stats = cli('lab', 'inspect')['tc']
        for name, row in stats['traffic_classes'].items():
            check(row['stats']['bytes'] > 0, f'no traffic in {name}')
        check(len(stats['traffic_classes']) == 4, 'missing class counters')
        receiver = str(state['namespaces'][1])
        run(['nsenter', '-t', receiver, '-n', 'ip', 'link', 'set', 'fc1', 'down'])
        run(['nsenter', '-t', receiver, '-n', 'ip', 'link', 'set', 'fc1', 'up'])
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            events = cli('metrics')['events']
            if any(row.get('source') == 'receiver' and row.get('up') is False for row in events):
                break
            time.sleep(.1)
        else:
            raise AssertionError('missing rtnetlink link-down notification')
        check(cli('policy', 'apply', 'fair')['capabilities']['cake'] == 'verified', 'CAKE unavailable')
        pids.extend(cli('lab', 'inspect')['namespaces'])
        cli('policy', 'apply', 'default')
        pids.extend(cli('lab', 'inspect')['namespaces'])
        cli('lab', 'configure', '--bandwidth-kbit', '16000', '--delay-ms', '12',
            '--jitter-ms', '2', '--loss-pct', '1', '--reorder-pct', '5')
        state = cli('lab', 'inspect')
        pids.extend(state['namespaces'])
        check(state['tc']['receiver_qdisc'][0]['kind'] == 'netem', 'missing netem')
        os.kill(state['namespaces'][0], signal.SIGTERM)
        await_status(lambda status: not status['lab_up'] and not status['healthy'])
        cli('lab', 'up')
        pids.extend(cli('lab', 'inspect')['namespaces'])
        check(not cli('lab', 'down')['lab_up'], 'lab did not clean up')
        cli('lab', 'up')
        pids.extend(cli('lab', 'inspect')['namespaces'])
    finally:
        if proc.poll() is None:
            proc.terminate()
        proc.wait(timeout=15)
        log.close()
    check(proc.returncode == 0, 'daemon shutdown failed')
    check(all(not Path(f'/proc/{pid}/ns/net').exists() for pid in pids), 'namespace cleanup failed')
    cli('status', code=3)

    with (output / 'crash.jsonl').open('w') as crash_log:
        crashed = subprocess.Popen([BUILD / 'flowcored'], stdout=crash_log, stderr=crash_log)
        try:
            await_status(lambda status: not status['lab_up'])
            cli('lab', 'up')
            holders = cli('lab', 'inspect')['namespaces']
        finally:
            crashed.kill()
            crashed.wait(timeout=5)
        deadline = time.monotonic() + 5
        while any(Path(f'/proc/{pid}/ns/net').exists() for pid in holders):
            check(time.monotonic() < deadline, 'holders survived daemon SIGKILL')
            time.sleep(.1)
        restarted = subprocess.Popen([BUILD / 'flowcored'], stdout=crash_log, stderr=crash_log)
        try:
            await_status(lambda status: not status['lab_up'])
            check(not list((ROOT / 'runtime').glob('lab-*')), 'stale workspace survived restart')
            cli('lab', 'up')
            cli('lab', 'down')
        finally:
            restarted.terminate()
            restarted.wait(timeout=10)
        check(restarted.returncode == 0, 'restart shutdown failed')

    service = Service(load(ROOT / 'config/service.json'))
    try:
        service.replace('default', service.profile)
        original = service.lab
        install = ServiceLab.install_policy
        def conflicting_root(candidate, policy, profile):
            candidate.tc('qdisc', 'add', 'dev', 'fc0', 'root', 'pfifo')
            install(candidate, policy, profile)
        with patch.object(ServiceLab, 'install_policy', conflicting_root):
            try:
                service.replace('fair', service.profile)
            except subprocess.CalledProcessError:
                pass
            else:
                raise AssertionError('expected real tc failure')
        check(service.lab is original and service.active == 'default', 'failed update replaced working lab')
        check(len(service.lab.counters()['traffic_classes']) == 4, 'rollback lost class configuration')
    finally:
        service.close_lab()
    print(f'PASS: Unix API, IPv4/IPv6 topology, four traffic classes, link events, CAKE, netem, recovery and rollback. {output}')


if __name__ == '__main__':
    main()
