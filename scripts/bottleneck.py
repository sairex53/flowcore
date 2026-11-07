"""Directional bottleneck evidence from bounded, monotonic observations."""
from collections import deque
import statistics
import time


EGRESS = 'LOCAL_EGRESS_CONGESTION'
INGRESS = 'LOCAL_INGRESS_PRESSURE'
REMOTE = 'REMOTE_PATH_SUSPECTED'
RECEIVER = 'RECEIVER_LIMITED_SUSPECTED'
APPLICATION = 'APPLICATION_LIMITED_SUSPECTED'
UNKNOWN = 'INSUFFICIENT_EVIDENCE'
DIRECTIONS = ('egress', 'ingress')


def nonnegative(value):
    return value if type(value) is int and value >= 0 else None


def counter_delta(new, old, key):
    current, previous = nonnegative(new.get(key)), nonnegative(old.get(key))
    return current - previous if current is not None and previous is not None and current >= previous else None


def shaper_rate(root, classes):
    if root.get('kind') == 'htb':
        htb = [row for row in classes if row.get('class') == 'htb']
        parents = [row for row in htb if row.get('handle') == '1:1' or row.get('root')]
        if len(parents) == 1:
            row = parents[0]
            rate = nonnegative(row.get('rate'))
            if rate and rate == row.get('ceil'):
                return rate / 125
    if root.get('kind') == 'cake':
        rate = nonnegative(root.get('options', {}).get('bandwidth'))
        if rate:
            return rate / 125
    return None


def direction(queue, classes, link, field, *, configured=None, nominal=None, owned=False, device=None):
    """Build one counter domain. TC and link byte accounting are distinct."""
    queue = queue or {}
    use_shaper = configured is not None and nonnegative(queue.get('bytes')) is not None
    return dict(kind=queue.get('kind'), device=device, owned=owned and use_shaper,
                configured_kbit=configured if use_shaper else None, nominal_kbit=nominal,
                byte_source='shaper_departures' if use_shaper else field,
                bytes=queue['bytes'] if use_shaper else link.get(field),
                backlog_bytes=queue.get('backlog', queue.get('backlog_bytes')),
                drops=queue.get('drops') if queue else link.get('rx_dropped') if field == 'rx_bytes' else None,
                classes=classes)


def leaf_classes(classes, qdiscs):
    leaves = {row.get('parent'): row for row in qdiscs if row.get('parent')}
    result = {}
    for name, row in classes.items():
        leaf = leaves.get(row['handle'], {})
        stats = dict(row.get('stats', {}))
        for field in ('drops', 'backlog'):
            if field in leaf:
                stats[field] = leaf[field]
        result[name] = dict(row, stats=stats)
    return result


def lab_directions(tc, *, egress_owned=False):
    root = next((row for row in tc.get('qdisc', []) if row.get('root')), {})
    ingress = tc.get('ingress', {})
    ingress_root = ingress.get('root')
    link = tc.get('link', {})
    # Without an ingress shaper, the interface exposes RX counters but has no
    # inspectable ingress queue. A clean counter is weaker than a quiet queue.
    ingress_classes = leaf_classes(ingress.get('classes', {}), ingress.get('qdiscs', []))
    egress_classes = leaf_classes(tc.get('traffic_classes', {}), tc.get('qdisc', []))
    rx = direction(ingress_root, ingress_classes, link, 'rx_bytes',
                   configured=ingress.get('capacity_kbit'), owned=ingress.get('verified', False),
                   device=ingress.get('device', 'fc0'))
    return dict(
        egress=direction(root, egress_classes, link, 'tx_bytes',
                         configured=shaper_rate(root, tc.get('class', [])), owned=egress_owned, device='fc0'),
        ingress=rx)


def lab_sockets(rows, interface_index, addresses):
    return [row for row in rows if row.get('namespace') == 'sender' and row.get('local_address') in addresses and
            (row.get('bound_interface_index') is None or row['bound_interface_index'] == interface_index)]


def socket_key(row):
    cookie = row.get('cookie')
    if not cookie or cookie == '0000000000000000' or row.get('state') != 'established':
        return None
    return (cookie, row.get('netns'), row.get('local_address'), row.get('remote_address'),
            row.get('local_port'), row.get('remote_port'), row.get('family'))


def tcp_evidence(current, previous, elapsed):
    matched = [(key, row, previous[key]) for key, row in current.items() if key in previous]
    active = []
    for key, row, old in matched:
        acked = counter_delta(row, old, 'bytes_acked')
        busy = counter_delta(row, old, 'busy_time_us')
        limited = counter_delta(row, old, 'receive_window_limited_us')
        if not acked and not (busy and busy >= elapsed * 1e6 * .25):
            continue
        retrans = counter_delta(row, old, 'total_retrans_segments')
        segments = counter_delta(row, old, 'data_segments_out')
        rtt = nonnegative(row.get('rtt_us'))
        minimum = nonnegative(row.get('min_rtt_us'))
        rwnd = limited / busy if limited is not None and busy and limited <= busy else None
        active.append(dict(port=key[5], rtt_us=rtt, min_rtt_us=minimum, rttvar_us=row.get('rttvar_us'),
                           retransmits=retrans, segments=segments, rwnd_fraction=rwnd,
                           window_pressure=(rwnd is not None and rwnd >= .65 and
                                            bool(row.get('notsent_bytes') or row.get('unacked_segments'))),
                           inflated=bool(segments and segments >= 20 and rtt and minimum and
                                         rtt >= 2 * minimum and rtt-minimum >= 2000),
                           stable=bool(rtt and minimum and rtt-minimum <= max(1500, minimum * .5))))
    def median(key):
        values = [row[key] for row in active if row[key] is not None]
        return statistics.median(values) if values else None
    retrans_pairs = [row for row in active if row['retransmits'] is not None and row['segments'] is not None]
    segments = sum(row['segments'] for row in retrans_pairs)
    retrans = sum(row['retransmits'] for row in retrans_pairs)
    path_rows = [row for row in retrans_pairs if not row['window_pressure']]
    path_segments = sum(row['segments'] for row in path_rows)
    path_retrans = sum(row['retransmits'] for row in path_rows)
    stable_ports = {row['port'] for row in active if row['stable']} - {
        row['port'] for row in active if not row['stable']}
    return dict(matched_sockets=len(matched), active_sockets=len(active), rtt_us=median('rtt_us'),
                rttvar_us=median('rttvar_us'), min_rtt_us=median('min_rtt_us'),
                retransmit_segments=retrans if retrans_pairs else None, data_segments=segments if retrans_pairs else None,
                retransmit_ratio=retrans / segments if segments else None,
                receive_window_limited_fraction=median('rwnd_fraction'),
                receiver_pressure=any(row['window_pressure'] for row in active),
                path_pressure=any(row['inflated'] and not row['window_pressure'] for row in active) or
                bool(path_segments >= 20 and path_retrans >= 2 and path_retrans / path_segments >= .02),
                stable_service_ports=sorted(stable_ports - {None})[:32],
                stable_rtt=bool(active) and all(row['stable'] for row in active))


def class_rates(current, previous, elapsed):
    result = {}
    for name, row in current.items():
        stats = row.get('stats', {})
        old = previous.get(name, {}).get('stats', {})
        served = counter_delta(stats, old, 'bytes')
        drops = counter_delta(stats, old, 'drops')
        backlog, earlier = nonnegative(stats.get('backlog')), nonnegative(old.get('backlog'))
        demand = None
        if served is not None and drops == 0 and backlog is not None and earlier is not None:
            demand = max(0, served + backlog - earlier) / (125 * elapsed)
        result[name] = dict(served_kbit=served / (125 * elapsed) if served is not None else None,
                            queue_accounted_demand_kbit=demand, backlog_bytes=backlog,
                            dropped_packets=drops, guaranteed_kbit=row['rate']/125 if 'rate' in row else None)
    return result


class Bottleneck:
    """One interface and two directional counter domains, without kernel writes."""

    def __init__(self):
        self.previous = None
        self.state = 'UNKNOWN'
        self.streak = 0
        self.streak_time = 0
        self.candidate = UNKNOWN
        self.peaks = {name: deque(maxlen=8) for name in DIRECTIONS}
        self.app_windows = deque(maxlen=8)
        self.latest = {}
        self.generation = 0
        self.last_capacity_reduction = None

    def reset(self, reason):
        self.previous = None
        self.state = 'UNKNOWN'
        self.streak = 0
        self.streak_time = 0
        self.candidate = UNKNOWN
        self.app_windows.clear()
        for window in self.peaks.values():
            window.clear()
        self.generation += 1
        self.latest = dict(reason=reason)
        self.last_capacity_reduction = None

    def view(self, now=None, max_age=5):
        now = time.monotonic() if now is None else now
        age = now-self.previous[0] if self.previous else None
        if age is None or age > max_age or age < 0:
            return dict(schema=1, state='UNKNOWN', classification=UNKNOWN, confidence='insufficient',
                        sample_age_seconds=age, generation=self.generation, action='OBSERVE',
                        reason=self.latest.get('reason', 'missing or stale observation'))
        return dict(self.latest, sample_age_seconds=age, last_capacity_reduction=self.last_capacity_reduction)

    def observe(self, now, directions, sockets=(), *, identity=None, application=(), max_gap=5,
                socket_coverage=True):
        rates = tuple((directions[name]['configured_kbit'], directions[name]['kind'],
                       directions[name]['byte_source'], directions[name]['nominal_kbit'],
                       directions[name]['owned']) for name in DIRECTIONS)
        current = {}
        if socket_coverage:
            for row in sockets[:4096]:
                key = socket_key(row)
                if key is not None:
                    current[key] = row['tcp_info']
        old = self.previous
        counter_reset = old and any(
            nonnegative(directions[name].get(key)) is not None
            and nonnegative(old[2][name].get(key)) is not None
            and directions[name][key] < old[2][name][key]
            for name in DIRECTIONS for key in ('bytes', 'drops'))
        if old and not counter_reset and 0 <= now-old[0] < .15 and (identity, rates) == old[1]:
            return self.view(now, max_gap)
        self.previous = (now, (identity, rates), directions, current)
        if not old or counter_reset or (identity, rates) != old[1] or not 0 < now-old[0] <= max_gap:
            reductions = {}
            if old and old[1][0] == identity:
                for name, before, after in zip(DIRECTIONS, old[1][1], rates):
                    if before[0] is not None and after[0] is not None and after[0] < before[0]:
                        reductions[name] = dict(before=before[0], after=after[0])
                    elif (before[0] is None and after[0] is None and before[3] is not None and
                          after[3] is not None and after[3] < before[3]):
                        reductions[name] = dict(before=before[3], after=after[3])
            sample = self.previous
            self.reset('counter baseline or topology changed')
            self.previous = sample
            if reductions:
                self.last_capacity_reduction = dict(time_monotonic_seconds=now, directions=reductions)
            self.latest = dict(schema=1, state='UNKNOWN', classification=UNKNOWN, confidence='insufficient',
                               generation=self.generation, action='OBSERVE',
                               reason='counter baseline or topology changed', capacity_reductions=reductions)
            return self.view(now, max_gap)

        elapsed = now-old[0]
        paths = {}
        for name in DIRECTIONS:
            row, previous = directions[name], old[2][name]
            served = counter_delta(row, previous, 'bytes')
            drops = counter_delta(row, previous, 'drops')
            throughput = served/(125*elapsed) if served is not None else None
            capacity = row['configured_kbit'] or row['nominal_kbit']
            utilization = throughput/capacity if capacity and throughput is not None else None
            if throughput is not None:
                self.peaks[name].append(throughput)
            backlog = nonnegative(row['backlog_bytes'])
            saturated = utilization >= .85 if utilization is not None else None
            pressure = (backlog is not None and backlog >= 1500 and
                        (row['configured_kbit'] is not None or saturated or bool(drops)))
            if name == 'ingress' and not row['owned'] and drops:
                pressure = True
            paths[name] = dict(device=row['device'], qdisc=row['kind'], controllable=row['owned'],
                               capacity_kbit=capacity, capacity_source=('qdisc_readback' if row['configured_kbit']
                               else 'reported_link_speed' if row['nominal_kbit'] else 'unknown'),
                               capacity_uncertainty=('configured_local_limit' if row['configured_kbit']
                               else 'nominal_only' if row['nominal_kbit'] else 'unbounded_by_observation'),
                               observed_peak_kbit=max(self.peaks[name], default=None),
                               throughput_kbit=throughput, utilization=utilization, saturated=saturated,
                               idle_headroom_kbit=max(0, capacity-throughput) if capacity and throughput is not None
                               and backlog == 0 and saturated is False else None,
                               backlog_bytes=backlog, dropped_packets=drops, pressure=bool(pressure),
                               byte_source=row['byte_source'], classes=class_rates(row['classes'], previous['classes'], elapsed))
        transport = tcp_evidence(current, old[3], elapsed)
        self.latest = self.classify(paths, transport, application, elapsed)
        return self.view(now, max_gap)

    def classify(self, paths, tcp, application, elapsed):
        tx, rx = paths['egress'], paths['ingress']
        valid = all(path['throughput_kbit'] is not None for path in paths.values())
        quiet = tx['backlog_bytes'] == 0 and (rx['backlog_bytes'] in (0, None)) and rx['dropped_packets'] == 0
        app = [dict(row, class_backlog_bytes=tx['classes'].get(row['class'], {}).get('backlog_bytes'))
               for row in application]
        self.app_windows.append({(row['service'], row['class']): (row['class_backlog_bytes'], row['p95_us'])
                                 for row in app if row['class_backlog_bytes'] is not None
                                 and row['p95_us'] is not None and row['samples'] >= 32})
        for row in app:
            key = (row['service'], row['class'])
            values = [window[key] for window in self.app_windows if key in window]
            correlation = None
            if len(values) >= 4:
                backlog, latency = zip(*values)
                if len(set(backlog)) > 1 and len(set(latency)) > 1:
                    correlation = statistics.correlation(backlog, latency)
            row.update(backlog_latency_pairs=len(values), backlog_latency_correlation=correlation)
        app_risk = any(row['samples'] >= 32 and row['p95_us'] is not None and
                       row['p95_us'] >= 1.2 * row['target_us'] and row.get('port') in tcp['stable_service_ports']
                       for row in app)
        cause, confidence, reason = UNKNOWN, 'insufficient', 'no directional attribution'
        if valid and tx['pressure'] and rx['pressure']:
            reason = 'both directions show pressure'
        elif valid and tx['pressure']:
            cause, confidence, reason = EGRESS, 'observed_local', 'egress queue pressure'
        elif valid and rx['pressure']:
            cause, confidence, reason = INGRESS, 'observed_local', 'IFB backlog or local RX drops'
        elif valid and quiet and tcp['receiver_pressure'] and tcp['path_pressure']:
            reason = 'conflicting TCP pressure signals across sockets'
            valid = False
        elif valid and quiet and tcp['receiver_pressure']:
            cause, confidence, reason = RECEIVER, 'suspected', 'matched active TCP socket limited by receive window'
        elif valid and quiet and tcp['path_pressure']:
            cause, confidence, reason = REMOTE, 'suspected', 'TCP inflation or retransmits without observed local pressure'
        elif valid and quiet and tcp['stable_rtt'] and app_risk:
            cause, confidence, reason = APPLICATION, 'suspected', 'reported service latency elevated with stable sampled TCP RTT'
        pressured = cause != UNKNOWN or (tx['pressure'] and rx['pressure'])
        if not valid or (not pressured and not quiet):
            self.state, self.streak, self.streak_time = 'UNKNOWN', 0, 0
        elif pressured:
            self.streak = min(3, self.streak + 1) if self.candidate == cause and self.state in ('BUILDING', 'CONGESTED') else 1
            self.streak_time = min(2, self.streak_time + elapsed) if self.streak > 1 else elapsed
            self.state = 'CONGESTED' if self.streak >= 2 and self.streak_time >= .5 else 'BUILDING'
        else:
            recovering = self.state in ('BUILDING', 'CONGESTED', 'RECOVERING')
            self.streak = min(3, self.streak + 1) if self.candidate == UNKNOWN and self.state == 'RECOVERING' else 1
            self.streak_time = min(2, self.streak_time + elapsed) if self.streak > 1 else elapsed
            self.state = 'RECOVERING' if recovering and (self.streak < 3 or self.streak_time < .75) else 'NORMAL'
        self.candidate = cause
        action = 'OBSERVE'
        if self.state == 'CONGESTED':
            if cause == EGRESS and tx['controllable']:
                action = 'REVIEW_EGRESS_ALLOCATION' if tx['qdisc'] == 'htb' else 'REVIEW_EGRESS_SHAPER'
            elif cause == INGRESS and rx['controllable']:
                action = 'REVIEW_INGRESS_IFB'
            elif cause != UNKNOWN:
                action = 'ADVISE'
        return dict(schema=1, state=self.state, classification=cause, confidence=confidence,
                    generation=self.generation, reason=reason, action=action, interval_seconds=elapsed,
                    directions=paths, transport=tcp, application=app, capacity_reductions={})
