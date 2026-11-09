"""Registry and paired-evidence selection for the supported strategy domains."""
import ctypes
from pathlib import Path

from paths import BINDIR
from service_config import keys, number

STRATEGIES = {
    'static': ('class', 'static'),
    'threshold': ('class', 'threshold'),
    'queue-aware': ('class', 'queue-aware'),
    'slo-aware': ('class', 'slo-aware'),
    'predictive': ('class', 'predictive'),
    'socket-pacing': ('socket', 'pacing-hybrid'),
    'ecn-aware': ('socket', 'ecn-aware'),
    'deadline-aware': ('socket', 'deadline-adaptive'),
}
CLASS = tuple(name for name, (domain, _) in STRATEGIES.items() if domain == 'class')
SOCKET = tuple(name for name, (domain, _) in STRATEGIES.items() if domain == 'socket')
GUARDS = ('minimum_samples', 'max_failure_ratio', 'max_p99_ms', 'p99_regression',
          'max_deadline_miss_ratio', 'minimum_background_kbit', 'background_floor_pct')


def names(values, eligible=STRATEGIES):
    if (not isinstance(values, list) or len(values) > len(eligible) or
            any(not isinstance(value, str) or value not in eligible for value in values) or
            len(set(values)) != len(values)):
        raise ValueError('expected distinct registered strategies')
    return values


def guards(value):
    keys(value, GUARDS)
    number(value['minimum_samples'], 20, 10000, True)
    for key in ('max_failure_ratio', 'p99_regression', 'max_deadline_miss_ratio'):
        number(value[key], 0, 1)
    number(value['max_p99_ms'], .001, 5000)
    number(value['minimum_background_kbit'], 1, 1000000, True)
    number(value['background_floor_pct'], 20, 60, True)
    return dict(value)


def inventory(service=None):
    try:
        native = ctypes.CDLL(str(BINDIR/'libflowcore_intelligence.so'))
    except OSError:
        native = None
    try:
        from pacing_workload import library, checked
        pacing = library().fc_pacing_capabilities
        pacing.argtypes = [ctypes.POINTER(ctypes.c_int)]
        pacing.restype = ctypes.c_int
        options = (ctypes.c_int*8)()
        checked(pacing(options), 'strategy socket options')
        sockets = not any(options[index] for index in (0, 6, 7))
    except (OSError, AttributeError, RuntimeError):
        sockets = False
    owned = bool(service and service.lab and service.controller.policy()['scheduler']=='htb'
                 and service.observer.cfg['enabled'])
    classes = [row['intent']['priority'] for row in service.controller.registry.rows.values()] if owned else []
    rows = {}
    for name, (domain, backend) in STRATEGIES.items():
        shared_class_conflict = name == 'threshold' and len(classes) != len(set(classes))
        if domain == 'socket':
            available = sockets and (name != 'deadline-aware' or hasattr(native, 'flowcore_deadline_estimate_v1'))
            if available and name == 'ecn-aware':
                try:
                    available = Path('/proc/sys/net/ipv4/tcp_ecn').read_text().strip().isdigit()
                except OSError:
                    available = False
            reason = ('requires a verified application-owned socket session and matching signals' if available
                      else 'socket option, ECN sysctl or native backend unavailable')
        else:
            available = name in ('static', 'threshold') or (
                hasattr(native, 'flowcore_allocate')
                and (name != 'predictive' or hasattr(native, 'flowcore_predict_v1')))
            reason = None
            if not available:
                reason = 'native backend unavailable'
            elif not owned:
                reason = 'owned HTB telemetry scope required'
            elif shared_class_conflict:
                reason = 'threshold requires exclusive service classes'
        rows[name] = dict(domain=domain, backend=backend, available=available,
                          canary=bool(available and owned and service.research and domain == 'class'
                                      and name != 'static' and not shared_class_conflict),
                          reason=reason)
    return rows


def conditions(manifest):
    """Exact preregistered workload and impairments, excluding seeds and arms."""
    return {key: manifest.get(key) for key in ('topology','network','workload','duration_seconds',
        'controller','static_critical_pct','faults','events','stages','deadline','ecn','online','socket_shadows')}


def profile(summary):
    from research_stats import aggregate
    manifest = summary['manifest']
    paired_design = (summary['version'] >= 2 and manifest.get('gates', {}).get('baseline') is not None
                     and len(manifest['modes']) > 1)
    return dict(evidence='randomized_paired_runs' if paired_design else 'measured_runs',
                experiment=summary.get('experiment_id'), condition=conditions(manifest), by_mode=aggregate(summary))


def recommend(summary, target, eligible, limits, switch_cost=.05, available=None):
    """Select only after paired noninferiority and positive P99 evidence."""
    from research_stats import compare_v2
    limits = guards(limits)
    names(eligible)
    number(switch_cost, 0, .5)
    common = conditions(summary['manifest']) == conditions(target)
    modes = summary['manifest']['modes']
    baseline = summary['manifest'].get('gates', {}).get('baseline')
    allowed_baseline = {'socket-pacing': ('static',), 'ecn-aware': ('ecn-static',),
                        'deadline-aware': ('deadline-static', 'deadline-paced-static')}
    evidence = {}
    safe = []
    for name in eligible:
        if name == 'static':
            continue
        domain, mode = STRATEGIES[name]
        if available is not None and not available[name]['available']:
            evidence[name] = dict(status='unavailable')
            continue
        if (not common or summary['version'] < 2 or baseline not in modes or mode not in modes
                or baseline not in allowed_baseline.get(name, ('static',))):
            evidence[name] = dict(status='unmatched_or_missing_paired_evidence')
            continue
        report = compare_v2(summary, summary, baseline, mode)
        rows = [row for row in summary['runs'] if row['mode']==mode]
        hard = limits['background_floor_pct'] <= 20 and bool(rows) and all(
            row['valid'] and row['offered'] >= limits['minimum_samples']
            and row.get('p99_ms') is not None and row['p99_ms'] <= limits['max_p99_ms']
            and row['failed']/row['offered'] <= limits['max_failure_ratio']
            and (row.get('deadline_attainment') is not None
                 and 1-row['deadline_attainment'] <= limits['max_deadline_miss_ratio'])
            and (row.get('background_mbps') or 0)*1000 >= limits['minimum_background_kbit'] for row in rows)
        acted = sum(1 for row in rows if row['valid'] and (
            row.get('adjustments', 0) if domain == 'class' else
            (row.get('socket_pacing') or {}).get('socket_actions', 0)))
        # Positive evidence must clear switching cost, in addition to every
        # predeclared benchmark gate. Failures censor successful-only P99.
        interval = next(row['simultaneous_median_ci'] for row in report['findings'] if row['metric']=='p99_ms')
        if not hard:
            status = 'guardrail_failed'
        elif report['outcome'] != 'PASS':
            status = report['outcome']
        elif not acted:
            status = 'no_verified_actuation'
        elif interval['high'] >= -switch_cost:
            status = 'switch_cost_not_cleared'
        else:
            status = 'validated'
            safe.append((interval['high'], name))
        evidence[name] = dict(status=status, backend_domain=domain,
                              runs_with_actuation=acted, comparative=report)
    return dict(selected=min(safe)[1] if safe else 'static', evidence=evidence,
                evidence_kind='randomized_paired_runs', condition_match=common,
                scope='recommendation_only', switch_cost=switch_cost)
