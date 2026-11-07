"""Validate bottleneck attribution on live, disposable Linux topologies."""
import argparse
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import json
import random
import socket
import statistics
from threading import Event
import time

from benchmark import RESULTS, ROOT, TC, owned_directory
from bottleneck import (APPLICATION, EGRESS, INGRESS, RECEIVER, REMOTE, UNKNOWN,
                        Bottleneck, lab_directions, lab_sockets)
from directional_qos import Ingress
from kernel_api_experiment import host_identity
from research_runner import exclusive, metadata
from service_config import CLASSES, load
from service_lab import ServiceLab
from tcp_diag import connections
from workload import percentile


CASES = dict(egress=EGRESS, ingress=INGRESS, remote=REMOTE, receiver=RECEIVER,
             application=APPLICATION, capacity_drop=EGRESS, idle=UNKNOWN)
CHUNK = bytes(16384)


def routed_pair(lab):
    lab.start_holders(3)
    for index, name, peer, local_address, router_address in (
            (0, 'fc0', 'r0', '10.203.0.1/30', '10.203.0.2/30'),
            (1, 'fc1', 'r1', '10.203.1.2/30', '10.203.1.1/30')):
        lab.command(index, ['ip', 'link', 'add', name, 'type', 'veth', 'peer', 'name', peer,
                            'netns', lab.namespace_path(2)])
        for ns, device, address in ((index, name, local_address), (2, peer, router_address)):
            lab.command(ns, ['ip', 'link', 'set', 'lo', 'up'])
            lab.command(ns, ['ip', 'addr', 'add', address, 'dev', device])
            lab.command(ns, ['ip', 'link', 'set', device, 'up'])
            lab.command(ns, ['ethtool', '-K', device, 'tso', 'off', 'gso', 'off', 'gro', 'off'])
    lab.command(0, ['ip', 'route', 'add', '10.203.1.0/30', 'via', '10.203.0.2'])
    lab.command(1, ['ip', 'route', 'add', '10.203.0.0/30', 'via', '10.203.1.1'])
    lab.command(2, ['sysctl', '-w', 'net.ipv4.ip_forward=1'])


def tcp_pair(lab, stack, destination, port, limited):
    with lab.enter(1):
        listener = stack.enter_context(socket.socket())
        listener.settimeout(2)
        if limited:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        listener.bind((destination, port))
        listener.listen(1)
    with lab.enter(0):
        client = stack.enter_context(socket.socket())
        client.settimeout(2)
        client.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 32768)
    client.connect(listener.getsockname())
    server = stack.enter_context(listener.accept()[0])
    for sock in (client, server):
        sock.settimeout(.2)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    return client, server


def send(stop, sock):
    while not stop.is_set():
        try:
            sock.send(CHUNK)
        except TimeoutError:
            continue


def receive(stop, sock, limited=False):
    delivered = 0
    while not stop.is_set():
        if limited and stop.wait(.025):
            break
        try:
            data = sock.recv(1024 if limited else 65536)
        except TimeoutError:
            continue
        if not data:
            break
        delivered += len(data)
    return delivered


def echo(stop, sock):
    while not stop.is_set():
        try:
            data = sock.recv(1)
        except TimeoutError:
            continue
        if not data:
            break
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_QUICKACK, 1)
        if stop.wait(.025):
            break
        sock.sendall(data)


def requests(stop, sock, reports):
    while not stop.is_set():
        began = time.monotonic()
        sock.sendall(b'q')
        try:
            reply = sock.recv(1)
        except TimeoutError:
            if stop.is_set():
                break
            raise
        if reply != b'q':
            raise RuntimeError('echo request payload mismatch')
        reports.append((time.monotonic()-began)*1e6)


def configure_egress(lab, config, case):
    settings = dict(config['lab'], bandwidth_kbit=12000 if case == 'ingress' else 4000,
                    delay_ms=0, jitter_ms=0)
    policy = config['policies']['default']
    lab.install_policy(policy, settings)
    if case == 'ingress':
        ingress = Ingress(2000)
        ingress.install(lab)
        lab.ingress = ingress
    else:
        for minor in CLASSES.values():
            lab.tc('qdisc', 'replace', 'dev', 'fc0', 'parent', f'1:{minor}',
                   'pfifo', 'limit', 4096)


def reduce_capacity(lab, policy):
    capacity = 2000
    for name, row in policy['classes'].items():
        lab.tc('class', 'change', 'dev', 'fc0', 'parent', '1:1', 'classid', f'1:{CLASSES[name]}',
               'htb', 'rate', f"{capacity*row['min_pct']//100}kbit",
               'ceil', f"{capacity*row['max_pct']//100}kbit", 'prio', row['priority'], 'quantum', 1514)
    lab.tc('class', 'change', 'dev', 'fc0', 'parent', '1:', 'classid', '1:1',
           'htb', 'rate', f'{capacity}kbit', 'ceil', f'{capacity}kbit')


def case_run(output, case, config):
    lab = ServiceLab(output)
    rows = []
    stop = Event()
    reports = deque(maxlen=256)
    monitor = Bottleneck()
    owned = case in ('egress', 'ingress', 'capacity_drop')
    try:
        if case == 'remote':
            routed_pair(lab)
        else:
            lab.start()
        if owned:
            configure_egress(lab, config, case)
        sender = next(row for row in lab.network(0)['interfaces'] if row['name'] == 'fc0')
        identity = tuple((holder.pid, lab.namespace_path(index)) for index, holder in enumerate(lab.holders))
        addresses = {row['address'] for row in sender['addresses']}
        router = None
        capacity_changed_at = None
        destination = '10.203.1.2' if case == 'remote' else '10.203.0.2'

        def sample(phase, started):
            tc = lab.counters()
            now = time.monotonic()
            with lab.enter(0):
                tcp, truncated = connections(64)
            rows_for_interface = lab_sockets([dict(row, namespace='sender') for row in tcp],
                                              sender['index'], addresses)
            report_copy = list(reports)
            app = [{'service': 'echo', 'class': 'critical', 'samples': len(report_copy),
                    'p95_us': percentile(report_copy, .95), 'target_us': 10000, 'port': 9000}] if report_copy else []
            result = monitor.observe(now, lab_directions(tc, egress_owned=owned), rows_for_interface,
                                     identity=identity, application=app, socket_coverage=not truncated)
            row = dict(phase=phase, seconds=now-started, **result)
            rows.append(row)
            return tc

        with ExitStack() as stack, ThreadPoolExecutor(max_workers=4) as pool:
            ports = (9000, 9001) if owned else (9000,) if case != 'idle' else ()
            pairs = [tcp_pair(lab, stack, destination, port, case == 'receiver') for port in ports]
            if case == 'remote':
                client, peer = pairs[0]
                client.sendall(b'a')
                if peer.recv(1) != b'a':
                    raise RuntimeError('remote fixture warmup failed')
            baseline = time.monotonic()
            for _ in range(3):
                sample('baseline', baseline)
                time.sleep(.25)
            started = time.monotonic()
            if case == 'remote':
                lab.command(2, [TC, 'qdisc', 'add', 'dev', 'r1', 'root', 'netem',
                                'delay', '35ms', 'rate', '4000kbit', 'limit', 4096])
            futures = []
            sinks = []
            try:
                for client, peer in pairs:
                    if case == 'application':
                        futures.append(pool.submit(echo, stop, peer))
                        futures.append(pool.submit(requests, stop, client, reports))
                    else:
                        source, sink = (peer, client) if case == 'ingress' else (client, peer)
                        futures.append(pool.submit(send, stop, source))
                        task = pool.submit(receive, stop, sink, case == 'receiver')
                        futures.append(task)
                        sinks.append(task)
                for step in range(14):
                    if case == 'capacity_drop' and step == 6:
                        reduce_capacity(lab, config['policies']['default'])
                        capacity_changed_at = time.monotonic()-started
                    time.sleep(max(0, started+(step+1)*.25-time.monotonic()))
                    tc = sample('load', started)
                if case == 'remote':
                    router = json.loads(lab.command(2, [TC, '-s', '-j', 'qdisc', 'show', 'dev', 'r1']))
            finally:
                stop.set()
                for task in futures:
                    task.result(timeout=2)
            received = [task.result() for task in sinks]
        if case in ('egress', 'ingress'):
            drained = time.monotonic()
            for _ in range(5):
                time.sleep(.25)
                sample('recovery', drained)
    finally:
        lab.close()
        (output/'observations.json').write_text(json.dumps(rows, indent=2)+'\n')

    # Ground truth is checked from topology and traffic readbacks, independently
    # of the classifier outputs. Only the monitored endpoint feeds the engine.
    if received and min(received) <= 0:
        raise RuntimeError(f'{case}: traffic did not reach every receiver')
    if case == 'remote' and not any(row['kind'] == 'netem' and row.get('backlog', 0) > 0 for row in router):
        raise RuntimeError('router fixture did not produce an observed queue')
    if owned and any(tc['traffic_classes'][name]['stats']['packets'] == 0 for name in ('critical', 'background')):
        raise RuntimeError('egress classes did not both carry packets')
    if case == 'ingress' and any(tc['ingress']['classes'][name]['stats']['packets'] == 0
                                 for name in ('critical', 'background')):
        raise RuntimeError('ingress classes did not both carry packets')
    if case == 'egress' and any(row.get('directions', {}).get('egress', {}).get('dropped_packets', 0)
                                for row in rows):
        raise RuntimeError('loss-free fixture dropped packets')

    load = [row for row in rows if row['phase'] == 'load']
    expected = CASES[case]
    detections = [row for row in load if row['state'] == 'CONGESTED' and row['classification'] == expected]
    steady = [row for row in load if row['seconds'] >= 1.25 and
              not (case == 'capacity_drop' and 1.5 <= row['seconds'] < 2.25)]
    false_positives = sum(row['state'] == 'CONGESTED' and
                          (row['phase'] == 'baseline' or expected == UNKNOWN or row['classification'] != expected)
                          for row in rows)
    false_negatives = sum(row['state'] != 'CONGESTED' or row['classification'] != expected
                          for row in steady) if expected != UNKNOWN else 0
    recovered = next((row['seconds']-capacity_changed_at for row in detections
                      if capacity_changed_at is not None and row['seconds'] >= capacity_changed_at), None)
    return dict(case=case, capacity_changed_at=capacity_changed_at, post_reduction_detection_seconds=recovered, expected=expected, detection_seconds=detections[0]['seconds'] if detections else None,
                false_positives=false_positives, false_negatives=false_negatives,
                steady_windows=len(steady), load_windows=len(load),
                classifications=dict(Counter(row['classification'] for row in load)),
                egress_eligible_windows=sum(row['action'] == 'REVIEW_EGRESS_ALLOCATION' for row in load),
                ingress_review_windows=sum(row['action'] == 'REVIEW_INGRESS_IFB' for row in load),
                capacity_reductions=[row['capacity_reductions'] for row in load if row.get('capacity_reductions')],
                max_egress_backlog=max((row.get('directions', {}).get('egress', {}).get('backlog_bytes') or 0
                                        for row in load), default=0),
                median_egress_kbit=statistics.median(rates) if (rates := [
                    row['directions']['egress']['throughput_kbit'] for row in load
                    if 'directions' in row and row['directions']['egress']['throughput_kbit'] is not None]) else None,
                application_reports=len(reports), delivered_bytes=received,
                recovery_states=[row['state'] for row in rows if row['phase'] == 'recovery'], router_qdisc=router, final_tc=tc)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repetitions', type=int, default=3, choices=range(1, 6))
    parser.add_argument('--seed', type=int, default=20261107)
    args = parser.parse_args()
    rng = random.Random(args.seed)
    with exclusive():
        output = RESULTS/f'bottleneck-{time.time_ns()}'
        owned_directory(output)
        config = load(ROOT/'config/service.json')
        host = host_identity()
        result = dict(status='running', inputs=vars(args), environment=metadata(), runs=[])
        print(f'Evidence: {output}', flush=True)
        try:
            for rep in range(args.repetitions):
                for case in rng.sample(list(CASES), len(CASES)):
                    directory = output/f'r{rep}-{case}'
                    owned_directory(directory)
                    row = dict(repetition=rep, artifact=directory.name, **case_run(directory, case, config))
                    result['runs'].append(row)
                    print(json.dumps({key: row[key] for key in ('case', 'detection_seconds', 'false_positives',
                                                                 'false_negatives', 'steady_windows')}), flush=True)
            result['status'] = 'complete'
        finally:
            result['host_identity_unchanged'] = host_identity() == host
            (output/'summary.json').write_text(json.dumps(result, indent=2)+'\n')
    if not result['host_identity_unchanged']:
        raise RuntimeError('host link or qdisc identity changed')


if __name__ == '__main__':
    main()
