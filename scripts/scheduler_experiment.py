#!/usr/bin/env python3
"""Paired, isolated application-intent scheduling experiment using stock Linux."""
import argparse
from contextlib import ExitStack
from pathlib import Path
import errno
import json
import os
import random
import resource
import selectors
import socket
import statistics
import struct
import sys
import time

from benchmark import GID, UID, Lab, RESULTS, owned_directory
from kernel_api_experiment import host_identity, open_socket, require, txtime_trial
from research_runner import exclusive, metadata, save
from research_stats import median_ci
from workload import percentile

MODES = ('fq_codel', 'cake', 'cake-diffserv', 'htb-static', 'intent')
CAKE_MODES = {'cake': 'besteffort', 'cake-diffserv': 'diffserv4'}
CAPACITY = 12000
DEADLINE_NS = 12_000_000
SO_TIMESTAMPING_NEW = 65
SCM_TS_OPT_ID = 81
TX_SOFTWARE, SOFTWARE, OPT_ID, TX_SCHED, OPT_TSONLY = (1 << n for n in (1, 4, 7, 8, 11))
ORIGIN_TIMESTAMPING = 4
PACKET = struct.Struct('!BI')
PROBE, ROUTINE, BACKGROUND = 0, 1, 2
BG_FLOWS = 4
ROUTINE_BYTES, PROBE_BYTES = 1200, 256
PRIORITY = getattr(socket, 'SO_PRIORITY', 12)
MARK = getattr(socket, 'SO_MARK', 36)


def urgent(deadline_ns, now_ns):
    """Only the admitted producer can label a datagram, within a fixed horizon."""
    slack = deadline_ns - now_ns
    if not 0 < slack <= 100_000_000:
        raise ValueError('deadline outside allowed horizon')
    return slack <= DEADLINE_NS


def offer(seed, duration):
    rng = random.Random(seed)
    # All offsets are frozen before a trial, with more contention in its middle.
    rows = []
    for kind, flow, rate in [(PROBE, 0, 120), (ROUTINE, 0, 1550)] + [
            (BACKGROUND, i, 430) for i in range(BG_FLOWS)]:
        period = 1_000_000_000 // rate
        for index, nominal in enumerate(range(period, duration*10**9, period)):
            if kind != PROBE and duration*10**9//3 <= nominal < 2*duration*10**9//3:
                nominal -= (index % 4)*period
            rows.append((nominal+rng.randrange(period//4), kind, flow))
    return sorted(rows)


def readback(lab):
    rows = {kind: json.loads(lab.tc('-j', '-s', kind, 'show', 'dev', 'fc0'))
            for kind in ('qdisc', 'class')}
    # tc's basic/ematch printer on Fedora emits invalid JSON for this expression.
    rows['filter'] = lab.tc('-s', 'filter', 'show', 'dev', 'fc0', 'parent', '1:')
    return rows


def class_change(lab, handle, rate, ceil, parent='1:1', priority=0):
    lab.tc('class', 'change', 'dev', 'fc0', 'parent', parent, 'classid', handle, 'htb',
           'rate', f'{rate}kbit', 'ceil', f'{ceil}kbit', 'prio', str(priority),
           'burst', '16k', 'cburst', '16k', 'quantum', '1514')


def install(lab, mode):
    if mode in CAKE_MODES:
        lab.tc('qdisc', 'add', 'dev', 'fc0', 'root', 'handle', '1:', 'cake',
               'bandwidth', f'{CAPACITY}kbit', CAKE_MODES[mode], 'flows', 'rtt', '40ms', 'memlimit', '1mb')
        return
    lab.tc('qdisc', 'add', 'dev', 'fc0', 'root', 'handle', '1:', 'htb', 'default', '20')

    def add(parent, minor, rate, ceil, priority=0):
        lab.tc('class', 'add', 'dev', 'fc0', 'parent', parent, 'classid', f'1:{minor}', 'htb',
               'rate', f'{rate}kbit', 'ceil', f'{ceil}kbit', 'prio', str(priority),
               'burst', '16k', 'cburst', '16k', 'quantum', '1514')

    add('1:', 1, CAPACITY, CAPACITY)
    if mode == 'fq_codel':
        classes = [(20, CAPACITY, CAPACITY, 0)]
    else:
        classes = [(10, CAPACITY*3//10, CAPACITY*6//10, 0),
                   (20, CAPACITY*7//10, CAPACITY, 1)]
        match = ['meta(nf_mark eq 42)']
        if mode == 'intent':
            match.extend(('and', 'meta(priority eq 6)'))
    for minor, rate, ceil, prio in classes:
        add('1:1', minor, rate, ceil, prio)
        lab.tc('qdisc', 'add', 'dev', 'fc0', 'parent', f'1:{minor}', 'handle', f'{minor}:',
               'fq_codel', 'limit', '256', 'memory_limit', '1mb', 'target', '5ms', 'interval', '100ms')

    if mode != 'fq_codel':
        lab.tc('filter', 'add', 'dev', 'fc0', 'parent', '1:', 'protocol', 'ip', 'pref', '10',
               'basic', 'match', *match, 'flowid', '1:10')


def verify(rows, mode, capacity):
    root = next(item for item in rows['qdisc'] if item.get('root'))
    if mode in CAKE_MODES:
        require(root['kind'] == 'cake' and root['options']['bandwidth'] == capacity*125
                and root['options']['diffserv'] == CAKE_MODES[mode]
                and root['memory_limit'] == 1024*1024, 'CAKE readback disagrees')
        return
    rates = {row['handle']: (row['rate'], row['ceil'], row.get('prio', 0))
             for row in rows['class'] if row.get('class') == 'htb'}
    # Linux reports byte/s.
    desired = {'1:1': (capacity*125, capacity*125, 0)}
    if mode == 'fq_codel':
        desired['1:20'] = desired['1:1']
    else:
        desired['1:10'] = (capacity*3//10*125, capacity*6//10*125, 0)
        desired['1:20'] = (capacity*7//10*125, capacity*125, 1)
        filters = rows['filter']
        require(filters.count('flowid 1:10') == 1 and 'basic' in filters and 'meta(nf_mark eq 42)' in filters
                and ('AND meta(priority eq 6)' in filters) == (mode == 'intent'),
                f'admission filter readback disagrees: {filters}')
    leaves = {row['parent']: row for row in rows['qdisc'] if 'parent' in row}
    require(root['kind'] == 'htb' and root['options']['default'] == '0x20' and rates == desired
            and leaves.keys() == desired.keys()-{'1:1'}
            and all(row['kind'] == 'fq_codel' and row['options']['limit'] == 256
                    and row['options']['memory_limit'] == 1024*1024 for row in leaves.values()),
            'HTB readback disagrees')


def timestamps(sock, samples):
    while True:
        try:
            _, ancillary, flags, _ = sock.recvmsg(1, 256, socket.MSG_ERRQUEUE)
        except BlockingIOError:
            return
        stamp = status = None
        for level, kind, value in ancillary:
            if (level, kind) == (socket.SOL_SOCKET, SO_TIMESTAMPING_NEW) and len(value) == 48:
                seconds, nanos = struct.unpack_from('=qq', value)
                stamp = seconds*10**9+nanos
            if (level, kind) == (socket.IPPROTO_IP, 11) and len(value) >= 16:  # IP_RECVERR
                _, origin, _, _, _, stage, index = struct.unpack_from('=IBBBBII', value)
                if origin == ORIGIN_TIMESTAMPING:
                    status = (index, stage)
        if not flags & socket.MSG_CTRUNC and stamp and status and status[0] in samples and status[1] in (0, 1):
            pair = samples[status[0]]
            pair[status[1]] = stamp if pair[status[1]] is None else min(stamp, pair[status[1]])


def usage():
    sample = resource.getrusage(resource.RUSAGE_SELF)
    return sample.ru_utime+sample.ru_stime, sample.ru_maxrss


def collapse(lab, mode):
    require(mode == 'intent', 'capacity transition only implemented for intent HTB')
    capacity = CAPACITY//2
    began = time.monotonic_ns()
    # Child guarantees fit before the parent is lowered. The old tree stays attached.
    class_change(lab, '1:10', capacity*3//10, capacity*6//10)
    class_change(lab, '1:20', capacity*7//10, capacity, priority=1)
    class_change(lab, '1:1', capacity, capacity, parent='1:')
    rows = readback(lab)
    verify(rows, mode, capacity)
    return dict(elapsed_ns=time.monotonic_ns()-began, capacity_kbit=capacity, readback=rows)


def workload(lab, rows, duration, mode, capacity_collapse=False):
    with ExitStack() as stack:
        poll = stack.enter_context(selectors.DefaultSelector())
        receiver = open_socket(lab, stack, 1, socket.AF_INET, socket.SOCK_DGRAM)
        receiver.bind(('10.203.0.2', 0))
        receiver.setblocking(False)
        poll.register(receiver, selectors.EVENT_READ, True)
        sender = open_socket(lab, stack, 0, socket.AF_INET, socket.SOCK_DGRAM)
        sender.setsockopt(socket.SOL_SOCKET, MARK, 42)
        # Bound socket memory consistently and let qdisc contention dominate.
        sender.setsockopt(socket.SOL_SOCKET, 32, 1024*1024)  # SO_SNDBUFFORCE
        sender.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 256*1024)
        sndbuf = sender.getsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF)
        sender.setsockopt(socket.SOL_SOCKET, SO_TIMESTAMPING_NEW, SOFTWARE|OPT_ID|OPT_TSONLY)
        sender.setblocking(False)
        sender.connect(receiver.getsockname())
        poll.register(sender, selectors.EVENT_READ, False)
        background = []
        for _ in range(BG_FLOWS):
            sock = open_socket(lab, stack, 0, socket.AF_INET, socket.SOCK_DGRAM)
            sock.setblocking(False)
            sock.connect(receiver.getsockname())
            background.append(sock)
        sent, completed, tx = {}, {}, {}
        delivered = [0]*BG_FLOWS
        windows = [[0]*BG_FLOWS for _ in range(duration*4)]
        sends_failed = [0, 0, 0]
        send_errors = {}
        delay_samples = []
        routine = 0
        index = 0
        update = None
        cpu_before, _ = usage()
        start = time.monotonic_ns()
        until = start+duration*10**9+200_000_000
        while time.monotonic_ns() < until:
            now = time.monotonic_ns()
            if capacity_collapse and update is None and now >= start+duration*10**9//2:
                update = collapse(lab, mode)
                update['actual_offset_ns'] = now-start
                now = time.monotonic_ns()
            stop = min(index+64, len(rows))
            while index < stop and start+rows[index][0] <= now:
                offset, kind, flow = rows[index]
                ident = index
                index += 1
                packet = PACKET.pack(kind, ident).ljust(PROBE_BYTES if kind == PROBE else ROUTINE_BYTES, b'x')
                sock = background[flow] if kind == BACKGROUND else sender
                control = []
                before = time.monotonic_ns()
                delay_samples.append(max(0, before-start-offset)/1e6)
                if kind == PROBE:
                    sent[ident] = before
                    tx[ident] = [None, None]
                    priority = 6 if urgent(before+DEADLINE_NS, before) else 0
                    control = [(socket.IPPROTO_IP, socket.IP_TOS, struct.pack('=i', 46 << 2)),
                               (socket.SOL_SOCKET, PRIORITY, struct.pack('=i', priority)),
                               (socket.SOL_SOCKET, SO_TIMESTAMPING_NEW, struct.pack('=I', TX_SOFTWARE|TX_SCHED)),
                               (socket.SOL_SOCKET, SCM_TS_OPT_ID, struct.pack('=I', ident))]
                try:
                    sock.sendmsg([packet], control)
                except OSError as error:
                    if error.errno not in (errno.ENOBUFS, errno.EAGAIN):
                        raise
                    sends_failed[kind] += 1
                    name = errno.errorcode[error.errno]
                    send_errors[name] = send_errors.get(name, 0)+1
            timestamps(sender, tx)
            next_send = start+rows[index][0] if index < len(rows) else until
            for key, _ in poll.select(max(0, min(.002, (next_send-time.monotonic_ns())/1e9))):
                for _ in range(256):
                    try:
                        packet, address = key.fileobj.recvfrom(ROUTINE_BYTES)
                    except BlockingIOError:
                        break
                    kind, ident = PACKET.unpack_from(packet)
                    if key.data:
                        if kind == PROBE:
                            receiver.sendto(packet, address)
                        elif kind == ROUTINE:
                            routine += len(packet)
                        elif kind == BACKGROUND:
                            flow = rows[ident][2]
                            slot = (time.monotonic_ns()-start)//250_000_000
                            if slot < len(windows):
                                windows[slot][flow] += len(packet)
                                delivered[flow] += len(packet)
                    else:
                        require(kind == PROBE and ident in sent and ident not in completed, 'unexpected echo')
                        completed[ident] = time.monotonic_ns()-sent[ident]
        timestamps(sender, tx)
        cpu_after, rss = usage()
    offered = sum(kind == PROBE for _, kind, _ in rows)
    completed_ms = [value/1e6 for value in completed.values()]
    sojourn = [(snd-sched)/1e6 for snd, sched in tx.values() if snd is not None and sched is not None and snd >= sched]
    jain = sum(delivered)**2/(BG_FLOWS*sum(value*value for value in delivered)) if any(delivered) else 0
    return dict(offered=offered, sent_to_socket=len(sent)-sends_failed[PROBE], completed=len(completed),
                failure_ratio=1-len(completed)/offered, p99_ms=percentile(completed_ms, .99),
                deadline_attainment=sum(value <= DEADLINE_NS for value in completed.values())/offered,
                probe_queue_span_p99_ms=percentile(sojourn, .99), timestamp_pairs=len(sojourn),
                probe_samples=[dict(id=ident, rtt_ns=completed.get(ident), sched_ns=pair[1], tx_ns=pair[0])
                               for ident, pair in tx.items()],
                offered_packets=len(rows), attempted_packets=index, send_failures=sends_failed, send_errors=send_errors,
                release_lag_p99_ms=percentile(delay_samples, .99),
                background_bytes=delivered, background_mbps=sum(delivered)*8/(duration*1e6),
                background_jain=jain, empty_background_flow_windows=sum(n == 0 for w in windows for n in w),
                routine_bytes=routine, worker_cpu_seconds=cpu_after-cpu_before,
                runner_peak_rss_kib=rss, shared_socket_sndbuf_bytes=sndbuf, capacity_update=update)


def probe_counters(rows):
    return {r['handle']: r['stats']['packets'] for r in rows['class'] if r.get('class') == 'htb'}


def spoof_check(lab):
    """Exercise actual classification with a separate, capability-free process."""
    before = probe_counters(readback(lab))
    code = ('import socket; s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); '
            's.setsockopt(socket.SOL_SOCKET,socket.SO_PRIORITY,6); '
            's.sendto(b"spoof",("10.203.0.2",39001))')
    require(UID != 0 and GID != 0, 'run from a nonroot account using sudo')
    lab.command(0, ['setpriv', f'--reuid={UID}', f'--regid={GID}', '--clear-groups',
                    '--bounding-set=-all', '--no-new-privs', '--', sys.executable, '-I', '-c', code])
    after = probe_counters(readback(lab))
    require(after['1:10'] == before['1:10'] and after['1:20'] == before['1:20']+1,
            'unprivileged priority bypassed the background default')
    return {key: after[key]-before[key] for key in after}


def trial(output, mode, seed, duration, stress=False):
    owned_directory(output)
    lab = Lab(output)
    try:
        lab.start()
        for index, interface in enumerate(('fc0', 'fc1')):
            lab.command(index, ['sysctl', '-w', f'net.ipv6.conf.{interface}.disable_ipv6=1'])
        lab.command(0, ['ping', '-q', '-c', '1', '-W', '1', '10.203.0.2'])
        install(lab, mode)
        before = readback(lab)
        verify(before, mode, CAPACITY)
        spoof = spoof_check(lab) if mode == 'intent' else None
        result = workload(lab, offer(seed, duration), duration, mode, stress)
        after = readback(lab)
        verify(after, mode, CAPACITY//2 if stress else CAPACITY)
        class_delta = {key: value-probe_counters(before).get(key, 0)
                       for key, value in probe_counters(after).items()}
        require(result['offered_packets'] == result['attempted_packets'], 'offered workload was truncated')
        if mode == 'cake-diffserv':
            root = next(row for row in after['qdisc'] if row.get('root'))
            require(result['completed'] <= root['tins'][3]['sent_packets'] <= result['offered'],
                    'CAKE voice tin counters disagree with probe traffic')
        if mode == 'intent':
            require(result['completed'] <= class_delta['1:10'] <= result['offered'],
                    'intent class counters disagree with probe traffic')
        result.update(mode=mode, seed=seed, spoof_class_delta=spoof, class_packet_delta=class_delta,
                      qdisc_drops=sum(row.get('drops', row.get('stats', {}).get('drops', 0)) for row in after['qdisc'] if not row.get('root')
                                      or mode in CAKE_MODES),
                      before=before, after=after)
        return result
    finally:
        lab.close()


def paired_summary(runs):
    fields = ('deadline_attainment', 'failure_ratio', 'p99_ms', 'background_mbps')
    rows = {(row['repetition'], row['mode']): row for row in runs}
    repetitions = sorted({rep for rep, _ in rows})
    comparisons = {}
    for mode in MODES[:-1]:
        metrics = {}
        for metric in fields:
            pairs = []
            for rep in repetitions:
                base, intent = rows[rep, mode], rows[rep, 'intent']
                if metric == 'p99_ms' and (base['failure_ratio'] or intent['failure_ratio']):
                    continue
                if base[metric] is not None and intent[metric] is not None:
                    pairs.append(intent[metric]-base[metric])
            metrics[metric] = dict(paired_differences=pairs, median=statistics.median(pairs) if pairs else None,
                                   median_ci_95=median_ci(pairs))
        comparisons[mode] = metrics
    return comparisons


def capabilities(path):
    lab = Lab(path)
    before = host_identity()
    result = {'status': 'invalid'}
    try:
        lab.start()
        for index, interface in enumerate(('fc0', 'fc1')):
            lab.command(index, ['sysctl', '-w', f'net.ipv6.conf.{interface}.disable_ipv6=1'])
        result['mptcp'] = lab.command(0, ['sysctl', 'net.mptcp.enabled', 'net.mptcp.scheduler',
                                          'net.mptcp.available_schedulers'])
        config = Path('/boot')/('config-'+os.uname().release)
        if config.is_file():
            prefixes = ('CONFIG_NET_SCH_', 'CONFIG_BPF_SYSCALL=', 'CONFIG_DEBUG_INFO_BTF=', 'CONFIG_MPTCP=')
            result['kernel_config'] = [line for line in config.read_text().splitlines() if line.startswith(prefixes)]
        lab.tc('qdisc', 'add', 'dev', 'fc0', 'root', 'handle', '1:', 'taprio', 'num_tc', '1',
               'map', *(['0']*16), 'queues', '1@0', 'base-time', '0', 'sched-entry', 'S', '01', '1000000',
               'sched-entry', 'S', '00', '1000000', 'clockid', 'CLOCK_TAI')
        lab.command(0, ['ping', '-q', '-c', '1', '-W', '1', '10.203.0.2'])
        result['taprio'] = readback(lab)['qdisc']
        require(any(row.get('root') and row['kind'] == 'taprio' and row['packets'] > 0
                    and row['options']['cycle_time'] == 2_000_000 for row in result['taprio']),
                'software TAPRIO readback missing')
        result['etf'] = txtime_trial(lab)
        result['status'] = 'complete'
    except BaseException as error:
        result['error'] = getattr(error, 'stderr', None) or str(error)
        raise
    finally:
        lab.close()
        result['host_unchanged'] = host_identity() == before
        result['namespace_holders_exited'] = all(p.poll() is not None for p in lab.holders)
        save(path/'summary.json', result)
    require(result['host_unchanged'] and result['namespace_holders_exited'], 'capability probe cleanup failed')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repetitions', type=int, default=10)
    parser.add_argument('--duration', type=int, default=2)
    parser.add_argument('--seed', type=int, default=81403297)
    parser.add_argument('--capabilities', action='store_true', help='probe software TAPRIO, ETF and MPTCP')
    args = parser.parse_args()
    if not 1 <= args.repetitions <= 20 or not 1 <= args.duration <= 5 or not 0 <= args.seed < 2**32:
        parser.error('expected 1–20 repetitions, duration 1–5 seconds and a u32 seed')
    with exclusive():
        prefix = 'scheduler-capabilities' if args.capabilities else 'scheduler-udp'
        path = RESULTS/f'{prefix}-{time.time_ns()}'
        owned_directory(path)
        print(f'Evidence: {path}', flush=True)
        if args.capabilities:
            capabilities(path)
            return
        before = host_identity()
        rng = random.Random(args.seed)
        summary = dict(status='running', environment=metadata(), parameters=vars(args), runs=[])
        blocks = [(seed, rng.sample(MODES, len(MODES))) for seed in rng.sample(range(1, 2**32), args.repetitions)]
        summary['blocks'] = blocks
        try:
            for rep, (seed, order) in enumerate(blocks):
                for mode in order:
                    name = f'r{rep}-{mode}'
                    row = trial(path/name, mode, seed, args.duration)
                    save(path/name/'result.json', row)
                    summary['runs'].append(dict(artifact=name, repetition=rep,
                                                **{k: v for k, v in row.items() if k not in ('probe_samples', 'before', 'after')}))
                    save(path/'summary.json', summary)
                    print(f"{name}: P99 {row['p99_ms']}, attainment {row['deadline_attainment']:.3f}", flush=True)
            summary['intent_minus_baseline'] = paired_summary(summary['runs'])
            summary['collapse'] = trial(path/'collapse', 'intent', args.seed, max(2, args.duration), True)
            save(path/'collapse'/'result.json', summary['collapse'])
            summary['status'] = 'complete'
        except BaseException as error:
            summary.update(status='invalid', error=getattr(error, 'stderr', None) or str(error))
            raise
        finally:
            summary['host_unchanged'] = host_identity() == before
            if not summary['host_unchanged']:
                summary['status'] = 'invalid'
            save(path/'summary.json', summary)
        require(summary['status'] == 'complete', 'invalid experiment')


if __name__ == '__main__':
    main()
