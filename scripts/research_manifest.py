"""Versioned and bounded Research Lab input format."""
import copy
import json
from pathlib import Path

from service_config import keys, number, profile, unique

LEGACY_MODES = ('linux', 'fifo', 'fq_codel', 'cake', 'static', 'threshold', 'slo')
MODES = (*LEGACY_MODES, 'linux-default', 'htb-fq_codel', 'static20', 'static30', 'static40', 'static35', 'slo-shadow', 'slo-single', 'slo-long', 'static-step')
V2_MODES = tuple(mode for mode in MODES if mode != 'linux')
CLASSIFIED = frozenset(('static', 'threshold', 'slo', 'htb-fq_codel', 'static20', 'static30', 'static40', 'static35', 'slo-shadow', 'slo-single', 'slo-long', 'static-step'))
GATE_METRICS = ('p99_ms', 'total_mbps', 'failure_ratio', 'deadline_attainment')
FAULTS = ('bandwidth_kbit', 'base_rtt_ms', 'jitter_ms', 'loss_pct', 'reorder_pct', 'link_up')
DEFAULT_LIMITS = dict(p99_ms=.10, total_mbps=.10, jain_index=.02,
                      cpu_seconds=.15, adjustments_per_minute=2, event_loss=5)


def net_profile(network):
    return profile(dict(bandwidth_kbit=network['bandwidth_kbit'], delay_ms=network['base_rtt_ms'],
                        jitter_ms=network['jitter_ms'], loss_pct=network['loss_pct'],
                        reorder_pct=network['reorder_pct']))


def validate(value):
    v2 = isinstance(value, dict) and value.get('version') == 2
    fields = ('version', 'topology', 'network', 'workload', 'duration_seconds', 'repetitions',
              'seed', 'modes', 'static_critical_pct', 'controller', 'faults', 'regression')
    keys(value, fields + (('gates',) if v2 else ()))
    number(value['version'], 1, 2, True)
    if value['topology'] != 'veth-pair':
        raise ValueError('only the owned veth-pair topology is supported')
    number(value['duration_seconds'], 4, 180, True)
    number(value['repetitions'], 1, 30, True)
    number(value['seed'], 0, 2**32-1, True)
    number(value['static_critical_pct'], 20, 40, True)
    modes = value['modes']
    if not isinstance(modes, list) or not modes or any(not isinstance(mode, str) or mode not in (V2_MODES if v2 else LEGACY_MODES) for mode in modes) or len(set(modes)) != len(modes):
        raise ValueError('modes must be unique supported comparisons')
    network = value['network']
    keys(network, FAULTS[:-1])
    net_profile(network)
    workload = value['workload']
    fields = ('phases', 'payload_bytes', 'max_inflight', 'priority_weights', 'bulk_bytes_per_stream')
    keys(workload, fields + (('warmup_seconds', 'deadline_ms', 'critical_services',
                             'connection_reuse', 'background_kbit_per_stream') if v2 else ()))
    if v2:
        number(workload['warmup_seconds'], 0, 30, True)
        number(workload['critical_services'], 1, 4, True)
        number(workload['background_kbit_per_stream'], 0, 1000000, True)
        if workload['connection_reuse'] != 'none':
            raise ValueError('version 2 supports a new connection per request')
        if workload['deadline_ms'] is not None:
            number(workload['deadline_ms'], .001, 5000)
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
                or any(network[key] for key in FAULTS[1:-1])):
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
        keys(phase, ('seconds', 'requests_per_second', 'background_streams'))
        number(phase['seconds'], 1, 180, True)
        number(phase['requests_per_second'], 0, 500, True)
        number(phase['background_streams'], 0, 32, True)
        total += phase['seconds']
    if v2 and not any(phase['requests_per_second'] for phase in phases):
        raise ValueError('version 2 requires an offered request cohort')
    if total != value['duration_seconds']:
        raise ValueError('workload phase durations must sum to experiment duration')
    controller = value['controller']
    keys(controller, ('target_p99_ms', 'window_seconds', 'minimum_samples', 'bpf'))
    number(controller['target_p99_ms'], .001, 600000)
    number(controller['window_seconds'], 2, 30, True)
    number(controller['minimum_samples'], 1, 10000, True)
    if controller['bpf'] not in ('auto', 'off'):
        raise ValueError('bpf must be auto or off')
    faults = value['faults']
    if not isinstance(faults, list) or len(faults) > 32:
        raise ValueError('at most 32 scheduled network transitions')
    if 'static-step' in modes and (faults or value['duration_seconds'] < 6):
        raise ValueError('static-step requires a fault-free experiment of at least six seconds')
    previous = 0
    current = dict(network)
    for fault in faults:
        if not isinstance(fault, dict) or not {'at_seconds'} < set(fault) or set(fault) - {'at_seconds', *FAULTS}:
            raise ValueError('invalid network transition')
        when = fault['at_seconds']
        number(when, .1, value['duration_seconds']-.1)
        if when <= previous:
            raise ValueError('transitions must have increasing timestamps')
        previous = when
        if 'link_up' in fault and type(fault['link_up']) is not bool:
            raise ValueError('link_up must be boolean')
        current.update({key: fault[key] for key in FAULTS[:-1] if key in fault})
        net_profile(current)
    limits = value['regression']
    keys(limits, DEFAULT_LIMITS)
    for threshold in limits.values():
        number(threshold, 0, 10000)
    return copy.deepcopy(value)


def load_manifest(path):
    with Path(path).open('rb') as stream:
        data = stream.read(65537)
    if len(data) > 65536:
        raise ValueError('manifest exceeds 64 KiB')
    return validate(json.loads(data, object_pairs_hook=unique))
