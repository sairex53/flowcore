#!/usr/bin/env python3
"""Isolated, repeatable namespace experiment. Run from the project root as root."""
import argparse
import hashlib
import json
import os
import random
import signal
import subprocess
import time
from pathlib import Path

from paths import BINDIR, RESULTS, ROOT, TC
ENV = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
if (ROOT/'tools/root/usr/lib64/tc').is_dir():
    ENV['TC_LIB_DIR'] = str(ROOT/'tools/root/usr/lib64/tc')
UID = int(os.environ.get('SUDO_UID', '0'))
GID = int(os.environ.get('SUDO_GID', '0'))
MODES = ['fifo', 'fq_codel', 'cake', 'static10', 'static20', 'static40', 'flowcore']


def run(argv):
    return subprocess.run([str(arg) for arg in argv], check=True, capture_output=True,
                          text=True, env=ENV, timeout=10).stdout


def owned_directory(path):
    path.mkdir()
    os.chown(path, UID, GID)


class Lab:
    def __init__(self, output):
        self.output = output
        self.processes = []
        self.holders = []
        self.files = []

    def close(self):
        # Worker groups first, namespace holders last. No host interface is created.
        for proc in list(reversed(self.processes)) + list(reversed(self.holders)):
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        for proc in self.processes + self.holders:
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
        for stream in self.files:
            stream.close()

    def ns(self, index, argv):
        return ['nsenter', '--target', str(self.holders[index].pid), '--net', '--'] + [str(arg) for arg in argv]

    def command(self, index, argv):
        command = self.ns(index, argv)
        with (self.output / 'commands.jsonl').open('a') as stream:
            stream.write(json.dumps(command) + '\n')
        return run(command)

    def holder_command(self):
        return ['unshare', '--net', '--', 'sleep', '3600']

    def start(self):
        for _ in range(2):
            proc = subprocess.Popen(self.holder_command(), start_new_session=True)
            self.holders.append(proc)
            for attempt in range(100):
                if proc.poll() is not None:
                    raise RuntimeError('network namespace creation failed')
                if os.stat(f'/proc/{proc.pid}/ns/net').st_ino != os.stat('/proc/self/ns/net').st_ino:
                    break
                time.sleep(.01)
            else:
                raise RuntimeError('network namespace creation timed out')
        (self.output / 'namespace-pids.json').write_text(json.dumps([p.pid for p in self.holders]))
        self.command(0, ['ip', 'link', 'add', 'fc0', 'type', 'veth', 'peer', 'name', 'fc1', 'netns', self.holders[1].pid])
        for index, interface, address in [(0, 'fc0', '10.203.0.1/30'), (1, 'fc1', '10.203.0.2/30')]:
            self.command(index, ['ip', 'link', 'set', 'lo', 'up'])
            self.command(index, ['ip', 'addr', 'add', address, 'dev', interface])
            self.command(index, ['ip', 'link', 'set', interface, 'mtu', '1500', 'up'])
            self.command(index, ['ethtool', '-K', interface, 'tso', 'off', 'gso', 'off', 'gro', 'off'])
            (self.output / f'offloads-{interface}.txt').write_text(self.command(index, ['ethtool', '-k', interface]))
        (self.output / 'tcp.txt').write_text(self.command(0, ['sysctl', 'net.ipv4.tcp_congestion_control', 'net.ipv4.tcp_ecn']))
        (self.output / 'rtnetlink.json').write_text(self.command(0, [BINDIR/'flowcore', '--link', 'fc0']))

    def tc(self, *args):
        return self.command(0, [TC, *args])

    def configure(self, mode):
        if mode == 'cake':
            self.tc('qdisc', 'add', 'dev', 'fc0', 'root', 'handle', '1:', 'cake', 'bandwidth', '20mbit', 'besteffort', 'flows', 'rtt', '100ms')
            return
        self.tc('qdisc', 'add', 'dev', 'fc0', 'root', 'handle', '1:', 'htb', 'default', '20')
        self.tc('class', 'add', 'dev', 'fc0', 'parent', '1:', 'classid', '1:1', 'htb', 'rate', '20mbit', 'ceil', '20mbit', 'burst', '16k', 'cburst', '16k')
        classified = mode.startswith('static') or mode == 'flowcore'
        share = int(mode.removeprefix('static')) if mode.startswith('static') else 20
        classes = [(10, share, 0 if mode.startswith('static') else 2), (20, 100 - share, 2)] if classified else [(20, 100, 2)]
        for classid, percent, priority in classes:
            self.tc('class', 'add', 'dev', 'fc0', 'parent', '1:1', 'classid', f'1:{classid}', 'htb', 'rate', f'{200 * percent}kbit', 'ceil', '20mbit', 'prio', str(priority), 'quantum', '1514', 'burst', '16k', 'cburst', '16k')
            leaf = ['pfifo', 'limit', '1000'] if mode == 'fifo' else ['fq_codel', 'limit', '1000', 'target', '5ms', 'interval', '100ms']
            self.tc('qdisc', 'add', 'dev', 'fc0', 'parent', f'1:{classid}', 'handle', f'{classid}:', *leaf)
        if classified:
            self.tc('filter', 'add', 'dev', 'fc0', 'parent', '1:', 'protocol', 'ip', 'pref', '10', 'flower', 'ip_proto', 'tcp', 'dst_port', '9000', 'classid', '1:10')

    def snapshot(self, label):
        for kind in ['qdisc', 'class', 'filter']:
            (self.output / f'{label}-{kind}.json').write_text(self.tc('-s', '-j', kind, 'show', 'dev', 'fc0'))
        (self.output / f'{label}-link.json').write_text(self.command(0, [BINDIR/'flowcore', '--link', 'fc0']))

    def spawn(self, index, argv, name, *, privileged=False, stdin=None, stdout=None):
        if not privileged:
            argv = ['setpriv', f'--reuid={UID}', f'--regid={GID}', '--clear-groups', '--', *argv]
        error = (self.output / f'{name}.stderr').open('w')
        self.files.append(error)
        if stdout is None:
            stdout = (self.output / f'{name}.stdout').open('w')
            self.files.append(stdout)
        proc = subprocess.Popen(self.ns(index, argv), stdin=stdin, stdout=stdout, stderr=error,
                                env=ENV, start_new_session=True)
        self.processes.append(proc)
        return proc


def receiver_metrics(path, start, end):
    records = [json.loads(line) for line in path.read_text().splitlines()]
    def interpolate(when):
        for a, b in zip(records, records[1:]):
            if a['time'] <= when <= b['time']:
                fraction = (when - a['time']) / (b['time'] - a['time'])
                return [left + fraction * (right - left) for left, right in zip(a['bytes'], b['bytes'])]
        raise RuntimeError('receiver sampling does not bracket measurement interval')
    a, b = interpolate(start), interpolate(end)
    rates = [(right - left) * 8 / (end - start) / 1e6 for left, right in zip(a, b)]
    denominator = len(rates) * sum(value * value for value in rates)
    return {'background_mbps': sum(rates), 'flow_mbps': rates,
            'jain_index': sum(rates) ** 2 / denominator if denominator else 0}


def trial(parent, mode, duration, label):
    output = parent / label
    owned_directory(output)
    lab = Lab(output)
    try:
        lab.start()
        lab.configure(mode)
        lab.snapshot('before')
        server = lab.spawn(1, ['python3', ROOT / 'scripts/workload.py', 'server', output / 'receiver.jsonl'], 'server')
        for _ in range(100):
            if (output / 'server.stdout').read_text().strip() == 'ready':
                break
            if server.poll() is not None:
                raise RuntimeError('server exited during startup')
            time.sleep(.05)
        else:
            raise RuntimeError('server startup timed out')
        controller = None
        if mode == 'flowcore':
            controller = lab.spawn(0, [BINDIR/'flowcore', '--config', ROOT / 'config/lab.conf'], 'controller', privileged=True, stdin=subprocess.PIPE)
        client = lab.spawn(0, ['python3', ROOT / 'scripts/workload.py', 'client', output,
                              '--duration', duration], 'client', stdout=controller.stdin if controller else subprocess.DEVNULL)
        if controller:
            controller.stdin.close()
        cpu_samples = []
        deadline = time.monotonic() + duration + 20
        while client.poll() is None:
            cpu_samples.append({'time': time.monotonic(),
                                'cpu': Path('/proc/stat').read_text().splitlines()[0],
                                'loadavg': Path('/proc/loadavg').read_text().strip()})
            if time.monotonic() > deadline:
                raise TimeoutError('client exceeded experiment deadline')
            time.sleep(.25)
        rc = client.returncode
        (output / 'cpu.json').write_text(json.dumps(cpu_samples, indent=2))
        time.sleep(.2)
        lab.snapshot('after')
        if rc not in (0, 2) or server.poll() is not None:
            raise RuntimeError(f'workload failed, client exit={rc}')
        if controller and controller.wait(timeout=5):
            raise RuntimeError('controller failed')
        result = json.loads((output / 'client.json').read_text())
        result.update(receiver_metrics(output / 'receiver.jsonl', result['start'], result['end']))
        first = [int(x) for x in cpu_samples[0]['cpu'].split()[1:9]]
        last = [int(x) for x in cpu_samples[-1]['cpu'].split()[1:9]]
        delta = [right - left for left, right in zip(first, last)]
        result['host_cpu_busy_percent'] = 100 * (1 - (delta[3] + delta[4]) / sum(delta)) if sum(delta) else None
        result.update(mode=mode, trial=label, duration=duration, valid=(rc == 0))
        result['bandwidth_gate'] = result['background_mbps'] >= 10 and result['jain_index'] >= .8
        (output / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
        print(f"{label}: p99={result['p99_ms']:.2f} ms bg={result['background_mbps']:.2f} Mbit/s Jain={result['jain_index']:.3f}", flush=True)
        return result
    except Exception as error:
        result = {'mode': mode, 'trial': label, 'valid': False, 'error': str(error)}
        if isinstance(error, subprocess.CalledProcessError):
            result['stderr'] = error.stderr
        (output / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
        raise
    finally:
        lab.close()


def environment():
    files = [*ROOT.glob('src/*'), *ROOT.glob('scripts/*.py'), *ROOT.glob('include/flowcore/*'), *ROOT.glob('config/*'), BINDIR/'flowcore']
    return {'kernel': run(['uname', '-a']).strip(), 'tc': run([TC, '-V']).strip(),
            'compiler': run(['/usr/bin/g++', '--version']), 'cpu_count': os.cpu_count(),
            'cgroup': Path('/proc/self/cgroup').read_text(), 'cgroup_controllers': Path('/sys/fs/cgroup/cgroup.controllers').read_text(),
            'meminfo': Path('/proc/meminfo').read_text(), 'loadavg': Path('/proc/loadavg').read_text(),
            'source_sha256': {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--duration', type=float, default=12)
    parser.add_argument('--repetitions', type=int, default=3)
    parser.add_argument('--modes', nargs='+', choices=MODES, default=MODES)
    parser.add_argument('--seed', type=int, default=20261105)
    parser.add_argument('--no-pilot', action='store_true')
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('run with sudo for disposable network namespace setup')
    if not 1 <= args.duration <= 600 or not 1 <= args.repetitions <= 30:
        parser.error('duration 1..600 and repetitions 1..30 required')
    os.chdir(ROOT)
    output = RESULTS / f'{time.strftime("%Y%m%d-%H%M%S", time.gmtime())}-{os.getpid()}'
    owned_directory(output)
    (output / 'environment.json').write_text(json.dumps(environment(), indent=2))
    (output / 'arguments.json').write_text(json.dumps(vars(args), indent=2))
    print(f'Evidence: {output}', flush=True)
    pilot = []
    if not args.no_pilot:
        for mode in ['static10', 'static20', 'static40']:
            pilot.append(trial(output, mode, args.duration, f'pilot-{mode}'))
    eligible = [row for row in pilot if row['bandwidth_gate'] and row['valid']]
    selected = min(eligible, key=lambda row: row['p99_ms'])['mode'] if eligible else None
    (output / 'tuning.json').write_text(json.dumps({'selected': selected, 'pilot': pilot}, indent=2))
    order = [(repeat, mode) for repeat in range(args.repetitions) for mode in args.modes]
    random.Random(args.seed).shuffle(order)
    (output / 'order.json').write_text(json.dumps(order))
    results = []
    for repeat, mode in order:
        results.append(trial(output, mode, args.duration, f'r{repeat}-{mode}'))
        (output / 'summary.json').write_text(json.dumps({'selected_static': selected, 'results': results}, indent=2))
    print('Completed all trials', flush=True)


def interrupted(signum, frame):
    raise KeyboardInterrupt(f'signal {signum}')


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, interrupted)
    main()
