#!/usr/bin/env python3
"""Run a reproducible IPv4 experiment matrix over Linux role workers."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import csv
import json
import os
from pathlib import Path, PurePosixPath
import random
import re
import select
import shlex
import signal
import statistics
import subprocess
import sys
import time
from types import SimpleNamespace

import adaptive
import intents
from multi_host_agent import (CLASSED, MAX_RESPONSE, MODES, address, integer,
                              number, ports, token)
from paths import ROOT

ROLES = ('client', 'server', 'shaper', 'bulk')
SERVICE_CLASSES = {'api': 'critical', 'interactive': 'interactive'}
HANDLES = {'critical': '10:', 'interactive': '11:', 'standard': '12:', 'background': '20:'}


def fields(value, allowed, required=()):
    if not isinstance(value, dict) or value.keys() - set(allowed) or set(required) - value.keys():
        raise ValueError(f'invalid fields; expected {", ".join(allowed)}')
    return value


def manifest(path):
    with path.open('rb') as stream:
        raw = stream.read(32769)
    if len(raw) > 32768:
        raise ValueError('manifest exceeds 32 KiB')
    cfg = fields(json.loads(raw), ('roles', 'owner', 'direction', 'capacity_kbit', 'ports',
                                   'critical_pct', 'workload', 'impairment'),
                 ('roles', 'owner', 'direction', 'capacity_kbit', 'ports', 'workload'))
    token(cfg['owner'])
    if cfg['direction'] not in ('forward', 'reverse'):
        raise ValueError('direction must be forward or reverse')
    integer(cfg['capacity_kbit'], 100, 1000000)
    integer(cfg.setdefault('critical_pct', 40), 10, 50)
    ports(cfg['ports'])
    roles = fields(cfg['roles'], ROLES, ('client', 'server'))
    for name, role in roles.items():
        fields(role, ('address', 'interface', 'ssh', 'checkout', 'namespace'), ('interface',))
        token(role['interface'], 15)
        if name != 'shaper':
            role['address'] = address(role['address'])
        if 'ssh' in role:
            if not isinstance(role['ssh'], str) or not re.fullmatch(
                    r'[A-Za-z0-9_][A-Za-z0-9_.@-]{0,127}', role['ssh']):
                raise ValueError('use an SSH config alias or user@hostname')
            checkout = role['checkout']
            if (not isinstance(checkout, str) or len(checkout) > 512 or
                    not PurePosixPath(checkout).is_absolute() or '..' in PurePosixPath(checkout).parts
                    or any(ord(char) < 32 for char in checkout)):
                raise ValueError('remote checkout must be an absolute path')
        elif 'checkout' in role:
            raise ValueError('checkout is only used for SSH roles')
        if 'namespace' in role:
            token(role['namespace'])
    if roles['client']['address'] == roles['server']['address']:
        raise ValueError('client and server data addresses must differ')
    work = fields(cfg['workload'], ('duration', 'api_rate', 'interactive_rate', 'concurrency',
                                    'deadline_ms', 'work_ms', 'bulk_bytes', 'bulk_timeout'),
                  ('duration', 'api_rate', 'interactive_rate', 'bulk_bytes'))
    integer(work['duration'], 2, 90)
    number(work['api_rate'], 1, 150)
    number(work['interactive_rate'], 1, 150)
    integer(work.setdefault('concurrency', 4), 1, 8)
    number(work.setdefault('deadline_ms', 100), 1, 3000)
    integer(work.setdefault('work_ms', 2), 0, 50)
    integer(work['bulk_bytes'], 1, 64 * 1024 * 1024)
    integer(work.setdefault('bulk_timeout', 30), 1, 100)
    if 'impairment' in cfg:
        imp = fields(cfg['impairment'], ('interface', 'after_seconds', 'delay_ms', 'loss_pct'),
                     ('interface', 'after_seconds', 'delay_ms', 'loss_pct'))
        if 'shaper' not in roles or token(imp['interface'], 15) == roles['shaper']['interface']:
            raise ValueError('impairment needs a second interface in the shaper namespace')
        number(imp['after_seconds'], 0, work['duration'] - 1)
        integer(imp['delay_ms'], 0, 2000)
        number(imp['loss_pct'], 0, 20)
    return cfg


def save(path, data):
    path.write_text(json.dumps(data, indent=2, allow_nan=False) + '\n')


class Peer:
    def __init__(self, name, role, owner, directory):
        self.name, self.role = name, role
        script = (PurePosixPath(role['checkout']) if 'ssh' in role else ROOT) / 'scripts/multi_host_agent.py'
        argv = ['python3', '-B', str(script), name, role['interface'], owner, '220']
        if 'namespace' in role:
            prefix = [] if 'ssh' not in role and os.geteuid() == 0 else ['sudo', '-n']
            argv = [*prefix, 'ip', 'netns', 'exec', role['namespace'], *argv]
        if 'ssh' in role:
            argv = ['ssh', '-T', '-oBatchMode=yes', '-oConnectTimeout=5',
                    '-oServerAliveInterval=5', '-oServerAliveCountMax=2',
                    role['ssh'], shlex.join(argv)]
        with (directory / f'{name}.stderr').open('wb') as errors:
            self.process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                            stderr=errors, bufsize=0)
        self.buffer = bytearray()

    def call(self, op, **args):
        start = time.monotonic() + 25
        self.process.stdin.write(json.dumps([op, args], allow_nan=False).encode() + b'\n')
        fd = self.process.stdout.fileno()
        while (end := self.buffer.find(b'\n')) == -1:
            remaining = start - time.monotonic()
            if remaining <= 0 or not select.select([fd], [], [], remaining)[0]:
                raise TimeoutError(f'{self.name} {op} response timed out')
            chunk = os.read(fd, 65536)
            if not chunk:
                raise RuntimeError(f'{self.name} {op} worker disconnected; see role stderr')
            self.buffer.extend(chunk)
            if len(self.buffer) > MAX_RESPONSE + 1:
                raise RuntimeError('oversized worker response')
        reply = json.loads(self.buffer[:end])
        del self.buffer[:end+1]
        if 'error' in reply:
            raise RuntimeError(f'{self.name} {op}: {reply["error"]}')
        return reply['ok']

    def clock(self):
        start_mono, start_wall = time.monotonic_ns(), time.time_ns()
        result = self.call('clock')
        end_mono, end_wall = time.monotonic_ns(), time.time_ns()
        return dict(result, coordinator_start_ns=start_mono, coordinator_end_ns=end_mono,
                    exchange_ns=end_mono-start_mono, coordinator_realtime_start_ns=start_wall,
                    coordinator_realtime_end_ns=end_wall)

    def close(self):
        self.process.stdin.close()
        try:
            status = self.process.wait(timeout=18)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=3)
            raise RuntimeError(f'{self.name} cleanup not acknowledged; inspect the dedicated namespace')
        finally:
            self.process.stdout.close()
        if status:
            raise RuntimeError(f'{self.name} cleanup or worker exit status {status}; see role stderr')


def close_peers(peers):
    failures = []
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(peer.close) for peer in peers.values()]
        for future in futures:
            try:
                future.result()
            except (OSError, RuntimeError, subprocess.SubprocessError) as error:
                failures.append(str(error))
    return failures


def sample_peers(peers, executor):
    pending = {name: executor.submit(peer.call, 'sample') for name, peer in peers.items()}
    return {name: future.result() for name, future in pending.items()}


def discover(cfg, output):
    peers = {}
    try:
        for name, role in cfg['roles'].items():
            peers[name] = Peer(name, role, cfg['owner'], output)
        with ThreadPoolExecutor(max_workers=4) as pool:
            pending = {name: pool.submit(peer.call, 'inventory') for name, peer in peers.items()}
            details = {name: job.result() for name, job in pending.items()}
            clocks = {name: [peer.clock() for _ in range(3)] for name, peer in peers.items()}
        return {'roles': details, 'clock_exchanges': clocks}
    finally:
        failures = close_peers(peers)
        if failures:
            raise RuntimeError('; '.join(failures))


def routes(cfg, peers):
    roles = cfg['roles']
    entries = {}
    for name in ('client', 'bulk'):
        if name not in peers:
            continue
        entries[name] = [(roles['server']['address'], roles[name]['address'])]
    entries['server'] = [(roles[name]['address'], roles['server']['address'])
                         for name in ('client', 'bulk') if name in peers]
    if 'shaper' in peers:
        targets = [roles['server']['address']] if cfg['direction'] == 'forward' else [
            roles[name]['address'] for name in ('client', 'bulk') if name in peers]
        entries['shaper'] = [(target, None) for target in targets]
    results = {}
    for name, probes in entries.items():
        results[name] = []
        for target, source in probes:
            lookup = peers[name].call('route', target=target, source=source)
            if not lookup or any(row.get('dev') != roles[name]['interface'] for row in lookup):
                raise ValueError(f'{name} route to {target} does not use the declared interface')
            results[name].append(dict(target=target, source=source, lookup=lookup))
    return results


def queues(snapshot):
    rows = snapshot['queues']['qdisc']
    selected = {name: next((q for q in rows if q.get('handle') == handle), {})
                for name, handle in HANDLES.items()}
    selected['root'] = next((q for q in rows if q.get('root')), {})
    return selected


def byte_delta(previous, current, name):
    before = queues(previous)[name].get('bytes')
    after = queues(current)[name].get('bytes')
    return after - before if before is not None and after is not None and after >= before else None


class Planner:
    """Bridge sampled remote data into the existing FLOWCORE strategy planner."""
    def __init__(self, cfg, strategy, current):
        capacity = cfg['capacity_kbit']
        service = SimpleNamespace(observer=SimpleNamespace(cfg={'services': {}}),
                                  config={}, profile={'bandwidth_kbit': capacity})
        self.controller = adaptive.Controller(service)
        self.controller.strategy = strategy
        self.controller.base = self.current = {name: tuple(values) for name, values in current.items()}
        for name, traffic_class in SERVICE_CLASSES.items():
            self.controller.registry.rows[name] = {'intent': dict(
                intents.DEFAULT, priority=traffic_class, target_p99_ms=cfg['workload']['deadline_ms'])}
        self.previous_proposal = None
        self.actions = 0
        self.next_action_ns = 0

    def step(self, snapshots, shaper):
        if len(snapshots) < 3:
            return None
        first, *_, last = snapshots[-5:]
        before, after = first['roles']['shaper'], last['roles']['shaper']
        elapsed = (after['host']['time_ns'] - before['host']['time_ns']) / 1e9
        if elapsed <= 0:
            return None
        window = dict(queue={}, application={}, backlog={}, rtt=None)
        sample_queues = [queues(row['roles']['shaper']) for row in snapshots[-5:]]
        for name in (*HANDLES, 'root'):
            delta = byte_delta(before, after, name)
            if delta is None:
                return None
            rows = [row[name] for row in sample_queues]
            drops = max(0, rows[-1].get('drops', 0) - rows[0].get('drops', 0))
            window['queue'][name] = dict(bytes_per_second=delta/elapsed,
                backlogged_samples=sum(row.get('backlog', 0) > 0 for row in rows),
                drops_per_second=drops/elapsed)
        window['queue']['root']['utilization'] = (
            window['queue']['root']['bytes_per_second'] / (self.controller.capacity()*125))
        window['backlog']['p95_bytes'] = max(row['root'].get('backlog', 0) for row in sample_queues)
        for name, job in last['roles']['client']['jobs'].items():
            if name not in SERVICE_CLASSES:
                continue
            result = job['summary']
            window['application'][name] = {'application': {
                'samples': result['completed'], 'p99_us': (None if result['p99_ms'] is None
                                                            else result['p99_ms'] * 1000),
                'failure': result['failed'], 'timeout': result['timeouts'],
                'deadline_miss': result['deadline_misses'], 'error_ratio': result['error_rate']}}
        plan = self.controller.plan(window, self.current)
        now = last['coordinator_ns']
        decision = dict(coordinator_ns=now, window=window, plan=plan)
        proposed = plan.get('proposed') if plan['action'] == 'transfer_reservation' else None
        if (proposed and proposed == self.previous_proposal and self.actions < 3
                and now >= self.next_action_ns):
            decision['transaction'] = shaper.call('adjust', expected=self.current, desired=proposed)
            self.current = proposed
            self.actions += 1
            self.next_action_ns = time.monotonic_ns() + 4_000_000_000
            proposed = None
        self.previous_proposal = proposed
        return decision


def validate_path(mode, first, last, bulk):
    if mode == 'unshaped':
        return {'state': 'unshaped', 'capacity_verified': False}
    keys = (*HANDLES, 'root') if mode in CLASSED else ('root',)
    deltas = {name: byte_delta(first, last, name) for name in keys}
    accounted = deltas['root'] is not None and deltas['root'] > 0
    if mode in CLASSED:
        accounted = accounted and all((deltas[name] or 0) > 0
                                      for name in ('critical', 'interactive', 'background'))
    if not bulk['error'] and bulk['bytes_transferred'] == bulk['bytes_expected']:
        key = 'background' if mode in CLASSED else 'root'
        accounted = accounted and (deltas[key] or 0) >= bulk['bytes_transferred']
    return {'state': 'observed' if accounted else 'missing_path_evidence',
            'capacity_verified': True, 'queue_byte_deltas': deltas}


def trial(cfg, mode, repetition, seed, output):
    output.mkdir()
    peers = {}
    result = dict(mode=mode, repetition=repetition, seed=seed, state='INVALID',
                  snapshots=[], decisions=[], fault=None)
    snapshots = result['snapshots']
    work = cfg['workload']
    try:
        for name, role in cfg['roles'].items():
            peers[name] = Peer(name, role, cfg['owner'], output)
        result['routes'] = routes(cfg, peers)
        result['clocks_before'] = {name: peer.clock() for name, peer in peers.items()}
        shaper = peers.get('shaper')
        planner = None
        if mode != 'unshaped':
            result['configuration'] = shaper.call('configure', mode=mode, capacity=cfg['capacity_kbit'],
                critical_pct=cfg['critical_pct'], ports=cfg['ports'], direction=cfg['direction'])
            if mode in ('queue-aware', 'hybrid'):
                planner = Planner(cfg, mode, result['configuration']['allocation'])
        server_address = cfg['roles']['server']['address']
        peers['server'].call('serve', address=server_address, ports=cfg['ports'])
        client, generator = peers['client'], peers.get('bulk', peers['client'])
        preflight = result['preflight'] = {}
        for index, name in enumerate(SERVICE_CLASSES):
            before = shaper.call('sample') if mode != 'unshaped' else None
            probe = client.call('probe_http', target=server_address,
                                source=client.role['address'], port=cfg['ports'][index])
            if before is not None:
                after = shaper.call('sample')
                key = SERVICE_CLASSES[name] if mode in CLASSED else 'root'
                probe['shaper_queue_bytes'] = byte_delta(before, after, key)
                if not probe['shaper_queue_bytes']:
                    raise RuntimeError(f'{name} probe did not traverse the declared shaper queue')
            preflight[name] = probe
        with ThreadPoolExecutor(max_workers=4) as executor:
            snapshots.append(dict(coordinator_ns=time.monotonic_ns(), roles=sample_peers(peers, executor)))
            origin = time.monotonic_ns()
            generator_args = dict(target=server_address, source=generator.role['address'],
                port=cfg['ports'][2], bytes=work['bulk_bytes'], timeout=work['bulk_timeout'],
                direction=cfg['direction'], lead_ms=1000)
            start_bulk = (executor.submit(generator.call, 'bulk', **generator_args)
                          if generator is not client else None)
            starts = {}
            for index, name in enumerate(SERVICE_CLASSES):
                starts[name] = client.call('request', name=name, target=server_address,
                    source=client.role['address'], port=cfg['ports'][index], lead_ms=1000,
                    rate=work[f'{name}_rate'], duration=work['duration'],
                    concurrency=work['concurrency'], deadline_ms=work['deadline_ms'],
                    work_ms=work['work_ms'], seed=(seed+index) % 2**32)
            starts['bulk'] = (start_bulk.result() if start_bulk else generator.call('bulk', **generator_args))
            result['starts'] = starts
            fault = cfg.get('impairment')
            next_sample = time.monotonic_ns()
            fault_due = origin + int((1 + (fault or {}).get('after_seconds', 0)) * 1e9)
            timeout = origin + (max(work['duration'], work['bulk_timeout']) + 10) * 10**9
            while time.monotonic_ns() < timeout:
                now = time.monotonic_ns()
                if fault and result['fault'] is None and now >= fault_due:
                    result['fault'] = dict(scheduled_coordinator_ns=fault_due,
                        invoked_coordinator_ns=now, **shaper.call(
                            'impair', seed=seed+1, **{k: v for k, v in fault.items() if k != 'after_seconds'}),
                        acknowledged_coordinator_ns=time.monotonic_ns())
                if now >= next_sample:
                    snapshot = dict(coordinator_ns=now, roles=sample_peers(peers, executor))
                    snapshots.append(snapshot)
                    jobs = [job for role in snapshot['roles'].values() for job in role['jobs'].values()]
                    if len(jobs) == 3 and all(job['done'] for job in jobs) and (not fault or result['fault']):
                        break
                    request_running = any(not job['done'] for name, job in
                                          snapshot['roles']['client']['jobs'].items() if name in SERVICE_CLASSES)
                    if planner and request_running:
                        decision = planner.step(snapshots, shaper)
                        if decision is not None:
                            result['decisions'].append(decision)
                    next_sample = now + 1_000_000_000
                wake = min(next_sample, fault_due) if fault and result['fault'] is None else next_sample
                time.sleep(min(.25, max(0, (wake - time.monotonic_ns())/1e9)))
            else:
                raise TimeoutError('workload exceeded experiment budget')
        result['workloads'] = client.call('results')
        if generator is not client:
            result['workloads'].update(generator.call('results'))
        bulk = result['workloads']['bulk']
        result['path_evidence'] = validate_path(mode, snapshots[0]['roles'].get('shaper'),
                                                snapshots[-1]['roles'].get('shaper'), bulk)
        result['clocks_after'] = {name: peer.clock() for name, peer in peers.items()}
        result['applied_actions'] = planner.actions if planner else 0
        if result['path_evidence']['state'] != 'missing_path_evidence':
            result['state'] = 'MEASURED'
        else:
            result['error'] = 'shaped path counters did not account for the workloads'
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        result['error'] = str(error)
    finally:
        result['cleanup_errors'] = close_peers(peers)
        if result['cleanup_errors']:
            result['state'] = 'INVALID'
        save(output / 'run.json', result)
    return result


def report(output, results):
    rows = []
    for run in results:
        for name in SERVICE_CLASSES:
            workload = run.get('workloads', {}).get(name, {})
            bulk = run.get('workloads', {}).get('bulk', {})
            rows.append(dict(mode=run['mode'], seed=run['seed'], repetition=run['repetition'],
                state=run['state'], service=name, **{key: workload.get('summary', {}).get(key)
                for key in ('attempted', 'completed', 'p50_ms', 'p95_ms', 'p99_ms', 'errors',
                            'timeouts', 'deadline_misses')}, bulk_goodput_mbps=bulk.get('goodput_mbps'),
                bulk_seconds=bulk.get('completion_seconds'), bulk_error=bulk.get('error'),
                actions=run.get('applied_actions'), experiment_error=run.get('error')))
    with (output / 'summary.csv').open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    measured = {(row['mode'], row['seed'], row['service']): row for row in rows if row['state'] == 'MEASURED'}
    comparisons = {}
    for mode in MODES[1:]:
        if mode == 'static':
            continue
        for service in SERVICE_CLASSES:
            deltas = []
            for (candidate, seed, name), row in measured.items():
                base = measured.get(('static', seed, name))
                if (candidate == mode and name == service and base and
                        row['p99_ms'] is not None and base['p99_ms'] is not None):
                    deltas.append(dict(seed=seed, p99_delta_ms=row['p99_ms']-base['p99_ms'],
                                       errors_delta=row['errors']-base['errors'],
                                       misses_delta=row['deadline_misses']-base['deadline_misses']))
            if deltas:
                comparisons[f'{mode}/{service}'] = {'pairs': deltas, 'median_p99_delta_ms':
                    statistics.median(row['p99_delta_ms'] for row in deltas)}
    save(output / 'paired.json', comparisons)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--discover-only', action='store_true')
    parser.add_argument('--modes', default='fq_codel,cake,static,queue-aware,hybrid')
    parser.add_argument('--repetitions', type=lambda v: integer(int(v), 1, 20), default=3)
    parser.add_argument('--seed', type=lambda v: integer(int(v), 0, 2**32-21), default=42)
    args = parser.parse_args()
    try:
        cfg = manifest(args.manifest)
        modes = args.modes.split(',')
        if len(set(modes)) != len(modes) or set(modes) - set(MODES):
            raise ValueError('modes must be unique members of ' + ', '.join(MODES))
        if not args.discover_only and set(modes) - {'unshaped'} and 'shaper' not in cfg['roles']:
            raise ValueError('shaped modes require an explicit shaper role')
    except (OSError, ValueError, TypeError, KeyError) as error:
        parser.error(str(error))
    args.output.mkdir(parents=True, mode=0o700)
    save(args.output / 'configuration.json', cfg)
    save(args.output / 'environment.json', discover(cfg, args.output))
    if args.discover_only:
        return 0
    rng = random.Random(args.seed)
    schedule = []
    for rep in range(args.repetitions):
        order = modes.copy()
        rng.shuffle(order)
        schedule.extend(dict(mode=mode, repetition=rep, seed=args.seed+rep) for mode in order)
    save(args.output / 'order.json', schedule)
    results = []
    for row in schedule:
        result = trial(cfg, **row, output=args.output / f"{row['mode']}-{row['repetition']:02d}")
        results.append(result)
        report(args.output, results)
        print(f"{row['mode']} rep={row['repetition']} {result['state']}", flush=True)
        if result['cleanup_errors']:
            break
    return int(len(results) != len(schedule) or any(row['state'] == 'INVALID' for row in results))


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    sys.exit(main())
