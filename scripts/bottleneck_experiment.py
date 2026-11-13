"""Validate bottleneck attribution on live, disposable Linux topologies."""
import argparse
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import json
import random
import socket
import statistics
from threading import Event, Lock
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
             application=APPLICATION, demand_load=EGRESS, demand_idle=UNKNOWN,
             demand_background=EGRESS, demand_bursty=EGRESS, demand_receiver=RECEIVER,
             demand_pacing=EGRESS, demand_class=EGRESS, demand_remote=REMOTE,
             demand_capacity_drop=EGRESS, demand_recovery=UNKNOWN, demand_mixed_udp=EGRESS,
             capacity_drop=EGRESS, idle=UNKNOWN)
CHUNK = bytes(16384)
DEMAND_RATE_BPS_PER_FLOW = 8_000_000
DEMAND_PERIOD_SECONDS = .1
DEMAND_CASES = {name for name in CASES if name.startswith('demand_')}
DEMAND_SCENARIO_MANIFEST = json.loads((ROOT/'scenarios/demand-network-v2.json').read_text())
DEMAND_PROFILE_BY_NAME = {row['name']: row for row in DEMAND_SCENARIO_MANIFEST['profiles']}
DEMAND_OWNED_CASES = {'demand_load', 'demand_idle', 'demand_background', 'demand_bursty',
                      'demand_pacing', 'demand_class', 'demand_capacity_drop', 'demand_recovery',
                      'demand_mixed_udp'}


class WindowTraffic:
    """Exact app payload counters for one bounded observation window."""
    def __init__(self):
        self.lock = Lock()
        self.values = dict(bytes_offered=0, bytes_admitted=0,
                           bytes_transmitted=0, bytes_received=0,
                           udp_messages_attempted=0, udp_messages_transmitted=0,
                           udp_send_failures=0, udp_urgent_messages=0,
                           udp_background_messages=0)

    def add(self, field, count):
        with self.lock:
            self.values[field] += count

    def record_tcp_send(self, offered, submitted):
        """Commit one generator release and its send result to one window."""
        with self.lock:
            self.values['bytes_offered'] += offered
            self.values['bytes_admitted'] += offered
            self.values['bytes_transmitted'] += submitted

    def record_udp_send(self, offered, submitted, urgent, failed):
        """Keep each datagram's release, result, and message counts atomic."""
        with self.lock:
            self.values['bytes_offered'] += offered
            self.values['bytes_admitted'] += offered
            self.values['bytes_transmitted'] += submitted
            self.values['udp_messages_attempted'] += 1
            self.values['udp_urgent_messages' if urgent else 'udp_background_messages'] += 1
            self.values['udp_messages_transmitted'] += int(submitted == offered)
            self.values['udp_send_failures'] += int(failed)

    def snapshot(self):
        with self.lock:
            value = self.values
            self.values = dict.fromkeys(value, 0)
        return value


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


def udp_pair(lab, stack, destination, port):
    with lab.enter(1):
        peer = stack.enter_context(socket.socket(socket.AF_INET, socket.SOCK_DGRAM))
        peer.settimeout(.2)
        peer.bind((destination, port))
    with lab.enter(0):
        client = stack.enter_context(socket.socket(socket.AF_INET, socket.SOCK_DGRAM))
        client.setblocking(False)
    return client, peer, peer.getsockname()


def send(stop, sock):
    while not stop.is_set():
        try:
            sock.send(CHUNK)
        except TimeoutError:
            continue


def paced_send(stop, sock, meter, rate_bps=DEMAND_RATE_BPS_PER_FLOW):
    period_bytes = int(rate_bps * DEMAND_PERIOD_SECONDS / 8)
    payload = bytes(period_bytes)
    period = DEMAND_PERIOD_SECONDS
    deadline = time.monotonic()+period
    sock.setblocking(False)
    while not stop.is_set():
        delay = deadline-time.monotonic()
        if delay > 0 and stop.wait(delay):
            break
        if stop.is_set():
            break
        try:
            admitted = sock.send(payload)
        except BlockingIOError:
            admitted = 0
        # The scheduled payload is admitted by this synthetic application;
        # bytes_transmitted records what the socket accepted for submission.
        meter.record_tcp_send(period_bytes, admitted)
        deadline += period
        now = time.monotonic()
        if deadline < now-period:
            deadline = now+period


def bursty_send(stop, sock, meter, payload_bytes=100_000, period_seconds=.5):
    payload = bytes(payload_bytes)
    deadline = time.monotonic()+period_seconds
    sock.setblocking(False)
    while not stop.is_set():
        delay = deadline-time.monotonic()
        if delay > 0 and stop.wait(delay):
            break
        if stop.is_set():
            break
        remaining = memoryview(payload)
        submitted = 0
        while remaining and not stop.is_set():
            try:
                accepted = sock.send(remaining)
            except BlockingIOError:
                if stop.wait(.001):
                    break
                continue
            if accepted <= 0:
                break
            submitted += accepted
            remaining = remaining[accepted:]
        meter.record_tcp_send(len(payload), submitted)
        deadline += period_seconds
        now = time.monotonic()
        if deadline < now-period_seconds:
            deadline = now+period_seconds


def udp_paced_send(stop, sock, destination, meter, period_seconds=.0012):
    payload = bytes(1200)
    deadline = time.monotonic()+period_seconds
    message = 0
    while not stop.is_set():
        delay = deadline-time.monotonic()
        if delay > 0 and stop.wait(delay):
            break
        if stop.is_set():
            break
        urgent = message % 10 == 0
        sent = 0
        failed = False
        try:
            sent = sock.sendto(payload, destination)
        except OSError:
            failed = True
        meter.record_udp_send(len(payload), sent, urgent, failed or sent != len(payload))
        message += 1
        deadline += period_seconds
        now = time.monotonic()
        if deadline < now-period_seconds:
            deadline = now+period_seconds


def udp_receive(stop, sock, meter):
    delivered = 0
    while not stop.is_set():
        try:
            data, _ = sock.recvfrom(65536)
        except TimeoutError:
            continue
        if not data:
            break
        delivered += len(data)
        meter.add('bytes_received', len(data))
    return delivered


def receive(stop, sock, limited=False, meter=None):
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
        if meter is not None:
            meter.add('bytes_received', len(data))
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
    capacity = (12000 if case in ('ingress', 'demand_class', 'demand_pacing') else
                2000 if case == 'demand_recovery' else 4000)
    settings = dict(config['lab'], bandwidth_kbit=capacity,
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


def set_capacity(lab, policy, capacity):
    for name, row in policy['classes'].items():
        lab.tc('class', 'change', 'dev', 'fc0', 'parent', '1:1', 'classid', f'1:{CLASSES[name]}',
               'htb', 'rate', f"{capacity*row['min_pct']//100}kbit",
               'ceil', f"{capacity*row['max_pct']//100}kbit", 'prio', row['priority'], 'quantum', 1514)
    lab.tc('class', 'change', 'dev', 'fc0', 'parent', '1:', 'classid', '1:1',
           'htb', 'rate', f'{capacity}kbit', 'ceil', f'{capacity}kbit')


def reduce_capacity(lab, policy):
    set_capacity(lab, policy, 2000)


def case_run(output, case, config):
    lab = ServiceLab(output)
    rows = []
    stop = Event()
    reports = deque(maxlen=256)
    monitor = Bottleneck()
    remote_case = case in ('remote', 'demand_remote')
    receiver_case = case in ('receiver', 'demand_receiver')
    owned = case in ('egress', 'ingress', 'demand_load', 'capacity_drop') or case in DEMAND_OWNED_CASES
    try:
        if remote_case:
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
        destination = '10.203.1.2' if remote_case else '10.203.0.2'
        if case == 'demand_idle':
            ports = ()
        elif case in ('demand_background', 'demand_pacing', 'demand_class', 'demand_recovery'):
            ports = (9001,)
        elif case == 'demand_mixed_udp':
            ports = (9001,)
        elif case in ('demand_receiver', 'demand_remote'):
            ports = (9000,)
        elif case == 'demand_bursty':
            ports = (9000, 9001)
        elif case.startswith('demand_') or case in ('egress', 'capacity_drop'):
            ports = (9000, 9001)
        elif case == 'idle':
            ports = ()
        else:
            ports = (9000,)
        demand_meters = ([WindowTraffic() for _ in range(max(1, len(ports)))]
                         if case in DEMAND_CASES else [])
        udp_meter = WindowTraffic() if case == 'demand_mixed_udp' else None

        def sample(phase, started):
            tc = lab.counters()
            now = time.monotonic()
            with lab.enter(0):
                tcp, truncated = connections(64)
            rows_for_interface = lab_sockets([dict(row, namespace='sender') for row in tcp],
                                              sender['index'], addresses)
            report_copy = list(reports)
            if case in DEMAND_CASES:
                app = []
                for index, meter in enumerate(demand_meters):
                    traffic = meter.snapshot()
                    port = ports[index] if index < len(ports) else 9000
                    traffic_class = 'critical' if port == 9000 else 'background' if port == 9001 else 'unknown'
                    app.append(dict(service=f'demand_flow_{index}', **{'class': traffic_class},
                        samples=0, p95_us=None, target_us=10000,
                        port=port, traffic_reports=1, traffic=traffic,
                        traffic_window_complete=True, request_window_complete=False))
                if udp_meter is not None:
                    traffic = udp_meter.snapshot()
                    messages = {name: traffic[name] for name in
                                ('udp_messages_attempted', 'udp_messages_transmitted', 'udp_send_failures',
                                 'udp_urgent_messages', 'udp_background_messages')}
                    app.append(dict(service='demand_flow_udp', **{'class': 'standard'},
                        samples=0, p95_us=None, target_us=10000, port=9004,
                        traffic_reports=1, traffic={name: traffic[name] for name in
                            ('bytes_offered', 'bytes_admitted', 'bytes_transmitted', 'bytes_received')},
                        traffic_window_complete=True, request_window_complete=False,
                        transports={'udp': {**messages, 'reports': 1, 'message_reports': 1}}))
            else:
                app = [{'service': 'echo', 'class': 'critical', 'samples': len(report_copy),
                        'p95_us': percentile(report_copy, .95), 'target_us': 10000, 'port': 9000}]
                if not report_copy:
                    app = []
            result = monitor.observe(now, lab_directions(tc, egress_owned=owned), rows_for_interface,
                                     identity=identity, application=app, socket_coverage=not truncated)
            row = dict(phase=phase, seconds=now-started, **result)
            rows.append(row)
            return tc

        with ExitStack() as stack, ThreadPoolExecutor(max_workers=4) as pool:
            pairs = [tcp_pair(lab, stack, destination, port, receiver_case) for port in ports]
            udp = udp_pair(lab, stack, '10.203.0.2', 9004) if udp_meter is not None else None
            if case == 'demand_class':
                lab.tc('class', 'change', 'dev', 'fc0', 'parent', '1:1', 'classid',
                       f"1:{CLASSES['background']}", 'htb', 'rate', '1000kbit', 'ceil', '1000kbit')
            if case == 'demand_pacing':
                lab.tc('qdisc', 'replace', 'dev', 'fc0', 'parent', f"1:{CLASSES['background']}", 'fq')
                for client, _ in pairs:
                    client.setsockopt(socket.SOL_SOCKET, 47, 125_000)  # SO_MAX_PACING_RATE, bytes/s.
            if remote_case:
                client, peer = pairs[0]
                client.sendall(b'a')
                if peer.recv(1) != b'a':
                    raise RuntimeError('remote fixture warmup failed')
            baseline = time.monotonic()
            for _ in range(3):
                sample('baseline', baseline)
                time.sleep(.25)
            started = time.monotonic()
            if remote_case:
                lab.command(2, [TC, 'qdisc', 'add', 'dev', 'r1', 'root', 'netem',
                                'delay', '35ms', 'rate', '4000kbit', 'limit', 4096])
            futures = []
            sinks = []
            try:
                for index, (client, peer) in enumerate(pairs):
                    if case == 'demand_load':
                        futures.append(pool.submit(paced_send, stop, client, demand_meters[index]))
                        task = pool.submit(receive, stop, peer, False, demand_meters[index])
                        futures.append(task)
                        sinks.append(task)
                    elif case in DEMAND_CASES:
                        if case == 'demand_bursty' and ports[index] == 9000:
                            sender_task = pool.submit(bursty_send, stop, client, demand_meters[index])
                        elif case == 'demand_bursty':
                            sender_task = pool.submit(paced_send, stop, client, demand_meters[index], 2_000_000)
                        else:
                            sender_task = pool.submit(paced_send, stop, client, demand_meters[index])
                        futures.append(sender_task)
                        task = pool.submit(receive, stop, peer, receiver_case, demand_meters[index])
                        futures.append(task)
                        sinks.append(task)
                    elif case == 'application':
                        futures.append(pool.submit(echo, stop, peer))
                        futures.append(pool.submit(requests, stop, client, reports))
                    else:
                        source, sink = (peer, client) if case == 'ingress' else (client, peer)
                        futures.append(pool.submit(send, stop, source))
                        task = pool.submit(receive, stop, sink, case == 'receiver')
                        futures.append(task)
                        sinks.append(task)
                if udp is not None:
                    udp_client, udp_peer, udp_destination = udp
                    futures.append(pool.submit(udp_paced_send, stop, udp_client,
                                               udp_destination, udp_meter))
                    task = pool.submit(udp_receive, stop, udp_peer, udp_meter)
                    futures.append(task)
                    sinks.append(task)
                for step in range(14):
                    if case in ('capacity_drop', 'demand_capacity_drop') and step == 6:
                        reduce_capacity(lab, config['policies']['default'])
                        capacity_changed_at = time.monotonic()-started
                    if case == 'demand_recovery' and step == 6:
                        set_capacity(lab, config['policies']['default'], 12000)
                        capacity_changed_at = time.monotonic()-started
                    time.sleep(max(0, started+(step+1)*.25-time.monotonic()))
                    tc = sample('load', started)
                if remote_case:
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
    if remote_case and not any(row['kind'] == 'netem' and row.get('backlog', 0) > 0 for row in router):
        raise RuntimeError('router fixture did not produce an observed queue')
    active_classes = ({'background', 'standard'} if case == 'demand_mixed_udp'
                      else {'background'} if case in ('demand_background', 'demand_pacing', 'demand_class', 'demand_recovery')
                      else {'critical', 'background'} if case in ('egress', 'demand_load', 'demand_bursty',
                                                                    'capacity_drop', 'demand_capacity_drop')
                      else set())
    if any(tc['traffic_classes'][name]['stats']['packets'] == 0 for name in active_classes):
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
              not (case in ('capacity_drop', 'demand_capacity_drop') and 1.5 <= row['seconds'] < 2.25)]
    false_positives = sum(row['state'] == 'CONGESTED' and (
        row['phase'] == 'baseline' or
        (row['phase'] == 'load' and expected == UNKNOWN and
         case in ('idle', 'demand_idle')) or
        (row['phase'] == 'load' and expected != UNKNOWN and row['classification'] != expected))
        for row in rows)
    false_negatives = sum(row['state'] != 'CONGESTED' or row['classification'] != expected
                          for row in steady) if expected != UNKNOWN else 0
    recovered = next((row['seconds']-capacity_changed_at for row in detections
                      if capacity_changed_at is not None and row['seconds'] >= capacity_changed_at), None)
    demand_windows = [row.get('demand', {}).get('overall') for row in load
                      if row.get('demand', {}).get('overall')]
    per_class_windows = {}
    for row in load:
        for name, estimate in row.get('demand', {}).get('classes', {}).items():
            per_class_windows.setdefault(name, []).append(estimate)
    demand_phase_stats = {}
    for phase in ('baseline', 'load', 'recovery'):
        phase_rows = [row.get('demand', {}).get('overall') for row in rows if row['phase'] == phase and
                      row.get('demand', {}).get('overall')]
        if phase_rows:
            demand_phase_stats[phase] = dict(classifications=dict(Counter(row['classification'] for row in phase_rows)),
                eligible_windows=sum(row.get('demand', {}).get('controller_eligible', False)
                                     for row in rows if row['phase'] == phase))
    demand_capacity_epochs = {}
    if capacity_changed_at is not None:
        for epoch, predicate in (
                ('before_change', lambda second: second < capacity_changed_at),
                ('after_change', lambda second: second >= capacity_changed_at + .5)):
            epoch_rows = [row for row in load if predicate(row['seconds']) and
                          row.get('demand', {}).get('overall')]
            if epoch_rows:
                demand_capacity_epochs[epoch] = dict(
                    classifications=dict(Counter(row['demand']['overall']['classification']
                                                 for row in epoch_rows)),
                    offered_bps=[row['demand']['overall']['offered_bps'] for row in epoch_rows],
                    observed_bps=[row['directions']['egress']['throughput_kbit'] * 1000
                                  for row in epoch_rows
                                  if row['directions']['egress'].get('throughput_kbit') is not None])
    demand_apps = [app for row in load for app in row.get('application', [])
                   if app.get('service', '').startswith('demand_flow_')]
    demand_traffic = [app['traffic'] for app in demand_apps]
    udp_messages = [app.get('transports', {}).get('udp', {}) for app in demand_apps
                    if 'udp' in app.get('transports', {})]
    udp_totals = {field: sum(report.get(field, 0) for report in udp_messages) for field in
                  ('udp_messages_attempted', 'udp_messages_transmitted', 'udp_send_failures',
                   'udp_urgent_messages', 'udp_background_messages')}
    profile = DEMAND_PROFILE_BY_NAME.get(case, {})
    warmup = DEMAND_SCENARIO_MANIFEST['evaluation']['warmup_exclusion_seconds']
    target_scope = profile.get('expected_scope', 'overall')

    def accuracy_for(expected, selected_rows):
        if target_scope.startswith('class:'):
            class_name = target_scope.split(':', 1)[1]
            actual = [row.get('demand', {}).get('classes', {}).get(class_name)
                      for row in selected_rows]
        else:
            actual = [row.get('demand', {}).get('overall') for row in selected_rows]
        actual = [row for row in actual if row]
        matches = sum(row['classification'] == expected for row in actual)
        limited = {'LOCAL_CAPACITY_LIMITED', 'CLASS_RESTRICTED', 'SOCKET_PACING_RESTRICTED',
                   'RECEIVER_LIMITED_SUSPECTED', 'REMOTE_BOTTLENECK_SUSPECTED'}
        false_positives = sum(expected == 'DEMAND_LIMITED' and row['classification'] in limited
                              for row in actual)
        false_negatives = sum(expected in limited and row['classification'] != expected for row in actual)
        return dict(expected=expected, evaluated_windows=len(actual), matching_windows=matches,
                    mismatching_windows=len(actual)-matches,
                    accuracy=matches/len(actual) if actual else None,
                    false_positive_limit_labels=false_positives,
                    false_negative_expected_label=false_negatives,
                    unknown_abstentions=sum(row['classification'] == 'UNKNOWN' for row in actual))

    def first_expected_window(expected, selected_rows):
        if target_scope.startswith('class:'):
            class_name = target_scope.split(':', 1)[1]
            estimate = lambda row: row.get('demand', {}).get('classes', {}).get(class_name)
        else:
            estimate = lambda row: row.get('demand', {}).get('overall')
        return next((row['seconds'] for row in selected_rows
                     if (item := estimate(row)) and item['classification'] == expected), None)

    accuracy_rows = [row for row in load if row['seconds'] >= warmup]
    if profile.get('expected_by_epoch') and capacity_changed_at is not None:
        demand_accuracy = {}
        demand_timing = {}
        for epoch, expected_label in profile['expected_by_epoch'].items():
            if epoch == 'before_change':
                selected = [row for row in accuracy_rows if row['seconds'] < capacity_changed_at]
            else:
                selected = [row for row in accuracy_rows if row['seconds'] >= capacity_changed_at + .5]
            demand_accuracy[epoch] = accuracy_for(expected_label, selected)
            first = first_expected_window(expected_label, selected)
            demand_timing[epoch] = dict(first_expected_window_seconds=first,
                delay_after_capacity_change_seconds=(first-capacity_changed_at
                    if epoch == 'after_change' and first is not None else None))
    elif profile.get('expected_demand_classification'):
        demand_accuracy = accuracy_for(profile['expected_demand_classification'], accuracy_rows)
        first = first_expected_window(profile['expected_demand_classification'], accuracy_rows)
        demand_timing = dict(first_expected_window_seconds=first,
                             warmup_exclusion_seconds=warmup)
    else:
        demand_accuracy = dict(expected=None, evaluated_windows=0, matching_windows=0,
                               mismatching_windows=0, accuracy=None,
                               reason='profile has window-dependent ground truth')
        demand_timing = dict(first_expected_window_seconds=None,
                             reason='profile has window-dependent ground truth')
    return dict(case=case, capacity_changed_at=capacity_changed_at, post_reduction_detection_seconds=recovered, expected=expected, detection_seconds=detections[0]['seconds'] if detections else None,
                false_positives=false_positives, false_negatives=false_negatives,
                steady_windows=len(steady), load_windows=len(load),
                classifications=dict(Counter(row['classification'] for row in load)),
                demand_classifications=dict(Counter(row['classification'] for row in demand_windows)),
                demand_class_classifications={name: dict(Counter(item['classification'] for item in estimates))
                                              for name, estimates in per_class_windows.items()},
                demand_accuracy=demand_accuracy,
                demand_timing=demand_timing,
                demand_capacity_epochs=demand_capacity_epochs,
                egress_eligible_windows=sum(row['action'] == 'REVIEW_EGRESS_ALLOCATION' for row in load),
                ingress_review_windows=sum(row['action'] == 'REVIEW_INGRESS_IFB' for row in load),
                capacity_reductions=[row['capacity_reductions'] for row in load if row.get('capacity_reductions')],
                max_egress_backlog=max((row.get('directions', {}).get('egress', {}).get('backlog_bytes') or 0
                                        for row in load), default=0),
                median_egress_kbit=statistics.median(rates) if (rates := [
                    row['directions']['egress']['throughput_kbit'] for row in load
                    if 'directions' in row and row['directions']['egress']['throughput_kbit'] is not None]) else None,
                demand_phase_stats=demand_phase_stats,
                demand_controller_eligible_windows=sum(row.get('demand', {}).get('controller_eligible', False)
                                                       for row in load),
                app_traffic_window_reports=len(demand_apps),
                app_bytes_offered=sum(row['bytes_offered'] for row in demand_traffic),
                app_bytes_admitted=sum(row['bytes_admitted'] for row in demand_traffic),
                app_bytes_transmitted=sum(row['bytes_transmitted'] for row in demand_traffic),
                app_bytes_received=sum(row['bytes_received'] for row in demand_traffic),
                udp_accounting_reports=len(udp_messages), **udp_totals,
                application_reports=len(reports), delivered_bytes=received,
                recovery_states=[row['state'] for row in rows if row['phase'] == 'recovery'], router_qdisc=router, final_tc=tc)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repetitions', type=int, default=3, choices=range(1, 6))
    parser.add_argument('--seed', type=int, default=20261107)
    parser.add_argument('--cases', nargs='+', choices=tuple(CASES), default=tuple(CASES),
                        help='select isolated namespace cases; ingress requires a preloaded IFB driver')
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
                for case in rng.sample(args.cases, len(args.cases)):
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
