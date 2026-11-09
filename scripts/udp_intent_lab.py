#!/usr/bin/env python3
"""Source-tree paired UDP intent SDK experiment on disposable veth links."""
import argparse
from contextlib import ExitStack, contextmanager
import ctypes
import hashlib
import json
import os
from pathlib import Path
import random
import socket
import statistics
import subprocess
import sys
import tempfile
import time

from benchmark import BINDIR, GID, UID, Lab, RESULTS, owned_directory
from kernel_api_experiment import host_identity, open_socket, require
from paths import ROOT, RUNTIME
from research_runner import exclusive, metadata, save
from research_stats import median_ci
from scheduler_experiment import CAPACITY, DEADLINE_NS, MARK, MODES, install, offer, probe_counters, readback, verify, workload
from service_config import load


@contextmanager
def sdk_daemon():
    config = load(ROOT/'config/service.json')
    cgroup = next(line[3:] for line in Path('/proc/self/cgroup').read_text().splitlines()
                  if line.startswith('0::'))
    config['intent_api'] = dict(priorities=['standard', 'critical'], udp_message_policies={
        'udp_lab': dict(priority='critical', uids=[os.getuid()], cgroup_prefix=cgroup,
                        socket_mark=42, urgent_rate_pps=1000, urgent_burst=256, max_deadline_ms=100)})
    with tempfile.TemporaryDirectory(prefix='udp-', dir=RUNTIME) as name:
        path = Path(name)
        (path/'config.json').write_text(json.dumps(config))
        env = dict(os.environ, FLOWCORE_RUNTIME=str(path), FLOWCORE_STATE=str(path/'state'))
        with (path/'daemon.log').open('w+') as log:
            daemon = subprocess.Popen([BINDIR/'flowcored', '--config', path/'config.json'],
                                      env=env, stdout=log, stderr=log)
            try:
                endpoint = path/'control.sock'
                for _ in range(160):
                    if endpoint.exists():
                        break
                    require(daemon.poll() is None, 'UDP SDK lab daemon exited during startup')
                    time.sleep(.025)
                require(endpoint.exists(), 'UDP SDK lab daemon startup timed out')
                yield endpoint
            finally:
                daemon.terminate()
                try:
                    daemon.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    daemon.kill()
                    daemon.wait(timeout=5)


def sdk_factory(endpoint):
    library = ctypes.CDLL(str(BINDIR/'libflowcore_udp_intent_lab.so'))
    library.flowcore_udp_lab_open.argtypes = (ctypes.c_char_p, ctypes.c_int)
    library.flowcore_udp_lab_open.restype = ctypes.c_void_p
    library.flowcore_udp_lab_send.argtypes = (ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t,
                                              ctypes.c_uint64, ctypes.c_uint64)
    library.flowcore_udp_lab_send.restype = ctypes.c_int
    library.flowcore_udp_lab_close.argtypes = (ctypes.c_void_p,)
    library.flowcore_udp_lab_close.restype = None

    @contextmanager
    def open_sender(sock):
        handle = library.flowcore_udp_lab_open(os.fsencode(endpoint), sock.fileno())
        require(handle, 'SDK could not obtain authorization for the marked socket')
        try:
            def send(payload, deadline, identifier):
                result = library.flowcore_udp_lab_send(handle, payload, len(payload), deadline, identifier)
                if result < 0:
                    raise OSError(-result, os.strerror(-result))
                require(result == len(payload), 'unexpected short UDP datagram submission')
            yield send
        finally:
            library.flowcore_udp_lab_close(handle)
    return open_sender


def prepare(lab, version, mode, *, urgent_police=None):
    lab.start()
    for index, iface in enumerate(('fc0', 'fc1')):
        if version == 4:
            lab.command(index, ['sysctl', '-w', f'net.ipv6.conf.{iface}.disable_ipv6=1'])
        else:
            lab.command(index, ['ip', '-6', 'addr', 'add', f'fd00:203::{index+1}/64',
                                'dev', iface, 'nodad'])
    address = '10.203.0.2' if version == 4 else 'fd00:203::2'
    lab.command(0, ['ping', f'-{version}', '-q', '-c', '1', '-W', '1', address])
    install(lab, mode, protocol='ip' if version == 4 else 'ipv6', urgent_police=urgent_police)
    return socket.AF_INET if version == 4 else socket.AF_INET6, address


def spoof(lab, family, address, port):
    code = """
import errno,socket,struct,sys
family,address,port=int(sys.argv[1]),sys.argv[2],int(sys.argv[3])
with socket.socket(family,socket.SOCK_DGRAM) as sock:
    try: sock.setsockopt(socket.SOL_SOCKET,36,42)
    except OSError as error:
        assert error.errno in (errno.EPERM,errno.EACCES)
    else: raise AssertionError('untrusted socket set SO_MARK')
    tos=(socket.IPPROTO_IP,socket.IP_TOS) if family==socket.AF_INET else (socket.IPPROTO_IPV6,socket.IPV6_TCLASS)
    sock.sendmsg([b'spoof'], [(*tos,struct.pack('=i',184)),
                            (socket.SOL_SOCKET,socket.SO_PRIORITY,struct.pack('=i',6))],0,(address,port))
"""
    require(UID != 0 and GID != 0, 'invoke with sudo from a nonroot account')
    lab.command(0, ['setpriv', f'--reuid={UID}', f'--regid={GID}', '--clear-groups',
                    '--bounding-set=-all', '--no-new-privs', '--', sys.executable, '-I', '-c',
                    code, family, address, port])


def classification(path, version, sdk):
    owned_directory(path)
    lab = Lab(path)
    try:
        family, address = prepare(lab, version, 'intent')
        with ExitStack() as stack:
            receiver = open_socket(lab, stack, 1, family, socket.SOCK_DGRAM)
            receiver.bind((address, 0))
            sender = open_socket(lab, stack, 0, family, socket.SOCK_DGRAM)
            sender.setsockopt(socket.SOL_SOCKET, MARK, 42)
            sender.connect(receiver.getsockname())
            send = stack.enter_context(sdk(sender))
            before = readback(lab)
            verify(before, 'intent', CAPACITY)
            steps = []
            for identifier, (payload, urgent) in enumerate(
                    [(b'ordinary-1', False), (b'urgent', True), (b'ordinary-2', False), (b'spoof', False)], 1):
                if payload == b'spoof':
                    spoof(lab, family, address, receiver.getsockname()[1])
                else:
                    send(payload, time.monotonic_ns()+DEADLINE_NS if urgent else 0, identifier)
                require(receiver.recv(128) == payload, 'classification fixture payload mismatch')
                after = readback(lab)
                counters = probe_counters(after)
                delta = {key: counters[key]-value for key, value in probe_counters(before).items()}
                require(delta['1:10'] == int(urgent) and delta['1:20'] >= int(not urgent),
                        f'unexpected per-packet queue counters: {payload!r} {delta}')
                steps.append(dict(payload=payload.decode(), packet_delta=delta, readback=after))
                before = after
            require(sender.getsockopt(socket.SOL_SOCKET, socket.SO_PRIORITY) == 0,
                    'permanent socket priority changed')
            verify(before, 'intent', CAPACITY)
        return steps
    finally:
        lab.close()


def aggregate_limit(path, version, sdk):
    """Two marked sockets share a kernel budget, even if a sender bypasses the SDK."""
    owned_directory(path)
    lab = Lab(path)
    try:
        family, address = prepare(lab, version, 'intent', urgent_police=(1, 1))
        with ExitStack() as stack:
            receiver = open_socket(lab, stack, 1, family, socket.SOCK_DGRAM)
            receiver.bind((address, 0))
            sockets = [open_socket(lab, stack, 0, family, socket.SOCK_DGRAM) for _ in range(2)]
            for sock in sockets:
                sock.setsockopt(socket.SOL_SOCKET, MARK, 42)
                sock.connect(receiver.getsockname())
            send = stack.enter_context(sdk(sockets[0]))
            before = readback(lab)
            verify(before, 'intent', CAPACITY)
            code = """
import socket,struct,sys
with socket.socket(fileno=int(sys.argv[1])) as sock:
    sock.sendmsg([b'raw-urgent'], [(socket.SOL_SOCKET,socket.SO_PRIORITY,struct.pack('=i',6))])
"""
            fd = sockets[1].fileno()
            command = lab.ns(0, ['setpriv', f'--reuid={UID}', f'--regid={GID}', '--clear-groups',
                '--bounding-set=-all', '--no-new-privs', '--', sys.executable, '-I', '-c', code, fd])
            # Linux packet policing starts without accumulated packet credits.
            time.sleep(1.05)
            started = time.monotonic_ns()
            send(b'sdk-urgent', started+DEADLINE_NS, 1)
            require(receiver.recv(128) == b'sdk-urgent', 'first urgency probe did not arrive')
            subprocess.run(command, pass_fds=(fd,), timeout=2, check=True)
            require(receiver.recv(128) == b'raw-urgent', 'over-budget datagram did not reach fallback class')
            require(time.monotonic_ns()-started < 1_000_000_000, 'policer fixture spanned a refill interval')
            after = readback(lab)
            verify(after, 'intent', CAPACITY)
            counts = probe_counters(after)
            delta = {key: counts[key]-value for key, value in probe_counters(before).items()}
            save(path/'readback.json', dict(packet_delta=delta, before=before, after=after))
            require(delta['1:10']==1 and delta['1:20']>=1 and
                    'pkts_rate 1 pkts_burst 1 action continue' in after['filter'] and
                    'dropped 0, overlimits 1' in after['filter'],
                    f'aggregate policing failed: {delta}')
            return dict(packet_delta=delta, before=before, after=after)
    finally:
        lab.close()


def trial(path, version, mode, seed, duration, sdk):
    owned_directory(path)
    lab = Lab(path)
    kernel_mode = 'intent' if mode == 'sdk' else mode
    try:
        family, address = prepare(lab, version, kernel_mode)
        before = readback(lab)
        verify(before, kernel_mode, CAPACITY)
        result = workload(lab, offer(seed, duration), duration, kernel_mode,
                          sdk=sdk if mode == 'sdk' else None, family=family, address=address,
                          enable_timestamps=False)
        after = readback(lab)
        verify(after, kernel_mode, CAPACITY)
        delta = {key: value-probe_counters(before).get(key, 0)
                 for key, value in probe_counters(after).items()}
        require(result['offered_packets'] == result['attempted_packets'], 'offered workload truncated')
        if kernel_mode == 'intent':
            require(result['completed'] <= delta['1:10'] <= result['offered'],
                    'urgent class counters disagree with application probe counts')
        if mode == 'cake-diffserv':
            root = next(row for row in after['qdisc'] if row.get('root'))
            require(result['completed'] <= root['tins'][3]['sent_packets'] <= result['offered'],
                    'CAKE voice tin counters disagree with probe traffic')
        drops = sum(row.get('drops', row.get('stats', {}).get('drops', 0)) for row in after['qdisc']
                    if not row.get('root') or kernel_mode in ('cake', 'cake-diffserv'))
        return dict(result, mode=mode, family=version, seed=seed, class_packet_delta=delta,
                    qdisc_drops=drops, before=before, after=after)
    finally:
        lab.close()


def summarize(runs, modes):
    groups = {(row['family'], row['repetition'], row['mode']): row for row in runs}
    metrics = ('p99_ms', 'deadline_attainment', 'failure_ratio', 'background_mbps',
               'background_jain', 'worker_cpu_seconds', 'submit_p50_us', 'submit_p99_us')
    report = {}
    for version in sorted({row['family'] for row in runs}):
        report[version] = {}
        for mode in modes:
            rows = [row for row in runs if row['family'] == version and row['mode'] == mode]
            values = {}
            for field in metrics:
                observed = [row[field] for row in rows if row[field] is not None]
                diffs = []
                for row in rows:
                    candidate = groups[version, row['repetition'], 'sdk']
                    if field == 'p99_ms' and (row['failure_ratio'] or candidate['failure_ratio']):
                        continue
                    if candidate[field] is not None and row[field] is not None:
                        diffs.append(candidate[field]-row[field])
                values[field] = dict(median=statistics.median(observed) if observed else None,
                                     sdk_minus_mode=statistics.median(diffs) if diffs else None,
                                     paired_samples=len(diffs), median_ci_95=median_ci(diffs))
            report[version][mode] = values
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repetitions', type=int, default=10)
    parser.add_argument('--duration', type=int, default=2)
    parser.add_argument('--seed', type=int, default=26110891)
    parser.add_argument('--families', type=int, choices=(4, 6), nargs='+', default=[4, 6])
    parser.add_argument('--classification-only', action='store_true')
    args = parser.parse_args()
    if (not 1 <= args.repetitions <= 20 or not 1 <= args.duration <= 5 or
            not 0 <= args.seed < 2**32 or len(set(args.families)) != len(args.families)):
        parser.error('invalid repetition, duration, seed, or family list')
    with exclusive():
        before = host_identity()
        path = RESULTS/f'udp-intent-{time.time_ns()}'
        owned_directory(path)
        print(f'Evidence: {path}', flush=True)
        report = dict(status='running', configuration=vars(args), environment=metadata(),
                      classification={}, aggregate_policer={}, runs=[], blocks=[],
                      sdk_bridge_sha256=hashlib.sha256(
                          (BINDIR/'libflowcore_udp_intent_lab.so').read_bytes()).hexdigest())
        try:
            with sdk_daemon() as endpoint:
                sdk = sdk_factory(endpoint)
                for version in args.families:
                    report['classification'][version] = classification(path/f'classification-v{version}', version, sdk)
                    report['aggregate_policer'][version] = aggregate_limit(path/f'aggregate-v{version}', version, sdk)
                if not args.classification_only:
                    rng = random.Random(args.seed)
                    modes = (*MODES, 'sdk')
                    for repetition, seed in enumerate(rng.sample(range(1, 2**32), args.repetitions)):
                        for version in rng.sample(args.families, len(args.families)):
                            order = rng.sample(modes, len(modes))
                            report['blocks'].append(dict(family=version, repetition=repetition, seed=seed, order=order))
                            for mode in order:
                                name = f'v{version}-r{repetition}-{mode}'
                                row = trial(path/name, version, mode, seed, args.duration, sdk)
                                save(path/name/'result.json', row)
                                report['runs'].append(dict(artifact=name, repetition=repetition,
                                    **{key: value for key, value in row.items()
                                       if key not in ('before', 'after', 'probe_samples')}))
                                save(path/'summary.json', report)
                                print(f"{name}: P99 {row['p99_ms']}, attainment {row['deadline_attainment']:.3f}",
                                      flush=True)
                    report['comparisons'] = summarize(report['runs'], modes)
            report['status'] = 'complete'
        except BaseException as error:
            report.update(status='invalid', error=getattr(error, 'stderr', None) or str(error))
            raise
        finally:
            report['host_unchanged'] = host_identity() == before
            if not report['host_unchanged']:
                report['status'] = 'invalid'
            save(path/'summary.json', report)
        require(report['status'] == 'complete', 'invalid UDP intent experiment')


if __name__ == '__main__':
    main()
