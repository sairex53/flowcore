"""Version 2 workload accounting and independent topology evidence."""
import csv
import os
import time
from collections import Counter
from pathlib import Path

from research_manifest import CLASSIFIED, FQ_MODES
from service_config import CLASSES
from workload import percentile

NANO = 10**9


def read(path):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def host_metadata():
    topology = Path('/sys/devices/system/cpu')
    fields = ('physical_package_id', 'core_id', 'thread_siblings_list')
    cpu = {path.parent.name: {key: read(path/key) for key in fields}
           for path in sorted(topology.glob('cpu[0-9]*/topology'))}
    return dict(cpu_topology=cpu, affinity=sorted(os.sched_getaffinity(0)),
                virtualization={key: read('/sys/class/dmi/id/'+key)
                                for key in ('sys_vendor', 'product_name')},
                model=read('/sys/firmware/devicetree/base/model'),
                hypervisor=read('/sys/hypervisor/type'),
                kernel_command_line=read('/proc/cmdline'),
                cpu_governors=sorted({value for path in topology.glob('cpu*/cpufreq/scaling_governor')
                                      if (value := read(path)) is not None}))


def host_sample():
    ticks = Path('/proc/stat').read_text().splitlines()[0].split()[1:9]
    pressures = {}
    for resource in ('cpu', 'memory', 'io'):
        raw = read('/proc/pressure/'+resource)
        if raw is not None:
            pressures[resource] = {line.split()[0]: int(line.split('total=')[1])
                                   for line in raw.splitlines()}
    return dict(time_ns=time.monotonic_ns(), cpu_ticks=list(map(int, ticks)),
                load_average=os.getloadavg(), pressure_total_us=pressures)


def account(path, offered, start, end, deadline_ms, payload, *, recovery_start=None):
    """Metrics use the offered cohort, with goodput limited to the window."""
    with path.open() as stream:
        rows = list(csv.DictReader(stream))
    if len(rows) != offered or [int(row['id']) for row in rows] != list(range(offered)):
        raise RuntimeError('request ledger does not match offered schedule')
    outcomes = Counter(row['outcome'] for row in rows)
    successful = [row for row in rows if row['outcome'] == 'ok']
    def on_time(row):
        budget = float(row.get('deadline_ms') or deadline_ms)
        if not 0 < budget <= 600000:
            raise RuntimeError('invalid deadline in request ledger')
        return row['outcome'] == 'ok' and float(row['latency_ms']) <= budget

    attained = None if deadline_ms is None else sum(map(on_time, rows))
    in_window = sum(float(row['completed']) <= end for row in successful)
    services = {}
    for service in sorted({row['service'] for row in rows}):
        cohort = [row for row in rows if row['service'] == service]
        latencies = [float(row['latency_ms']) for row in cohort if row['outcome'] == 'ok']
        services[service] = dict(offered=len(cohort), completed=len(latencies),
                                 traffic_class=cohort[0]['traffic_class'],
                                 payload_mbps=sum(row['outcome']=='ok' and float(row['completed'])<=end
                                                  for row in cohort)*payload*8/(end-start)/1e6,
                                 p99_ms=percentile(latencies, .99),
                                 failure_ratio=1-len(latencies)/len(cohort),
                                 deadline_attainment=None if deadline_ms is None else
                                 sum(map(on_time, cohort))/len(cohort))
    recovery = dict(status='not_applicable', seconds=None)
    if recovery_start is not None and deadline_ms is not None:
        # Only fully observed windows beginning after kernel-confirmed recovery.
        horizon = max(0, end-recovery_start)
        recovery = dict(status='right_censored', seconds=None, observed_seconds=horizon)
        bins = [[] for _ in range(int(horizon))]
        for row in rows:
            offset = float(row['scheduled'])-recovery_start
            if row['traffic_class']=='critical' and 0 <= offset < len(bins):
                bins[int(offset)].append(row)
        streak = 0
        for i, bucket in enumerate(bins, 1):
            healthy = len(bucket)>=5 and sum(map(on_time, bucket))*20 >= len(bucket)*19
            streak = streak+1 if healthy else 0
            if streak == 2:
                recovery = dict(status='observed', seconds=i, observed_seconds=horizon)
                break
    return dict(outcomes=dict(outcomes), timeout_requests=outcomes['TimeoutError'],
                admission_rejected=outcomes['admission_rejected'], admission_deferred=outcomes['admission_deferred'],
                sent_request_failures=offered-len(successful)-outcomes['admission_rejected']-
                                      outcomes['admission_deferred']-outcomes['inflight_limit'],
                failed_requests=offered-len(successful), completion_ratio=len(successful)/offered,
                failure_ratio=(offered-len(successful))/offered, offered_rps=offered/(end-start),
                deadline_ms=deadline_ms, deadline_attainment=None if attained is None else attained/offered,
                deadline_misses=None if attained is None else offered-attained,
                successful_within_window=in_window, request_payload_mbps=in_window*payload*8/(end-start)/1e6,
                dispatch_lag_p99_ms=percentile([float(row['dispatch_lag_ms']) for row in rows], .99),
                services=services, slo_recovery=recovery)


def interface(snapshot, index):
    name = ('fc0', 'fc1')[index]
    return next(row for row in snapshot['links'][index] if row['name'] == name)


def root(rows):
    return next(row for row in rows if row.get('root'))


def verify(snapshot, mode, expected_capacity, configured_ports):
    sender, receiver = (interface(snapshot, i) for i in (0, 1))
    for index, link in enumerate((sender, receiver)):
        if ({row['name'] for row in snapshot['links'][index]} != {'lo', link['name']}
                or link['kind'] != 'veth' or link['mtu'] != 1500
                or f'10.203.0.{index+1}' not in {a['address'] for a in link['addresses']}):
            raise RuntimeError('unexpected veth topology or address')
    if 'setup' in snapshot:
        for device in ('fc0','fc1'):
            settings = snapshot['setup'][f'offloads-{device}.txt'].splitlines()
            for name in ('tcp-segmentation-offload','generic-segmentation-offload','generic-receive-offload'):
                if not any(line.startswith(f'{name}: off') for line in settings):
                    raise RuntimeError(f'{device} offload readback disagrees with experiment')
    tc = snapshot['tc']
    qdisc, reverse = root(tc['qdisc']), root(tc['receiver_qdisc'])
    want = 'noqueue' if mode == 'linux-default' else 'cake' if mode in ('cake','ecn-cake','cake-diffserv') else 'htb'
    if qdisc['kind'] != want or reverse['kind'] != ('noqueue' if want == 'noqueue' else 'netem'):
        raise RuntimeError('unexpected sender or receiver root qdisc')
    filters = {(row['protocol'], opt.get('classid'), opt['keys'].get('dst_port'))
               for row in tc['filter'] if (opt := row.get('options', {})).get('keys')
               and opt['keys'].get('ip_proto') == 'tcp'}
    leaf = {row['parent']: row['kind'] for row in tc['qdisc'] if 'parent' in row}
    rates = {row['handle']: (row['rate'], row['ceil']) for row in tc['class'] if row.get('class') == 'htb'}
    readback = None
    if want == 'htb':
        classified = mode in CLASSIFIED
        rate_key = '1:1' if classified else '1:20'
        rate, ceil = rates[rate_key]
        if rate != ceil or rate != expected_capacity*125:
            raise RuntimeError('HTB bottleneck rate readback disagrees with manifest')
        if classified:
            expected = {(proto, f'1:{CLASSES[name]}', port) for name, port in configured_ports.items()
                        for proto in ('ip', 'ipv6')}
            children = {f'1:{minor}' for minor in CLASSES.values()}
            expected_leaf = 'fq' if mode == 'pacing-htb-fq' else 'fq_codel'
            if filters != expected or leaf != dict.fromkeys(children, expected_leaf) or set(rates) != children | {'1:1'}:
                raise RuntimeError('classified HTB hierarchy or flower readback mismatch')
        else:
            expected_leaf = 'fq' if mode in FQ_MODES else 'pfifo' if mode == 'fifo' else 'fq_codel'
            if filters or leaf != {'1:20': expected_leaf} or set(rates) != {'1:20'}:
                raise RuntimeError('single-class baseline hierarchy mismatch')
        readback = rate/125
    elif want == 'cake':
        options = qdisc['options']
        readback = options['bandwidth']/125
        diffserv = 'diffserv4' if mode == 'cake-diffserv' else 'besteffort'
        if (readback != expected_capacity or options['diffserv'] != diffserv
                or options['flowmode'] != 'flows' or options.get('ingress') or filters or rates or leaf):
            raise RuntimeError('CAKE readback disagrees with baseline')
    elif rates or filters or leaf:
        raise RuntimeError('Linux default contains shaping or classification')
    return dict(sender=qdisc['kind'], receiver=reverse['kind'], cap_kbit_readback=readback,
                leaves=leaf, filters=sorted(filters), htb_class_bytes_per_second=rates)


def deltas(before, after, names):
    return {name: after[name]-before[name]
            if type(before.get(name)) is int and type(after.get(name)) is int and after[name]>=before[name]
            else None for name in names}


def observed(before, after, configured):
    sender, receiver = [deltas(interface(before, i)['counters'], interface(after, i)['counters'],
                              ('tx_bytes', 'rx_bytes', 'tx_packets', 'rx_packets', 'rx_dropped'))
                        for i in (0, 1)]
    queue_fields = ('bytes', 'packets', 'drops', 'overlimits')
    first_queue, last_queue = (root(item['tc']['qdisc']) for item in (before, after))
    queue = (dict.fromkeys(queue_fields) if first_queue['kind'] == 'noqueue'
             else deltas(first_queue, last_queue, queue_fields))
    class_bytes = {name: deltas(row.get('stats',row), after_row.get('stats',after_row), ('bytes','packets'))
                   for name, row in before['tc']['traffic_classes'].items()
                   if (after_row := after['tc']['traffic_classes'].get(name))}
    class_total = (sum(row['bytes'] for row in class_bytes.values()) if class_bytes and
                   all(row['bytes'] is not None for row in class_bytes.values()) else None)
    seconds = ((after['begin_ns']+after['end_ns'])-(before['begin_ns']+before['end_ns']))/(2*NANO)
    mbps = sender['tx_bytes']*8/seconds/1e6 if seconds>0 and sender['tx_bytes'] is not None else None
    def gap(a, b):
        return abs(a-b)/max(a,b,1) if a is not None and b is not None else None
    ratio = mbps*1000/configured if mbps is not None and configured else None
    return dict(seconds=seconds, sender_link=sender, receiver_link=receiver, sender_qdisc=queue,
                tc_class=class_bytes, sender_egress_mbps=mbps, configured_kbit=configured,
                measured_to_configured_ratio=ratio,
                saturation_supported=ratio is not None and .85<=ratio<=1.15 and bool(queue['overlimits']),
                forward_link_bytes_relative_gap=gap(sender['tx_bytes'],receiver['rx_bytes']),
                forward_link_packets_relative_gap=gap(sender['tx_packets'],receiver['rx_packets']),
                qdisc_to_sender_bytes_relative_gap=gap(queue['bytes'],sender['tx_bytes']),
                class_to_qdisc_bytes_relative_gap=gap(class_total,queue['bytes']))


def noise(before, after, metrics, rate_windows):
    warnings = []
    ticks = [b-a for a,b in zip(before['cpu_ticks'], after['cpu_ticks'])]
    if sum(ticks) and ticks[7]/sum(ticks) > .02:
        warnings.append('guest CPU steal exceeded 2%')
    seconds = (after['time_ns']-before['time_ns'])/NANO
    for resource in ('cpu', 'memory', 'io'):
        name = 'some' if resource == 'cpu' else 'full'
        a = before['pressure_total_us'].get(resource, {}).get(name)
        b = after['pressure_total_us'].get(resource, {}).get(name)
        if a is not None and b is not None and seconds>0 and (b-a)/(seconds*1e6) > .05:
            warnings.append(f'host {resource} PSI {name} exceeded 5%')
    if metrics['dispatch_lag_p99_ms'] > 10:
        warnings.append('request dispatch P99 lag exceeded 10 ms')
    for row in rate_windows:
        if any((row[key] or 0) > .05 for key in ('forward_link_bytes_relative_gap',
                'forward_link_packets_relative_gap','qdisc_to_sender_bytes_relative_gap','class_to_qdisc_bytes_relative_gap')):
            warnings.append('counter domains differ by more than 5% in a sampled span')
            break
    return warnings
