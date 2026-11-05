"""Compare observer modes against the same live namespace TCP workload."""
from paths import BINDIR, RESULTS
import argparse
import json
import os
import random
import signal
import subprocess
import sys
import time
from pathlib import Path

from benchmark import ROOT, owned_directory, receiver_metrics, run
from cli import request
from daemon import SOCKET
from service_config import load


def await_service(proc):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError('flowcored exited during startup')
        try:
            return request('status')
        except OSError:
            time.sleep(.05)
    raise TimeoutError('flowcored startup timeout')


def stop(proc):
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def command(pid, mode, path, options):
    return ['nsenter', '-t', str(pid), '-n', sys.executable, '-B', ROOT / 'scripts/workload.py',
            mode, path, *options]


def cpu_ticks():
    fields = [int(x) for x in Path('/proc/stat').read_text().splitlines()[0].split()[1:9]]
    return sum(fields), fields[3] + fields[4]


def trial(output, mode, duration, rate, report=False):
    owned_directory(output)
    cfg = load(ROOT / 'config/service.json')
    cfg['telemetry_seconds'] = 1
    cfg['observability']['enabled'] = mode != 'off'
    cfg['observability']['bpf'] = 'auto' if mode == 'bpf' else 'off'
    (output / 'config.json').write_text(json.dumps(cfg, indent=2))
    server = daemon = client = None
    with (output / 'daemon.log').open('w') as daemon_log, (output / 'server.log').open('w') as server_log, (output / 'client.log').open('w') as client_log:
        try:
            daemon = subprocess.Popen([BINDIR/'flowcored', '--config', output / 'config.json'],
                                      stdout=daemon_log, stderr=daemon_log)
            await_service(daemon)
            request('lab_up')
            sender, receiver = request('inspect')['namespaces']
            args = ['--duration', str(duration), '--rate', str(rate)]
            server = subprocess.Popen(command(receiver, 'server', output / 'receiver.jsonl', args),
                                      stdout=server_log, stderr=server_log)
            deadline = time.monotonic() + 5
            while 'ready' not in (output / 'server.log').read_text():
                if server.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError('TCP server startup failed')
                time.sleep(.05)
            if mode == 'bpf':
                deadline = time.monotonic() + 5
                while not request('health')['backend']['active'] and time.monotonic() < deadline:
                    time.sleep(.05)
            before = request('observe')
            if report:
                args += ['--report-service', 'demo', '--deadline-ms', '20']
            start, ticks = time.monotonic(), cpu_ticks()
            client = subprocess.Popen(command(sender, 'client', output, args),
                                      stdout=client_log, stderr=client_log)
            first_connections = None
            # A single identical CLI poll in every mode, during the workload.
            time.sleep(2)
            first_connections = request('connections')
            rc = client.wait(timeout=duration + 15)
            end, end_ticks = time.monotonic(), cpu_ticks()
            time.sleep(.2)
            after = request('observe')
            events = request('events')
            if rc not in (0, 2):
                raise RuntimeError(f'TCP client exit {rc}')
            row = json.loads((output / 'client.json').read_text())
            row.update(receiver_metrics(output / 'receiver.jsonl', row['start'], row['end']))
            overhead = {}
            for key in ('daemon_cpu_seconds', 'child_cpu_seconds', 'bpf_cpu_seconds', 'polling_cpu_seconds'):
                a, b = before['overhead'].get(key), after['overhead'].get(key)
                overhead[key] = b-a if a is not None and b is not None else None
            all_ticks = end_ticks[0] - ticks[0]
            overhead['host_cpu_busy_ratio'] = 1 - (end_ticks[1] - ticks[1]) / all_ticks if all_ticks else None
            overhead['wall_seconds'] = end-start
            overhead['daemon_rss_bytes'] = after['overhead']['daemon_rss_bytes']
            overhead['bpf_rss_bytes'] = after['overhead']['bpf_rss_bytes']
            row.update(mode=mode, backend=after['bpf'], overhead=overhead,
                       tcp_records=after['overhead']['records_processed'])
            for filename, data in (('observation.json', after), ('connections.json', first_connections),
                                   ('events.json', events), ('result.json', row)):
                (output / filename).write_text(json.dumps(data, indent=2) + '\n')
            print(f"{output.name}: p99_ms={row['p99_ms']} failed={row['failed']} "
                  f"bg={row['background_mbps']:.2f} backend={after['bpf']['active']}", flush=True)
            if report:
                if row.get('application_reports', {}).get('accepted', 0) == 0 or after['socket_count'] == 0:
                    raise RuntimeError('live application or INET_DIAG telemetry missing')
                (output / 'prometheus.txt').write_text(run([BINDIR/'flowcore', 'metrics', '--prometheus']))
                (output / 'connections.csv').write_text(run([BINDIR/'flowcore', 'connections', '--csv']))
                (output / 'events.jsonl').write_text(run([BINDIR/'flowcore', 'events', '--jsonl']))
                # A reset from a closed listening port should be visible on the sender.
                code = ("import errno,socket,time; s=socket.socket(); s.settimeout(2); start=time.monotonic_ns(); "
                        "assert s.connect_ex(('10.203.0.2',19999)) == errno.ECONNREFUSED; "
                        "print((time.monotonic_ns()-start)/1000)")
                duration_us = float(run(['nsenter', '-t', sender, '-n', sys.executable, '-c', code]))
                request('request_sample', service='demo', outcome='failure', duration_us=duration_us,
                        failure_reason='connect_refused')
                time.sleep(.2)
                refused = request('observe')
                (output / 'after-refused-connect.json').write_text(json.dumps(refused, indent=2))
                if refused['windows']['10']['application']['demo']['application']['failure_reasons']['connect_refused'] != 1:
                    raise RuntimeError('connect refusal report not counted')
                if after['bpf']['active']:
                    traced = refused['windows']['10']['traced']
                    hooks = after['bpf']['hooks']
                    if ('tcp_state' in hooks and not traced['state']) or ('tcp_reset' in hooks and not traced['receive_resets']):
                        raise RuntimeError('tracepoint state or reset event was not observed')
                    children = Path(f'/proc/{daemon.pid}/task/{daemon.pid}/children').read_text().split()
                    helpers = [int(pid) for pid in children if
                               (Path(f'/proc/{pid}/comm').read_text().strip() == 'flowcore-bpf')]
                    if len(helpers) != 1:
                        raise RuntimeError('could not identify owned collector process')
                    os.kill(helpers[0], signal.SIGTERM)
                    deadline = time.monotonic() + 4
                    while time.monotonic() < deadline:
                        health = request('health')
                        if not health['backend']['active'] and health['sample_age_seconds'] is not None:
                            (output / 'after-collector-exit.json').write_text(json.dumps(health, indent=2))
                            break
                        time.sleep(.05)
                    else:
                        raise RuntimeError('INET_DIAG fallback did not survive collector exit')

            return row
        finally:
            stop(client)
            stop(server)
            stop(daemon)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--duration', type=int, default=8)
    parser.add_argument('--rate', type=int, default=100)
    parser.add_argument('--repetitions', type=int, default=2)
    parser.add_argument('--seed', type=int, default=20261105)
    parser.add_argument('--verify-live', action='store_true', help='also send real application reports')
    parser.add_argument('--live-only', action='store_true', help='run only real telemetry verification')
    args = parser.parse_args()
    if os.geteuid() != 0 or SOCKET.exists():
        parser.error('run as root with the management daemon stopped')
    if not 4 <= args.duration <= 120 or not 1 <= args.rate <= 1000 or not 1 <= args.repetitions <= 10:
        parser.error('duration 4..120, rate 1..1000, repetitions 1..10')
    directory = RESULTS / f'observer-perf-{time.time_ns()}'
    owned_directory(directory)
    (directory / 'environment.json').write_text(json.dumps(dict(kernel=os.uname().release,
        architecture=os.uname().machine, cpu_count=os.cpu_count(), **vars(args)), indent=2))
    print(f'Evidence: {directory}', flush=True)
    order = [(repeat, mode) for repeat in range(args.repetitions) for mode in ('off', 'diag', 'bpf')]
    random.Random(args.seed).shuffle(order)
    if args.live_only:
        trial(directory / 'live', 'bpf', args.duration, args.rate, report=True)
        return
    rows = [trial(directory / f'{repeat}-{mode}', mode, args.duration, args.rate) for repeat, mode in order]
    if args.verify_live:
        trial(directory / 'live', 'bpf', args.duration, args.rate, report=True)
    (directory / 'summary.json').write_text(json.dumps(rows, indent=2) + '\n')


if __name__ == '__main__':
    main()
