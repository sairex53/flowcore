"""Run-level summaries and conservative paired regression tests."""
import json
import math
import statistics
from pathlib import Path

from research_manifest import validate

METRICS = ('p50_ms','p95_ms','p99_ms','total_mbps','background_mbps','jain_index','cpu_seconds',
           'peak_daemon_rss_bytes','adjustments_per_minute','event_loss','response_seconds',
           'first_verdict_seconds','convergence_seconds','failure_ratio','sampled_retransmit_segments',
           'qdisc_drop_delta','netem_drop_delta','peak_backlog_bytes','oscillation_guard',
           'offered_rps','completion_ratio','deadline_attainment','deadline_misses','timeout_requests',
           'request_payload_mbps','dispatch_lag_p99_ms')
RULES = dict(p99_ms=('higher','relative'), total_mbps=('lower','relative'),
             jain_index=('lower','absolute'), cpu_seconds=('higher','relative'),
             adjustments_per_minute=('higher','absolute'), event_loss=('higher','absolute'),
             failure_ratio=('higher','absolute'), oscillation_guard=('higher','absolute'))


def load_summary(path):
    with Path(path).open('rb') as stream:
        payload = stream.read(16*1024*1024+1)
    if len(payload)>16*1024*1024:
        raise ValueError('summary exceeds 16 MiB')
    data = json.loads(payload)
    if not isinstance(data,dict) or data.get('version') not in (1,2) or not isinstance(data.get('environment'),dict):
        raise ValueError('invalid research summary header')
    data['manifest'] = validate(data.get('manifest'))
    if data['manifest']['version'] != data['version']:
        raise ValueError('summary and manifest versions disagree')
    if data['version'] == 2:
        seeds = data.get('block_seeds')
        if (not isinstance(data.get('experiment_id'), str) or not isinstance(seeds, list)
                or len(seeds) != data['manifest']['repetitions']
                or any(type(seed) is not int or not 1<=seed<2**32 for seed in seeds)
                or len(set(seeds)) != len(seeds)):
            raise ValueError('invalid benchmark block identities')
    rows = data.get('runs')
    if not isinstance(rows,list) or len(rows)>360:
        raise ValueError('invalid research run list')
    seen = set()
    for row in rows:
        if not isinstance(row,dict):
            raise ValueError('invalid research run')
        mode,rep = row.get('mode'),row.get('repetition')
        if (mode not in data['manifest']['modes'] or type(rep) is not int
                or not 0<=rep<data['manifest']['repetitions'] or type(row.get('valid')) is not bool
                or row.get('artifact')!=f'r{rep}-{mode}' or (rep,mode) in seen):
            raise ValueError('invalid or repeated research run identity')
        if data['version'] == 2 and row.get('workload_seed') != seeds[rep]:
            raise ValueError('run seed differs from its block')
        seen.add((rep,mode))
    return data


def value(row, key):
    if key=='failure_ratio':
        return row['failed']/row['offered'] if row.get('offered') else None
    v = row.get(key)
    return v if type(v) in (float,int) and math.isfinite(v) else None


def median_ci(values, confidence=.95):
    """Distribution-free median interval from order statistics."""
    n = len(values)
    values = sorted(values)
    result = None
    for k in range(1,n//2+1):
        coverage = 1-2*sum(math.comb(n,j) for j in range(k))/2**n
        if coverage < confidence:
            break
        result = dict(low=values[k-1],high=values[n-k],coverage=coverage)
    return result


def aggregate(summary):
    results = {}
    for mode in summary['manifest']['modes']:
        rows = [r for r in summary['runs'] if r['mode']==mode]
        valid = [r for r in rows if r['valid']]
        metrics = {}
        for key in METRICS:
            values = [v for row in valid if (v:=value(row,key)) is not None]
            metrics[key] = dict(n=len(values),median=statistics.median(values) if values else None,
                                mean=statistics.mean(values) if values else None,
                                variance=statistics.variance(values) if len(values)>1 else None,
                                median_ci_95=median_ci(values))
        results[mode] = dict(valid=len(valid),invalid=len(rows)-len(valid),metrics=metrics)
    return results


def comparable(a,b):
    ignored = {'modes','repetitions','regression'}
    return ({k:v for k,v in a['manifest'].items() if k not in ignored}
            == {k:v for k,v in b['manifest'].items() if k not in ignored}
            and all(a['environment'].get(k)==b['environment'].get(k) for k in ('kernel','architecture','cpus','tc')))


GATE_DIRECTIONS = dict(p99_ms=('higher', 'relative'), total_mbps=('lower', 'relative'),
                       failure_ratio=('higher', 'absolute'), deadline_attainment=('lower', 'absolute'))


def compare_v2(base, candidate, baseline, contender):
    gates = candidate['manifest']['gates']
    margins = gates['margins']
    equal = (base.get('version') == candidate.get('version') == 2
             and base.get('experiment_id') == candidate.get('experiment_id')
             and base.get('block_seeds') == candidate.get('block_seeds')
             and gates['baseline'] == baseline and comparable(base, candidate))
    left = {row['repetition']: row for row in base['runs'] if row['mode'] == baseline}
    right = {row['repetition']: row for row in candidate['runs'] if row['mode'] == contender}
    bad = any(not row['valid'] for row in (*left.values(), *right.values()))
    invalid = not equal or bad
    family = max(1, len(candidate['manifest']['modes'])-1)*len(margins)
    confidence = 1-.05/family
    findings = []
    for metric, margin in margins.items():
        direction, unit = GATE_DIRECTIONS[metric]
        diffs = []
        for rep in sorted(left.keys() & right.keys()) if equal else ():
            a, b = left[rep], right[rep]
            if not a['valid'] or not b['valid'] or a['workload_seed'] != b['workload_seed']:
                continue
            x, y = value(a,metric), value(b,metric)
            if x is None or y is None or (unit == 'relative' and x <= 0):
                continue
            if metric == 'p99_ms' and (a['failed'] or b['failed']):
                continue
            change = y-x if direction == 'higher' else x-y
            diffs.append(change/x if unit == 'relative' else change)
        interval = median_ci(diffs, confidence)
        status = 'INVALID EXPERIMENT' if invalid else 'INCONCLUSIVE'
        if not invalid and len(diffs) >= gates['min_pairs'] and interval:
            if interval['low'] > margin:
                status = 'REGRESSION'
            elif interval['high'] <= margin:
                status = 'PASS'
        findings.append(dict(baseline=baseline, candidate=contender, metric=metric, unit=unit,
                             threshold=margin, pairs=len(diffs), status=status,
                             median_regression=statistics.median(diffs) if diffs else None,
                             variance=statistics.variance(diffs) if len(diffs)>1 else None,
                             simultaneous_median_ci=interval, paired_effects=diffs))
    statuses = {row['status'] for row in findings}
    result = ('INVALID EXPERIMENT' if invalid else 'REGRESSION' if 'REGRESSION' in statuses
              else 'PASS' if statuses == {'PASS'} else 'INCONCLUSIVE')
    latency = next(row for row in findings if row['metric'] == 'p99_ms')
    return dict(outcome=result, comparable_manifest_and_environment=equal,
                method='paired order-statistic median intervals, Bonferroni family coverage >= 95%',
                planned_tests=family, lower_p99_supported=result=='PASS' and
                latency['simultaneous_median_ci']['high']<0, findings=findings)


def compare(base,candidate,base_mode=None,candidate_mode=None):
    if candidate.get('version') == 2:
        if (base_mode is None) != (candidate_mode is None):
            raise ValueError('specify both baseline and candidate mode')
        if not base_mode or base_mode == candidate_mode:
            raise ValueError('version 2 comparisons require two distinct modes in a randomized experiment')
        if base_mode not in base['manifest']['modes'] or candidate_mode not in candidate['manifest']['modes']:
            raise ValueError('unknown comparison mode')
        return compare_v2(base, candidate, base_mode, candidate_mode)
    if (base_mode is None) != (candidate_mode is None):
        raise ValueError('specify both baseline and candidate mode')
    modes = [(base_mode,candidate_mode)] if base_mode else [(m,m) for m in base['manifest']['modes']
                                                           if m in candidate['manifest']['modes']]
    if not modes or any(a not in base['manifest']['modes'] or b not in candidate['manifest']['modes'] for a,b in modes):
        raise ValueError('no matching comparison modes')
    equivalent = comparable(base,candidate)
    # Bonferroni across all planned mode/metric comparisons, including unavailable metrics.
    tests = len(modes)*len(RULES)
    results = []
    for a,b in modes:
        left = {r['repetition']:r for r in base['runs'] if r['mode']==a and r['valid']}
        right = {r['repetition']:r for r in candidate['runs'] if r['mode']==b and r['valid']}
        for key,(direction,unit) in RULES.items():
            threshold = candidate['manifest']['regression'].get(key,0 if key=='oscillation_guard' else .01)
            diffs = []
            for rep in sorted(left.keys() & right.keys()):
                x,y = value(left[rep],key),value(right[rep],key)
                if x is None or y is None or (unit=='relative' and x<=0):
                    continue
                if key=='p99_ms' and (left[rep]['failed'] or right[rep]['failed']):
                    continue  # successful-only tails are not comparable when failures censor them
                diff = (y-x) if direction=='higher' else (x-y)
                diffs.append(diff/x if unit=='relative' else diff)
            n = len(diffs)
            positive = sum(diff>threshold for diff in diffs)
            p = sum(math.comb(n,j) for j in range(positive,n+1))/2**n if n else None
            adjusted = min(1,p*tests) if p is not None else None
            median = statistics.median(diffs) if diffs else None
            interval = median_ci(diffs)
            status = ('confirmed_regression' if equivalent and n>=6 and adjusted<.05 else
                      'no_regression_detected' if equivalent and interval and interval['high']<=threshold else
                      'inconclusive')
            results.append(dict(baseline=a,candidate=b,metric=key,unit=unit,threshold=threshold,pairs=n,
                                median_regression=median,median_ci_95=interval,
                                positive_pairs=positive,adjusted_p=adjusted,status=status))
    return dict(comparable_manifest_and_environment=equivalent,method='one-sided paired sign test with Bonferroni',
                findings=results)
