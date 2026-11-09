"""Versioned operator objectives and bounded native Pareto comparison."""
import ctypes as c
from functools import cache

from paths import BINDIR
from service_config import CLASSES, keys, number

# Direction and upper numeric domain in the public metric's own units.
METRICS = {
    'critical_p99_ms': ('min', 600_000),
    'deadline_miss_ratio': ('min', 1),
    'success_ratio': ('max', 1),
    'background_mbps': ('max', 1_000),
    'service_mbps': ('max', 1_000),
    'fairness': ('max', 1),
    'utilization': ('max', 1),
    'cpu_pct': ('min', 3_200),
    'updates_per_min': ('min', 100_000),
    'recovery_seconds': ('min', 180),
}
LIMITS = {**METRICS, 'rss_mib': ('min', 16_384)}
ESSENTIAL = ('critical_p99_ms', 'deadline_miss_ratio', 'success_ratio', 'background_mbps')
LIVE_METRICS = (*ESSENTIAL, 'fairness', 'utilization', 'cpu_pct')
MAX_POLICIES = 8
FIXED = 1_000_000


def preferences(value, permitted=METRICS):
    if not isinstance(value, dict) or not value or set(value)-set(permitted):
        raise ValueError('objective set must be nonempty and supported')
    for metric, spec in value.items():
        keys(spec, ('weight', 'scale'))
        number(spec['weight'], 1, 100, True)
        maximum = METRICS[metric][1]
        number(spec['scale'], maximum/FIXED, maximum)
    return {name: dict(value[name]) for name in METRICS if name in value}


def profile(value):
    keys(value, ('version', 'baseline', 'min_pairs', 'min_critical_samples',
                 'objectives', 'limits', 'guardrails', 'resources', 'min_score_gain', 'solver_cpu_ms'))
    number(value['version'], 1, 1, True)
    number(value['min_pairs'], 10, 30, True)
    number(value['min_critical_samples'], 10, 10_000, True)
    number(value['min_score_gain'], 0, 1_000, False)
    number(value['solver_cpu_ms'], 1, 1_000, True)
    if not isinstance(value['baseline'], str) or not 1 <= len(value['baseline']) <= 32:
        raise ValueError('invalid SLO baseline')
    objectives = preferences(value['objectives'])
    limits = value['limits']
    if not isinstance(limits, dict) or set(limits)-LIMITS.keys() or not {
            'rss_mib', 'cpu_pct', 'updates_per_min'} <= limits.keys():
        raise ValueError('limits must include CPU, RSS and policy update budgets')
    for metric, threshold in limits.items():
        number(threshold, 0, LIMITS[metric][1])
    keys(value['guardrails'], ESSENTIAL)
    for metric, margin in value['guardrails'].items():
        number(margin, 0, METRICS[metric][1])
    resources = value['resources']
    keys(resources, ('capacity_kbit_max', 'classes'))
    number(resources['capacity_kbit_max'], 100, 1_000_000, True)
    keys(resources['classes'], CLASSES)
    for bounds in resources['classes'].values():
        keys(bounds, ('floor_pct', 'ceiling_pct'))
        number(bounds['floor_pct'], 1, 100, True)
        number(bounds['ceiling_pct'], 1, 100, True)
    return dict(value, objectives=objectives)


def resource_conflicts(capacity, resources, actual=None):
    """Use HTB kbit/s integer allocation semantics. No service SLA is inferred."""
    if type(capacity) is not int or not 100 <= capacity <= resources['capacity_kbit_max']:
        return [{'code': 'CAPACITY_OUTSIDE_AUTHORIZATION', 'capacity_kbit': capacity}]
    bounds = resources['classes']
    floor = {name: capacity*row['floor_pct']//100 for name, row in bounds.items()}
    limit = {name: capacity*row['ceiling_pct']//100 for name, row in bounds.items()}
    errors = [dict(code='CONFLICTING_CLASS_BOUNDS', traffic_class=name)
              for name in CLASSES if not 0 < floor[name] <= limit[name]]
    if sum(floor.values()) > capacity:
        errors.append(dict(code='PROTECTED_SHARES_OVERCOMMIT', required_kbit=sum(floor.values())))
    if actual is not None:
        if (not isinstance(actual, dict) or set(actual) != set(CLASSES)
                or any(not isinstance(pair, (list, tuple)) or len(pair)!=2
                       or any(type(rate) is not int for rate in pair) for pair in actual.values())):
            return errors+[dict(code='INVALID_ALLOCATION_EVIDENCE')]
        if sum(pair[0] for pair in actual.values()) > capacity:
            errors.append(dict(code='OVERCOMMITTED_CLASSES'))
        for name, (rate, ceil) in actual.items():
            if not floor[name] <= rate <= ceil <= limit[name]:
                errors.append(dict(code='ALLOCATION_OUTSIDE_AUTHORIZATION', traffic_class=name,
                                   rate_kbit=rate, ceiling_kbit=ceil))
    return errors


def loss(name, value):
    number(value, 0, LIMITS[name][1])
    return value if LIMITS[name][0] == 'min' else -value


def fixed_losses(observed, objectives):
    return [round(FIXED*loss(name, observed[name])/spec['scale']) for name, spec in objectives.items()]


def score(observed, objectives):
    return sum(spec['weight']*value for spec, value in zip(objectives.values(),
               fixed_losses(observed, objectives)))/FIXED


@cache
def native():
    fn = c.CDLL(str(BINDIR/'libflowcore_intelligence.so')).flowcore_slo_front_v1
    i64 = c.POINTER(c.c_int64)
    fn.argtypes = [c.c_size_t, c.c_size_t, i64, i64, c.POINTER(c.c_uint32), i64]
    fn.restype = c.c_int
    return fn


def solve(points, objectives):
    if not 1 <= len(points) <= MAX_POLICIES or not 1 <= len(objectives) <= len(METRICS):
        raise ValueError('Pareto solver bounds exceeded')
    names, dimensions = list(points), len(objectives)
    vectors = [item for point in points.values() for item in fixed_losses(point, objectives)]
    values = (c.c_int64*len(vectors))(*vectors)
    weights = (c.c_int64*dimensions)(*(spec['weight'] for spec in objectives.values()))
    scores = (c.c_int64*len(names))()
    mask = c.c_uint32()
    if native()(len(names), dimensions, values, weights, c.byref(mask), scores):
        raise ValueError('native Pareto solver rejected its bounded numeric inputs')
    return [name for i, name in enumerate(names) if mask.value&(1<<i)], {
        name: scores[i]/FIXED for i, name in enumerate(names)}


def hybrid_tradeoff(before, after, guard):
    """Same-cohort descriptive comparison. This can veto a probe, not authorize one."""
    objectives = preferences(guard['objectives'], LIVE_METRICS)

    def metrics(row):
        return dict(critical_p99_ms=row['p99_us']/1000, deadline_miss_ratio=row['deadline_miss_ratio'],
                    success_ratio=row['success_ratio'], background_mbps=row['goodput']*8/1e6,
                    fairness=row['fairness'], utilization=min(1, row['utilization']),
                    cpu_pct=100*row['cpu_seconds']/row['interval_seconds'])
    try:
        baseline = score(metrics(before), objectives)
        changes = [score(metrics(row), objectives)-baseline for row in after]
    except (KeyError, ValueError, ZeroDivisionError):
        return dict(provenance='within_trial_windows', gain_met=False, status='INSUFFICIENT_MEASUREMENTS')
    return dict(provenance='within_trial_windows', gain_met=all(delta < -guard['min_score_gain'] for delta in changes),
                weighted_loss_deltas=changes, minimum_gain=guard['min_score_gain'])
