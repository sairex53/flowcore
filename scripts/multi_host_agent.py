"""Restricted experiment worker. Communicate over SSH stdin/stdout, never a TCP control port."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import http.client
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import select
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from types import SimpleNamespace

import adaptive_tc
from paths import ROOT, TC
from service_config import CLASSES

sys.path.insert(0, str(ROOT / 'examples' / 'real_apps'))
from http_benchmark import background_transfer, request_worker, summarize
from http_service import BoundedHTTPServer, Handler, METRICS
from flowcore_adapter import FlowcoreAdapter

MODES = ('unshaped', 'fq_codel', 'cake', 'static', 'queue-aware', 'hybrid')
CLASSED = ('static', 'queue-aware', 'hybrid')
MAX_REQUEST = 8192
MAX_RESPONSE = 16 * 1024 * 1024
PRIORITIES = dict(critical=0, interactive=1, standard=2, background=3)


def token(value, maximum=48):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,%d}' % (maximum - 1), value):
        raise ValueError('invalid lab identifier')
    return value


def address(value):
    if not isinstance(value, str):
        raise ValueError('expected IPv4 address string')
    ip = ipaddress.IPv4Address(value)
    if ip.is_unspecified or ip.is_multicast or ip.is_loopback or ip.is_reserved:
        raise ValueError('expected a unicast, non-loopback IPv4 test address')
    return str(ip)


def integer(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f'expected integer in [{low}, {high}]')
    return value


def number(value, low, high):
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f'expected number in [{low}, {high}]')
    return value


def ports(value):
    if not isinstance(value, list) or len(value) != 3 or len(set(value)) != 3:
        raise ValueError('provide three distinct service ports')
    return [integer(port, 1024, 65535) for port in value]


def read_file(path):
    try:
        with open(path, errors='replace') as stream:
            return stream.read(256 * 1024).strip()
    except OSError:
        return None


def command(*args, env=None):
    result = subprocess.run(list(map(str, args)), capture_output=True, text=True,
                            env=dict(os.environ, LC_ALL='C') if env is None else env,
                            timeout=5)
    if result.returncode:
        raise RuntimeError(f'{Path(args[0]).name} exited {result.returncode}: {result.stderr[:1000].strip()}')
    return result.stdout


def probe(*args):
    try:
        return {'output': command(*args)}
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
        return {'unavailable': str(error)}


def link(interface):
    return json.loads(command('ip', '-j', '-d', '-s', 'link', 'show', 'dev', token(interface, 15)))[0]


def host_sample():
    return {'time_ns': time.monotonic_ns(), 'cpu': (read_file('/proc/stat') or '').splitlines()[:1],
            'pressure': {name: read_file(f'/proc/pressure/{name}') for name in ('cpu', 'memory', 'io')},
            'loadavg': read_file('/proc/loadavg'), 'netstat': read_file('/proc/net/netstat'),
            'snmp': read_file('/proc/net/snmp')}


def inventory(interface):
    info = link(interface)
    path = Path('/sys/class/net') / interface
    timing = []
    for _ in range(16):
        before = time.monotonic_ns()
        time.sleep(.001)
        timing.append(time.monotonic_ns() - before - 1_000_000)
    tcp = ('tcp_congestion_control', 'tcp_ecn', 'tcp_sack', 'tcp_timestamps',
           'tcp_rmem', 'tcp_wmem', 'ip_forward')
    clock = time.get_clock_info('monotonic')
    executable = str(TC) if TC.is_file() else shutil.which('tc') or 'tc'
    irqs = sorted((path / 'device' / 'msi_irqs').glob('*'))
    return {
        'architecture': os.uname().machine, 'kernel': os.uname().release,
        'os_release': read_file('/etc/os-release'), 'cpu_count': os.cpu_count(),
        'cpu_affinity': sorted(os.sched_getaffinity(0)), 'cpu_topology': probe('lscpu', '-J'),
        'boot_id': read_file('/proc/sys/kernel/random/boot_id'),
        'netns_inode': os.stat('/proc/self/ns/net').st_ino,
        'virtualization': probe('systemd-detect-virt'),
        'interfaces': json.loads(command('ip', '-j', '-d', 'link', 'show')),
        'addresses': json.loads(command('ip', '-j', 'address', 'show')),
        'routes': json.loads(command('ip', '-j', 'route', 'show', 'table', 'all')),
        'selected_link': info, 'reported_link_speed_mbps': read_file(path / 'speed'),
        'ethtool': {key: probe('ethtool', *flag, interface) for key, flag in {
            'settings': (), 'driver': ('-i',), 'offloads': ('-k',), 'channels': ('-l',),
            'rings': ('-g',), 'timestamping': ('-T',)}.items()},
        'sysfs_queues': {queue.name: {name: read_file(queue / name)
            for name in ('rps_cpus', 'rps_flow_cnt', 'xps_cpus', 'xps_rxqs')}
            for queue in sorted((path / 'queues').glob('*'))},
        'interrupts': read_file('/proc/interrupts'),
        'nic_msi_irqs': {irq.name: {key: read_file(f'/proc/irq/{irq.name}/{key}')
            for key in ('smp_affinity_list', 'effective_affinity_list')} for irq in irqs},
        'worker_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'http_benchmark_sha256': hashlib.sha256(
            (ROOT / 'examples/real_apps/http_benchmark.py').read_bytes()).hexdigest(),
        'sysctls': {name: read_file('/proc/sys/net/ipv4/' + name) for name in tcp},
        'net_core_sysctls': {name: read_file('/proc/sys/net/core/' + name)
                             for name in ('default_qdisc', 'netdev_max_backlog', 'rmem_max', 'wmem_max')},
        'tc_version': probe(executable, '-V'),
        'clock': {'implementation': clock.implementation, 'resolution_s': clock.resolution,
                  'sleep_1ms_overshoot_ns': timing,
                  'ntp': probe('timedatectl', 'show', '-p', 'NTPSynchronized'),
                  'chrony': probe('chronyc', 'tracking')},
        'revision': probe('git', '-C', str(ROOT), 'rev-parse', 'HEAD'),
        'dirty': probe('git', '-C', str(ROOT), 'status', '--porcelain'),
        'host_sample': host_sample(),
    }


def policy(share):
    shares = dict(critical=share, interactive=20, standard=10, background=70-share)
    return {'classes': {name: dict(min_pct=shares[name], max_pct=100, priority=PRIORITIES[name])
                        for name in CLASSES}}


class OwnedQueue:
    def __init__(self, interface, owner):
        self.interface = token(interface, 15)
        if os.geteuid() != 0 or os.stat('/proc/self/ns/net') == os.stat('/proc/1/ns/net'):
            raise PermissionError('queue changes require root inside a dedicated network namespace')
        self.identity = link(interface)['ifindex']
        self.alias = 'flowcore-lab:' + token(owner)
        self.kind = None
        self.assert_owned_sender()
        self.lease = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            self.lease.bind(f'\0flowcore-multihost-{self.identity}')
            roots = self.rows('qdisc')
            mq = any(row.get('root') and row.get('kind') == 'mq' for row in roots)
            if any(row.get('handle') != '0:' or
                   not (row.get('root') or (mq and row.get('parent', '').startswith(':'))) or
                   row.get('kind') not in ('noqueue', 'fq_codel', 'pfifo_fast', 'fq', 'mq')
                   for row in roots):
                raise PermissionError('interface has configured queues or ingress hooks')
        except BaseException:
            self.lease.close()
            raise

    def assert_owned_sender(self):
        info = link(self.interface)
        if (info['ifindex'] != self.identity or info.get('ifalias') != self.alias or
                info.get('link_type') != 'ether' or 'UP' not in info.get('flags', [])):
            raise PermissionError('interface identity or ownership alias changed')

    def tc(self, *args):
        args = list(args)
        index = args.index('dev') + 1
        # The shared adaptive actuator names the owned egress device fc0.
        args[index] = self.interface
        executable = str(TC) if TC.is_file() else shutil.which('tc')
        if not executable:
            raise RuntimeError('tc unavailable on this role')
        env = dict(os.environ, LC_ALL='C')
        plugins = ROOT / 'tools/root/usr/lib64/tc'
        if plugins.is_dir() and executable == str(TC):
            env['TC_LIB_DIR'] = str(plugins)
        return command(executable, *args, env=env)

    def rows(self, kind):
        return json.loads(self.tc('-s', '-j', kind, 'show', 'dev', self.interface))

    def install(self, handle, *options):
        self.assert_owned_sender()
        self.tc('qdisc', 'add', 'dev', self.interface, 'root', 'handle', handle, *options)
        self.kind, self.handle = options[0], handle
        self.verify()

    def verify(self):
        self.assert_owned_sender()
        root = next((row for row in self.rows('qdisc') if row.get('root')), {})
        if root.get('handle') != self.handle or root.get('kind') != self.kind:
            raise RuntimeError('owned qdisc readback mismatch')
        return root

    def close(self):
        try:
            if self.kind is not None:
                self.verify()
                self.tc('qdisc', 'del', 'dev', self.interface, 'root', 'handle', self.handle)
                if any(q.get('handle') == self.handle for q in self.rows('qdisc')):
                    raise RuntimeError('owned root remains after cleanup')
        finally:
            self.lease.close()

    def configure(self, mode, capacity, share, service_ports, direction):
        rate = f'{capacity}kbit'
        if mode == 'cake':
            self.install('1:', 'cake', 'bandwidth', rate, 'besteffort', 'flows')
            if self.verify().get('options', {}).get('bandwidth') != capacity * 125:
                raise RuntimeError('CAKE bandwidth readback mismatch')
            return None
        self.install('1:', 'htb', 'default', '20')
        self.tc('class', 'add', 'dev', self.interface, 'parent', '1:', 'classid', '1:1',
                'htb', 'rate', rate, 'ceil', rate)
        cfg = policy(share)
        for name, minor in CLASSES.items():
            if mode == 'fq_codel' and name != 'background':
                continue
            cls = cfg['classes'][name]
            minimum = capacity if mode == 'fq_codel' else capacity * cls['min_pct'] // 100
            self.tc('class', 'add', 'dev', self.interface, 'parent', '1:1',
                    'classid', f'1:{minor}', 'htb', 'rate', f'{minimum}kbit',
                    'ceil', rate, 'prio', cls['priority'], 'quantum', '1514')
            self.tc('qdisc', 'add', 'dev', self.interface, 'parent', f'1:{minor}',
                    'handle', f'{minor}:', 'fq_codel')
        if mode == 'fq_codel':
            classes = {row['handle']: row for row in self.rows('class') if row.get('class') == 'htb'}
            if set(classes) != {'1:1', '1:20'} or any(
                    row.get('rate') != capacity * 125 or row.get('ceil') != capacity * 125
                    for row in classes.values()):
                raise RuntimeError('single-class HTB bandwidth readback mismatch')
            return None
        for name, port in zip(('critical', 'interactive', 'background'), service_ports):
            minor = CLASSES[name]
            self.tc('filter', 'add', 'dev', self.interface, 'parent', '1:',
                    'protocol', 'ip', 'pref', minor, 'flower', 'skip_hw', 'ip_proto', 'tcp',
                    'dst_port' if direction == 'forward' else 'src_port', port, 'classid', f'1:{minor}')
        expected = {name: (capacity * cls['min_pct'] // 100, capacity)
                    for name, cls in cfg['classes'].items()}
        if adaptive_tc.read(self, capacity, cfg) != expected:
            raise RuntimeError('class allocation readback mismatch')
        return expected


class Agent:
    def __init__(self, interface, role, owner):
        self.interface, self.role, self.owner = interface, role, owner
        self.queues, self.servers, self.jobs = [], [], {}
        self.pool = ThreadPoolExecutor(max_workers=17)
        self.directory = tempfile.TemporaryDirectory(prefix='flowcore-multi-host-')
        self.shaper = None
        self.adjustments = 0
        self.last_adjustment = 0

    def dispatch(self, operation, args):
        if operation == 'clock':
            return {'monotonic_ns': time.monotonic_ns(), 'realtime_ns': time.time_ns()}
        if operation == 'inventory':
            return inventory(self.interface)
        if operation == 'route':
            target = address(args['target'])
            argv = ['ip', '-j', 'route', 'get', target]
            if args.get('source'):
                argv += ['from', address(args['source'])]
            return json.loads(command(*argv))
        if operation == 'sample':
            return self.sample()
        if operation == 'serve' and self.role == 'server':
            return self.serve(args)
        if operation == 'probe_http' and self.role == 'client':
            host, source = address(args['target']), self.local_address(args['source'])
            port = integer(args['port'], 1024, 65535)
            connection = http.client.HTTPConnection(host, port, timeout=2, source_address=(source, 0))
            try:
                started = time.monotonic_ns()
                connection.request('GET', '/health')
                response = connection.getresponse()
                response.read(4096)
                if response.status != 200:
                    raise RuntimeError('unexpected HTTP health status')
                return {'elapsed_ns': time.monotonic_ns() - started, 'status': response.status}
            finally:
                connection.close()
        if operation == 'request' and self.role == 'client':
            return self.request(args)
        if operation == 'bulk' and self.role in ('client', 'bulk'):
            return self.bulk(args)
        if operation == 'results':
            return self.results()
        if self.role == 'shaper':
            if operation == 'configure':
                return self.configure(args)
            if operation == 'impair':
                return self.impair(args)
            if operation == 'adjust':
                return self.adjust(args)
        raise ValueError('operation not available to this role')

    def local_address(self, value):
        value = address(value)
        links = json.loads(command('ip', '-j', '-4', 'address', 'show', 'dev', self.interface))
        if not any(row.get('local') == value for dev in links for row in dev.get('addr_info', [])):
            raise ValueError('address not assigned to selected interface')
        return value

    def serve(self, args):
        if self.servers:
            raise ValueError('listeners already started')
        bind = self.local_address(args['address'])
        for port, role in zip(ports(args['ports']), ('critical', 'critical', 'background')):
            server = BoundedHTTPServer((bind, port), Handler, 32)
            server.role, server.adapter, server.artifact_fd = role, None, -1
            self.servers.append(server)
            threading.Thread(target=server.serve_forever, daemon=True).start()
        return {'bind': bind, 'ports': args['ports']}

    def request(self, args):
        name = token(args['name'])
        if name not in ('api', 'interactive') or name in self.jobs:
            raise ValueError('invalid or duplicate service job')
        options = SimpleNamespace(host=address(args['target']), port=integer(args['port'], 1024, 65535),
            rate=number(args['rate'], 1, 150), duration=integer(args['duration'], 2, 90),
            concurrency=integer(args['concurrency'], 1, 8), deadline_ms=number(args['deadline_ms'], 1, 3000),
            work_ms=integer(args['work_ms'], 0, 50), timeout=3,
            seed=integer(args['seed'], 0, 2**32-1), source_address=self.local_address(args['source']))
        start = time.monotonic_ns() + integer(args['lead_ms'], 0, 3000) * 1_000_000
        rows, lock = [], threading.Lock()
        count = math.ceil(options.rate * options.duration)
        adapter = FlowcoreAdapter(name, enabled=False)
        futures = [self.pool.submit(request_worker, i, options, start,
                   len(range(i, count, options.concurrency)), adapter, rows, lock)
                   for i in range(options.concurrency)]
        self.jobs[name] = (options, start, rows, lock, futures)
        return {'scheduled_start_ns': start}

    def bulk(self, args):
        if 'bulk' in self.jobs or args['direction'] not in ('forward', 'reverse'):
            raise ValueError('invalid or duplicate bulk job')
        options = SimpleNamespace(host=address(args['target']), background_port=integer(args['port'], 1024, 65535),
            background_bytes=integer(args['bytes'], 1, 64 * 1024 * 1024),
            background_timeout=integer(args['timeout'], 1, 100), background_path='',
            background_upload_file='', source_address=self.local_address(args['source']))
        if args['direction'] == 'forward':
            artifact = Path(self.directory.name) / 'upload'
            with artifact.open('wb') as stream:
                stream.truncate(options.background_bytes)
            options.background_upload_file = str(artifact)
        start = time.monotonic_ns() + integer(args['lead_ms'], 0, 3000) * 1_000_000
        self.jobs['bulk'] = self.pool.submit(background_transfer, options, start)
        return {'scheduled_start_ns': start}

    def sample(self):
        snapshot = dict(host=host_sample(), link=link(self.interface))
        if self.shaper:
            snapshot['queues'] = {kind: self.shaper.rows(kind) for kind in ('qdisc', 'class', 'filter')}
        if self.role == 'server':
            snapshot['server_metrics'] = METRICS.prometheus()
        recent = {}
        for name, job in self.jobs.items():
            if name == 'bulk':
                recent[name] = {'done': job.done()}
                continue
            options, start, rows, lock, futures = job
            cutoff = time.monotonic_ns() - 4_000_000_000
            with lock:
                window = [row for row in rows if row['end_ns'] >= cutoff]
            recent[name] = dict(done=all(future.done() for future in futures),
                                summary=summarize(window, options.rate, 4), scheduled_start_ns=start)
        return dict(snapshot, jobs=recent)

    def results(self):
        result = {}
        for name, job in self.jobs.items():
            if name == 'bulk':
                result[name] = job.result(timeout=0)
                continue
            options, start, rows, _, futures = job
            for future in futures:
                future.result(timeout=0)
            result[name] = dict(summary=summarize(rows, options.rate, options.duration),
                                requests=sorted(rows, key=lambda row: row['request']), scheduled_start_ns=start)
        return result

    def configure(self, args):
        if self.shaper or args['mode'] not in MODES[1:] or args['direction'] not in ('forward', 'reverse'):
            raise ValueError('invalid or duplicate queue configuration')
        mode = args['mode']
        capacity = integer(args['capacity'], 100, 1000000)
        share = integer(args['critical_pct'], 10, 50)
        service_ports = ports(args['ports'])
        self.shaper = OwnedQueue(self.interface, self.owner)
        self.queues.append(self.shaper)
        allocation = self.shaper.configure(mode, capacity, share, service_ports, args['direction'])
        self.capacity, self.share, self.mode = capacity, share, mode
        return {'allocation': allocation, 'readback': self.sample()['queues']}

    def impair(self, args):
        if len(self.queues) > int(self.shaper is not None) or args['interface'] == self.interface:
            raise ValueError('impairment requires a distinct dedicated egress interface')
        delay = integer(args['delay_ms'], 0, 2000)
        loss = number(args['loss_pct'], 0, 20)
        queue = OwnedQueue(args['interface'], self.owner)
        self.queues.append(queue)
        options = ['delay', f'{delay}ms', 'loss', 'random', f'{loss}%']
        if loss:
            options += ['seed', str(integer(args['seed'], 1, 2**32))]
        queue.install('2:', 'netem', *options)
        applied = time.monotonic_ns()
        readback = queue.verify()
        actual = readback.get('options', {})
        if abs(actual.get('delay', {}).get('delay', 0) - delay/1000) > .00001:
            raise RuntimeError('netem delay readback mismatch')
        if abs(actual.get('loss-random', {}).get('loss', 0) - loss/100) > .000001:
            raise RuntimeError('netem loss readback mismatch')
        if loss and actual.get('seed') != args['seed']:
            raise RuntimeError('netem seed readback mismatch')
        return {'applied_ns': applied, 'readback': readback}

    def adjust(self, args):
        if (not self.shaper or self.mode not in ('queue-aware', 'hybrid') or
                self.adjustments >= 3 or time.monotonic() - self.last_adjustment < 4):
            raise PermissionError('adaptive updates require an explicitly selected strategy')
        expected = {key: tuple(value) for key, value in args['expected'].items()}
        desired = {key: tuple(value) for key, value in args['desired'].items()}
        adaptive_tc.validate(self.capacity, expected)
        adaptive_tc.validate(self.capacity, desired)
        cfg = policy(self.share)
        if (any(ceil != self.capacity or abs(rate - expected[name][0]) > self.capacity * 5 // 100
                or rate > self.capacity * (cfg['classes'][name]['min_pct'] + 15) // 100
                for name, (rate, ceil) in desired.items()) or
                sum(rate for rate, _ in desired.values()) != sum(rate for rate, _ in expected.values())):
            raise PermissionError('allocation outside multi-host lab action budget')
        self.adjustments += 1
        self.last_adjustment = time.monotonic()
        return adaptive_tc.change(self.shaper, self.capacity, cfg, expected, desired,
                                  background_floor=self.capacity * 20 // 100)

    def close(self):
        failures = []
        for queue in reversed(self.queues):
            try:
                queue.close()
            except (RuntimeError, OSError, subprocess.SubprocessError) as error:
                failures.append(str(error))
        for server in self.servers:
            server.shutdown()
            server.server_close()
        self.pool.shutdown(wait=False, cancel_futures=True)
        self.directory.cleanup()
        if failures:
            raise RuntimeError('cleanup: ' + '; '.join(failures))


def send_frame(payload, deadline):
    remaining = memoryview(payload)
    while remaining:
        if not select.select([], [1], [], max(0, deadline - time.monotonic()))[1]:
            raise TimeoutError('worker response transport stalled')
        try:
            remaining = remaining[os.write(1, remaining):]
        except BlockingIOError:
            continue


def serve_agent(args):
    worker = Agent(args.interface, args.role, args.owner)
    deadline = time.monotonic() + args.lease_seconds
    os.set_blocking(1, False)
    try:
        pending = bytearray()
        while time.monotonic() < deadline:
            if b'\n' not in pending:
                if not select.select([0], [], [], max(0, deadline - time.monotonic()))[0]:
                    break
                chunk = os.read(0, MAX_REQUEST)
                if not chunk:
                    break
                pending.extend(chunk)
            end = pending.find(b'\n')
            if end >= MAX_REQUEST or (end < 0 and len(pending) >= MAX_REQUEST):
                raise ValueError('oversized control frame')
            if end < 0:
                continue
            raw = bytes(pending[:end])
            del pending[:end+1]
            try:
                op, values = json.loads(raw)
                if op == 'close':
                    break
                result = {'ok': worker.dispatch(op, values)}
                payload = json.dumps(result, allow_nan=False).encode()
                if len(payload) > MAX_RESPONSE:
                    raise ValueError('response exceeds bound')
            except Exception as error:
                payload = json.dumps({'error': f'{type(error).__name__}: {error}'}).encode()
            send_frame(payload + b'\n', min(deadline, time.monotonic() + 12))
    finally:
        worker.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('role', choices=('client', 'server', 'bulk', 'shaper'))
    parser.add_argument('interface', type=lambda value: token(value, 15))
    parser.add_argument('owner', type=token)
    parser.add_argument('lease_seconds', type=lambda value: integer(int(value), 15, 240))
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    signal.signal(signal.SIGHUP, lambda *_: sys.exit(129))
    signal.signal(signal.SIGINT, lambda *_: sys.exit(130))
    try:
        serve_agent(args)
    except SystemExit as error:
        os._exit(error.code)
    except Exception:
        traceback.print_exc()
        os._exit(1)
    # Queue and listener cleanup completed. End any workload threads whose
    # remote client disconnected while they were blocked in network I/O.
    os._exit(0)


if __name__ == '__main__':
    main()
