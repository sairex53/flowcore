"""Bounded, monotonic incident evidence and deterministic offline evaluation."""
from collections import deque
import json
import math
import re
import statistics
import uuid

from service_config import keys, unique

NS = 10**9
WINDOW = 4*NS
CONTEXT = 4*WINDOW
MAX_BUNDLE = 768*1024
MIN_TAIL = 100
STAGES = ('dns', 'connect', 'tls', 'processing', 'network_wait', 'queueing', 'response_transfer')
FIELDS = {kind: set(fields.split()) for kind, fields in {
    'window': 'end_ns traffic_class attempts successes failures timeouts deadlines deadline_misses p99_us target_us throughput drops retransmits segments quality event_evictions request_evictions',
    'queue': 'interface direction traffic_class backlog drops throughput demand capacity saturated owned',
    'tcp': 'interface rtt_us min_rtt_us rttvar_us retransmits segments receiver_limited path_pressure coverage',
    'request': 'traffic_class request_ref start_ns outcome duration_us deadline_us socket_ref association cgroup_id netns_id interface interface_association observed_class sample_ns rtt_us min_rtt_us transport ' + ' '.join(STAGES),
    'interface': 'interface change up carrier',
    'policy': 'policy capacity generation rate_critical ceil_critical rate_interactive ceil_interactive rate_standard ceil_standard rate_background ceil_background',
    'controller': 'result action strategy generation changed comparable degraded post_start_ns post_end_ns',
    'resource': 'cpu_pct memory_bytes cpu_stall_pct memory_stall_pct io_stall_pct source',
    'mptcp_path': 'transport degraded error source path_id up',
    'fault': 'action status capacity link_up scheduled_ns',
    'quality': 'reason',
}.items()}
REQUIRED = {'window': {'end_ns'}, 'queue': {'interface','direction'}, 'request': {'start_ns','outcome','duration_us'}}
STRINGS = set('traffic_class interface direction quality interface_association observed_class request_ref outcome socket_ref association transport change policy result strategy source error action status reason'.split())
BOOLEANS = set('saturated owned receiver_limited path_pressure coverage up carrier changed comparable degraded link_up'.split())
INTEGERS = set('end_ns attempts successes failures timeouts deadlines deadline_misses drops retransmits segments backlog start_ns sample_ns scheduled_ns post_start_ns post_end_ns cgroup_id netns_id event_evictions request_evictions generation memory_bytes path_id'.split())
TOKEN = re.compile(r'[a-zA-Z0-9_.:-]{1,80}', re.ASCII)
NAME = re.compile(r'[a-zA-Z0-9_-]{1,32}', re.ASCII)
RUN = re.compile(r'[0-9a-f]{32}', re.ASCII)


def metrics(row):
    return row['data'] if row else {}


def timing_fields(value, duration_us):
    if not isinstance(value, dict) or not value or value.keys() - set(STAGES):
        raise ValueError('expected named, non-overlapping request stages')
    if any(type(v) not in (int,float) or not 0 <= v <= duration_us for v in value.values()) or sum(value.values()) > duration_us:
        raise ValueError('stage durations exceed the reported request duration')
    return value


def signals(now, prior):
    out = []
    tail, base = now.get('p99_us'), prior.get('p99_us')
    if (min(now.get('successes',0), prior.get('successes',0)) >= MIN_TAIL
            and tail is not None and base is not None and tail-base >= max(1000,.3*base)):
        out.append('P99_REGRESSION')
    target = now.get('target_us')
    if now.get('successes',0) >= MIN_TAIL and tail is not None and target and tail > target:
        out.append('SLO_P99_EXCEEDED')
    for field, denominator, label in (('failures','attempts','FAILURES_INCREASED'),
                                      ('deadline_misses','deadlines','DEADLINE_MISSES_INCREASED')):
        n, p = now.get(denominator,0), prior.get(denominator,0)
        a, b = now.get(field,0), prior.get(field,0)
        if min(n,p) >= 10 and a >= 3 and a/n-b/p >= .10:
            out.append(label)
    rate, old = now.get('throughput'), prior.get('throughput')
    if old is not None and old >= 100 and rate is not None and rate < .7*old:
        out.append('THROUGHPUT_REDUCTION')
    drops, old_drops = now.get('drops'), prior.get('drops')
    if old_drops is not None and drops is not None and drops >= max(5, old_drops*3):
        out.append('DROP_BURST')
    seg, old_seg = now.get('segments'), prior.get('segments')
    retry, old_retry = now.get('retransmits'), prior.get('retransmits')
    if seg and seg >= 20 and old_seg and old_seg >= 20 and retry is not None and old_retry is not None:
        if retry >= 3 and retry/seg-old_retry/old_seg >= .05:
            out.append('RETRANSMISSION_INCREASE')
    return out


def report(events, anchor, baseline):
    by_seq = {row['seq']: row for row in events}
    target = by_seq[anchor]
    prior = by_seq.get(baseline)
    data = metrics(target)
    end = data['end_ns']
    begin = end-WINDOW
    service = target['service']
    kinds = {kind: [] for kind in FIELDS}
    for row in events:
        if (row['scope'] == target['scope'] and begin <= row['time_ns'] < end
                and row['service'] in (None,service) and row['seq'] <= anchor):
            kinds[row['kind']].append(row)
    queues = kinds['queue']
    directions = {metrics(row)['direction'] for row in queues}
    pressure = []
    saturated = []
    scoped_pressure = []
    root = [row for row in queues if metrics(row).get('traffic_class') is None]
    for direction in sorted(directions):
        roots = [row for row in root if metrics(row)['direction'] == direction]
        same_class = [row for row in queues if metrics(row)['direction'] == direction
                      and data.get('traffic_class') is not None
                      and metrics(row).get('traffic_class') == data['traffic_class']]
        busy = [row for row in (same_class or roots) if (metrics(row).get('backlog') or 0) >= 1500]
        if len(busy) >= 2:
            pressure.extend(busy)
            if same_class and direction == 'egress':
                scoped_pressure.extend(busy)
            saturated.extend(row for row in roots if metrics(row).get('saturated') and metrics(row).get('owned'))
    quiet = len(root) >= 2 and all(metrics(row).get('backlog') == 0 and metrics(row).get('drops') == 0 for row in root)
    transport = [row for row in kinds['tcp'] if metrics(row).get('coverage')]
    path = [row for row in transport if metrics(row).get('path_pressure')]
    receiver = [row for row in transport if metrics(row).get('receiver_limited')]
    requests = kinds['request']
    exact = [row for row in requests if metrics(row).get('association') == 'fd_and_sample']
    timed = [row for row in requests if metrics(row).get('processing') is not None]
    processing = [row for row in timed if metrics(row)['processing'] >= max(1000,.65*metrics(row)['duration_us'])]
    matched_paths = [row for row in exact if (m := metrics(row)).get('rtt_us') is not None
                     and m.get('min_rtt_us') and m['rtt_us'] >= max(2*m['min_rtt_us'],m['min_rtt_us']+2000)]
    controller = [row for row in events if row['kind'] == 'controller' and row['scope'] == target['scope']
                  and row['service'] in (None,service) and row['seq'] <= anchor]
    changes = [row for row in controller if metrics(row).get('changed') and begin-WINDOW <= row['time_ns'] < end]
    degraded = [row for row in controller if (m := metrics(row)).get('comparable') and m.get('degraded')
                and ((begin <= row['time_ns'] < end) or
                     (m.get('post_start_ns') is not None and m.get('post_end_ns') is not None
                      and m['post_start_ns'] < end and m['post_end_ns'] > begin))]
    invalid = bool(kinds['quality'] or data.get('quality') == 'partial')
    symptoms = signals(data, metrics(prior))
    candidates = []

    def candidate(name, supported, rows, evidence, against, action):
        candidates.append(dict(name=name, strength='supported' if supported and not invalid else 'contextual',
                               evidence=[row['seq'] for row in rows[:12]], evidence_for=evidence,
                               evidence_against=against, safe_action=action))

    if pressure:
        candidate('QUEUE_SATURATION', bool(scoped_pressure), scoped_pressure or pressure,
                  'persistent backlog in the configured service class' if scoped_pressure else 'persistent interface queue backlog',
                  ['class membership of the observed packets is not linked to individual requests'],
                  'Review class counters and compare with reduced load in the lab')
    if len(saturated) >= 2:
        candidate('INSUFFICIENT_LOCAL_BANDWIDTH', False, saturated, 'owned shaper near configured capacity with queue pressure',
                  ['configured rate is not physical capacity', 'demand may have changed'],
                  'Compare with a verified alternate shaper rate in the lab')
    if path or matched_paths:
        candidate('NETWORK_PATH_DEGRADATION', len({metrics(row).get('sample_ns') for row in matched_paths}-{None}) >= 2 and quiet, matched_paths+path,
                  'RTT inflation or retransmissions were sampled',
                  ['local queue pressure is an alternative'] if pressure else ['samples do not locate loss or delay'],
                  'Inspect route changes and repeat a paired path probe')
    if receiver:
        candidate('RECEIVER_LIMITATIONS', False, receiver, 'active sampled TCP receive-window pressure',
                  ['interface evidence may be from another service', 'socket settings and scheduling are alternatives'],
                  'Inspect receive queues and receiver application scheduling')
    if len(processing) >= 3:
        candidate('APPLICATION_PROCESSING_DELAY', len(processing)*2 >= len(timed), processing,
                  'processing dominated retained application timing reports',
                  ['opt-in timing samples may be unrepresentative', 'reported processing can include local scheduling'],
                  'Compare server spans and CPU scheduling during the incident')
    if len(changes) >= 2 or (changes and degraded):
        if degraded and service is not None and any(s in symptoms for s in
                ('P99_REGRESSION','FAILURES_INCREASED','DEADLINE_MISSES_INCREASED')):
            symptoms.append('POSSIBLE_CONTROLLER_REGRESSION')
        candidate('CONTROLLER_RECONFIGURATION', False, changes+degraded,
                  'verified changes or a degraded controller evaluation overlapped this window',
                  ['controller may be responding to the same workload change'],
                  'Compare against a frozen policy with identical offered workload')
    if invalid:
        candidate('TELEMETRY_ARTIFACT', False, kinds['quality'], 'collector coverage was partial or missing',
                  ['an incident can still be real during telemetry loss'], 'Verify sample completeness and repeat measurements')
    candidates.sort(key=lambda row: row['strength'] != 'supported')
    missing = []
    if not prior:
        missing.append('no adjacent comparison window')
    if service and data.get('successes',0) < MIN_TAIL:
        missing.append('fewer than 100 successes for P99')
    if not queues:
        missing.append('no eligible local queue samples')
    if not exact and service:
        missing.append('no overlapping verified socket sample')
    if not timed and service:
        missing.append('no explicit application processing timings')
    if invalid:
        missing.append('some telemetry is partial or missing')
    demand = [row['seq'] for row in queues if metrics(row).get('demand') is not None]
    graph = [
        dict(source='workload_demand', target='queue_pressure',
             relationship='queue_accounted_estimate' if demand else 'hypothesis', evidence=demand[:4]),
        dict(source='queue_pressure', target='transport_behavior', relationship='co_observed' if pressure and transport else 'missing',
             evidence=[row['seq'] for row in pressure[:2]+transport[:2]]),
        dict(source='transport_behavior', target='application_latency', relationship='same_socket_observed' if exact else 'missing',
             evidence=[row['seq'] for row in exact[:4]]),
        dict(source='application_latency', target='slo_violation', relationship='measured_threshold' if 'SLO_P99_EXCEEDED' in symptoms else 'not_established',
             evidence=[anchor]),
    ]
    timeline_rows = {row['seq']: row for kind in ('fault','mptcp_path','interface','policy','controller','resource','quality')
                     for row in kinds[kind]}
    timeline_rows.update((row['seq'],row) for row in changes+degraded)
    timeline = sorted(timeline_rows.values(),key=lambda row:(row['time_ns'],row['seq']))[-24:]
    ranks = [c for c in candidates if c['name'] != 'TELEMETRY_ARTIFACT']
    ranking = 'UNRESOLVED' if not ranks else 'AMBIGUOUS' if len(ranks)>1 and ranks[0]['strength']==ranks[1]['strength'] else 'LEADING_CANDIDATE'
    stages = {stage: dict(samples=len(values), median_us=statistics.median(values)) for stage in STAGES
              if (values := [metrics(row)[stage] for row in requests if stage in metrics(row)])}
    return dict(schema=1, service=service, scope=target['scope'], window_start_ns=begin, window_end_ns=end,
                observed_at_ns=target['time_ns'], measurements=data, baseline=metrics(prior),
                signals=symptoms, candidates=candidates, graph=graph, missing_evidence=missing,
                assessment='CANDIDATES_ONLY' if symptoms and any(c['name'] != 'TELEMETRY_ARTIFACT' for c in candidates) else 'INSUFFICIENT_EVIDENCE' if symptoms or missing else 'NO_REGRESSION_DETECTED',
                causal_effect_established=False, request_details=len(requests), socket_matches=len(exact),
                reported_stages=stages, ranking=ranking, timeline=timeline)


class Recorder:
    def __init__(self):
        self.run = uuid.uuid4().hex
        self.events = deque(maxlen=383)
        self.requests = deque(maxlen=128)
        self.incidents = deque(maxlen=16)
        self.prior = {}
        self.latest = {}
        self.seq = 0
        self.event_evictions = self.request_evictions = self.incident_evictions = 0
        self.scope = 0

    def reset_scope(self):
        self.scope += 1
        self.prior.clear()
        self.latest.clear()

    def record(self, kind, stamp, service=None, **detail):
        self.seq += 1
        row = dict(schema=1, seq=self.seq, scope=self.scope, time_ns=stamp, service=service, kind=kind, data=detail)
        if kind == 'request':
            self.request_evictions += len(self.requests)==self.requests.maxlen
            self.requests.append(row)
        else:
            self.event_evictions += len(self.events)==self.events.maxlen
            self.events.append(row)
        return row

    def bundle(self, row, prior):
        end = row['data']['end_ns']
        selected = [event for ring in (self.events,self.requests) for event in ring if
                    event['scope'] == row['scope'] and event['service'] in (None,row['service'])
                    and (event is row or (event['seq'] <= row['seq'] and (end-CONTEXT <= event['time_ns'] < end or
                          (event['kind']=='controller' and end <= event['time_ns'] <= row['time_ns']))))]
        if prior and prior not in selected:
            selected.append(prior)
        return dict(schema=1, run=self.run, id=f"{self.run}-{row['seq']}", anchor=row['seq'],
                    baseline=prior['seq'] if prior else None, events=sorted(selected,key=lambda event:event['seq']))

    def window(self, stamp, end_ns, service=None, **detail):
        row = self.record('window', stamp, service, end_ns=end_ns, event_evictions=self.event_evictions,
                          request_evictions=self.request_evictions, **detail)
        prior, active, clear = self.prior.get(service, (None,False,0))
        if (not prior or prior['data']['end_ns'] != end_ns-WINDOW
                or any(prior['data'].get(key) != detail.get(key) for key in ('traffic_class','target_us'))):
            prior, active, clear = None, False, 0
        triggered = signals(row['data'], metrics(prior))
        if triggered and not active:
            self.incident_evictions += len(self.incidents)==self.incidents.maxlen
            self.incidents.append(self.bundle(row,prior))
        clear = 0 if triggered else min(2,clear+1)
        self.prior[service] = row, (bool(triggered) or active) and clear < 2, clear
        self.latest[service] = row, prior
        return row

    def diagnose(self, service):
        if service not in self.latest:
            return dict(schema=1, service=service, assessment='INSUFFICIENT_EVIDENCE',
                        missing_evidence=['no completed diagnostic window'], causal_effect_established=False)
        return replay(self.bundle(*self.latest[service]))

    def find(self, identifier):
        for bundle in self.incidents:
            if bundle['id'] == identifier:
                return bundle
        raise ValueError('unknown or expired incident')


def decode_bundle(raw):
    if len(raw) > MAX_BUNDLE:
        raise ValueError('incident bundle exceeds size limit')

    def reject_constant(_):
        raise ValueError('invalid JSON constant')

    try:
        bundle = json.loads(raw, object_pairs_hook=unique, parse_constant=reject_constant)
    except (RecursionError, UnicodeError) as error:
        raise ValueError('invalid incident JSON') from error
    keys(bundle, ('schema','run','id','anchor','baseline','events'))
    run, anchor, baseline, events = (bundle[key] for key in ('run','anchor','baseline','events'))
    if (type(bundle['schema']) is not int or bundle['schema'] != 1 or not isinstance(run,str) or not RUN.fullmatch(run)
            or type(anchor) is not int or not 0 < anchor < 2**63 or bundle['id'] != f'{run}-{anchor}'
            or (baseline is not None and (type(baseline) is not int or not 0 < baseline < anchor))
            or not isinstance(events,list) or not 1 <= len(events) <= 512):
        raise ValueError('invalid incident header')
    previous = 0
    indexed = {}
    for row in events:
        keys(row, ('schema','seq','scope','time_ns','service','kind','data'))
        kind, data = row['kind'], row['data']
        required = REQUIRED.get(kind,set()) if isinstance(kind,str) else set()
        if (type(row['schema']) is not int or row['schema'] != 1
                or any(type(row[key]) is not int or not 0 <= row[key] < 2**63 for key in ('seq','scope','time_ns'))
                or not previous < row['seq'] <= anchor
                or (row['service'] is not None and (not isinstance(row['service'],str) or not NAME.fullmatch(row['service'])))
                or not isinstance(kind,str) or kind not in FIELDS or not isinstance(data,dict)
                or data.keys()-FIELDS[kind] or required-data.keys()):
            raise ValueError('invalid incident event')
        for key, value in data.items():
            if value is None and key not in required:
                continue
            if key in STRINGS:
                valid = isinstance(value,str) and TOKEN.fullmatch(value)
            elif key in BOOLEANS:
                valid = type(value) is bool
            else:
                valid = (type(value) in ((int,) if key in INTEGERS else (int,float))
                         and 0 <= value < 2**64 and math.isfinite(value))
            if not valid:
                raise ValueError(f'invalid incident event field {key}')
        if kind == 'window':
            numeric = ('attempts','successes','failures','timeouts','deadlines','deadline_misses')
            if (not WINDOW <= data['end_ns'] <= row['time_ns']
                    or any(key in data and data[key] is None for key in numeric)
                    or data.get('failures',0) > data.get('attempts',0)
                    or data.get('deadline_misses',0) > data.get('deadlines',0)):
                raise ValueError('invalid comparison window')
        if kind == 'request':
            if not data['start_ns'] <= row['time_ns'] or data['outcome'] not in ('ok','timeout','failure','cancelled'):
                raise ValueError('invalid request lifecycle')
            if data.get('sample_ns') is not None and not data['start_ns'] <= data['sample_ns'] <= row['time_ns']:
                raise ValueError('socket observation falls outside request')
            stages = {stage:data[stage] for stage in STAGES if stage in data}
            if stages:
                timing_fields(stages, data['duration_us'])
        indexed[row['seq']] = row
        previous = row['seq']
    last = indexed.get(anchor)
    first = indexed.get(baseline)
    if not last or last['kind'] != 'window' or (baseline is not None and
            (not first or first['kind'] != 'window' or first['scope'] != last['scope']
             or first['service'] != last['service'] or first['data']['end_ns'] != last['data']['end_ns']-WINDOW)):
        raise ValueError('invalid incident comparison window')
    return bundle


def replay(bundle):
    return report(bundle['events'], bundle['anchor'], bundle['baseline'])
