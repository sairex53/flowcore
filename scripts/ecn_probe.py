"""Measure ECN negotiation, IP codepoints and TCP feedback on disposable veth pairs."""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import json
import select
import socket
import struct
import subprocess
import threading
import time

import ecn_policy
from benchmark import Lab, RESULTS, owned_directory
from ecn_feedback import aqm_counters, mode
from kernel_api_experiment import host_identity, open_socket
from tcp_diag import tcp_info

SENDER, RECEIVER = '10.203.0.1', '10.203.0.2'
PAYLOAD = bytes(384 * 1024)
ETH_ALL = 3
PACKET_STATISTICS = 6
SOL_PACKET = 263
CASES = {
    'loss': ('fq_codel', 4, ((0, 0, 'cubic'),)),
    'classic': ('fq_codel', 4, ((1, 1, 'cubic'),)),
    'mixed': ('fq_codel', 22, ((1, 1, 'cubic'), (1, 0, 'reno'))),
    'accurate': ('fq_codel', 4, ((3, 3, 'cubic'),)),
    'dctcp': ('fq_codel', 4, ((1, 1, 'dctcp'), (1, 1, 'dctcp'))),
    'cake': ('cake', 22, ((1, 1, 'cubic'),)),
}


def capture_fields(data):
    """Parse only IPv4/TCP header bytes. Return no packet payload."""
    if len(data) < 54 or data[12:14] != b'\x08\x00' or data[14] >> 4 != 4 or data[23] != 6:
        return None
    ihl = (data[14] & 15)*4
    tcp = 14+ihl
    if ihl < 20 or len(data) < tcp+20:
        return None
    length, fragment = struct.unpack_from('!H2xH', data, 16)
    tcp_hlen = (data[tcp+12] >> 4)*4
    if fragment & 0x3fff or tcp_hlen < 20 or length < ihl+tcp_hlen:
        return None
    source = socket.inet_ntoa(data[26:30])
    sport, dport = struct.unpack_from('!HH', data, tcp)
    flags = data[tcp+13]
    ace = (data[tcp+12] & 1)*4 + (flags >> 6)
    return source, sport, dport, length > ihl+tcp_hlen, data[15] & 3, flags, ace


def capture(sock, stop):
    counts = {}
    while not stop.is_set() or select.select([sock], [], [], 0)[0]:
        if not select.select([sock], [], [], .03)[0]:
            continue
        try:
            packet = capture_fields(sock.recv(96))
        except BlockingIOError:
            continue
        if packet is None:
            continue
        source, sport, dport, data, ecn, flags, ace = packet
        if source not in (SENDER, RECEIVER):
            continue
        key = (sport, dport) if source == SENDER else (dport, sport)
        if len(counts) >= 16 and key not in counts:
            continue
        values = counts.setdefault(key, Counter())
        if flags & 2:
            values[f'{"syn" if source == SENDER else "syn_ack"}_ace_{ace}'] += 1
        if data and source == SENDER:
            values[('not_ect', 'ect1', 'ect0', 'ce')[ecn]] += 1
        if source == RECEIVER and flags & 0x40:
            values['reverse_ece_bit'] += 1
    packet_count, capture_drops = struct.unpack('=II', sock.getsockopt(SOL_PACKET, PACKET_STATISTICS, 8))
    return counts, packet_count, capture_drops


def pair(lab, stack, left, right, algorithm):
    ecn_policy.endpoint(lab, 0, left, algorithm)
    ecn_policy.endpoint(lab, 1, right, algorithm)
    listener = open_socket(lab, stack, 1, socket.AF_INET, socket.SOCK_STREAM)
    client = open_socket(lab, stack, 0, socket.AF_INET, socket.SOCK_STREAM)
    for sock in (listener, client):
        sock.settimeout(8)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_CONGESTION, algorithm.encode())
    listener.bind((RECEIVER, 0))
    listener.listen(1)
    client.connect(listener.getsockname())
    accepted, _ = listener.accept()
    server = stack.enter_context(accepted)
    server.settimeout(8)
    actual = [sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_CONGESTION, 16).split(b'\0', 1)[0].decode()
              for sock in (client, server)]
    if actual != [algorithm, algorithm]:
        raise RuntimeError(f'TCP congestion-control fallback: {actual}')
    return client, server


def info(sock):
    return tcp_info(sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_INFO, 512))


def receive(sock):
    received = 0
    while received < len(PAYLOAD):
        block = sock.recv(65536)
        if not block:
            raise RuntimeError('probe stream closed early')
        received += len(block)
    return received


def case(lab, name, configuration):
    kind, rtt, flows = configuration
    lab.start()
    lab.command(0, ['ping', '-q', '-c', '1', '-W', '1', RECEIVER])
    lab.command(1, [str(ecn_policy.TC), 'qdisc', 'add', 'dev', 'fc1', 'root', 'netem', 'delay', f'{rtt}ms'])
    if kind == 'cake':
        lab.tc('qdisc', 'add', 'dev', 'fc0', 'root', 'cake', 'bandwidth', '4mbit', 'besteffort', 'rtt', '40ms')
    else:
        lab.tc('qdisc', 'add', 'dev', 'fc0', 'root', 'handle', '1:', 'htb', 'default', '20')
        lab.tc('class', 'add', 'dev', 'fc0', 'parent', '1:', 'classid', '1:20', 'htb', 'rate', '4mbit',
               'ceil', '4mbit', 'burst', '8k', 'cburst', '8k')
        # The classic and Accurate probes use a shallow experimental threshold
        # to make marks reproducible. The paired classic matrix uses normal CoDel ECN.
        profile = dict(ecn_policy.DEFAULT, threshold_us=5000)
        lab.tc('qdisc', 'add', 'dev', 'fc0', 'parent', '1:20',
               *ecn_policy.leaf_args(profile, f'ecn-{name}'))
    with ExitStack() as stack, ThreadPoolExecutor(max_workers=5) as pool:
        wire = open_socket(lab, stack, 1, socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETH_ALL))
        wire.bind(('fc1', ETH_ALL))
        wire.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024*1024)
        wire.setblocking(False)
        stop = threading.Event()
        observer = pool.submit(capture, wire, stop)
        try:
            sockets = [pair(lab, stack, *config) for config in flows]
            before = [info(tx) for tx, _ in sockets]
            start = time.monotonic()
            work = [(pool.submit(tx.sendall, PAYLOAD), pool.submit(receive, rx)) for tx, rx in sockets]
            totals = []
            for send, recv in work:
                send.result(timeout=9)
                totals.append(recv.result(timeout=9))
            elapsed = time.monotonic()-start
            # Reverse netem can delay the final ACK after application delivery.
            time.sleep(.12)
            measurements = [(info(tx), info(rx)) for tx, rx in sockets]
        finally:
            stop.set()
        observed, packets, drops = observer.result(timeout=2)
        rows = []
        for config, (tx, _), prior, (sender, receiver), amount in zip(flows, sockets, before, measurements, totals):
            left, right, algorithm = config
            wire_counts = dict(observed.get((tx.getsockname()[1], tx.getpeername()[1]), {}))
            if not any(key.startswith('syn_ack_ace_') for key in wire_counts):
                raise RuntimeError('wire capture did not see SYN/ACK')
            if drops or sender.get('ecn_negotiated') != bool(left and right):
                raise RuntimeError('capture loss or unexpected ECN negotiation')
            if name == 'accurate' and (mode(sender) != 'accurate' or mode(receiver) != 'accurate'):
                raise RuntimeError('AccECN negotiation was not confirmed at both endpoints')
            if left and right:
                if name != 'accurate' and not wire_counts.get('reverse_ece_bit'):
                    raise RuntimeError('classic ECN capture did not observe a reverse ECE bit')
                if not wire_counts.get('ce') or sender.get('delivered_ce_segments', 0) <= prior.get('delivered_ce_segments', 0):
                    raise RuntimeError('expected IP CE and sender TCP CE feedback')
            elif wire_counts.get('ce') or wire_counts.get('ect0') or wire_counts.get('ect1'):
                raise RuntimeError('non-ECN peer received marked data')
            rows.append(dict(algorithm=algorithm, sender_ecn=left, receiver_ecn=right, goodput_bytes=amount,
                             wire=wire_counts, sender=sender, receiver=receiver))
    aqm = json.loads(lab.tc('-j', '-s', 'qdisc', 'show', 'dev', 'fc0'))
    return dict(status='verified', qdisc=aqm, counters=[aqm_counters(row) for row in aqm],
                seconds=elapsed, capture_packets=packets, capture_drops=drops, flows=rows)


def run():
    from research_runner import exclusive, save
    with exclusive():
        before = (host_identity(), ecn_policy.local())
        path = RESULTS/f'ecn-probe-{time.time_ns()}'
        owned_directory(path)
        result = dict(schema=1, artifact=str(path), cases={})
        try:
            for name, configuration in CASES.items():
                directory = path/name
                owned_directory(directory)
                lab = Lab(directory)
                try:
                    result['cases'][name] = case(lab, name, configuration)
                except (OSError, ValueError, subprocess.CalledProcessError) as error:
                    result['cases'][name] = dict(status='unavailable', reason=getattr(error, 'stderr', None) or str(error))
                except RuntimeError as error:
                    result['cases'][name] = dict(status='invalid', reason=str(error))
                finally:
                    lab.close()
        finally:
            result['host_unchanged'] = before == (host_identity(), ecn_policy.local())
            save(path/'summary.json', result)
        return result
