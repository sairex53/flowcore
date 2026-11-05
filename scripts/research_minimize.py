"""Bounded real reruns for minimizing a reproducible request-failure scenario."""
import copy
import math
import time

from benchmark import owned_directory
from research_runner import RESULTS, execute, save


def minimize(manifest, mode, ratio, budget):
    directory = RESULTS/f'minimized-{time.time_ns()}'
    owned_directory(directory)
    baseline = copy.deepcopy(manifest)
    baseline['modes'] = [mode]
    baseline['repetitions'] = 2
    save(directory/'original.json',baseline)
    attempts = []
    def check(candidate):
        path,result = execute(candidate,label='attempt',parent=directory)
        ratios = [r['failed']/r['offered'] if r['valid'] and r['offered'] else None for r in result['runs']]
        reproduces = len(ratios)==2 and all(r is not None and r>=ratio for r in ratios)
        attempts.append(dict(path=path.name,faults=len(candidate['faults']),
                             duration=candidate['duration_seconds'],ratios=ratios,reproduces=reproduces))
        save(directory/'attempts.json',attempts)
        return reproduces
    if not check(baseline):
        raise ValueError(f'failure did not reproduce twice. Evidence: {directory}')
    current = baseline
    parts = 2
    while current['faults'] and len(attempts)<budget:
        width = math.ceil(len(current['faults'])/parts)
        kept = False
        for start in range(0,len(current['faults']),width):
            if len(attempts)>=budget:
                break
            candidate = copy.deepcopy(current)
            del candidate['faults'][start:start+width]
            try:
                from research_manifest import validate
                validate(candidate)
            except ValueError:
                continue  # removing a delay phase can make later reordering invalid
            if check(candidate):
                current = candidate
                parts = max(2,parts-1)
                kept = True
                break
        if not kept:
            if parts>=len(current['faults']):
                break
            parts = min(len(current['faults']),parts*2)
    if len(attempts)<budget:
        candidate = copy.deepcopy(current)
        minimum = max(4,math.ceil(max((f['at_seconds'] for f in current['faults']),default=0)+1))
        proposed = max(minimum,current['duration_seconds']//2)
        if proposed<current['duration_seconds']:
            remaining = proposed
            phases = []
            for phase in candidate['workload']['phases']:
                if remaining<=0:
                    break
                length = min(phase['seconds'],remaining)
                phases.append(dict(phase,seconds=length))
                remaining -= length
            candidate['duration_seconds'] = proposed
            candidate['workload']['phases'] = phases
            if check(candidate):
                current = candidate
    save(directory/'minimized.json',current)
    return dict(directory=str(directory),attempts=len(attempts),faults=len(current['faults']),
                duration_seconds=current['duration_seconds'],seed=current['seed'])
