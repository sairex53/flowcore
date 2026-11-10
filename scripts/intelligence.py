"""Pure estimation, strategy scoring, native constraints and legal control states."""
import ctypes
from pathlib import Path
from paths import BINDIR

CLASSES = ('critical','interactive','standard','background')
STATES = {
    'DISABLED': {'OBSERVING','SAFE_FALLBACK'},
    'OBSERVING': {'HEALTHY','CONGESTED','DEGRADED','DISABLED','SAFE_FALLBACK'},
    'HEALTHY': {'OBSERVING','CONGESTED','DEGRADED','DISABLED','SAFE_FALLBACK'},
    'CONGESTED': {'OBSERVING','HEALTHY','ADJUSTING','DEGRADED','DISABLED','SAFE_FALLBACK'},
    'ADJUSTING': {'EVALUATING','SAFE_FALLBACK','DEGRADED','DISABLED'},
    'EVALUATING': {'HEALTHY','RECOVERING','DEGRADED','SAFE_FALLBACK','DISABLED'},
    'RECOVERING': {'OBSERVING','HEALTHY','CONGESTED','DEGRADED','SAFE_FALLBACK','DISABLED'},
    'DEGRADED': {'OBSERVING','SAFE_FALLBACK','DISABLED'},
    'SAFE_FALLBACK': {'OBSERVING','DISABLED'},
}


class State:
    def __init__(self):
        self.name = 'DISABLED'
        self.reason = 'not activated'

    def move(self, target, reason):
        if target != self.name and target not in STATES[self.name]:
            raise RuntimeError(f'illegal controller transition {self.name} to {target}')
        self.name, self.reason = target, reason


class Allocator:
    def __init__(self):
        self.function = None

    def allocate(self, capacity, current, maxima, floor, step, scores):
        if self.function is None:
            library = ctypes.CDLL(str(Path(BINDIR)/'libflowcore_intelligence.so'))
            function = library.flowcore_allocate
            integer = ctypes.c_int64
            pointer = ctypes.POINTER(integer)
            function.argtypes = [integer,pointer,pointer,pointer,integer,integer,
                                 ctypes.POINTER(ctypes.c_double),pointer]
            function.restype = ctypes.c_int
            self.function = function
        integers = ctypes.c_int64*4
        output = integers()
        selected = self.function(capacity,integers(*(current[c][0] for c in CLASSES)),
            integers(*(current[c][1] for c in CLASSES)),integers(*(maxima[c] for c in CLASSES)),
            floor,step,(ctypes.c_double*4)(*(scores.get(c,0) for c in CLASSES)),output)
        if selected == -2:
            raise ValueError('infeasible allocation bounds')
        if selected == -1:
            return None
        return CLASSES[selected], {c:(output[i],current[c][1]) for i,c in enumerate(CLASSES)}


def estimate(window, name, intent, measured, rate, minimum):
    queue = window['queue'].get(intent['priority'], {})
    root = window['queue'].get('root', {})
    backlog = queue.get('backlogged_samples',0)
    demand = measured['throughput_kbit']
    network = ((root.get('utilization') or 0) >= .75 and backlog >= 2
               and demand is not None and demand >= .9*rate)
    quality = (measured['samples'] >= max(100,minimum) and not measured['failures'] and measured['p99_us'] is not None)
    return dict(network_congestion=network, eligible=network and quality,
                success_samples=measured['samples'], minimum_successes=max(100,minimum),
                backlogged_samples=backlog, demand_kbit=demand,
                guarantee_kbit=rate, drops_per_second=queue.get('drops_per_second'),
                borrowed=queue.get('borrowed'), deadline_misses=measured['deadline_miss'],
                rtt_context=window.get('rtt'), confidence='heuristic' if quality else 'insufficient',
                attribution='network_evidence' if network else 'unattributed_slo_violation')


def score(strategy, observation, objective, weight, boost):
    if not observation['eligible']:
        return 0
    violation = max(0,objective['p99_us']/objective['target_p99_us']-1)
    if strategy == 'queue-aware':
        utility = 1
    elif strategy == 'slo-aware':
        utility = violation if objective['outside_deadband'] else 0
    elif strategy == 'hybrid':
        utility = ((violation + objective['deadline_miss']/max(1,objective['reports']))
                   if objective['outside_deadband'] else 0)
        utility *= 1 + min(1,observation['drops_per_second'] or 0)
    else:
        return 0
    # Diminishing benefit promotes sharing among eligible services in a tier.
    return weight*utility/(1+boost)
