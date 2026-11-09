"""Versioned and bounded Research Lab input format."""
import copy
import json
import re
from pathlib import Path

import ecn_policy
import deadline_runtime
from strategy_registry import CLASS, SOCKET, guards, names
from service_config import keys, number, profile, unique

LEGACY_MODES = ('linux', 'fifo', 'fq_codel', 'cake', 'static', 'threshold', 'slo')
STATIC_MODES = tuple(f'static{pct}' for pct in range(20, 41))
FQ_MODES = ('tcp-fq', 'pacing-static', 'pacing-queue', 'pacing-slo', 'pacing-lowat',
            'tcp-cubic', 'tcp-reno', 'tcp-bbr')
PACING_HTB_MODES = ('pacing-htb-fq', 'pacing-htb-static', 'pacing-htb-queue', 'pacing-htb-slo', 'pacing-guarded')
PACING_MODES = (*FQ_MODES, 'pacing-hybrid', *PACING_HTB_MODES)
MODES = (*LEGACY_MODES, 'linux-default', 'htb-fq_codel', *STATIC_MODES, 'slo-shadow', 'slo-single', 'slo-long', 'static-step', 'queue-aware', 'slo-aware', 'hybrid', 'predictive', 'predictive-shadow', 'optimizer', 'online-select', *PACING_MODES, *ecn_policy.MODES, *deadline_runtime.MODES)
V2_MODES = tuple(mode for mode in MODES if mode != 'linux')
CLASSIFIED = frozenset((*STATIC_MODES, 'static', 'threshold', 'slo', 'htb-fq_codel', 'slo-shadow', 'slo-single', 'slo-long', 'static-step', 'queue-aware', 'slo-aware', 'hybrid', 'predictive', 'predictive-shadow', 'optimizer', 'online-select', 'pacing-hybrid', *PACING_HTB_MODES, *ecn_policy.CLASSIFIED, *deadline_runtime.MODES[1:]))
GATE_METRICS = ('p99_ms', 'total_mbps', 'failure_ratio', 'deadline_attainment')
DEFAULT_LIMITS = dict(p99_ms=.10, total_mbps=.10, jain_index=.02,
                      cpu_seconds=.15, adjustments_per_minute=2, event_loss=5)
NETWORK_FIELDS = ('bandwidth_kbit', 'base_rtt_ms', 'jitter_ms', 'loss_pct', 'reorder_pct')
FAULTS = (*NETWORK_FIELDS, 'link_up')
OPTIONAL_NETWORK_FIELDS = ('reverse_bandwidth_kbit', 'loss_correlation_pct', 'duplicate_pct')
EVENT_EFFECTS = (*NETWORK_FIELDS, *OPTIONAL_NETWORK_FIELDS, 'link_up')
STAGES = ('warmup', 'baseline', 'congestion', 'fault_injection', 'recovery', 'post_recovery_validation')
_ID = re.compile(r'[a-z][a-z0-9_-]{0,31}\Z')


def net_profile(network):
    return profile(dict(bandwidth_kbit=network['bandwidth_kbit'], delay_ms=network['base_rtt_ms'],
                        jitter_ms=network['jitter_ms'], loss_pct=network['loss_pct'],
                        reorder_pct=network['reorder_pct']))


def validate(value):
    if not isinstance(value, dict):
        raise ValueError('manifest must be an object')
    version = value.get('version')
    v2 = version in (2, 3)
    v3 = version == 3
    fields = ('version', 'topology', 'network', 'workload', 'duration_seconds', 'repetitions',
              'seed', 'modes', 'static_critical_pct', 'controller', 'faults', 'regression')
    optional = tuple(key for key in ('ecn', 'deadline', 'online', 'socket_shadows') if key in value)
    keys(value, fields + (('gates',) if v2 else ()) + (('stages', 'events') if v3 else ()) + optional)
    number(version, 1, 3, True)
    if value['topology'] != 'veth-pair':
        raise ValueError('only the owned veth-pair topology is supported')
    number(value['duration_seconds'], 4 if not v3 else 6, 180, True)
    number(value['repetitions'], 1, 30, True)
    number(value['seed'], 0, 2**32-1, True)
    number(value['static_critical_pct'], 20, 40, True)
    modes = value['modes']
    if not isinstance(modes, list) or not modes or any(not isinstance(mode, str) or mode not in (V2_MODES if v2 else LEGACY_MODES) for mode in modes) or len(set(modes)) != len(modes):
        raise ValueError('modes must be unique supported comparisons')
    if 'socket_shadows' in value:
        names(value['socket_shadows'], SOCKET)
        if not v2 or not set(modes) & {*PACING_MODES,*ecn_policy.MODES,*deadline_runtime.MODES}:
            raise ValueError('socket shadows require instrumented Research Lab socket profiles')
    if ('online-select' in modes) != ('online' in value):
        raise ValueError('online-select requires its versioned experiment profile')
    if 'online' in value:
        online = value['online']
        keys(online, ('version','candidate','shadows','seconds','guards'))
        number(online['version'], 1, 1, True)
        number(online['seconds'], 8, min(45,value['duration_seconds']), True)
        if version != 2 or online['candidate'] not in CLASS[1:]:
            raise ValueError('online canary requires a v2 manifest and a class candidate')
        if set(modes) - {*CLASS, 'online-select'}:
            raise ValueError('online trials require comparable classified class modes')
        names(online['shadows'], CLASS)
        guards(online['guards'])
    ecn_modes = set(modes) & set(ecn_policy.MODES)
    if bool(ecn_modes) != ('ecn' in value) or (ecn_modes and ecn_modes != set(modes)):
        raise ValueError('ECN comparisons require a versioned profile and exclusively ECN modes')
    if ecn_modes:
        ecn_policy.validate(value['ecn'])
    deadline_modes = set(modes) & set(deadline_runtime.MODES)
    if bool(deadline_modes) != ('deadline' in value) or (deadline_modes and deadline_modes != set(modes)):
        raise ValueError('deadline comparisons require an exclusive versioned deadline profile')
    if deadline_modes:
        if not v2:
            raise ValueError('deadline experiments require a version 2 or 3 manifest')
        deadline_runtime.profile(value['deadline'])
    network = value['network']
    keys(network, (*NETWORK_FIELDS, *(OPTIONAL_NETWORK_FIELDS if v3 else ())))
    net_profile(network)
    if set(modes) & {'integrated', 'pacing-guarded'} and (version != 2 or network['bandwidth_kbit'] > 12000):
        raise ValueError('integrated mode requires a v2 owned HTB profile at up to 12000 kbit/s')
    if v3:
        number(network.get('reverse_bandwidth_kbit', network['bandwidth_kbit']), 100, 1000000, True)
        number(network.get('loss_correlation_pct', 0), 0, 100)
        number(network.get('duplicate_pct', 0), 0, 100)
        if network.get('loss_correlation_pct',0) and not network['loss_pct']:
            raise ValueError('correlated loss requires a nonzero loss probability')
    workload = value['workload']
    fields = ('phases', 'payload_bytes', 'max_inflight', 'priority_weights', 'bulk_bytes_per_stream')
    keys(workload, fields + (('warmup_seconds', 'deadline_ms', 'critical_services',
                             'connection_reuse', 'background_kbit_per_stream') if v2 else ()) +
         (('address_family', 'udp_kbit_per_stream') if v3 else ()))
    if v3 and workload['address_family'] not in ('ipv4','ipv6'):
        raise ValueError('version 3 address_family must be ipv4 or ipv6')
    if v3:
        number(workload['udp_kbit_per_stream'], 0, 1000000, True)
    if v2:
        number(workload['warmup_seconds'], 0, 30, True)
        number(workload['critical_services'], 1, 4, True)
        number(workload['background_kbit_per_stream'], 0, 1000000, True)
        if workload['connection_reuse'] != 'none':
            raise ValueError('version 2 supports a new connection per request')
        if workload['deadline_ms'] is not None:
            number(workload['deadline_ms'], .001, 5000)
        if deadline_modes and (workload['deadline_ms'] is None or workload['deadline_ms'] <= value['deadline']['tight_deadline_ms']):
            raise ValueError('deadline experiments require a regular budget larger than the tight budget')
        if set(modes) & {'integrated', 'pacing-guarded'} and workload['deadline_ms'] is None:
            raise ValueError('integrated mode requires application deadlines')
        if workload['warmup_seconds'] and workload['bulk_bytes_per_stream']:
            raise ValueError('finite background transfers require zero warmup')
        gates = value['gates']
        keys(gates, ('baseline', 'min_pairs', 'margins'))
        if gates['baseline'] is not None and gates['baseline'] not in modes:
            raise ValueError('gate baseline must be in the matrix')
        number(gates['min_pairs'], 10, 30, True)
        metrics = GATE_METRICS if workload['deadline_ms'] is not None else GATE_METRICS[:-1]
        keys(gates['margins'], metrics)
        for margin in gates['margins'].values():
            number(margin, 0, 1)
        if 'linux-default' in modes and (modes != ['linux-default'] or value['faults']
                or any(network[key] for key in ('base_rtt_ms','jitter_ms','loss_pct','reorder_pct'))
                or (v3 and (network.get('loss_correlation_pct',0) or network.get('duplicate_pct',0)))):
            raise ValueError('Linux default is a separate unshaped, unimpaired matrix')
    number(workload['payload_bytes'], 1, 65536, True)
    number(workload['max_inflight'], 1, 512, True)
    number(workload['bulk_bytes_per_stream'], 0, 2**34, True)
    weights = workload['priority_weights']
    keys(weights, ('critical', 'interactive', 'standard'))
    for weight in weights.values():
        number(weight, 0, 100, True)
    if sum(weights.values()) != 100:
        raise ValueError('priority weights must total 100')
    phases = workload['phases']
    if not isinstance(phases, list) or not 1 <= len(phases) <= 16:
        raise ValueError('1–16 workload phases required')
    total = 0
    for phase in phases:
        keys(phase, ('seconds', 'requests_per_second', 'background_streams') +
             (('udp_streams',) if v3 else ()))
        number(phase['seconds'], 1, 180, True)
        number(phase['requests_per_second'], 0, 500, True)
        number(phase['background_streams'], 0, 32, True)
        if v3:
            number(phase['udp_streams'], 0, 32, True)
        total += phase['seconds']
    if set(modes) & {'integrated', 'pacing-guarded'} and not any(phase['background_streams'] for phase in phases):
        raise ValueError('integrated mode requires bulk traffic')
    if v3 and any(phase['udp_streams'] for phase in phases) and not workload['udp_kbit_per_stream']:
        raise ValueError('UDP background streams require a positive pacing rate')
    if v2 and not any(phase['requests_per_second'] for phase in phases):
        raise ValueError('version 2 requires an offered request cohort')
    if total != value['duration_seconds']:
        raise ValueError('workload phase durations must sum to experiment duration')
    controller = value['controller']
    keys(controller, ('target_p99_ms', 'window_seconds', 'minimum_samples', 'bpf'))
    number(controller['target_p99_ms'], .001, 600000)
    if set(modes) & {'integrated', 'pacing-guarded'} and controller['target_p99_ms'] > 30000:
        raise ValueError('integrated controller target exceeds the native socket planner range')
    number(controller['window_seconds'], 2, 30, True)
    if 'online' in value and (controller['window_seconds'] > 8 or
                              value['online']['seconds'] < 3*controller['window_seconds']+2):
        raise ValueError('online lease must fit baseline, wash-in and candidate windows')
    number(controller['minimum_samples'], 1, 10000, True)
    if controller['bpf'] not in ('auto', 'off'):
        raise ValueError('bpf must be auto or off')
    if deadline_modes and not any(phase['background_streams'] for phase in phases):
        raise ValueError('deadline experiments require background flows')
    faults = value['faults']
    if not isinstance(faults, list) or len(faults) > 32:
        raise ValueError('at most 32 legacy network transitions')
    if v3 and faults:
        raise ValueError('version 3 uses duration-bounded events instead of legacy faults')
    if 'static-step' in modes and (faults or value['duration_seconds'] < 6):
        raise ValueError('static-step requires a fault-free experiment of at least six seconds')
    previous = 0
    current = dict(network)
    for fault in faults:
        if not isinstance(fault, dict) or not {'at_seconds'} < set(fault) or set(fault) - {'at_seconds', *NETWORK_FIELDS, 'link_up'}:
            raise ValueError('invalid network transition')
        when = fault['at_seconds']
        number(when, .1, value['duration_seconds']-.1)
        if when <= previous:
            raise ValueError('transitions must have increasing timestamps')
        previous = when
        if 'link_up' in fault and type(fault['link_up']) is not bool:
            raise ValueError('link_up must be boolean')
        current.update({key: fault[key] for key in NETWORK_FIELDS if key in fault})
        net_profile(current)
        if set(modes) & {'integrated', 'pacing-guarded'} and current['bandwidth_kbit'] > network['bandwidth_kbit']:
            raise ValueError('integrated faults cannot exceed the admitted initial capacity')
    if v3:
        stages = value['stages']
        if not isinstance(stages, list) or len(stages) != len(STAGES):
            raise ValueError('version 3 requires all six ordered scenario stages')
        stage_names = []
        stage_total = 0
        for stage in stages:
            keys(stage, ('name', 'seconds'))
            if stage['name'] not in STAGES or stage_names and STAGES.index(stage['name']) <= STAGES.index(stage_names[-1]):
                raise ValueError('scenario stages must use the required order')
            stage_names.append(stage['name'])
            number(stage['seconds'], 1, 180, True)
            stage_total += stage['seconds']
        if tuple(stage_names) != STAGES or stage_total != value['duration_seconds']:
            raise ValueError('scenario stages must partition the complete experiment')
        events = value['events']
        if not isinstance(events, list) or not 1 <= len(events) <= 64:
            raise ValueError('version 3 requires 1–64 scheduled fault events')
        ids = set()
        for event in events:
            keys(event, ('id', 'at_seconds', 'duration_seconds', 'effects'))
            if not isinstance(event['id'], str) or not _ID.fullmatch(event['id']) or event['id'] in ids:
                raise ValueError('event IDs must be unique lowercase identifiers')
            ids.add(event['id'])
            when, duration = event['at_seconds'], event['duration_seconds']
            number(when, .1, value['duration_seconds']-.2)
            number(duration, .1, value['duration_seconds']-when)
            effects = event['effects']
            if not isinstance(effects, dict) or not effects or set(effects)-set(EVENT_EFFECTS):
                raise ValueError('event effects are empty or unsupported')
            for key, item in effects.items():
                if key in ('bandwidth_kbit', 'reverse_bandwidth_kbit'):
                    number(item, 100, 1000000, True)
                elif key in ('base_rtt_ms', 'jitter_ms'):
                    number(item, 0, 5000)
                elif key in ('loss_pct', 'loss_correlation_pct', 'reorder_pct', 'duplicate_pct'):
                    number(item, 0, 100)
                elif key == 'link_up' and type(item) is not bool:
                    raise ValueError('link_up must be boolean')
            if effects.get('loss_correlation_pct',0) and not effects.get('loss_pct',0):
                raise ValueError('correlated loss requires a nonzero event loss probability')
            if network.get('loss_correlation_pct',0) and effects.get('loss_pct',0):
                raise ValueError('event loss overlaps a correlated base-loss model')
            profile(dict(bandwidth_kbit=effects.get('bandwidth_kbit', network['bandwidth_kbit']),
                         delay_ms=max(effects.get('base_rtt_ms',network['base_rtt_ms']),network['base_rtt_ms']),
                         jitter_ms=max(effects.get('jitter_ms',network['jitter_ms']),network['jitter_ms']),
                         loss_pct=effects.get('loss_pct',network['loss_pct']),
                         reorder_pct=effects.get('reorder_pct',network['reorder_pct'])))
        points = sorted(((event['at_seconds'], event['at_seconds']+event['duration_seconds'], event)
                         for event in events), key=lambda item: (item[0], item[1], item[2]['id']))
        for left_index, left in enumerate(points):
            for right_start, _, right in points[left_index+1:]:
                if right_start >= left[1]:
                    break
                left_effects, right_effects = left[2]['effects'], right['effects']
                left_correlated = left_effects.get('loss_correlation_pct',0) > 0
                right_correlated = right_effects.get('loss_correlation_pct',0) > 0
                if ((left_correlated and (right_correlated or right_effects.get('loss_pct',0)))
                        or (right_correlated and left_effects.get('loss_pct',0))):
                    raise ValueError('overlapping correlated-loss models are ambiguous')
                if network.get('loss_correlation_pct',0) and (left_effects.get('loss_pct',0) or right_effects.get('loss_pct',0)):
                    raise ValueError('event loss overlaps a correlated base-loss model')
    limits = value['regression']
    keys(limits, DEFAULT_LIMITS)
    for threshold in limits.values():
        number(threshold, 0, 10000)
    if deadline_modes and deadline_runtime.pacing_floor(value) > deadline_runtime.PACING_BASELINE:
        raise ValueError('configured background share exceeds the deadline laboratory socket baseline')
    return copy.deepcopy(value)


def load_manifest(path):
    with Path(path).open('rb') as stream:
        data = stream.read(65537)
    if len(data) > 65536:
        raise ValueError('manifest exceeds 64 KiB')
    return validate(json.loads(data, object_pairs_hook=unique))
