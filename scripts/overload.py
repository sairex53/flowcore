"""Read-only overload evidence from Research Lab V3 and opt-in pressure advice."""
from collections import Counter, defaultdict
import csv
import io
import json
import math
import time

from measurement import MAX_REQUESTS, TIMING_FIELDS, read_bounded

REJECTIONS = {
    'inflight_limit': 'CLIENT_INFLIGHT_LIMIT',
    'admission_rejected': 'APPLICATION_ADMISSION_REJECTED',
    'admission_deferred': 'APPLICATION_ADMISSION_DEFERRED',
}
STAMP_ORDER = TIMING_FIELDS[2:]
NANO = 1_000_000_000


def failure(row):
    outcome = row['outcome']
    if outcome == 'ok':
        return None
    if outcome in REJECTIONS:
        return REJECTIONS[outcome]
    if outcome == 'TimeoutError':
        if row['drained_ns']:
            return 'TIMEOUT_AWAITING_RESPONSE'
        if row['submitted_ns']:
            return 'TIMEOUT_DURING_WRITE_DRAIN'
        if row['connect_ns'] and not row['connected_ns']:
            return 'TIMEOUT_CONNECTING'
        return 'TIMEOUT_BEFORE_SUBMISSION'
    if outcome in ('ConnectionError', 'ConnectionRefusedError', 'ConnectionResetError',
                   'ConnectionAbortedError', 'BrokenPipeError', 'OSError'):
        if row['connect_ns'] and not row['connected_ns']:
            return 'CONNECTION_FAILURE'
        if row['connected_ns']:
            return 'CONNECTED_IO_FAILURE'
    if outcome == 'IncompleteReadError' and row['drained_ns']:
        return 'RESPONSE_EOF'
    return 'UNKNOWN'


def csv_rows(path, limit):
    reader = csv.DictReader(io.StringIO(read_bounded(path, limit).decode('utf-8')))
    for row in reader:
        if None in row or any(value is None for value in row.values()):
            raise ValueError('invalid ledger row width')
        yield row


def accounting(directory, result, payload):
    timing = {}
    for row in csv_rows(directory/'request-timing.csv', MAX_REQUESTS*256):
        idx = int(row['id'])
        if tuple(row) != TIMING_FIELDS or not 0 <= idx < MAX_REQUESTS or idx in timing:
            raise ValueError('invalid or duplicate timing record')
        timing[idx] = row
    counts = Counter()
    reasons = Counter()
    service_reasons = defaultdict(Counter)
    services = defaultdict(Counter)
    end_ns = round(result['end']*NANO)
    for idx, request in enumerate(csv_rows(directory/'requests.csv', MAX_REQUESTS*256)):
        row = timing.pop(idx, None)
        if row is None or int(request['id']) != idx or row['outcome'] != request['outcome']:
            raise ValueError('request and timing ledgers disagree')
        stamps = [int(row[key]) for key in STAMP_ORDER if row[key]]
        reference = int(row['scheduled_ns'] or row['released_ns'])
        completed_ns = int(row['completed_ns'])
        if (not stamps or stamps[0] <= 0 or stamps[-1] >= 2**63 or not row['released_ns']
                or any(a > b for a, b in zip(stamps, stamps[1:]))
                or not math.isclose(float(request['latency_ms']), (completed_ns-reference)/1e6, abs_tol=.000002)):
            raise ValueError('invalid timing sequence')
        reason = failure(row)
        admitted = row['outcome'] not in REJECTIONS
        submitted = bool(row['submitted_ns'])
        if (not admitted and submitted) or (reason is None and not row['received_ns']):
            raise ValueError('outcome contradicts submission evidence')
        deadline = request.get('deadline_ms') or result.get('deadline_ms')
        if deadline is not None:
            deadline = float(deadline)
            if not 0 < deadline <= 600000:
                raise ValueError('invalid request deadline')
        attained = None if deadline is None else reason is None and (completed_ns-reference)/1e6 <= deadline
        facts = dict(offered=1, scheduled=bool(row['scheduled_ns']), released=1,
                     admitted=admitted, rejected=not admitted, submission_attempted=submitted,
                     write_drained=bool(row['drained_ns']), completed=reason is None,
                     failed=reason is not None, timed_out=row['outcome']=='TimeoutError',
                     connection_failure=reason in ('CONNECTION_FAILURE', 'TIMEOUT_CONNECTING'),
                     deadline_measured=attained is not None, deadline_missed=attained is False,
                     completed_in_window=reason is None and completed_ns <= end_ns)
        counts.update(facts)
        services[request['service']].update(facts)
        if reason:
            reasons[reason] += 1
            service_reasons[request['service']][reason] += 1
    if timing or any(counts[key] != result[key] for key in ('offered', 'completed', 'failed')):
        raise ValueError('incomplete or inconsistent offered cohort')
    duration = result['end'] - result['start']
    if not 0 < duration <= 3600 or not 0 < payload <= 1048576:
        raise ValueError('invalid observation duration or payload')

    def rates(row):
        return dict(row, offered_payload_mbps=row['offered']*payload*8/duration/1e6,
                    admitted_payload_mbps=row['admitted']*payload*8/duration/1e6,
                    goodput_mbps=row['completed_in_window']*payload*8/duration/1e6)

    return dict(cohort=rates(counts), by_service={name: rates(row) for name, row in services.items()},
                failure_reasons=dict(reasons), failure_reasons_by_service=dict(service_reasons),
                application_processing_failures=None)


class Pressure:
    """Hysteresis over fresh observations from a single capacity scope."""
    def __init__(self):
        self.state = 'INSUFFICIENT_DATA'
        self.previous_capacity = None
        self.scope = None
        self.advice_latched = False
        self.last_ns = None
        self.streak = 0

    def invalidate(self):
        self.state, self.streak, self.previous_capacity = 'INSUFFICIENT_DATA', 0, None

    def fresh(self, sampled_ns, now_ns):
        return (type(sampled_ns) is int and 0 <= now_ns-sampled_ns <= 3*NANO
                and (self.last_ns is None or sampled_ns >= self.last_ns))

    def reduction(self, before, after, sampled_ns, *, now_ns):
        if (self.fresh(sampled_ns, now_ns) and sampled_ns != self.last_ns
                and type(before) in (int, float) and type(after) in (int, float)
                and self.previous_capacity == before and 0 < after <= .85*before):
            self.state, self.streak = 'CAPACITY_COLLAPSE', 0
            self.previous_capacity, self.last_ns = after, sampled_ns
            return self.state
        if self.fresh(sampled_ns, now_ns) and sampled_ns == self.last_ns:
            return self.state
        raise ValueError('capacity transition lacks a matching fresh baseline')

    def observe(self, capacity_kbit, utilization, backlog, sampled_ns, *, now_ns):
        if (not self.fresh(sampled_ns, now_ns)
                or type(backlog) is not int or backlog < 0
                or type(capacity_kbit) not in (int, float) or not 0 < capacity_kbit <= 1_000_000_000
                or type(utilization) not in (int, float) or not 0 <= utilization <= 2):
            self.invalidate()
            return self.state
        if sampled_ns == self.last_ns:
            return self.state
        reduced = self.previous_capacity is not None and capacity_kbit <= .85*self.previous_capacity
        self.previous_capacity, self.last_ns = capacity_kbit, sampled_ns
        congested = utilization >= .9 and backlog > 0
        healthy = utilization < .8 and backlog == 0
        if reduced:
            self.state, self.streak = 'CAPACITY_COLLAPSE', 0
        elif self.state in ('CAPACITY_COLLAPSE', 'OVERLOADED', 'RECOVERING'):
            if healthy:
                self.streak = self.streak+1 if self.state == 'RECOVERING' else 1
                self.state = 'NORMAL' if self.streak >= 2 else 'RECOVERING'
            elif self.state == 'RECOVERING':
                self.state, self.streak = 'OVERLOADED', 0
        elif congested:
            self.streak = self.streak+1 if self.state == 'APPROACHING_CAPACITY' else 1
            self.state = 'OVERLOADED' if self.streak >= 2 else 'APPROACHING_CAPACITY'
        else:
            self.state = 'NORMAL' if healthy else 'APPROACHING_CAPACITY'
            self.streak = 0
        return self.state


def advise(state, sampled_ns, concurrency, *, now_ns=None):
    """Return a cooperative recommendation. The caller owns its admission policy."""
    if type(concurrency) is not int or not 1 <= concurrency <= 65535:
        raise ValueError('concurrency must be an integer in [1, 65535]')
    now_ns = time.monotonic_ns() if now_ns is None else now_ns
    if (type(sampled_ns) is not int or not 0 <= now_ns-sampled_ns <= 3*NANO
            or state not in ('OVERLOADED', 'CAPACITY_COLLAPSE')):
        return dict(action='NO_INTERVENTION', recommended_concurrency=concurrency)
    return dict(action='RECOMMEND_BACKPRESSURE', recommended_concurrency=max(1, concurrency*3//4))


def configuration(snapshot):
    tc = snapshot['tc']
    queues = {}
    for direction in ('qdisc', 'receiver_qdisc'):
        queues[direction] = sorted(
            (dict(kind=row['kind'], parent=row.get('parent'), root=bool(row.get('root')),
                  options={key: value for key, value in row.get('options', {}).items()
                           if key not in ('direct_packets_stat',)})
             for row in tc[direction]), key=lambda row: (row['parent'] or '', row['kind']))
    classes = {row['handle']: {key: row[key] for key in ('parent', 'rate', 'ceil', 'prio', 'burst', 'cburst')
                              if key in row}
               for row in tc['class'] if row.get('class')=='htb'}
    filters = [dict(family=row['protocol'], preference=row['pref'], kind=row['kind'],
                    parent=row.get('parent'), chain=row.get('chain'), rule=row['options'])
               for row in tc['filter'] if row.get('options')]
    return dict(**queues, htb_classes_bytes_per_second=classes,
                filters=sorted(filters, key=lambda row: (row['preference'], row['family'])))


def diagnose(directory):
    def load(name, limit=8*1024*1024):
        return json.loads(read_bounded(directory/name, limit))
    result, settings = load('result.json'), load('workload.json')
    measurement = result.get('measurement', {})
    if measurement.get('state') not in ('VALID', 'FAILED_WORKLOAD', 'INCONCLUSIVE'):
        return dict(schema=1, state='INSUFFICIENT_DATA', infrastructure=measurement.get('state'),
                    action='NO_INTERVENTION', issues=measurement.get('issues'))
    report = accounting(directory, result, settings['payload_bytes'])
    samples = load('topology.json', 16*1024*1024)
    pressure = Pressure()
    windows = []
    for (first, last), rate in zip(zip(samples[1::2], samples[2::2]), result['observed_rate_windows'], strict=True):
        queue = next((row for row in last['tc']['qdisc'] if row.get('root')), {})
        backlog = queue.get('backlog')
        sampled_ns = last['end_ns']
        state = pressure.observe(first['configured_kbit'], rate['measured_to_configured_ratio'], backlog,
                                 sampled_ns, now_ns=sampled_ns)
        windows.append(dict(state=state, backlog_bytes=backlog, configured_kbit=first['configured_kbit'],
                            observed_egress_mbps=rate['sender_egress_mbps'],
                            saturation_supported=rate['saturation_supported'], sample_ns=sampled_ns))
    cause_evidence = dict(
        client_admission=dict(observed=report['failure_reasons'].get('CLIENT_INFLIGHT_LIMIT', 0),
                              interpretation='rejected before a socket operation'),
        application_admission=dict(observed=sum(report['failure_reasons'].get(key, 0) for key in
            ('APPLICATION_ADMISSION_REJECTED', 'APPLICATION_ADMISSION_DEFERRED'))),
        connection_pressure=dict(failures=report['cohort']['connection_failure'],
                                 interpretation='socket stage only'),
        local_egress_pressure=dict(saturated_windows=sum(row['saturation_supported'] is True for row in windows),
                                  interpretation='configured local limit and observed counters'),
        socket_send_queue='UNKNOWN',
        remote_service_saturation='UNKNOWN',
        per_request_loss_attribution='UNKNOWN')
    return dict(schema=1, mode=result['mode'], observation='RECORDED_LAB', **report,
                cause_evidence=cause_evidence,
                infrastructure=measurement['state'], pressure_windows=windows,
                kernel_configurations=[dict(label=row['label'], **configuration(row)) for row in samples],
                evidence=dict(controller_updates=result.get('slo_policy_updates'),
                              capacity_rebases=result.get('capacity_rebases'),
                              pacing=result.get('socket_pacing'),
                              kernel_policy_generation=samples[-1].get('kernel_policy_generation'),
                              qdisc_drops=result.get('qdisc_drop_delta'),
                              netem_drops=result.get('netem_drop_delta'),
                              sampled_retransmits=result.get('sampled_retransmit_segments'),
                              background_mbps=result.get('background_mbps'),
                              udp_mbps=result.get('udp_mbps'), fairness=result.get('jain_index'),
                              cpu_seconds=result.get('cpu_seconds'), p99_ms=result.get('p99_ms'),
                              generator=measurement.get('timing'),
                              peak_inflight=measurement.get('client', {}).get('peak_inflight'),
                              request_cap=measurement.get('client', {}).get('request_cap')),
                action='NO_INTERVENTION')



def compare_configuration(reference, candidate):
    before, after = (row.get('kernel_configurations') for row in (reference, candidate))
    if not before or not after or [row['label'] for row in before] != [row['label'] for row in after]:
        return dict(status='INSUFFICIENT_DATA', reason='recorded topology stages differ or are absent')
    differences = []
    for index, (old, new) in enumerate(zip(before, after)):
        fields = {key: dict(before=old[key], after=value)
                  for key, value in new.items() if value != old[key]}
        if fields:
            differences.append(dict(stage=index, label=new['label'], changed=fields))
    return dict(status='DIFFERENT' if differences else 'MATCHING_RECORDED_CONFIGURATION',
                differences=differences, reference_mode=reference['mode'])


def cooperative_poll(query, pressure, concurrency, *, now_ns=None):
    """Poll the existing read-only daemon API using the caller's IPC transport."""
    try:
        evidence = query('capacity_estimate')
        now_ns = time.monotonic_ns() if now_ns is None else now_ns
        # This adapter is local to the daemon's monotonic clock and owned lab.
        age = evidence.get('sample_age_seconds')
        if (evidence.get('scope') != 'owned_lab_sender' or not isinstance(age, (float, int))
                or not 0 <= age <= 3):
            raise ValueError('missing fresh owned egress evidence')
        scope = tuple(evidence['namespace_identity'])
        if len(scope) != 2:
            raise ValueError('missing namespace identity')
        if scope != pressure.scope:
            pressure.invalidate()
            pressure.last_ns, pressure.scope, pressure.advice_latched = None, scope, False
        sampled = evidence['sample_monotonic_ns']
        reduction = evidence.get('capacity_reductions', {}).get('egress')
        if reduction:
            state = pressure.reduction(reduction['before'], reduction['after'], sampled, now_ns=now_ns)
        else:
            egress = evidence['directions']['egress']
            if egress['capacity_source'] != 'qdisc_readback' or not egress['controllable']:
                raise ValueError('capacity is outside the controlled scope')
            state = pressure.observe(egress['capacity_kbit'], egress['utilization'], egress['backlog_bytes'],
                                     sampled, now_ns=now_ns)
    except (OSError, EOFError, KeyError, RuntimeError, ValueError, TypeError):
        pressure.invalidate()
        return dict(state='INSUFFICIENT_DATA', **advise('INSUFFICIENT_DATA', None, concurrency))
    if state == 'NORMAL':
        pressure.advice_latched = False
    action = advise(state if not pressure.advice_latched else 'HOLD', sampled, concurrency, now_ns=now_ns)
    pressure.advice_latched |= action['action'] == 'RECOMMEND_BACKPRESSURE'
    return dict(state=state, **action)


def guard(summary, baseline, candidate):
    """Review failure-aware paired evidence without authorizing a mutation."""
    from research_stats import compare
    if (not summary['manifest'].get('integrity') or baseline == candidate
            or not {baseline, candidate} <= set(summary['manifest']['modes'])):
        raise ValueError('guard requires two distinct arms of one integrity-enabled matrix')
    comparison = compare(summary, summary, baseline, candidate)
    eligible = (comparison['outcome'] != 'INVALID EXPERIMENT'
                and comparison['complete_paired_blocks'])
    regressions = []
    for item in comparison['findings']:
        interval = item['simultaneous_median_ci']
        sufficient = (not item['missing_data'] and not item['censored_p99_repetitions']
                      and (item['metric'] != 'p99_ms' or not comparison['insufficient_data_blocks']))
        if eligible and sufficient and interval and interval['low'] > item['threshold']:
            regressions.append(item['metric'])
    return dict(schema=1, action='KEEP_BASELINE' if regressions else 'NO_INTERVENTION',
                observed_regressions=regressions, comparison=comparison,
                candidate_adaptive_updates=sum(row.get('slo_policy_updates') or 0
                                               for row in summary['runs'] if row['mode']==candidate),
                authorization='ADVISORY_ONLY')
