"""Explainable path-quality summaries and bounded MPTCP endpoint plans."""
import math

STRATEGIES = (
    'linux-default',
    'static-endpoints',
    'latency-preferred',
    'reliability-preferred',
    'throughput-preferred',
    'slo-aware',
)


def percentile(values, fraction):
    ordered = sorted(values)
    if not ordered:
        return None
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]


def summarize_probe(path, latency_samples, successes, attempts, bytes_sent, elapsed_seconds):
    if (type(successes) is not int or type(attempts) is not int or attempts < 1 or
            not 0 <= successes <= attempts or type(bytes_sent) is not int or bytes_sent < 0 or
            not math.isfinite(elapsed_seconds) or elapsed_seconds <= 0):
        raise ValueError('invalid path probe accounting')
    latencies = []
    for value in latency_samples:
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise ValueError('invalid path latency sample')
        latencies.append(float(value))
    if len(latencies) != successes:
        raise ValueError('successful probe count and latency samples differ')
    return dict(
        path_id=path['id'],
        interface=path['client_interface'],
        source=path['client_address'],
        destination=path['server_address'],
        configured=path['profile'],
        attempts=attempts,
        successes=successes,
        availability=successes / attempts,
        probe_failure_ratio=(attempts - successes) / attempts,
        rtt_p50_ms=percentile(latencies, .50),
        rtt_p95_ms=percentile(latencies, .95),
        rtt_p99_ms=percentile(latencies, .99),
        rtt_sample_count=len(latencies),
        throughput_mbps=bytes_sent * 8 / elapsed_seconds / 1e6 if bytes_sent else 0.0,
        throughput_bytes=bytes_sent,
        throughput_interval_seconds=elapsed_seconds,
        rtt_spread_ms=max(latencies) - min(latencies) if len(latencies) > 1 else 0.0,
    )


def _validate_quality(row):
    if not isinstance(row, dict):
        raise ValueError('path quality must be an object')
    path_id = row.get('path_id')
    if type(path_id) is not int or path_id < 1:
        raise ValueError('path identifiers must be positive integers')
    attempts = row.get('attempts')
    successes = row.get('successes')
    if (type(attempts) is not int or attempts < 1 or type(successes) is not int or
            not 0 <= successes <= attempts):
        raise ValueError('invalid path probe counts')
    for key in ('availability', 'probe_failure_ratio', 'throughput_mbps'):
        value = row.get(key)
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError(f'invalid path quality field: {key}')
    if (not 0 <= row['availability'] <= 1 or
            not 0 <= row['probe_failure_ratio'] <= 1 or
            row['throughput_mbps'] < 0):
        raise ValueError('path quality is outside its valid range')
    latency = row.get('rtt_p95_ms')
    if latency is not None and (type(latency) not in (int, float) or
                                not math.isfinite(latency) or latency < 0):
        raise ValueError('invalid path RTT P95')
    return path_id


def choose_path(strategy, qualities, target_latency_ms=None, unavailable=()):
    if strategy not in STRATEGIES:
        raise ValueError('unsupported multipath strategy')
    if not isinstance(qualities, list) or not qualities:
        raise ValueError('at least one path quality record is required')
    ids = [_validate_quality(row) for row in qualities]
    if len(set(ids)) != len(ids):
        raise ValueError('path identifiers must be unique')
    if not isinstance(unavailable, (list, tuple, set, frozenset)):
        raise ValueError('unavailable path identifiers must be a collection')
    if any(type(item) is not int for item in unavailable):
        raise ValueError('unavailable path identifiers must be integers')
    unavailable = set(unavailable)
    if unavailable - set(ids):
        raise ValueError('unavailable path is not part of this lab')
    usable = [row for row in qualities
              if row['path_id'] not in unavailable and row['successes'] > 0]
    if not usable:
        raise ValueError('no measured usable path remains')

    def latency(row):
        return (row['rtt_p95_ms'] if row['rtt_p95_ms'] is not None else math.inf,
                row['probe_failure_ratio'], row['path_id'])

    def reliability(row):
        return (row['probe_failure_ratio'], -row['availability'],
                row['rtt_p95_ms'] if row['rtt_p95_ms'] is not None else math.inf,
                row['path_id'])

    def throughput(row):
        return (-row['throughput_mbps'], row['probe_failure_ratio'],
                row['rtt_p95_ms'] if row['rtt_p95_ms'] is not None else math.inf,
                row['path_id'])

    feasible = None
    if strategy in ('linux-default', 'static-endpoints'):
        chosen = next((row for row in usable if row['path_id'] == ids[0]), usable[0])
    elif strategy == 'latency-preferred':
        chosen = min(usable, key=latency)
    elif strategy == 'reliability-preferred':
        chosen = min(usable, key=reliability)
    elif strategy == 'throughput-preferred':
        chosen = min(usable, key=throughput)
    else:
        if (type(target_latency_ms) not in (int, float) or
                not math.isfinite(target_latency_ms) or target_latency_ms <= 0):
            raise ValueError('SLO-aware strategy requires a positive probe RTT target')
        eligible = [row for row in usable if row['rtt_p95_ms'] is not None and
                    row['rtt_p95_ms'] <= target_latency_ms and row['availability'] >= .9]
        feasible = bool(eligible)
        chosen = min(eligible or usable, key=latency)

    rationale = {
        'linux-default': 'Keep every configured endpoint active; leave path choice to the Linux MPTCP scheduler.',
        'static-endpoints': 'Keep path 1 as the fixed primary; mark other owned endpoints as backups.',
        'latency-preferred': 'Choose the lowest measured probe RTT P95 as primary; use other paths as backups.',
        'reliability-preferred': 'Choose the lowest observed probe failure ratio, then availability and RTT; keep other paths as backups.',
        'throughput-preferred': 'Choose the highest measured per-path probe goodput as primary; keep other paths as backups.',
        'slo-aware': 'Use probe RTT P95 and availability as a path-selection heuristic; application SLO attainment is measured separately.',
    }[strategy]
    all_active = strategy == 'linux-default'
    backup = [] if all_active else [row['path_id'] for row in qualities
                                    if row['path_id'] != chosen['path_id']]
    plan = dict(
        strategy=strategy,
        chosen_path_id=chosen['path_id'],
        backup_path_ids=backup,
        all_paths_active=all_active,
        target_latency_ms=target_latency_ms,
        target_semantics='probe RTT P95 heuristic only; not an application-latency guarantee',
        probe_target_feasible=feasible,
        rationale=rationale,
        observations=[dict(
            path_id=row['path_id'],
            rtt_p95_ms=row['rtt_p95_ms'],
            probe_failure_ratio=row['probe_failure_ratio'],
            availability=row['availability'],
            throughput_mbps=row['throughput_mbps'],
            available=row['path_id'] not in unavailable,
        ) for row in qualities],
    )
    validate_plan(plan, ids)
    return plan


def validate_plan(plan, owned_ids):
    if not isinstance(plan, dict) or not isinstance(owned_ids, (list, tuple, set)):
        raise ValueError('path policy requires owned endpoint identifiers')
    owned = set(owned_ids)
    chosen = plan.get('chosen_path_id')
    backups = plan.get('backup_path_ids')
    if not owned or type(chosen) is not int or chosen not in owned or not isinstance(backups, list):
        raise ValueError('path plan references an unowned endpoint')
    if (any(type(item) is not int or item not in owned for item in backups) or
            chosen in backups or len(backups) != len(set(backups))):
        raise ValueError('invalid backup endpoint set')
    if plan.get('all_paths_active') is True:
        if backups:
            raise ValueError('all-active plan cannot mark backup endpoints')
    elif set(backups) != owned - {chosen}:
        raise ValueError('single-primary plan must account for every owned backup endpoint')
    return True
