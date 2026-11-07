"""Compare existing socket and TC mechanisms in disposable veth namespaces."""
import argparse
from contextlib import ExitStack
import errno
import hashlib
import json
import os
from pathlib import Path
import random
import resource
import selectors
import socket
import struct
import subprocess
import sys
import time

from benchmark import BINDIR, Lab, RESULTS, TC, owned_directory, run
from helper_netlink import QDISC
from network_state import attrs, dump
from research_runner import exclusive, metadata
from tcp_diag import tcp_info

SERVER = '10.203.0.2'
BULK = b'bulk'.ljust(64, b'.')
URGENT = b'urgent'.ljust(64, b'.')
SO_TXTIME = getattr(socket, 'SO_TXTIME', 61)
SO_MAX_PACING_RATE = getattr(socket, 'SO_MAX_PACING_RATE', 47)
CLOCK_TAI = getattr(time, 'CLOCK_TAI', 11)
IP_RECVERR = getattr(socket, 'IP_RECVERR', 11)
SOF_TXTIME_REPORT_ERRORS = 2
SO_EE_ORIGIN_TXTIME = 6
NS_PER_MS = 1_000_000


def require(condition, explanation):
    if not condition:
        raise RuntimeError(explanation)


def open_socket(lab, stack, namespace, *args):
    with lab.enter(namespace):
        sock = stack.enter_context(socket.socket(*args))
    sock.settimeout(3)
    return sock


def tcp_pair(lab, stack, listener):
    sender = open_socket(lab, stack, 0, socket.AF_INET, socket.SOCK_STREAM)
    sender.setsockopt(socket.SOL_SOCKET, socket.SO_PRIORITY, 6)
    sender.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    sender.connect(listener.getsockname())
    receiver = stack.enter_context(listener.accept()[0])
    receiver.settimeout(3)
    return sender, receiver


def tcp_listener(lab, stack, port=0):
    listener = open_socket(lab, stack, 1, socket.AF_INET, socket.SOCK_STREAM)
    listener.bind((SERVER, port))
    listener.listen(2)
    return listener


def udp_pair(lab, stack, port=0):
    receiver = open_socket(lab, stack, 1, socket.AF_INET, socket.SOCK_DGRAM)
    receiver.bind((SERVER, port))
    sender = open_socket(lab, stack, 0, socket.AF_INET, socket.SOCK_DGRAM)
    sender.connect(receiver.getsockname())
    return sender, receiver


def observe_order(packet, receivers, origin):
    """Observe the first copy of each marker at RX and at the application."""
    wire, application = {}, {}
    pending = {sock: bytearray() for sock in receivers}
    with selectors.DefaultSelector() as poll:
        poll.register(packet, selectors.EVENT_READ)
        for sock in receivers:
            poll.register(sock, selectors.EVENT_READ)
        deadline = time.monotonic() + 2
        while len(wire) < 2 or len(application) < 2:
            remaining = deadline - time.monotonic()
            require(remaining > 0, 'missing receiver marker')
            for key, _ in poll.select(remaining):
                sock = key.fileobj
                if sock is packet:
                    ip = sock.recv(2048)
                    observed = time.monotonic_ns()
                    if len(ip) < 40 or ip[0] >> 4 != 4 or ip[9] != socket.IPPROTO_TCP:
                        continue
                    offset = (ip[0] & 15) * 4
                    if len(ip) < offset + 20:
                        continue
                    sequence, = struct.unpack_from('!I', ip, offset + 4)
                    end_header = offset + (ip[offset + 12] >> 4) * 4
                    ip_length, = struct.unpack_from('!H', ip, 2)
                    payload = ip[end_header:ip_length]
                    if payload in (BULK, URGENT):
                        label = 'bulk' if payload == BULK else 'urgent'
                        wire.setdefault(label, dict(ms=(observed-origin)/NS_PER_MS, seq=sequence))
                else:
                    data = sock.recv(1024)
                    observed = time.monotonic_ns()
                    require(data, 'unexpected EOF before both markers')
                    pending[sock].extend(data)
                    while len(pending[sock]) >= len(BULK):
                        payload = bytes(pending[sock][:len(BULK)])
                        del pending[sock][:len(BULK)]
                        require(payload in (BULK, URGENT), 'unexpected application payload')
                        label = 'bulk' if payload == BULK else 'urgent'
                        require(label not in application, 'duplicate application marker')
                        application[label] = (observed-origin)/NS_PER_MS
                        if not pending[sock] and (len(receivers) == 2 or len(application) == 2):
                            poll.unregister(sock)
    return dict(wire=wire, application_ms=application)


def stream_trial(lab, mode):
    with ExitStack() as stack:
        listener = tcp_listener(lab, stack)
        sender, receiver = tcp_pair(lab, stack, listener)
        urgent_sender, urgent_receiver = (tcp_pair(lab, stack, listener)
                                          if mode == 'split' else (sender, receiver))
        packet = open_socket(lab, stack, 1, socket.AF_PACKET, socket.SOCK_DGRAM,
                             socket.htons(0x0800))
        packet.bind(('fc1', 0x0800))
        sender.setsockopt(socket.SOL_SOCKET, socket.SO_PRIORITY, 0)
        sender.sendall(BULK)
        # Explicitly push small writes even when the earlier skb is still queued.
        sender.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        start = time.monotonic_ns()
        urgent_sender.setsockopt(socket.SOL_SOCKET, socket.SO_PRIORITY, 6)
        urgent_sender.sendall(URGENT)
        urgent_sender.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        receivers = [receiver, urgent_receiver] if mode == 'split' else [receiver]
        row = observe_order(packet, receivers, start)
    wire, app = row['wire'], row['application_ms']
    require(wire['bulk']['ms'] - wire['urgent']['ms'] > 100,
            'invalid fixture: urgent packet did not bypass the delayed bulk packet')
    require(app['urgent'] < 100 if mode == 'split' else app['urgent'] >= 100,
            'unexpected stream delivery relative to the 100 ms budget')
    require(list(app) == (['urgent', 'bulk'] if mode == 'split' else ['bulk', 'urgent']),
            'unexpected application delivery order')
    if mode == 'shared':
        require((wire['urgent']['seq'] - wire['bulk']['seq']) % 2**32 == len(BULK),
                'expected consecutive sequence ranges on the shared TCP connection')
    return dict(mode=mode, **row)


def queue_pulse(lab):
    lab.tc('qdisc', 'add', 'dev', 'fc0', 'root', 'handle', '1:', 'prio',
           'bands', '2', 'priomap', *(['0']*16))
    lab.tc('qdisc', 'add', 'dev', 'fc0', 'parent', '1:2', 'handle', '20:',
           'netem', 'delay', '250ms')
    with ExitStack() as stack:
        sender, receiver = udp_pair(lab, stack)
        sender.setsockopt(socket.SOL_SOCKET, socket.SO_PRIORITY, 6)
        sender.send(b'warm')
        require(receiver.recv(32) == b'warm', 'queue warmup failed')
        lab.tc('filter', 'add', 'dev', 'fc0', 'parent', '1:', 'protocol', 'ip',
               'pref', '1', 'flower', 'ip_proto', 'udp', 'dst_port',
               receiver.getsockname()[1], 'classid', '1:2')
        sock = open_socket(lab, stack, 0, socket.AF_NETLINK, socket.SOCK_RAW, socket.NETLINK_ROUTE)
        sock.bind((0, 0))
        with lab.enter(0):
            index = socket.if_nametoindex('fc0')
        request = QDISC.pack(socket.AF_UNSPEC, 0, 0, index, 0, 0, 0)

        def backlog():
            # RTM_GETQDISC / RTM_NEWQDISC. Consume the whole multipart reply.
            rows = list(dump(sock, 38, request, max_datagrams=8))
            for kind, body in rows:
                _, _, _, interface, handle, _, _ = QDISC.unpack_from(body)
                if kind == 36 and interface == index and handle == 0x200000:
                    stats = attrs(attrs(body[QDISC.size:])[7])
                    qlen, byte_count = struct.unpack_from('=2I', stats[3])
                    return dict(qlen=qlen, backlog_bytes=byte_count)
            raise RuntimeError('owned netem leaf missing')

        before = backlog()
        sender.setsockopt(socket.SOL_SOCKET, socket.SO_PRIORITY, 0)
        start = time.monotonic()
        sender.send(BULK)
        samples = []
        for step in range(40):
            time.sleep(max(0, start+step*.01-time.monotonic()))
            samples.append(dict(ms=(time.monotonic()-start)*1000, **backlog()))
        require(receiver.recv(128) == BULK, 'queue pulse payload missing')
        time.sleep(max(0, start+1-time.monotonic()))
        after = dict(ms=(time.monotonic()-start)*1000, **backlog())
        require(before['backlog_bytes'] == after['backlog_bytes'] == 0 and
                any(sample['backlog_bytes'] > 0 for sample in samples),
                f'expected short backlog pulse: before={before}, after={after}, '
                f'peak={max(sample["backlog_bytes"] for sample in samples)}')
    lab.tc('qdisc', 'del', 'dev', 'fc0', 'root')
    return dict(before=before, after=after, samples=samples)


def priority_trials(lab, repetitions, rng, output):
    priomap = ['0' if index == 6 else '1' for index in range(16)]
    lab.tc('qdisc', 'replace', 'dev', 'fc0', 'root', 'handle', '1:', 'prio',
           'bands', '2', 'priomap', *priomap)
    lab.tc('qdisc', 'add', 'dev', 'fc0', 'parent', '1:2', 'handle', '20:',
           'netem', 'delay', '250ms')
    with (output/'stream-runs.jsonl').open('w') as stream:
        rows = []
        for rep in range(repetitions):
            for mode in rng.sample(['shared', 'split'], 2):
                row = dict(repetition=rep, **stream_trial(lab, mode))
                rows.append(row)
                stream.write(json.dumps(row) + '\n')
                stream.flush()

    with ExitStack() as stack:
        sender, receiver = udp_pair(lab, stack)
        sender.setsockopt(socket.SOL_SOCKET, socket.SO_PRIORITY, 0)
        sender.send(BULK)
        start = time.monotonic_ns()
        sender.sendmsg([URGENT], [(socket.SOL_SOCKET, socket.SO_PRIORITY, struct.pack('=I', 6))])
        udp = []
        for _ in range(2):
            data = receiver.recv(128)
            udp.append((data, (time.monotonic_ns()-start)/NS_PER_MS))
        require([data for data, _ in udp] == [URGENT, BULK] and udp[1][1]-udp[0][1] > 100,
                'UDP ancillary priority did not select the fast band')
        require(sender.getsockopt(socket.SOL_SOCKET, socket.SO_PRIORITY) == 0,
                'ancillary priority changed the socket default')
        listener = tcp_listener(lab, stack)
        sender, receiver = tcp_pair(lab, stack, listener)
        sender.setsockopt(socket.SOL_SOCKET, socket.SO_PRIORITY, 0)
        start = time.monotonic_ns()
        sent = sender.sendmsg([URGENT], [(socket.SOL_SOCKET, socket.SO_PRIORITY, struct.pack('=I', 6))])
        require(sent == len(URGENT) and receiver.recv(len(URGENT), socket.MSG_WAITALL) == URGENT,
                'TCP ancillary priority payload failed')
        tcp_ms = (time.monotonic_ns()-start)/NS_PER_MS
    lab.tc('filter', 'add', 'dev', 'fc0', 'parent', '1:', 'protocol', 'ip',
           'pref', '1', 'handle', '0x2a', 'fw', 'classid', '1:1')
    with ExitStack() as stack:
        sender, receiver = udp_pair(lab, stack)
        sender.send(BULK)
        start = time.monotonic_ns()
        sender.setsockopt(socket.SOL_SOCKET, socket.SO_MARK, 0x2a)
        sender.send(URGENT)
        require(receiver.recv(128) == URGENT, 'mark did not select the fast class')
        marked_ms = (time.monotonic_ns()-start)/NS_PER_MS
        require(receiver.recv(128) == BULK, 'unmarked payload missing')
        unmarked_ms = (time.monotonic_ns()-start)/NS_PER_MS
        require(unmarked_ms-marked_ms > 100, 'mark fixture lacks separation')
    return dict(stream=rows, marked_udp_ms=marked_ms,
                unmarked_udp_ms=unmarked_ms, udp_urgent_ms=udp[0][1], udp_bulk_ms=udp[1][1],
                tcp_cmsg_receive_ms=tcp_ms, qdiscs=json.loads(lab.tc('-s', '-j', 'qdisc', 'show', 'dev', 'fc0')))


def txtime_trial(lab):
    with ExitStack() as stack:
        sender, receiver = udp_pair(lab, stack)
        # Resolve the peer before installing ETF, which rejects untagged traffic.
        sender.setsockopt(socket.SOL_SOCKET, socket.SO_PRIORITY, 6)
        sender.send(b'warm')
        require(receiver.recv(32) == b'warm', 'UDP warmup failed')
        lab.tc('qdisc', 'del', 'dev', 'fc0', 'root')
        lab.tc('qdisc', 'add', 'dev', 'fc0', 'root', 'handle', '1:', 'prio',
               'bands', '2', 'priomap', *(['1']*16))
        lab.tc('filter', 'add', 'dev', 'fc0', 'parent', '1:', 'protocol', 'ip',
               'pref', '1', 'flower', 'ip_proto', 'udp', 'dst_port',
               receiver.getsockname()[1], 'classid', '1:1')
        lab.tc('qdisc', 'add', 'dev', 'fc0', 'parent', '1:1', 'handle', '30:',
               'etf', 'clockid', 'CLOCK_TAI', 'delta', '10000000')
        sender.setsockopt(socket.IPPROTO_IP, IP_RECVERR, 1)
        sender.setsockopt(socket.SOL_SOCKET, SO_TXTIME, struct.pack('=iI', CLOCK_TAI, SOF_TXTIME_REPORT_ERRORS))
        start = time.monotonic_ns()
        target = time.clock_gettime_ns(CLOCK_TAI) + 150*NS_PER_MS
        sender.sendmsg([URGENT], [(socket.SOL_SOCKET, SO_TXTIME, struct.pack('=Q', target))])
        require(receiver.recv(128) == URGENT, 'ETF future payload mismatch')
        arrival = (time.monotonic_ns()-start)/NS_PER_MS
        require(130 <= arrival <= 500, 'ETF delivery outside the fixture timing bound')
        expired = time.clock_gettime_ns(CLOCK_TAI) - 100*NS_PER_MS
        send_errno = None
        try:
            sender.sendmsg([BULK], [(socket.SOL_SOCKET, SO_TXTIME, struct.pack('=Q', expired))])
        except OSError as error:
            if error.errno != errno.ENOBUFS:
                raise
            send_errno = error.errno
        with selectors.PollSelector() as poll:
            poll.register(sender, selectors.EVENT_READ)
            require(poll.select(1), 'ETF missing transmit-time error notification')
        _, ancillary, _, _ = sender.recvmsg(128, 128, socket.MSG_ERRQUEUE)
        errors = [struct.unpack_from('=IBBBBII', data) for level, kind, data in ancillary
                  if level == socket.IPPROTO_IP and kind == IP_RECVERR and len(data) >= 16]
        require(any(error[1] == SO_EE_ORIGIN_TXTIME and error[3] in (1, 2) for error in errors),
                'missing SO_EE_ORIGIN_TXTIME rejection')
        with selectors.DefaultSelector() as poll:
            poll.register(receiver, selectors.EVENT_READ)
            require(not poll.select(.05), 'expired ETF datagram reached the application')
    return dict(future_delay_ms=arrival, expired_send_errno=send_errno, error_queue=errors,
                qdiscs=json.loads(lab.tc('-s', '-j', 'qdisc', 'show', 'dev', 'fc0')))


def pacing_trials(lab, rng):
    lab.tc('qdisc', 'replace', 'dev', 'fc0', 'root', 'fq')
    txtime = {}
    with ExitStack() as stack:
        listener = tcp_listener(lab, stack)
        tcp = tcp_pair(lab, stack, listener)
        udp = udp_pair(lab, stack)
        for protocol, (sender, receiver) in [('tcp', tcp), ('udp', udp)]:
            sender.setsockopt(socket.SOL_SOCKET, SO_TXTIME, struct.pack('=iI', time.CLOCK_MONOTONIC, 0))
            start = time.monotonic_ns()
            control = [(socket.SOL_SOCKET, SO_TXTIME, struct.pack('=Q', start+150*NS_PER_MS))]
            require(sender.sendmsg([URGENT], control) == len(URGENT), 'FQ timed send failed')
            require(receiver.recv(len(URGENT), socket.MSG_WAITALL) == URGENT, 'FQ timed receive failed')
            txtime[protocol] = (time.monotonic_ns()-start)/NS_PER_MS
    rows = []
    payload = b'x' * 262144
    for rep in range(5):
        for cap in rng.sample([None, 262144], 2):
            with ExitStack() as stack:
                listener = tcp_listener(lab, stack)
                sender, receiver = tcp_pair(lab, stack, listener)
                if cap is not None:
                    sender.setsockopt(socket.SOL_SOCKET, SO_MAX_PACING_RATE, struct.pack('=Q', cap))
                    actual, = struct.unpack('=Q', sender.getsockopt(socket.SOL_SOCKET, SO_MAX_PACING_RATE, 8))
                    require(actual == cap, 'pacing rate readback mismatch')
                sender.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, len(payload)*2)
                start = time.monotonic_ns()
                sender.sendall(payload)
                received = bytearray()
                while len(received) < len(payload):
                    data = receiver.recv(len(payload)-len(received))
                    require(data, 'unexpected TCP EOF during pacing')
                    received.extend(data)
                elapsed = (time.monotonic_ns()-start)/1e9
                require(received == payload, 'pacing payload mismatch')
                rows.append(dict(repetition=rep, cap_bytes_per_second=cap, bytes=len(received),
                                 seconds=elapsed, goodput_mbps=len(received)*8/elapsed/1e6,
                                 tcp_info=tcp_info(sender.getsockopt(socket.IPPROTO_TCP, socket.TCP_INFO, 512))))
    return dict(runs=rows, requested_txtime_ms=150, txtime_receive_ms=txtime, qdiscs=json.loads(lab.tc('-s', '-j', 'qdisc', 'show', 'dev', 'fc0')))


def native_classes(sock, index):
    classes = {}
    request = QDISC.pack(socket.AF_UNSPEC, 0, 0, index, 0, 0, 0)
    # RTM_GETTCLASS / RTM_NEWTCLASS, TCA_STATS2 and nested gnet basic stats.
    for kind, body in dump(sock, 42, request, max_datagrams=8):
        _, _, _, interface, handle, _, _ = QDISC.unpack_from(body)
        require(kind == 40 and interface == index and len(classes) < 16,
                'unexpected class dump response')
        a = attrs(body[QDISC.size:])
        if a.get(1) != b'htb\0':
            continue  # The same dump can contain fq_codel flow classes.
        stats = attrs(a[7])
        byte_count, packet_count = struct.unpack_from('=QI', stats[1])
        if 8 in stats:  # TCA_STATS_PKT64
            packet_count, = struct.unpack('=Q', stats[8])
        qlen, backlog, drops, requeues, overlimits = struct.unpack_from('=5I', stats[3])
        classes[f'{handle >> 16:x}:{handle & 0xffff:x}'] = dict(
            bytes=byte_count, packets=packet_count, qlen=qlen, backlog=backlog,
            drops=drops, requeues=requeues, overlimits=overlimits)
    return classes


def feedback_trials(lab, rng, pairs, output):
    lab.configure('static40')
    lab.tc('filter', 'add', 'dev', 'fc0', 'parent', '1:', 'protocol', 'ip',
           'pref', '11', 'flower', 'ip_proto', 'udp', 'dst_port', '9000', 'classid', '1:10')
    with ExitStack() as stack:
        for port, marker in [(0, BULK), (9000, URGENT)]:
            sender, receiver = udp_pair(lab, stack, port)
            sender.send(marker)
            require(receiver.recv(128) == marker, 'HTB counter fixture payload missing')

    with ExitStack() as stack:
        sock = open_socket(lab, stack, 0, socket.AF_NETLINK, socket.SOCK_RAW, socket.NETLINK_ROUTE)
        sock.bind((0, 0))
        with lab.enter(0):
            index = socket.if_nametoindex('fc0')
        tc = lab.ns(0, [TC, '-s', '-j', 'class', 'show', 'dev', 'fc0'])
        def cli():
            rows = json.loads(run(tc))
            try:
                keys = ('bytes', 'packets', 'qlen', 'backlog', 'drops', 'requeues', 'overlimits')
                return {row['handle']: {key: row['stats'][key] for key in keys}
                        for row in rows if row['class'] == 'htb'}
            except KeyError:
                (output/'tc-class-unexpected.json').write_text(json.dumps(rows, indent=2)+'\n')
                raise
        backends = {'tc': cli, 'rtnetlink': lambda: native_classes(sock, index)}
        expected = cli()
        require(set(expected) == {'1:1', '1:10', '1:20'} and all(row['bytes'] > 0 and row['packets'] > 0 for row in expected.values()),
                'expected nonzero HTB counters')
        require(backends['rtnetlink']() == expected, 'rtnetlink disagrees with tc')
        samples = []
        for rep in range(pairs):
            for method in rng.sample(list(backends), 2):
                start_wall, start_cpu = time.monotonic_ns(), cpu_ns()
                counters = backends[method]()
                cpu = cpu_ns() - start_cpu
                wall = time.monotonic_ns() - start_wall
                if counters != expected:
                    (output/'counter-mismatch.json').write_text(json.dumps(
                        dict(repetition=rep, method=method, expected=expected, actual=counters),
                        indent=2)+'\n')
                    raise RuntimeError('class counters changed or decoders disagreed')
                samples.append(dict(repetition=rep, method=method, wall_ns=wall, cpu_ns=cpu))
    lab.tc('qdisc', 'del', 'dev', 'fc0', 'root')
    return dict(counters=expected, samples=samples)


def cpu_ns():
    children = resource.getrusage(resource.RUSAGE_CHILDREN)
    return time.process_time_ns() + int((children.ru_utime+children.ru_stime)*1e9)


def permission_trial(lab):
    code = r"""
import json, socket, struct, time
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
choices = [
    ('priority_6', socket.SO_PRIORITY, 6),
    ('priority_7', socket.SO_PRIORITY, 7),
    ('mark', socket.SO_MARK, 1),
    ('txtime_monotonic', getattr(socket, 'SO_TXTIME', 61), struct.pack('=iI', time.CLOCK_MONOTONIC, 0)),
    ('txtime_invalid_flags', getattr(socket, 'SO_TXTIME', 61), struct.pack('=iI', time.CLOCK_MONOTONIC, 1 << 31)),
    ('txtime_tai', getattr(socket, 'SO_TXTIME', 61), struct.pack('=iI', getattr(time, 'CLOCK_TAI', 11), 0)),
]
out = {}
for name, option, value in choices:
    try:
        s.setsockopt(socket.SOL_SOCKET, option, value)
        out[name] = 0
    except OSError as error:
        out[name] = error.errno
with open('/proc/self/status') as status:
    out['effective_caps'] = int(next(line.split()[1] for line in status if line.startswith('CapEff:')), 16)
print(json.dumps(out))
"""
    result = json.loads(lab.command(0, ['setpriv', '--reuid=65534', '--regid=65534', '--clear-groups',
                                        '--bounding-set=-all', '--inh-caps=-all', '--ambient-caps=-all',
                                        '--no-new-privs', sys.executable, '-I', '-B', '-c', code]))
    require(result['effective_caps'] == 0, 'permission fixture has capabilities')
    return result


def host_identity():
    return dict(
        boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
        links=json.loads(run(['ip', '-j', 'link', 'show'])),
        qdiscs=[{key: row.get(key) for key in ('dev', 'kind', 'handle', 'parent', 'options')}
                for row in json.loads(run([TC, '-j', 'qdisc', 'show']))],
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repetitions', type=int, default=12)
    parser.add_argument('--feedback-pairs', type=int, default=40)
    parser.add_argument('--seed', type=int, default=870493)
    args = parser.parse_args()
    if not 1 <= args.repetitions <= 30 or not 1 <= args.feedback_pairs <= 100:
        parser.error('expected 1..30 repetitions and 1..100 feedback pairs')
    rng = random.Random(args.seed)
    with exclusive():
        output = RESULTS/f'kernel-api-{time.time_ns()}'
        owned_directory(output)
        print(f'Evidence: {output}', flush=True)
        lab = Lab(output)
        host_before = host_identity()
        original_ns = os.stat('/proc/self/ns/net').st_ino
        result = dict(environment=metadata(), inputs=vars(args), status='running')
        try:
            lab.start()
            for index, interface in enumerate(('fc0', 'fc1')):
                lab.command(index, ['sysctl', '-w', f'net.ipv6.conf.{interface}.disable_ipv6=1'])
            result['permissions'] = permission_trial(lab)
            result['feedback'] = feedback_trials(lab, rng, args.feedback_pairs, output)
            result['queue_pulse'] = queue_pulse(lab)
            result['priority'] = priority_trials(lab, args.repetitions, rng, output)
            result['txtime'] = txtime_trial(lab)
            result['pacing'] = pacing_trials(lab, rng)
            storage = BINDIR/'kernel_socket_storage_probe'
            result['socket_storage'] = (json.loads(lab.command(0, [storage])) if storage.is_file()
                                        else dict(status='not_run', reason='optional libbpf probe not built'))
            if storage.is_file():
                result['socket_storage']['binary_sha256'] = hashlib.sha256(storage.read_bytes()).hexdigest()
            result['status'] = 'complete'
        except BaseException as error:
            detail = error.stderr.strip() if isinstance(error, subprocess.CalledProcessError) else str(error)
            result.update(status='invalid', error=detail)
            raise
        finally:
            lab.close()
            result['cleanup'] = dict(holders_reaped=all(holder.poll() is not None for holder in lab.holders),
                                     original_namespace=os.stat('/proc/self/ns/net').st_ino == original_ns,
                                     host_identity_unchanged=host_identity() == host_before)
            if not all(result['cleanup'].values()):
                result.update(status='invalid', error='lab cleanup or host identity check failed')
            (output/'summary.json').write_text(json.dumps(result, indent=2)+'\n')
    require(result['status'] == 'complete', result.get('error', 'experiment incomplete'))
    print('PASS: stock Linux socket and TC experiments', flush=True)


if __name__ == '__main__':
    main()
