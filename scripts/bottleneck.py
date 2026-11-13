"""Directional bottleneck evidence from bounded, monotonic observations."""
from collections import deque
import os
import math
import statistics
import time

try:
    from demand_intelligence import NativeDemand, EVIDENCE as DEMAND_EVIDENCE
except (ImportError, OSError):
    NativeDemand = None
    DEMAND_EVIDENCE = {}


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
        info, old_info = row['tcp_info'], old['tcp_info']
        acked = counter_delta(info, old_info, 'bytes_acked')
        busy = counter_delta(info, old_info, 'busy_time_us')
        limited = counter_delta(info, old_info, 'receive_window_limited_us')
        sendbuf_limited = counter_delta(info, old_info, 'send_buffer_limited_us')
        if not acked and not (busy and busy >= elapsed * 1e6 * .25):
            continue
        retrans = counter_delta(info, old_info, 'total_retrans_segments')
        segments = counter_delta(info, old_info, 'data_segments_out')
        rtt = nonnegative(info.get('rtt_us'))
        minimum = nonnegative(info.get('min_rtt_us'))
        rwnd = limited / busy if limited is not None and busy and limited <= busy else None
        snd = sendbuf_limited / busy if sendbuf_limited is not None and busy and sendbuf_limited <= busy else None
        active.append(dict(port=key[5], rtt_us=rtt, min_rtt_us=minimum, rttvar_us=info.get('rttvar_us'),
                           retransmits=retrans, segments=segments, rwnd_fraction=rwnd,
                           sendbuf_fraction=snd, busy_time_us=busy,
                           window_pressure=(rwnd is not None and rwnd >= .65 and
                                            bool(info.get('notsent_bytes') or info.get('unacked_segments'))),
                           inflated=bool(segments and segments >= 20 and rtt and minimum and
                                         rtt >= 2 * minimum and rtt-minimum >= 2000),
                           stable=bool(rtt and minimum and rtt-minimum <= max(1500, minimum * .5)),
                           app_limited=bool(info.get('delivery_rate_app_limited')),
                           delivery_rate=info.get('delivery_rate_bytes_per_second'),
                           pacing_cap=info.get('max_pacing_rate_bytes_per_second'),
                           send_queue_before=old.get('send_queue_bytes'),
                           send_queue_after=row.get('send_queue_bytes'),
                           notsent=info.get('notsent_bytes'),
                           sndbuf=row.get('send_buffer_bytes')))
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
    busy_total = sum(row['busy_time_us'] or 0 for row in active)
    rwnd_limited = sum(row['rwnd_fraction'] * row['busy_time_us'] for row in active
                       if row['rwnd_fraction'] is not None and row['busy_time_us'])
    sendbuf_limited = sum(row['sendbuf_fraction'] * row['busy_time_us'] for row in active
                          if row['sendbuf_fraction'] is not None and row['busy_time_us'])
    rates = [row['delivery_rate'] for row in active if type(row['delivery_rate']) is int]
    pacing = [row['pacing_cap'] for row in active if type(row['pacing_cap']) is int and
              0 < row['pacing_cap'] < 1_000_000_000_000]
    send_before = [row['send_queue_before'] for row in active if type(row['send_queue_before']) is int]
    send_after = [row['send_queue_after'] for row in active if type(row['send_queue_after']) is int]
    notsent = [row['notsent'] for row in active if type(row['notsent']) is int]
    sndbuf = [row['sndbuf'] for row in active if type(row['sndbuf']) is int]
    return dict(matched_sockets=len(matched), active_sockets=len(active), rtt_us=median('rtt_us'),
                rttvar_us=median('rttvar_us'), min_rtt_us=median('min_rtt_us'),
                retransmit_segments=retrans if retrans_pairs else None, data_segments=segments if retrans_pairs else None,
                retransmit_ratio=retrans / segments if segments else None,
                receive_window_limited_fraction=median('rwnd_fraction'),
                rwnd_limited_fraction=rwnd_limited / busy_total if busy_total and rwnd_limited <= busy_total else None,
                sendbuf_limited_fraction=sendbuf_limited / busy_total if busy_total and sendbuf_limited <= busy_total else None,
                app_limited_fraction=sum(row['app_limited'] for row in active) / len(active) if active else None,
                socket_delivery_bps=sum(rates) * 8 if active and len(rates) == len(active) else None,
                socket_pacing_bps=min(pacing) * 8 if active and len(pacing) == len(active) else None,
                socket_send_queue_before_bytes=sum(send_before) if active and len(send_before) == len(active) else None,
                socket_send_queue_after_bytes=sum(send_after) if active and len(send_after) == len(active) else None,
                socket_notsent_bytes=sum(notsent) if active and len(notsent) == len(active) else None,
                socket_sndbuf_bytes=sum(sndbuf) if active and len(sndbuf) == len(active) else None,
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
                            backlog_before_bytes=earlier, served_bytes=served, dropped_packets=drops,
                            class_rate_bps=row['rate'] * 8 if type(row.get('rate')) is int else None,
                            class_ceil_bps=row['ceil'] * 8 if type(row.get('ceil')) is int else None,
                            guaranteed_kbit=row['rate']/125 if type(row.get('rate')) is int else None)
    return result


def application_totals(rows):
    totals = dict(requests_attempted=None, requests_admitted=None, requests_completed=None,
                  requests_failed=None, requests_rejected=None, deadline_misses=0,
                  terminal_outcomes_observed=0, request_counts_complete=bool(rows),
                  request_window_complete=bool(rows), traffic_reports=0,
                  udp_reports=0, udp_message_reports=0)
    fields = ('bytes_offered', 'bytes_admitted', 'bytes_transmitted', 'bytes_received',
              'udp_messages_attempted', 'udp_messages_transmitted', 'udp_send_failures',
              'udp_urgent_messages', 'udp_background_messages')
    totals.update({field: 0 for field in fields})
    exact_counts = ('requests_attempted', 'requests_admitted', 'requests_completed',
                    'requests_failed', 'requests_rejected')
    count_sums = {field: 0 for field in exact_counts}
    traffic_rows = []
    traffic_bytes_complete = True
    udp_counters_complete = True
    for row in rows:
        outcomes = row.get('ok', 0) + row.get('failure', 0) + row.get('timeout', 0)
        totals['terminal_outcomes_observed'] += outcomes
        totals['deadline_misses'] += row.get('deadline_misses', row.get('deadline_miss', 0))
        totals['request_counts_complete'] &= bool(row.get('request_counts_complete', False))
        totals['request_window_complete'] &= bool(row.get('request_window_complete', False))
        for field in exact_counts:
            value = row.get(field)
            if type(value) is not int or value < 0:
                totals['request_counts_complete'] = False
            else:
                count_sums[field] += value
        reports = row.get('traffic_reports', 0)
        if type(reports) is not int or reports < 0:
            traffic_bytes_complete = False
            reports = 0
        totals['traffic_reports'] += reports
        traffic = row.get('traffic', {})
        if reports:
            traffic_rows.append(row)
            if not isinstance(traffic, dict):
                traffic_bytes_complete = False
                traffic = {}
            for field in fields[:4]:
                value = traffic.get(field)
                if type(value) is not int or value < 0:
                    traffic_bytes_complete = False
                else:
                    totals[field] += value
        udp = row.get('transports', {}).get('udp')
        if udp:
            reports, message_reports = udp.get('reports'), udp.get('message_reports')
            if (type(reports) is not int or reports < 0 or
                    type(message_reports) is not int or message_reports < 0):
                udp_counters_complete = False
                reports = message_reports = 0
            totals['udp_reports'] += reports
            totals['udp_message_reports'] += message_reports
            for field in fields[4:]:
                value = udp.get(field)
                if type(value) is not int or value < 0:
                    udp_counters_complete = False
                else:
                    totals[field] += value
    if totals['request_counts_complete']:
        totals.update(count_sums)
    totals['udp_accounting_complete'] = bool(udp_counters_complete and totals['udp_reports'] and
                                             totals['udp_message_reports'] == totals['udp_reports'])
    explicit_windows = bool(traffic_rows) and all(
        row.get('traffic_window_complete') is True for row in traffic_rows)
    request_window = bool(totals['request_window_complete'] and
                           totals['traffic_reports'] == totals['terminal_outcomes_observed'])
    totals['traffic_accounting_complete'] = bool(
        totals['traffic_reports'] and traffic_bytes_complete and (explicit_windows or request_window))
    return totals


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
        self.demand_history = {}
        self.demand_forecast_capacity = None
        self.demand_forecast_last_ns = 0
        self.demand_forecasts = {}
        try:
            self.demand_model = NativeDemand() if NativeDemand else None
            self.demand_model_error = None if self.demand_model else 'native demand library unavailable'
        except (OSError, AttributeError) as error:
            self.demand_model = None
            self.demand_model_error = str(error)

    def assess_demand(self, paths, tcp, application, elapsed, previous, traffic_class=None):
        if not self.demand_model:
            return None
        tx = paths['egress']
        rows = [row for row in application if traffic_class is None or row['class'] == traffic_class]
        app = application_totals(rows)
        value = dict(interval_seconds=elapsed, evidence=[],
                     terminal_outcomes_observed=app['terminal_outcomes_observed'])
        evidence = value['evidence']
        if app['request_counts_complete']:
            evidence.append('app_counts')
            value.update({field: app[field] for field in
                          ('requests_attempted', 'requests_admitted', 'requests_completed',
                           'requests_failed', 'requests_rejected', 'deadline_misses')})
        if app['traffic_reports']:
            evidence.append('app_traffic')
            for field, native_field in (('bytes_offered', 'app_offered_bytes'),
                                        ('bytes_admitted', 'app_admitted_bytes'),
                                        ('bytes_transmitted', 'app_transmitted_bytes'),
                                        ('bytes_received', 'app_received_bytes')):
                value[native_field] = app[field]
            if not app['traffic_accounting_complete']:
                evidence.append('app_incomplete')
        if app['udp_accounting_complete']:
            evidence.append('udp_accounting')
            value.update({field: app[field] for field in
                          ('udp_messages_attempted', 'udp_messages_transmitted', 'udp_send_failures',
                           'udp_urgent_messages', 'udp_background_messages')})

        capacity_kbit = tx.get('capacity_kbit')
        if (type(capacity_kbit) in (int, float) and math.isfinite(capacity_kbit) and
                capacity_kbit > 0 and type(tx.get('throughput_kbit')) in (int, float) and
                math.isfinite(tx['throughput_kbit'])):
            evidence.append('capacity')
            value['capacity_bps'] = capacity_kbit * 1000
            value['observed_bps'] = tx['throughput_kbit'] * 1000
            if tx.get('capacity_source') == 'qdisc_readback':
                evidence.append('configured_cap')
                value['configured_shaping_bps'] = capacity_kbit * 1000
            elif tx.get('capacity_source') == 'reported_link_speed':
                evidence.append('nominal_cap')
        before = previous.get('egress', {}).get('backlog_bytes') if previous else None
        after = tx.get('backlog_bytes')
        if type(before) is int and type(after) is int:
            evidence.append('root_queue')
            value.update(root_backlog_before_bytes=before, root_backlog_after_bytes=after)
            if tx.get('dropped_packets') is not None:
                evidence.append('root_drops')
                value['root_drops'] = tx['dropped_packets']
        if tx.get('throughput_kbit') is not None:
            evidence.append('delivered')
            value['delivered_bytes'] = tx['throughput_kbit'] * 125 * elapsed

        class_rows = tx.get('classes', {})
        if traffic_class is not None:
            selected = class_rows.get(traffic_class)
            selected_rows = [selected] if selected else []
        else:
            selected_rows = list(class_rows.values())
        complete = [row for row in selected_rows if type(row.get('served_bytes')) is int and
                    type(row.get('backlog_before_bytes')) is int and
                    type(row.get('backlog_bytes')) is int and row.get('class_rate_bps') is not None]
        if selected_rows and len(complete) == len(selected_rows):
            evidence.append('class')
            value.update(class_served_bytes=sum(row['served_bytes'] for row in complete),
                         class_backlog_before_bytes=sum(row['backlog_before_bytes'] for row in complete),
                         class_backlog_after_bytes=sum(row['backlog_bytes'] for row in complete),
                         class_rate_bps=sum(row['class_rate_bps'] for row in complete),
                         class_ceil_bps=sum(row['class_ceil_bps'] or row['class_rate_bps'] for row in complete))
            if all(row.get('dropped_packets') is not None for row in complete):
                evidence.append('class_drops')
                value['class_drops'] = sum(row['dropped_packets'] for row in complete)

        if tcp.get('active_sockets'):
            evidence.append('tcp')
            for field, source in (('socket_delivery_bps', 'socket_delivery_bps'),
                                  ('tcp_app_limited_fraction', 'app_limited_fraction')):
                if tcp.get(source) is not None:
                    value[field] = tcp[source]
            if all(tcp.get(field) is not None for field in
                   ('socket_pacing_bps', 'socket_send_queue_before_bytes',
                    'socket_send_queue_after_bytes', 'socket_notsent_bytes')):
                evidence.append('socket_pacing')
                value.update({field: tcp[field] for field in
                              ('socket_pacing_bps', 'socket_send_queue_before_bytes',
                               'socket_send_queue_after_bytes', 'socket_notsent_bytes')})
                value['socket_sndbuf_bytes'] = tcp.get('socket_sndbuf_bytes') or 0
            if tcp.get('rwnd_limited_fraction') is not None and tcp.get('sendbuf_limited_fraction') is not None:
                evidence.append('tcp_limits')
                value.update(tcp_rwnd_limited_fraction=tcp['rwnd_limited_fraction'],
                             tcp_sendbuf_limited_fraction=tcp['sendbuf_limited_fraction'])
            if tcp.get('path_pressure'):
                evidence.append('remote_pressure')
        estimate = self.demand_model.assess(value)
        if estimate['queue_accounted_demand_bps'] is not None:
            class_valid = ('class' in evidence and 'class_drops' in evidence and
                           value.get('class_drops') == 0 and
                           value.get('class_backlog_after_bytes', -1) >=
                           value.get('class_backlog_before_bytes', 0))
            root_valid = ('root_queue' in evidence and 'root_drops' in evidence and
                          value.get('root_drops') == 0 and 'delivered' in evidence and
                          value.get('root_backlog_after_bytes', -1) >=
                          value.get('root_backlog_before_bytes', 0))
            estimate['queue_accounted_scope'] = 'class' if class_valid else 'root' if root_valid else None
        else:
            estimate['queue_accounted_scope'] = None
        return estimate

    def update_demand_forecasts(self, now):
        demand = self.latest.get('demand')
        if not demand or not self.demand_model:
            return
        direction = self.latest.get('directions', {}).get('egress', {})
        capacity = direction.get('capacity_kbit')
        if type(capacity) is not int or capacity <= 0 or direction.get('capacity_source') != 'qdisc_readback':
            demand['forecast'] = dict(state='INSUFFICIENT_DATA', reason='verified configured capacity unavailable',
                                      classes={})
            return
        capacity_bps = capacity * 1000
        if self.demand_forecast_capacity != capacity_bps:
            self.demand_forecast_capacity = capacity_bps
            self.demand_history.clear()
            self.demand_forecasts.clear()
            self.demand_forecast_last_ns = 0
        stamp = round(now * 1e9)
        if stamp <= 0 or (self.demand_forecast_last_ns and
                          stamp - self.demand_forecast_last_ns < 4_500_000_000):
            demand['forecast'] = dict(state='INSUFFICIENT_DATA',
                                      reason='waiting for non-overlapping complete app windows',
                                      classes=self.demand_forecasts.copy())
            return
        sources = {'overall': demand.get('overall')}
        sources.update(demand.get('classes', {}))
        for name, estimate in sources.items():
            if not estimate or estimate['offered_bps'] is None or estimate['demand_uncertainty_bps'] is None or \
                    'APPLICATION_REPORT_INCOMPLETE' in estimate['flags']:
                continue
            history = self.demand_history.setdefault(name, deque(maxlen=64))
            history.append(dict(time_ns=stamp, demand_bps=estimate['offered_bps'], capacity_bps=capacity_bps))
            if len(history) >= 4:
                forecast = self.demand_model.forecast(list(history), horizon_seconds=2.0, half_life_seconds=2.0)
                if forecast:
                    self.demand_forecasts[name] = dict(state='SHADOW', **forecast,
                        compared_to='last_value_baseline_bps', controller_use=False)
        self.demand_forecast_last_ns = stamp
        demand['forecast'] = dict(state='READY' if self.demand_forecasts else 'INSUFFICIENT_DATA',
                                  reason=None if self.demand_forecasts else 'four complete app windows required',
                                  classes=self.demand_forecasts.copy())

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
        self.demand_history.clear()
        self.demand_forecast_capacity = None
        self.demand_forecast_last_ns = 0
        self.demand_forecasts.clear()

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
                    current[key] = dict(tcp_info=row['tcp_info'], send_queue_bytes=row.get('send_queue'),
                                        send_buffer_bytes=row.get('memory', {}).get('send_buffer_bytes'))
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
        self.latest = self.classify(paths, transport, application, elapsed, old[2])
        return self.view(now, max_gap)

    def classify(self, paths, tcp, application, elapsed, previous_directions=None):
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
        result = dict(schema=1, state=self.state, classification=cause, confidence=confidence,
                      generation=self.generation, reason=reason, action=action, interval_seconds=elapsed,
                      directions=paths, transport=tcp, application=app, capacity_reductions={})
        if self.demand_model:
            overall = self.assess_demand(paths, tcp, app, elapsed, previous_directions or {})
            per_class = {name: self.assess_demand(paths, tcp, app, elapsed,
                                                   previous_directions or {}, name)
                         for name in tx['classes']}
            constrained = [row for row in per_class.values() if row and
                row['classification'] == 'CLASS_RESTRICTED' and row['confidence'] >= .75 and
                row['offered_bps'] is not None and row['demand_uncertainty_bps'] is not None and
                row['class_ceil_bps'] is not None and row['offered_bps'] > row['class_ceil_bps'] and
                'CAPACITY_UNCERTAIN' not in row['flags'] and
                'APPLICATION_REPORT_INCOMPLETE' not in row['flags']]
            actionable_overall = bool(overall and overall['classification'] == 'LOCAL_CAPACITY_LIMITED' and
                overall['confidence'] >= .75 and overall['selected_demand_bps'] is not None and
                overall['demand_uncertainty_bps'] is not None and 'CAPACITY_UNCERTAIN' not in overall['flags'] and
                'APPLICATION_REPORT_INCOMPLETE' not in overall['flags'])
            eligible = actionable_overall or bool(constrained)
            result['demand'] = dict(version=1, overall=overall, classes=per_class,
                                   controller_eligible=eligible,
                                   controller_reason=('measured local saturation with usable demand evidence'
                                       if actionable_overall else 'measured class limit with complete app demand'
                                       if constrained else 'hold: demand, capacity, or bottleneck evidence is incomplete'),
                                   estimator='native_bounded_v1', source_uncertainty=self.demand_model_error)
            self.latest = result
            self.update_demand_forecasts(time.monotonic())
        else:
            result['demand'] = dict(version=1, overall=None, classes={}, controller_eligible=False,
                                    controller_reason='hold: native demand estimator unavailable',
                                    estimator=None, source_uncertainty=self.demand_model_error)
        return result
