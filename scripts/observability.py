"""Correlate kernel snapshots, scoped trace events and explicit application samples."""
import os
import resource
import struct
import time
from collections import deque
from pathlib import Path

from bpf_source import BpfSource
from service_config import number
from tcp_diag import connections
from telemetry_windows import Windows

NANO = 1_000_000_000
FAILURE_REASONS = ('connect_refused', 'connect_timeout', 'reset', 'request_timeout', 'other')
CLOSING = {'fin_wait_1', 'fin_wait_2', 'closing', 'close_wait', 'last_ack', 'time_wait', 'closed'}


def delta(current, previous, key):
    a, b = current.get(key), previous.get(key)
    return a - b if type(a) is int and type(b) is int and a >= b else None


def rss(pid):
    try:
        return int(Path(f'/proc/{pid}/statm').read_text().split()[1]) * os.sysconf('SC_PAGE_SIZE')
    except (OSError, ValueError, IndexError):
        return None


def cpu_seconds(pid):
    try:
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        return (int(fields[11]) + int(fields[12])) / os.sysconf('SC_CLK_TCK')
    except (OSError, ValueError, IndexError):
        return None


class Observer:
    def __init__(self, cfg, clock=time.monotonic_ns):
        self.cfg, self.clock = cfg, clock
        self.bpf = BpfSource(cfg['bpf'])
        self.events = deque(maxlen=1024)
        self.event_sequence = 0
        self.generation = 0
        self.reset()

    def reset(self):
        self.windows = Windows(max(*self.cfg['windows'], self.cfg['thresholds']['window_seconds']))
        self.requests = {}
        self.current = {}
        self.previous = {}
        self.last_rows = []
        self.queue = {}
        self.queue_time = None
        self.sampled_ns = None
        self.truncated = False
        self.errors = {}
        self.detectors = {}
        self.last_detect = None
        self.processed = 0
        self.sample_cpu_ns = 0
        self.started = self.clock()

    def start(self, lab):
        self.generation += 1
        self.reset()
        if self.cfg['enabled']:
            self.bpf.start(lab.namespace_fds, lab.output)
        else:
            self.bpf.reason = 'observability disabled'

    def close(self):
        self.bpf.close()
        self.bpf.reason = 'no lab' if self.cfg['enabled'] else 'observability disabled'
        self.bpf.started = self.bpf.events = self.bpf.parse_drops = 0
        self.bpf.loss = {'kernel': None, 'transport': None}
        self.reset()

    def event(self, kind, ts, **detail):
        self.event_sequence += 1
        self.events.append(dict(sequence=self.event_sequence, time_ns=ts, generation=self.generation,
                                kind=kind, **detail))

    def ingest(self, request, uid):
        if not self.cfg['enabled']:
            raise ValueError('observability disabled')
        name = request.get('service')
        if not isinstance(name, str) or name not in self.cfg['services']:
            raise ValueError('unregistered service')
        cls = self.cfg['services'][name]['class']
        if request.get('class', cls) != cls:
            raise ValueError('service class does not match registration')
        now = self.clock()
        operation = request['op']
        deadline = request.get('deadline_ms')
        if deadline is not None:
            number(deadline, .001, 600000)
        for token, item in list(self.requests.items()):
            if now - item[0] > 600 * NANO:
                del self.requests[token]
        if operation == 'request_start':
            if len(self.requests) >= 1024:
                raise ValueError('too many in-flight requests')
            import secrets
            token = secrets.token_hex(16)
            self.requests[token] = now, name, uid, deadline
            return dict(token=token, started_ns=now, source='daemon IPC boundary')
        outcome = request.get('outcome')
        if outcome not in ('ok', 'timeout', 'failure'):
            raise ValueError('outcome must be ok, timeout or failure')
        reason = request.get('failure_reason')
        if reason is not None and (outcome == 'ok' or reason not in FAILURE_REASONS):
            raise ValueError('invalid failure reason for outcome')
        if operation == 'request_complete':
            token = request.get('token')
            item = self.requests.get(token) if isinstance(token, str) else None
            if not item or item[1:3] != (name, uid):
                raise ValueError('unknown or expired request token')
            started, _, _, deadline = self.requests.pop(token)
            duration = (now - started) / 1000
            source = 'ipc_boundary'
        else:
            duration = request.get('duration_us')
            number(duration, 0, 600_000_000)
            source = 'application'
        bucket = self.windows.at(now)
        if bucket is None:
            raise RuntimeError('application timestamp expired')
        key = f'app.{name}.{source}'
        bucket.counters[f'{key}.{outcome}'] += 1
        if reason:
            bucket.counters[f'{key}.reason.{reason}'] += 1
        if outcome == 'ok':
            bucket.observe(key, duration)
        if deadline is not None and (duration > deadline * 1000 or outcome == 'timeout'):
            bucket.counters[f'{key}.deadline_miss'] += 1
        self.processed += 1
        return dict(accepted=True, service=name, traffic_class=cls, provenance=source)

    def drain(self):
        for row in self.bpf.drain():
            ts = row['time_ns']
            bucket = self.windows.at(ts)
            if not bucket:
                continue
            kind = row['kind']
            self.processed += 1
            bucket.counters['bpf.events'] += 1
            if kind == 1:
                bucket.counters['bpf.state'] += 1
                old, new = row['old'], row['new']
                if new == 1:
                    bucket.counters['bpf.established'] += 1
                if old in (2, 3) and new == 7:
                    bucket.counters['bpf.handshake_close_candidates'] += 1
                if row['lifetime_ns']:
                    bucket.observe('bpf.lifetime', row['lifetime_ns'] / 1000)
                self.event('tcp_state', ts, source='bpf', **{k: row[k] for k in
                           ('cookie', 'flow_id', 'origin_state', 'netns', 'old', 'new', 'sport', 'dport', 'lifetime_ns')})
            elif kind == 2:
                bucket.counters['bpf.retransmit_events'] += 1
            elif kind == 3:
                bucket.counters['bpf.receive_resets'] += 1
                self.event('tcp_receive_reset', ts, source='bpf', cookie=row['cookie'], netns=row['netns'])

    def track(self, rows, truncated, ts):
        bucket = self.windows.at(ts)
        tracked = {}
        for row in rows:
            info = row['tcp_info']
            cookie = row['cookie']
            key = (row['namespace'], cookie) if cookie and cookie != '0000000000000000' else None
            old = self.current.get(key) if key else None
            identity = ('local_address', 'remote_address', 'local_port', 'remote_port', 'family')
            if old and any(row[field] != old[field] for field in identity):
                old = None
            row['first_seen_ns'] = old['first_seen_ns'] if old else ts
            row['observed_age_ms'] = (ts - row['first_seen_ns']) / 1e6 if key else None
            row['closing'] = row['state'] in CLOSING
            if old and old['state'] != row['state']:
                self.event('sampled_state_change', ts, source='inet_diag', cookie=cookie,
                           old=old['state'], new=row['state'], namespace=row['namespace'])
            if key:
                tracked[key] = row
                if not old and row['state'] != 'listen':
                    bucket.counters['diag.first_observed'] += 1
            if row['namespace'] == 'sender':
                if row['state'] == 'established' and info.get('rtt_us', 0) > 0:
                    bucket.observe('rtt', info['rtt_us'])
                if old and not truncated and not self.truncated:
                    retrans = delta(info, old['tcp_info'], 'total_retrans_segments')
                    segments = delta(info, old['tcp_info'], 'data_segments_out')
                    if retrans is not None and segments is not None:
                        bucket.counters['tcp.retrans'] += retrans
                        bucket.counters['tcp.segments'] += segments
                        bucket.counters['tcp.pairs'] += 1
        if not truncated and not self.truncated:
            bucket.counters['diag.no_longer_observed'] += len(self.current.keys() - tracked.keys())
        self.current = tracked
        self.last_rows = rows
        self.truncated = truncated
        self.sampled_ns = ts
        self.processed += len(rows)

    def sample_queue(self, tc, capacity, ts):
        root = next((row for row in tc.get('qdisc', []) if row.get('root')), {})
        link = tc.get('link', {})
        current = {'root': root, 'link': link}
        current.update({name: row.get('stats', {}) for name, row in tc.get('traffic_classes', {}).items()})
        bucket = self.windows.at(ts)
        if type(root.get('backlog')) is int:
            bucket.observe('backlog', root['backlog'])
        if self.queue_time is not None:
            elapsed = (ts - self.queue_time) / NANO
            for name, values in current.items():
                prior = self.previous.get(name, {})
                valid = False
                for field in ('bytes', 'drops', 'tx_bytes', 'tx_packets', 'rx_packets', 'tx_errors', 'rx_errors'):
                    value = delta(values, prior, field)
                    if value is not None:
                        bucket.counters[f'q.{name}.{field}'] += value
                        valid = True
                if valid:
                    bucket.counters[f'q.{name}.seconds'] += elapsed
                    bucket.counters[f'q.{name}.samples'] += 1
        self.previous, self.queue_time = current, ts
        self.queue = dict(capacity_kbit=capacity, root=root, link=link, classes=tc.get('traffic_classes', {}),
                          source='tc qdisc/class and RTM_GETLINK IFLA_STATS64')

    def sample(self, lab, tc, capacity):
        if not self.cfg['enabled']:
            return
        cpu = time.process_time_ns()
        ts = self.clock()
        self.sample_queue(tc, capacity, ts)
        rows = []
        truncated = False
        try:
            for index, name in enumerate(('sender', 'receiver')):
                with lab.enter(index):
                    found, limited = connections(self.cfg['max_sockets'] // 2)
                for row in found:
                    row['namespace'] = name
                rows.extend(found)
                truncated |= limited
            self.track(rows, truncated, self.clock())
            self.errors.pop('inet_diag', None)
        except (OSError, RuntimeError, ValueError, struct.error) as error:
            self.errors['inet_diag'] = str(error)
            self.current.clear()
            self.last_rows = []
            self.sampled_ns = None
        self.detect(self.clock())
        self.sample_cpu_ns += time.process_time_ns() - cpu

    def window(self, now, seconds):
        bucket = self.windows.summarize(now, seconds)
        counters = bucket.counters
        empty = dict(samples=0, overflow_samples=0, p50_us=None, p95_us=None, p99_us=None)
        hist = {key: item.summary('bytes' if key == 'backlog' else 'us') for key, item in bucket.histograms.items()}
        app = {}
        for name, settings in self.cfg['services'].items():
            app[name] = dict(traffic_class=settings['class'])
            for source in ('application', 'ipc_boundary'):
                key = f'app.{name}.{source}'
                counts = {outcome: counters[f'{key}.{outcome}'] for outcome in ('ok', 'failure', 'timeout', 'deadline_miss')}
                total = counts['ok'] + counts['failure'] + counts['timeout']
                app[name][source] = dict(**hist.get(key, empty), **counts,
                                         failure_reasons={reason: counters[f'{key}.reason.{reason}']
                                                          for reason in FAILURE_REASONS},
                                         error_ratio=(counts['failure'] + counts['timeout']) / total if total else None)
        queue = {}
        for name in self.previous:
            prefix = f'q.{name}.'
            observed = counters[prefix + 'seconds']
            byte_field = 'tx_bytes' if name == 'link' else 'bytes'
            throughput = counters[prefix + byte_field] / observed if observed and prefix + byte_field in counters else None
            capacity = self.queue.get('capacity_kbit', 0) * 125
            queue[name] = dict(samples=counters[prefix + 'samples'], observed_seconds=observed,
                               bytes_per_second=throughput,
                               utilization=throughput / capacity if throughput is not None and capacity else None,
                               drops_per_second=counters[prefix + 'drops'] / observed if observed and prefix + 'drops' in counters else None)
        segments = counters['tcp.segments']
        hooks = self.bpf.hooks
        traced = {
            key: counters[f'bpf.{key}'] if hook in hooks else None
            for key, hook in (('state', 'tcp_state'), ('established', 'tcp_state'),
                              ('handshake_close_candidates', 'tcp_state'),
                              ('retransmit_events', 'tcp_retransmit'), ('receive_resets', 'tcp_reset'))
        }
        return dict(seconds=seconds, application=app, rtt=hist.get('rtt', empty),
                    backlog=hist.get('backlog', {'samples': 0, 'p95_bytes': None}), queue=queue,
                    retransmit_ratio=counters['tcp.retrans'] / segments if segments else None,
                    retransmit_data_segments=segments, traced=traced, lifetime=hist.get('bpf.lifetime', empty),
                    diag_first_observed=counters['diag.first_observed'],
                    diag_no_longer_observed=counters['diag.no_longer_observed'])

    def detect(self, now):
        cfg = self.cfg['thresholds']
        seconds = cfg['window_seconds']
        if self.last_detect is not None and now - self.last_detect < seconds * NANO:
            return
        previous_tick = self.last_detect
        self.last_detect = now
        view = self.window(now, seconds)
        root = view['queue'].get('root', {})
        minimum = cfg['min_samples']
        rtt, backlog = view['rtt'], view['backlog']
        conditions = {
            'rtt_high': (rtt['p95_us'], cfg['rtt_us'], rtt['samples']),
            'retransmit_ratio_high': (view['retransmit_ratio'], cfg['retransmit_ratio'], view['retransmit_data_segments']),
            'queue_backlog_high': (backlog['p95_bytes'], cfg['backlog_bytes'], backlog['samples']),
            'queue_drops_high': (root.get('drops_per_second'), cfg['drops_per_second'], root.get('samples', 0)),
            'handshake_close_burst': ((view['traced']['handshake_close_candidates'] or 0) / seconds
                                      if view['traced']['handshake_close_candidates'] is not None else None,
                                      cfg['failures_per_second'], seconds),
        }
        if cfg['min_tx_bytes_per_second']:
            value = root.get('bytes_per_second')
            conditions['throughput_below_floor'] = (-value if value is not None else None,
                                                   -cfg['min_tx_bytes_per_second'], root.get('samples', 0))
        for name, service in self.cfg['services'].items():
            app = view['application'][name]['application']
            conditions[f'{name}.slo'] = (app['p99_us'], service['slo_ms'] * 1000, app['samples'])
            total = app['ok'] + app['failure'] + app['timeout']
            conditions[f'{name}.failure_burst'] = ((app['failure'] + app['timeout']) / seconds,
                                                   cfg['failures_per_second'], total)
        entered = []
        for name, (value, threshold, count) in conditions.items():
            state = self.detectors.setdefault(name, dict(bad=0, good=0, since=None, spent_ns=0, available=False))
            available = value is not None and count >= minimum
            if available and state['available'] and state['since'] is not None and previous_tick is not None:
                state['spent_ns'] += min(now - previous_tick, seconds * NANO)
            state['available'] = available
            if not available:
                state['bad'] = state['good'] = 0
                continue
            bad = value > threshold
            state['bad'] = state['bad'] + 1 if bad and state['since'] is None else 0
            state['good'] = state['good'] + 1 if not bad and state['since'] is not None else 0
            if state['bad'] >= cfg['enter_windows']:
                state['since'] = now
                state['bad'] = 0
                entered.append(name)
                self.event('degradation_enter', now, signal=name, value=value, threshold=threshold, samples=count)
            elif state['good'] >= cfg['exit_windows']:
                state['since'] = None
                self.event('degradation_exit', now, signal=name, value=value, samples=count)
        active = [name for name, state in self.detectors.items() if state['since'] is not None and state['available']]
        if entered and len(active) > 1:
            self.event('correlated_observations', now, signals=active,
                       evidence={name: conditions[name][0] for name in active}, causal_claim=False)

    def snapshot(self):
        now = self.clock()
        windows = {str(sec): self.window(now, sec) for sec in self.cfg['windows']}
        pid = self.bpf.proc.pid if self.bpf.proc else None
        utilization = windows[str(max(self.cfg['windows']))]['queue'].get('root', {}).get('utilization')
        backlog = self.queue.get('root', {}).get('backlog')
        saturation = (utilization >= .9 and backlog > 0) if utilization is not None and backlog is not None else None
        children = resource.getrusage(resource.RUSAGE_CHILDREN)
        return dict(enabled=self.cfg['enabled'], time_ns=now, generation=self.generation,
                    clock='CLOCK_MONOTONIC', socket_count=len(self.last_rows), truncated=self.truncated,
                    sample_age_seconds=(now - self.sampled_ns) / NANO if self.sampled_ns is not None else None,
                    collector_errors=self.errors, bpf=self.bpf.status(), windows=windows, queue=self.queue, sender_shaper_saturation_indicator=saturation,
                    findings={key: dict(active=val['since'] is not None,
                        data_available=val['available'], degraded_seconds=val['spent_ns']/NANO)
                        for key, val in self.detectors.items()},
                    overhead=dict(daemon_cpu_seconds=time.process_time(), daemon_rss_bytes=rss('self'),
                        child_cpu_seconds=children.ru_utime + children.ru_stime,
                        bpf_cpu_seconds=cpu_seconds(pid) if pid else None, bpf_rss_bytes=rss(pid) if pid else None,
                        polling_cpu_seconds=self.sample_cpu_ns / NANO,
                        records_processed=self.processed, late_event_drops=self.windows.late_drops,
                        records_per_second=self.processed / max(.001, (now-self.started) / NANO)),
                    provenance={'rtt': 'sender INET_DIAG TCP_INFO, repeated per-socket samples',
                                'application': 'explicit authenticated reports; separate IPC timings',
                                'queue': 'tc and rtnetlink; deltas attributed to interval end'})
