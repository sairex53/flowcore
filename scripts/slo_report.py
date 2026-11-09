"""Compare measured, paired Research Lab policies without changing kernel state."""
from collections import Counter
from itertools import combinations
from statistics import median
import time

from durable_state import read_json
from nextgen_report import read_records
from research_stats import load_summary, median_ci
from service_config import CLASSES
from slo_optimizer import ESSENTIAL, LIMITS, MAX_POLICIES, METRICS, loss, resource_conflicts, score, solve


def observations(run, duration, critical_services):
    critical = run.get('classes', {}).get('critical', {})
    services = [row for row in run.get('services', {}).values() if row.get('traffic_class')=='critical']
    windows = run.get('observed_rate_windows', [])
    wire = [row for row in windows if row.get('measured_to_configured_ratio') is not None
            and row['seconds'] > 0]
    coverage = sum(row['seconds'] for row in wire)
    utilization = (min(1, sum(row['measured_to_configured_ratio']*row['seconds'] for row in wire)/coverage)
                   if wire and len(wire)==len(windows) else None)
    recovery = run.get('slo_recovery', {})
    updates = run.get('slo_policy_updates')
    return dict(critical_p99_ms=critical.get('p99_ms'), deadline_miss_ratio=(
                    run['deadline_misses']/run['offered'] if run.get('deadline_misses') is not None else None),
                success_ratio=run['completed']/run['offered'], background_mbps=run.get('background_mbps'),
                service_mbps=min(row['payload_mbps'] for row in services)
                    if len(services)==critical_services and all('payload_mbps' in row for row in services) else None,
                fairness=run.get('jain_index'), utilization=utilization,
                cpu_pct=100*run['cpu_seconds']/duration if run.get('cpu_seconds') is not None else None,
                rss_mib=run['peak_daemon_rss_bytes']/2**20 if run.get('peak_daemon_rss_bytes') else None,
                updates_per_min=60*updates/duration if type(updates) is int else None,
                recovery_seconds=recovery.get('seconds') if recovery.get('status')=='observed' else None)


def allocations(path, resources):
    """Check setup and sampled kernel state from a completed owned lab trial."""
    try:
        snapshots = read_json(path/'topology.json', limit=4*1024*1024)
        timeline = read_records(path/'timeline.jsonl')
    except (OSError, ValueError):
        return [{'code': 'MISSING_KERNEL_EVIDENCE'}]
    if not isinstance(snapshots, list) or not 3 <= len(snapshots) <= 80 or not 2 <= len(timeline) <= 300:
        return [{'code': 'INCOMPLETE_KERNEL_EVIDENCE'}]
    namespaces = snapshots[0].get('namespaces')
    if (not isinstance(namespaces, list) or len(namespaces)!=2 or namespaces[0]==namespaces[1]
            or any(type(value) is not int or value<=0 for value in namespaces)
            or any(row.get('namespaces') != namespaces for row in snapshots)):
        return [{'code': 'OWNERSHIP_EVIDENCE_MISMATCH'}]
    expected = {'1:1', *(f'1:{handle}' for handle in CLASSES.values())}
    phase_capacities = {entry.get('configured_kbit') for entry in snapshots}
    checked = set()
    for entry in (*snapshots, *timeline):
        tc = entry.get('tc', {})
        roots = [(row.get('kind'), row.get('handle')) for row in tc.get('qdisc', []) if row.get('root')]
        rows = {row['handle']: (row.get('rate'), row.get('ceil')) for row in tc.get('class', [])
                if row.get('class')=='htb'}
        if roots != [('htb', '1:')] or set(rows)!=expected:
            return [{'code': 'UNSUPPORTED_OR_UNVERIFIED_HTB'}]
        if any(type(value) is not int or value<=0 or value%125 for pair in rows.values() for value in pair):
            return [{'code': 'INVALID_KERNEL_RATE_EVIDENCE'}]
        capacity, ceil = (value//125 for value in rows['1:1'])
        if capacity != ceil or capacity not in phase_capacities:
            return [{'code': 'INVALID_PARENT_CAPACITY'}]
        actual = {name: tuple(value//125 for value in rows[f'1:{handle}']) for name, handle in CLASSES.items()}
        state = capacity, tuple(actual.values())
        if state in checked:
            continue
        checked.add(state)
        if conflicts := resource_conflicts(capacity, resources, actual):
            return conflicts
    return []



def transition_costs(directory, mode):
    if mode == 'hybrid-v3':
        history = read_records(directory/'hybrid-v3.jsonl')
        if len(history)>512:
            raise ValueError('Hybrid journal exceeds report row budget')
        timings = [row.get('timing') for row in history if row['event'] in ('verified', 'restored_static')]
    else:
        history = read_json(directory/'decisions.json', limit=1024*1024)
        if not isinstance(history, list) or len(history)>512:
            raise ValueError('Controller journal exceeds report row budget')
        timings = [row.get(key) for row in history for key in ('timing', 'rollback')]
    stages = ('pre_read_ns', 'apply_ns', 'verify_ns')
    htb = [sum(timing[key] for key in stages)/1e6 for timing in timings if isinstance(timing, dict)
           and all(type(timing.get(key)) is int and timing[key]>=0 for key in stages)]
    sockets = read_records(directory/'socket-actuation.jsonl')
    if len(sockets)>512:
        raise ValueError('Socket journal exceeds report row budget')
    pacing = [(row['completed_ns']-row['started_ns'])/1e6 for row in sockets
              if row.get('verified') is True and type(row.get('started_ns')) is int and type(row.get('completed_ns')) is int
              and row['completed_ns'] >= row['started_ns']]
    return dict(htb_verified_wall_ms=htb, socket_verified_wall_ms=pacing,
                tc_rejections=sum(row.get('result')=='transaction_rejected' for row in history))

def evidence(path, summary, profile):
    modes = summary['manifest']['modes']
    duration = summary['manifest']['duration_seconds']
    critical_services = summary['manifest']['workload']['critical_services']
    rows = {mode: {} for mode in modes}
    blockers = {mode: [] for mode in modes}
    diagnostics = {mode: Counter() for mode in modes}
    costs = {mode: {'htb_verified_wall_ms': [], 'socket_verified_wall_ms': []} for mode in modes}
    requirements = set(profile['objectives']) | set(profile['limits']) | set(ESSENTIAL)
    for run in summary['runs']:
        mode, block = run['mode'], run['repetition']
        reasons = []
        if not run['valid']:
            blockers[mode].append(dict(block=block, code='INVALID_TRIAL'))
            continue
        try:
            observed = observations(run, duration, critical_services)
            critical = run.get('classes', {}).get('critical', {})
            enough = (type(critical.get('completed')) is int and
                      critical['completed'] >= profile['min_critical_samples'] and critical.get('failed') == 0)
        except (AttributeError, KeyError, TypeError, ZeroDivisionError):
            blockers[mode].append(dict(block=block, code='MALFORMED_MEASUREMENT'))
            continue
        rows[mode][block] = observed
        if not enough:
            # Tail of successful requests can be censored by failures.
            observed['critical_p99_ms'] = None
        for name, value in observed.items():
            if value is None:
                if name in requirements:
                    reasons.append(dict(code='INSUFFICIENT_MEASUREMENT', metric=name))
                continue
            try:
                loss(name, value)
            except ValueError:
                reasons.append(dict(code='INVALID_MEASUREMENT', metric=name))
                observed[name] = None
                continue
            if name in profile['limits'] and loss(name, value) > loss(name, profile['limits'][name]):
                reasons.append(dict(code='OBSERVED_BUDGET_VIOLATION', metric=name,
                                    observed=value, limit=profile['limits'][name]))
        if run.get('controller_error'):
            reasons.append(dict(code='CONTROLLER_ERROR', detail=run['controller_error']))
        try:
            reasons.extend(allocations(path.parent/run['artifact'], profile['resources']))
        except (AttributeError, KeyError, TypeError):
            reasons.append(dict(code='MALFORMED_KERNEL_EVIDENCE'))
        blockers[mode].extend(dict(block=block, **reason) for reason in reasons)
        try:
            timing = transition_costs(path.parent/run['artifact'], mode)
        except (AttributeError, KeyError, OSError, TypeError, ValueError):
            diagnostics[mode]['trials_missing_cost_evidence'] += 1
        else:
            diagnostics[mode]['tc_rejections'] += timing['tc_rejections']
            if timing['tc_rejections']:
                blockers[mode].append(dict(block=block, code='KERNEL_TRANSACTION_REJECTED'))
            for key, samples in costs[mode].items():
                samples.extend(timing[key])
        for field, label in (('slo_policy_updates', 'verified_updates'),
                             ('slo_harmful_actions', 'observed_harmful_actions')):
            count = run.get(field)
            if type(count) is int and count >= 0:
                diagnostics[mode][label] += count
        recovery = run.get('slo_recovery', {}).get('status')
        label = recovery if recovery in ('observed', 'right_censored', 'not_applicable') else 'unavailable'
        diagnostics[mode]['recovery_'+label] += 1
    for mode in modes:
        if len(rows[mode]) < profile['min_pairs'] or len(rows[mode]) != summary['manifest']['repetitions']:
            blockers[mode].append(dict(code='INCOMPLETE_INDEPENDENT_BLOCKS', count=len(rows[mode])))
    return rows, blockers, diagnostics, {
        mode: {key: dict(n=len(samples), median=median(samples) if samples else None,
                         maximum=max(samples) if samples else None)
               for key, samples in metrics.items()} for mode, metrics in costs.items()}


def comparison(left, right, rows, profile, confidence):
    objectives = profile['objectives']
    names = [name for name in METRICS if name in objectives or name in ESSENTIAL]
    blocks = sorted(rows[left].keys() & rows[right].keys())
    def interval(values):
        return dict(pairs=len(values), median_delta=median(values) if values else None,
                    interval=median_ci(values, confidence) if len(values)>=profile['min_pairs'] else None)

    metrics = {}
    for name in names:
        values = [loss(name, rows[left][block][name])-loss(name, rows[right][block][name])
                  for block in blocks if rows[left][block][name] is not None and rows[right][block][name] is not None]
        metrics[name] = interval(values)
    paired_scores = interval([score(rows[left][block], objectives)-score(rows[right][block], objectives)
                              for block in blocks if all(rows[left][block][name] is not None and
                                                        rows[right][block][name] is not None for name in objectives)])
    wins = [name for name, item in metrics.items() if item['interval'] and item['interval']['high'] < 0]
    losses = [name for name, item in metrics.items() if item['interval'] and item['interval']['low'] > 0]
    relation = ('MEASURED_TRADEOFF' if wins and losses else 'DIRECTIONAL_DIFFERENCES' if wins or losses
                else 'NO_DEMONSTRATED_DIRECTION')
    explanation = []
    if wins:
        explanation.append(f'{left} improved {", ".join(wins)} relative to {right}.')
    if losses:
        explanation.append(f'{left} regressed {", ".join(losses)} relative to {right}.')
    if not explanation:
        explanation.append('Neither policy has a demonstrated directional difference on the tested metrics.')
    return dict(candidate=left, reference=right, relation=relation, improvements=wins, regressions=losses,
                explanation=' '.join(explanation), metric_loss_deltas=metrics, weighted_loss_delta=paired_scores)


def choose(rows, blockers, profile, *, cpu_clock=time.process_time_ns):
    started = cpu_clock()
    baseline = profile['baseline']
    objectives = profile['objectives']
    all_medians = {name: {metric: median(values) if (values := [
        row[metric] for row in values_by_block.values() if row[metric] is not None]) else None
        for metric in LIMITS} for name, values_by_block in rows.items()}
    eligible = [name for name in rows if not blockers[name]]
    result = dict(candidate_medians=all_medians, eligible=eligible, exclusions=blockers,
                  point_front=[], evidence_front=[], comparisons=[], baseline=baseline,
                  recommendation=dict(status='KEEP_PREVIOUS_VALIDATED_POLICY', mode=None,
                                      reason='BASELINE_INELIGIBLE_OR_INCOMPLETE'))
    def timed_out():
        return cpu_clock()-started > profile['solver_cpu_ms']*1_000_000

    def finish():
        result['solver_cpu_ms'] = (cpu_clock()-started)/1e6
        if result['solver_cpu_ms'] > profile['solver_cpu_ms']:
            result['recommendation'] = dict(status='SOLVER_BUDGET_EXCEEDED', mode=None)
        return result

    if not eligible or timed_out():
        return finish()
    try:
        point_front, scores = solve({name: all_medians[name] for name in eligible}, objectives)
    except (OSError, ValueError, AttributeError) as error:
        result['recommendation'] = dict(status='SOLVER_UNAVAILABLE', mode=None, detail=str(error))
        return finish()
    best = min(scores.values())
    result.update(point_front=point_front, point_scores=scores,
                  descriptive_regret={name: value-best for name, value in scores.items()})
    # Include the whole planned family, including excluded candidates.
    family = max(1, len(rows)*(len(rows)-1)//2)*(len(set(objectives)|set(ESSENTIAL))+1)
    confidence = 1-.05/family
    result.update(comparison_family_size=family, per_interval_confidence=confidence)
    dominated, qualified = set(), []
    for a, b in combinations(eligible, 2):
        if timed_out():
            return finish()
        left, right = (b, a) if a == baseline else (a, b)
        report = comparison(left, right, rows, profile, confidence)
        result['comparisons'].append(report)
        metric_intervals = {key: report['metric_loss_deltas'][key]['interval'] for key in objectives}
        if all(metric_intervals.values()):
            intervals = list(metric_intervals.values())
            if all(item['high']<=0 for item in intervals) and any(item['high']<0 for item in intervals):
                dominated.add(right)
            if all(item['low']>=0 for item in intervals) and any(item['low']>0 for item in intervals):
                dominated.add(left)
        if right == baseline:
            guard = {key: report['metric_loss_deltas'][key]['interval'] for key in ESSENTIAL}
            regressions = [key for key, item in guard.items()
                           if not item or item['high'] > profile['guardrails'][key]]
            weighted = report['weighted_loss_delta']
            report['baseline_guardrails_unproven'] = regressions
            if not regressions and weighted['interval'] and weighted['interval']['high'] < -profile['min_score_gain']:
                qualified.append((weighted['median_delta'], left))
    result['evidence_front'] = [name for name in eligible if name not in dominated]
    alternatives = sorted((delta, name) for delta, name in qualified if name not in dominated)
    if alternatives:
        delta, name = alternatives[0]
        result['recommendation'] = dict(status='MEASURED_ELIGIBLE_ALTERNATIVE', mode=name,
                                        paired_median_weighted_delta=delta)
    elif baseline in eligible:
        result['recommendation'] = dict(status='KEEP_PREVIOUS_VALIDATED_POLICY', mode=baseline,
                                        reason='ALTERNATIVE_NOT_DEMONSTRATED')
    return finish()


def report(path, profile):
    summary = load_summary(path)
    modes = summary['manifest']['modes']
    if (summary['version'] not in (2,3) or summary['status']!='complete' or len(modes)>MAX_POLICIES
            or profile['baseline'] not in modes or summary['manifest']['gates']['baseline']!=profile['baseline']):
        raise ValueError('SLO comparison requires one complete paired matrix of up to eight modes and its declared baseline')
    rows, blockers, diagnostics, costs = evidence(path, summary, profile)
    result = choose(rows, blockers, profile)
    return dict(version=1, evidence='measured_research_lab', experiment_id=summary['experiment_id'],
                environment={name: summary['environment'].get(name) for name in ('architecture','kernel','git_commit','git_dirty')},
                resources=profile['resources'], objectives=profile['objectives'], limits=profile['limits'],
                guardrails=profile['guardrails'], diagnostic_counts=diagnostics, transition_costs=costs, **result)
