"""Measurement Integrity V3 for explicitly instrumented Research Lab trials."""
import csv
import hashlib
import math
import os
import resource
import stat
import time

from service_config import keys, number

VERSION = 3
NANO = 1_000_000_000
MAX_REQUESTS = 90_000
STAMP_FIELDS = ('scheduled_ns', 'released_ns', 'dispatch_ns', 'connect_ns', 'connected_ns',
                'submitted_ns', 'drained_ns', 'received_ns', 'completed_ns')
TIMING_FIELDS = ('id', 'outcome', *STAMP_FIELDS)
EXTRA_GATES = ('background_mbps', 'jain_index', 'cpu_seconds')


class WorkloadExit(RuntimeError):
    """The client exited before delivering a complete request ledger."""


def profile(value):
    keys(value, ('version', 'load', 'concurrency', 'ordering', 'cooldown_seconds', 'self_test',
                 'max_release_p99_ms', 'max_loop_lag_ms', 'max_cpu_psi_ratio', 'max_steal_ratio',
                 'max_counter_gap_ratio', 'max_failure_ratio', 'min_successful_requests',
                 'bootstrap_resamples', 'power_direction_probability', 'extra_gates'))
    number(value['version'], VERSION, VERSION, True)
    number(value['cooldown_seconds'], 0, 10, True)
    number(value['bootstrap_resamples'], 0, 4000, True)
    number(value['power_direction_probability'], .51, .99)
    number(value['min_successful_requests'], 1, MAX_REQUESTS, True)
    if value['load'] not in ('open', 'closed') or value['ordering'] not in ('randomized', 'counterbalanced'):
        raise ValueError('invalid measurement load or ordering')
    number(value['concurrency'], 1 if value['load']=='closed' else 0, 64 if value['load']=='closed' else 0, True)
    if value['self_test'] not in ('none', 'dispatch_delay', 'client_exit'):
        raise ValueError('unknown measurement self-test')
    for key in ('max_release_p99_ms', 'max_loop_lag_ms'):
        number(value[key], .01, 5000)
    for key in ('max_cpu_psi_ratio', 'max_steal_ratio', 'max_counter_gap_ratio', 'max_failure_ratio'):
        number(value[key], 0, 1)
    keys(value['extra_gates'], EXTRA_GATES)
    for limit in value['extra_gates'].values():
        number(limit, 0, 1)
    return value


def process_sample():
    row = resource.getrusage(resource.RUSAGE_SELF)
    return dict(time_ns=time.monotonic_ns(), user_cpu_seconds=row.ru_utime, system_cpu_seconds=row.ru_stime,
                voluntary_switches=row.ru_nvcsw, involuntary_switches=row.ru_nivcsw,
                peak_rss_kib=row.ru_maxrss)


def process_delta(before, after):
    return {key: after[key]-before[key] for key in before if key not in ('time_ns', 'peak_rss_kib')} | {
        'elapsed_seconds': (after['time_ns']-before['time_ns'])/NANO,
        'peak_rss_kib': after['peak_rss_kib']}


def block_order(modes, repetitions, rng, ordering):
    if ordering == 'randomized':
        return [rng.sample(modes, len(modes)) for _ in range(repetitions)]
    # Williams sequences balance positions and first-order carryover over a
    # full cycle. For odd order include the reverse sequences.
    count = len(modes)
    labels = rng.sample(modes, count)
    pattern = [0, *(i//2+1 if i%2==0 else count-(i+1)//2 for i in range(count-1))]
    cycle = [[labels[(item+start)%count] for item in pattern] for start in range(count)]
    if count%2 and count>1:
        cycle += [list(reversed(row)) for row in cycle]
    rng.shuffle(cycle)
    return [cycle[i%len(cycle)] for i in range(repetitions)]


def partial_timing_ledger(directory):
    """Account only flushed attempts after a client crash; never infer total offered load."""
    import io
    from pathlib import Path
    data = read_bounded(Path(directory)/'request-timing.csv', MAX_REQUESTS*256)
    reader = csv.DictReader(io.StringIO(data.decode('utf-8')))
    if tuple(reader.fieldnames or ()) != TIMING_FIELDS:
        return dict(available=True, valid=False, rows=0, error='INVALID_TIMING_SCHEMA')
    ids, outcomes = set(), {}
    count = 0
    for row in reader:
        count += 1
        idx = int(row['id'])
        if count > MAX_REQUESTS or not 0 <= idx < MAX_REQUESTS or idx in ids:
            return dict(available=True, valid=False, rows=count, error='DUPLICATE_OR_OUT_OF_RANGE_TIMING_ID')
        ids.add(idx)
        outcomes[row['outcome']] = outcomes.get(row['outcome'],0)+1
        stamps = [int(row[key]) if row[key] else None for key in STAMP_FIELDS]
        if stamps[1] is None or stamps[-1] is None:
            return dict(available=True, valid=False, rows=count, error='PARTIAL_ATTEMPT_MISSING_TERMINAL_STAMP')
        present = [stamp for stamp in stamps if stamp is not None]
        if any(stamp<=0 or stamp>=2**63 for stamp in present) or any(b<a for a,b in zip(present,present[1:])):
            return dict(available=True, valid=False, rows=count, error='NONMONOTONIC_PARTIAL_TIMESTAMPS')
    return dict(available=True, valid=True, completed_timing_records=count, outcome_counts=outcomes,
                offered_total_unknown=True)


def timing_audit(directory, result, manifest):
    from workload import percentile
    p = manifest['integrity']
    start, end = (round(result[key]*NANO) for key in ('start', 'end'))
    open_loop = p['load']=='open'
    trace = {}
    with (directory/'request-timing.csv').open() as stream:
        reader = csv.DictReader(stream)
        if tuple(reader.fieldnames or ()) != TIMING_FIELDS:
            raise ValueError('INVALID_TIMING_SCHEMA')
        for row in reader:
            idx = int(row['id'])
            if not 0 <= idx < result['offered'] or idx in trace or len(trace)>=MAX_REQUESTS:
                raise ValueError('DUPLICATE_OR_OUT_OF_RANGE_TIMING_ID')
            trace[idx] = row
    if len(trace) != result['offered'] or not trace:
        raise ValueError('INCOMPLETE_TIMING_LEDGER')

    def offsets():
        cursor = 0
        for phase in manifest['workload']['phases']:
            rate = phase['requests_per_second']
            for index in range(phase['seconds']*rate):
                yield round((cursor+index/rate)*NANO)
            cursor += phase['seconds']

    planned = iter(offsets())
    release, dispatch, network, total, client = [], [], [], [], []
    connect_time, drain_time, response_exchange = [], [], []
    fingerprint = hashlib.sha256()
    processed = 0
    with (directory/'requests.csv').open() as stream:
        for row in csv.DictReader(stream):
            idx = int(row['id'])
            if idx != processed or idx not in trace or trace[idx]['outcome'] != row['outcome']:
                raise ValueError('REQUEST_LEDGER_MISMATCH')
            processed += 1
            stamps = [int(trace[idx][key]) if trace[idx][key] else None for key in STAMP_FIELDS]
            scheduled, released, run, connect, connected, submitted, drained, received, complete = stamps
            if (any(stamp is not None and not 0 < stamp < 2**63 for stamp in stamps)
                    or released is None or complete is None or released < start
                    or (not open_loop and released >= end)):
                raise ValueError('TIMESTAMP_OUTSIDE_MEASUREMENT_DOMAIN')
            present = [stamp for stamp in stamps if stamp is not None]
            if any(b<a for a,b in zip(present, present[1:])):
                raise ValueError('NONMONOTONIC_REQUEST_TIMESTAMPS')
            if row['outcome']=='ok' and any(stamp is None for stamp in stamps[1:]):
                raise ValueError('SUCCESS_WITH_MISSING_TIMESTAMPS')
            if open_loop:
                offset = next(planned, None)
                if offset is None or scheduled is None or abs(scheduled-start-offset)>2 or scheduled>=end:
                    raise ValueError('OFFERED_SCHEDULE_MISMATCH')
                release.append((released-scheduled)/1e6)
                fingerprint.update(f"{offset},{row['traffic_class']},{row['service']}\n".encode())
            elif scheduled is not None:
                raise ValueError('CLOSED_LOOP_HAS_SCHEDULED_TIMESTAMP')
            reference = scheduled if open_loop else released
            if (abs(round(float(row['scheduled'])*NANO)-reference)>2
                    or abs(round(float(row['completed'])*NANO)-complete)>2
                    or not math.isclose(float(row['latency_ms']), (complete-reference)/1e6, abs_tol=.000002)):
                raise ValueError('TIMESTAMP_CONVERSION_MISMATCH')
            if run is not None:
                dispatch.append((run-released)/1e6)
            if row['outcome']=='ok':
                network.append((received-submitted)/1e6)
                connect_time.append((connected-connect)/1e6)
                drain_time.append((drained-submitted)/1e6)
                response_exchange.append((received-drained)/1e6)
                client.append((complete-released)/1e6)
                total.append((complete-reference)/1e6)
    if processed!=len(trace) or (open_loop and next(planned, None) is not None):
        raise ValueError('INCOMPLETE_OFFERED_COHORT')
    if ((total and not math.isclose(percentile(total,.99), result['p99_ms'], abs_tol=.000002))
            or (not total and result['p99_ms'] is not None)):
        raise ValueError('PERCENTILE_LEDGER_MISMATCH')
    return dict(clock='CLOCK_MONOTONIC', records=len(trace), scheduled_workload_sha256=(
        fingerprint.hexdigest() if open_loop else None), release_lag_p99_ms=percentile(release,.99),
        task_dispatch_p99_ms=percentile(dispatch,.99), tcp_connect_p99_ms=percentile(connect_time,.99),
        write_drain_p99_ms=percentile(drain_time,.99), response_after_drain_p99_ms=percentile(response_exchange,.99),
        submit_to_response_p99_ms=percentile(network,.99), release_to_completion_p99_ms=percentile(client,.99),
        kernel_packet_timestamps=None)


def classify(directory, result, manifest, host, runner):
    p = manifest['integrity']
    issues, cautions = [], []
    try:
        timing = timing_audit(directory, result, manifest)
    except (OSError, ValueError, TypeError, KeyError) as error:
        timing = {}
        issues.append(dict(code='TIMING_INTEGRITY', detail=str(error)))
    client = result['measurement_client']
    before, after = host['start'], host['end']
    elapsed = (after['time_ns']-before['time_ns'])/NANO
    ticks = [b-a for a,b in zip(before['cpu_ticks'], after['cpu_ticks'])]
    if any(delta<0 for delta in ticks) or sum(ticks)<=0 or elapsed<=0:
        issues.append(dict(code='HOST_COUNTER_RESET'))
    pressure = {}
    for name in ('cpu','memory','io'):
        key = 'some' if name=='cpu' else 'full'
        a = before['pressure_total_us'].get(name, {}).get(key)
        b = after['pressure_total_us'].get(name, {}).get(key)
        pressure[name] = (b-a)/(elapsed*1e6) if a is not None and b is not None and b>=a and elapsed>0 else None
        if pressure[name] is None:
            cautions.append(dict(code='PRESSURE_SENSOR_UNAVAILABLE', resource=name))
    steal = ticks[7]/sum(ticks) if len(ticks)>7 and all(x>=0 for x in ticks) and sum(ticks)>0 else None
    for code, value, limit in (
            ('OPEN_RELEASE_P99', timing.get('release_lag_p99_ms'), p['max_release_p99_ms']),
            ('EVENT_LOOP_LAG', client['loop_lag_max_ms'], p['max_loop_lag_ms']),
            ('CPU_PSI', pressure['cpu'], p['max_cpu_psi_ratio']),
            ('CPU_STEAL', steal, p['max_steal_ratio'])):
        if value is not None and value>limit:
            issues.append(dict(code=code, observed=value, limit=limit))
    if client['request_ceiling_hit']:
        issues.append(dict(code='GENERATOR_REQUEST_CEILING'))
    if client['load']=='closed' and client['peak_inflight']>client['configured_concurrency']:
        issues.append(dict(code='CLOSED_LOOP_CONCURRENCY_EXCEEDED'))
    if client['load']=='open' and client['peak_outstanding']>client['request_cap']:
        issues.append(dict(code='OPEN_LOOP_REQUEST_CAP_EXCEEDED'))
    if client['load']=='closed' and client['peak_inflight']<client['configured_concurrency']:
        cautions.append(dict(code='CLOSED_LOOP_DID_NOT_REACH_CONFIGURED_CONCURRENCY',
                             configured=client['configured_concurrency'],observed=client['peak_inflight']))
    windows = result['observed_rate_windows']
    expected_bytes = sum(row['sender_link']['tx_bytes'] or 0 for row in windows)
    payload_bytes = result['total_mbps']*1e6*(result['end']-result['start'])/8
    if not windows or any(row['sender_link']['tx_bytes'] is None for row in windows):
        issues.append(dict(code='MISSING_WIRE_COUNTERS'))
    else:
        if payload_bytes > expected_bytes*(1+p['max_counter_gap_ratio']):
            issues.append(dict(code='PAYLOAD_EXCEEDS_WIRE_BYTES', payload_bytes=payload_bytes, wire_bytes=expected_bytes))
        if any((row['forward_link_bytes_relative_gap'] or 0)>p['max_counter_gap_ratio'] for row in windows):
            issues.append(dict(code='FORWARD_COUNTER_MISMATCH'))
    if result.get('outcomes', {}).get('inflight_limit'):
        cautions.append(dict(code='OPEN_LOOP_INFLIGHT_LIMIT', count=result['outcomes']['inflight_limit']))
    expected_probes = max(0,(round((result['end']-result['start'])*NANO)-1)//50_000_000)
    observed_probes = client['loop_samples']+client.get('missed_loop_ticks',0)
    if not client['loop_samples'] or observed_probes != expected_probes:
        issues.append(dict(code='EVENT_LOOP_PROBE_ACCOUNTING', expected=expected_probes,
                           observed=observed_probes,delivered=client['loop_samples'],
                           missed=client.get('missed_loop_ticks',0)))
    if result.get('event_loss') not in (None,0):
        cautions.append(dict(code='FLOWCORE_TELEMETRY_EVENT_LOSS', count=result['event_loss']))
    if result.get('udp_send_errors'):
        cautions.append(dict(code='UDP_SEND_ERRORS', count=len(result['udp_send_errors'])
                             if isinstance(result['udp_send_errors'],list) else result['udp_send_errors']))
    state = ('INVALID_INFRASTRUCTURE' if issues else 'FAILED_WORKLOAD'
             if result['failure_ratio']>p['max_failure_ratio'] else 'INCONCLUSIVE'
             if result['completed']<p['min_successful_requests'] else 'VALID')
    return dict(version=VERSION, state=state, issues=issues, cautions=cautions, timing=timing, client=client,
                host_pressure_ratios=pressure, steal_ratio=steal, runner=runner,
                achieved_release_rps=result['offered']/(result['end']-result['start']),
                achieved_completion_rps=result['completed']/(result['end']-result['start']),
                host_runnable_at_start=before.get('runnable_tasks'), host_runnable_at_end=after.get('runnable_tasks'),
                visible_cpu_count=os.cpu_count())


def digest(path):
    # Avoid following a replaced symlink during post-run verification.
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 128*1024*1024:
            raise ValueError('raw artifact is not a bounded regular file')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            return hashlib.file_digest(stream, 'sha256').hexdigest()
    finally:
        os.close(fd)


def read_bounded(path, limit):
    import stat
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError('artifact is not a bounded regular file')
        chunks = []
        remaining = limit+1
        while remaining:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b''.join(chunks)
        if len(data)>limit:
            raise ValueError('artifact exceeds configured size limit')
        return data
    finally:
        os.close(fd)


def verify(directory, row):
    import json
    from pathlib import Path

    directory = Path(directory)
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError('artifact directory is not a real directory')
    sidecar = directory/'checksums.json'
    sidecar_digest = digest(sidecar)
    if sidecar_digest != row.get('evidence_sha256'):
        raise ValueError('checksums.json digest differs from summary')
    sidecar_data = read_bounded(sidecar, 1024*1024)
    if hashlib.sha256(sidecar_data).hexdigest() != row.get('evidence_sha256'):
        raise ValueError('checksums.json changed during verification')
    manifest = json.loads(sidecar_data)
    checksums = manifest.get('sha256') if manifest.get('version') == VERSION else None
    if not isinstance(checksums, dict) or len(checksums) > 128:
        raise ValueError('invalid checksum manifest')
    for name, expected in checksums.items():
        if not isinstance(name, str) or name in ('', '.', '..') or Path(name).name != name:
            raise ValueError('unsafe checksum path')
        if not isinstance(expected, str) or len(expected) != 64:
            raise ValueError('invalid artifact digest')
        if digest(directory/name) != expected:
            raise ValueError(f'artifact digest mismatch: {name}')
    result_data = read_bounded(directory/'result.json', 16*1024*1024)
    if hashlib.sha256(result_data).hexdigest() != checksums.get('result.json'):
        raise ValueError('result.json changed during verification')
    result = json.loads(result_data)
    expected_result = {key:value for key,value in row.items() if key not in ('evidence_sha256','artifact_integrity')}
    if result != expected_result:
        raise ValueError('result.json differs from summary row')
    return dict(verified=len(checksums), evidence_sha256=row['evidence_sha256'])


def seal(directory):
    import json

    raw = {}
    for item in sorted(directory.iterdir()):
        if item.is_symlink() or not item.is_file() or item.name in ('checksums.json', 'transitions.csv'):
            continue
        if len(raw)>=128 or item.stat().st_size > 128*1024*1024:
            raise ValueError('raw artifact exceeds checksum budget')
        raw[item.name] = digest(item)
    manifest = directory/'checksums.json'
    with manifest.open('x') as stream:
        json.dump(dict(version=VERSION, sha256=raw), stream, indent=2)
        stream.write('\n')
    return digest(manifest)
