"""Bounded, evidence-preserving minimization of reproducible research failures."""
import copy
import math
import time

from benchmark import owned_directory
from research_runner import RESULTS, execute, save
from research_manifest import validate


def reduce_duration(candidate):
    end = max((event['at_seconds']+event['duration_seconds'] for event in candidate.get('events',[])),
              default=max((fault['at_seconds'] for fault in candidate.get('faults',[])),default=0))
    minimum = max(4 if candidate['version'] < 3 else 6, math.ceil(end+.1))
    proposed = max(minimum, candidate['duration_seconds']//2)
    if proposed >= candidate['duration_seconds']:
        return None
    result = copy.deepcopy(candidate)
    remaining = proposed
    phases = []
    for phase in result['workload']['phases']:
        if remaining <= 0:
            break
        length = min(phase['seconds'], remaining)
        phases.append(dict(phase,seconds=length))
        remaining -= length
    if remaining:
        return None
    result['workload']['phases'] = phases
    result['duration_seconds'] = proposed
    if result['version'] == 3:
        to_remove = candidate['duration_seconds']-proposed
        stages = [dict(stage) for stage in result['stages']]
        for stage in reversed(stages):
            cut = min(to_remove,stage['seconds']-1)
            stage['seconds'] -= cut
            to_remove -= cut
        if to_remove:
            return None
        result['stages'] = stages
    return result


def candidates(current):
    if current['version'] == 3:
        events = current['events']
        for i in range(len(events)):
            result = copy.deepcopy(current)
            del result['events'][i]
            if result['events']:
                yield result, 'remove_event'
        for i,event in enumerate(events):
            if event['duration_seconds'] > .1:
                result = copy.deepcopy(current)
                result['events'][i]['duration_seconds'] = max(.1,round(event['duration_seconds']/2,2))
                yield result, 'shorten_event'
            for key,value in event['effects'].items():
                result = copy.deepcopy(current)
                del result['events'][i]['effects'][key]
                if result['events'][i]['effects']:
                    yield result, 'remove_effect'
                baseline = current['network'].get(key)
                if key in ('bandwidth_kbit','reverse_bandwidth_kbit') and baseline is not None:
                    result = copy.deepcopy(current)
                    result['events'][i]['effects'][key] = (value+baseline)//2
                    yield result, 'weaken_rate_change'
                elif key in ('base_rtt_ms','jitter_ms','loss_pct','reorder_pct','duplicate_pct'):
                    result = copy.deepcopy(current)
                    result['events'][i]['effects'][key] = value/2
                    yield result, 'weaken_impairment'
        result = copy.deepcopy(current)
        changed = False
        for phase in result['workload']['phases']:
            new_rate, new_streams = phase['requests_per_second']//2, phase['background_streams']//2
            old_udp=phase.get('udp_streams',0)
            new_udp=old_udp//2
            changed |= (new_rate,new_streams,new_udp)!=(phase['requests_per_second'],phase['background_streams'],old_udp)
            phase['requests_per_second'],phase['background_streams'] = new_rate,new_streams
            if 'udp_streams' in phase:
                phase['udp_streams']=new_udp
        if changed:
            yield result, 'reduce_workload'
        shorter = reduce_duration(current)
        if shorter:
            yield shorter, 'shorten_scenario'
    else:
        for i in range(len(current['faults'])):
            result = copy.deepcopy(current)
            del result['faults'][i]
            yield result, 'remove_fault'
        shorter = reduce_duration(current)
        if shorter:
            yield shorter, 'shorten_scenario'


def minimize(manifest, mode, ratio, budget):
    directory = RESULTS/f'minimized-{time.time_ns()}'
    owned_directory(directory)
    baseline = copy.deepcopy(manifest)
    baseline['modes'] = [mode]
    baseline['repetitions'] = 2
    save(directory/'original.json',baseline)
    attempts = []

    def check(candidate, operation):
        path,result = execute(candidate,label='attempt',parent=directory)
        ratios = [r['failed']/r['offered'] if r['valid'] and r['offered'] else None for r in result['runs']]
        reproduces = len(ratios)==2 and all(r is not None and r>=ratio for r in ratios)
        attempts.append(dict(path=path.name,operation=operation,
                             events=len(candidate.get('events',candidate['faults'])),
                             duration=candidate['duration_seconds'],ratios=ratios,reproduces=reproduces))
        save(directory/'attempts.json',attempts)
        return reproduces

    if not check(baseline,'baseline_reproduction'):
        raise ValueError(f'failure did not reproduce twice. Evidence: {directory}')
    current = baseline
    cursor = 0
    while len(attempts)<budget:
        options = list(candidates(current))
        accepted = False
        while cursor < len(options) and len(attempts)<budget:
            candidate,operation = options[cursor]
            cursor += 1
            try:
                validate(candidate)
            except ValueError:
                continue
            if check(candidate,operation):
                current = candidate
                cursor = 0
                accepted = True
                break
        if not accepted:
            break
    save(directory/'minimized.json',current)
    return dict(directory=str(directory),attempts=len(attempts),
                faults=len(current['faults']),events=len(current.get('events',[])),
                duration_seconds=current['duration_seconds'],seed=current['seed'])
