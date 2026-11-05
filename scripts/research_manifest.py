"""Versioned and bounded Research Lab input format."""
import copy
import json
from pathlib import Path

from service_config import keys, number, profile, unique

MODES = ('linux', 'fifo', 'fq_codel', 'cake', 'static', 'threshold', 'slo')
FAULTS = ('bandwidth_kbit', 'base_rtt_ms', 'jitter_ms', 'loss_pct', 'reorder_pct', 'link_up')
DEFAULT_LIMITS = dict(p99_ms=.10, total_mbps=.10, jain_index=.02,
                      cpu_seconds=.15, adjustments_per_minute=2, event_loss=5)


def net_profile(network):
    return profile(dict(bandwidth_kbit=network['bandwidth_kbit'], delay_ms=network['base_rtt_ms'],
                        jitter_ms=network['jitter_ms'], loss_pct=network['loss_pct'],
                        reorder_pct=network['reorder_pct']))


def validate(value):
    keys(value, ('version', 'topology', 'network', 'workload', 'duration_seconds', 'repetitions',
                 'seed', 'modes', 'static_critical_pct', 'controller', 'faults', 'regression'))
    number(value['version'], 1, 1, True)
    if value['topology'] != 'veth-pair':
        raise ValueError('only the owned veth-pair topology is supported')
    number(value['duration_seconds'], 4, 180, True)
    number(value['repetitions'], 1, 30, True)
    number(value['seed'], 0, 2**32-1, True)
    number(value['static_critical_pct'], 20, 40, True)
    modes = value['modes']
    if not isinstance(modes, list) or not modes or any(mode not in MODES for mode in modes) or len(set(modes)) != len(modes):
        raise ValueError('modes must be unique supported comparisons')
    network = value['network']
    keys(network, FAULTS[:-1])
    net_profile(network)
    workload = value['workload']
    keys(workload, ('phases', 'payload_bytes', 'max_inflight', 'priority_weights', 'bulk_bytes_per_stream'))
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
