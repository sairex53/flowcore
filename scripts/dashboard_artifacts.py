"""Explicit, bounded adapters for FLOWCORE's recorded experiment artifacts."""
import json
import os
from pathlib import Path
import stat
import threading

from dashboard_model import decision, fields

ARTIFACT_BYTES = 16 * 1024 * 1024
MAX_RUNS = 512
MAX_DECISIONS = 64


def parse(raw):
    def invalid(_):
        raise ValueError('non-finite artifact value')
    try:
        return json.loads(raw, parse_constant=invalid)
    except RecursionError as error:
        raise ValueError('artifact nesting limit') from error


def record(value):
    return value if isinstance(value, dict) else {}


def sequence(value, limit):
    return value[:limit] if isinstance(value, list) else []


def latency(metrics):
    metrics = record(metrics)
    value = fields(metrics, ('p50_ms', 'p95_ms', 'p99_ms', 'error_rate', 'deadline_miss_rate',
                             'errors', 'deadline_misses', 'attempted', 'completed'))
    for canonical, source in (('error_rate', 'failure_ratio'), ('errors', 'failed'), ('attempted', 'offered')):
        if value[canonical] is None:
            value[canonical] = metrics.get(source)
    return value


def real_apps(rows):
    runs = []
    for row in rows:
        measured = (row.get('status') == 'MEASURED' and isinstance(row.get('host'), dict)
                    and isinstance(row.get('http'), dict))
        control = record(row.get('controller_state'))
        session = control.get('controller_session')
        history = [item for item in sequence(row.get('policy_history'), 128) if isinstance(item, dict)
                   and session and item.get('controller_session') == session]
        runs.append(dict(mode=row.get('mode'), seed=row.get('seed'), repetition=row.get('repetition'),
                         status=row.get('status'), measured=measured, latency=latency(row.get('http')),
                         throughput_mbps=record(row.get('background_transfer')).get('goodput_mbps'),
                         cpu_seconds=row.get('daemon_cpu_seconds'),
                         fairness=record(control.get('fairness')).get('jain_active_classes'),
                         controller_actions=control.get('actions'),
                         configuration=fields(row, ('configured_capacity_kbit', 'effective_shaper_capacity_kbit',
                                                    'configured_conditions', 'host')),
                         decisions=[decision(item) for item in history[-MAX_DECISIONS:]]))
    return 'real-applications', runs, {'latency': 'HTTP API', 'throughput': 'background transfer goodput',
        'fairness': 'controller-reported active class Jain index, when available',
        'cpu': 'daemon CPU seconds, including the runner measurement interval'}


def fabric_runs(rows):
    runs = []
    for row in rows:
        workload = record(row.get('workload'))
        control = record(workload.get('control_cost'))
        processes = record(control.get('processes'))
        runs.append(dict(mode=row.get('mode'), seed=row.get('seed'), status='completed',
                         measured=bool(row.get('shared_readback') and workload.get('requests')),
                         latency=latency(record(workload.get('requests')).get('a')),
                         throughput_mbps=(workload['rates_kbit']['shared'] / 1000
                            if type(record(workload.get('rates_kbit')).get('shared')) in (int, float) else None),
                         fairness=workload.get('jain'),
                         cpu_seconds=record(processes.get('coordinator')).get('cpu_seconds'),
                         configuration=fields(row, ('initial', 'shared_readback')),
                         decisions=[], recovery=row.get('failure')))
    return 'fabric', runs, {'latency': 'host A HTTP API', 'throughput': 'shared router egress during overlap',
                            'fairness': 'Jain index across sender rates', 'cpu': 'coordinator CPU seconds'}


def multipath(data, rows):
    runs = []
    for row in rows:
        metrics = record(row.get('request_metrics'))
        runs.append(dict(mode=row.get('strategy'), seed=row.get('seed'), repetition=row.get('repetition'),
                         status=row.get('status'), measured=row.get('valid') is True,
                         latency=latency(metrics), throughput_mbps=row.get('post_recovery_goodput_mbps'),
                         fairness=None, cpu_seconds=None,
                         configuration=fields(row, ('scenario', 'configured_path_count', 'initial_policy')),
                         transport=fields(row, ('transport', 'mptcp_confirmed', 'fallback_observed',
                                  'observed_mptcp_subflows', 'observed_subflow_source_addresses', 'connection_survived')),
                         paths=sequence(row.get('path_quality'), 8),
                         transitions=[fields(item, ('event', 'name', 'path_id', 'interface', 'scheduled_ns',
                                    'actual_application_ns', 'kernel_confirmed_ns', 'operation_latency_ns',
                                    'kernel_state', 'active_paths')) for item in sequence(row.get('transitions'), 32)
                                    if isinstance(item, dict)],
                         recovery=row.get('recovery'),
                         decisions=sequence(row.get('policy_decisions'), MAX_DECISIONS)))
    return 'multipath', runs, {'latency': 'measured application request completion',
          'throughput': 'post-recovery goodput', 'configuration': fields(data, (
              'scenario', 'path_count', 'duration_seconds', 'target_latency_ms', 'requested_strategies'))}


def normalize(data):
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict) and data.get('experiment') and isinstance(data.get('runs'), list):
        rows = data['runs']
    else:
        raise ValueError('unsupported artifact format')
    if not 1 <= len(rows) <= MAX_RUNS or any(not isinstance(row, dict) for row in rows):
        raise ValueError('invalid or oversized run matrix')
    if isinstance(data, dict) and all('observed_mptcp_subflows' in row and 'path_quality' in row for row in rows):
        kind, runs, units = multipath(data, rows)
    elif all('mode' in row and 'status' in row and ('http' in row or row['status'] != 'MEASURED') for row in rows):
        kind, runs, units = real_apps(rows)
    elif all(row.get('mode') in ('independent', 'fabric') and 'shared_readback' in row and 'workload' in row for row in rows):
        kind, runs, units = fabric_runs(rows)
    else:
        raise ValueError('unsupported artifact format')
    simulated = ((isinstance(data, dict) and (data.get('simulated') or data.get('dry_run'))) or
                 any(row.get('simulated') or row.get('dry_run') or
                     str(row.get('status', '')).lower() in ('simulated', 'synthetic', 'dry-run') for row in rows))
    if simulated:
        for row in runs:
            row['measured'] = False
    return dict(kind=kind, provenance='simulated' if simulated else 'recorded-artifact',
                source_note='operator-provided artifact; format and recorded outcome only, no attestation',
                measured_runs=sum(row['measured'] for row in runs), units=units, runs=runs)


class Artifacts:
    def __init__(self, configured):
        self.paths = {identifier: Path(path) for identifier, path in configured.items()}
        self.cache = {}
        self.lock = threading.Lock()

    def read(self, identifier):
        path = self.paths[identifier]
        with self.lock:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
            with os.fdopen(fd, 'rb') as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not 1 <= info.st_size <= ARTIFACT_BYTES:
                    raise ValueError('artifact must be a bounded regular file')
                stamp = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
                cached = self.cache.get(identifier)
                if cached and cached[0] == stamp:
                    return cached[1]
                data = stream.read(ARTIFACT_BYTES+1)
                if len(data) != info.st_size:
                    raise ValueError('artifact changed while reading')
            # Keep only one projected matrix. Large configured files cannot
            # accumulate across successive dashboard views.
            projected = dict(id=identifier, **normalize(parse(data)))
            self.cache = {identifier: (stamp, projected)}
            return projected

    def catalogue(self):
        return [{'id': name} for name in sorted(self.paths)]
