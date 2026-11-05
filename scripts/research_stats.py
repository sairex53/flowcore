"""Run-level summaries and conservative paired regression tests."""
import json
import math
import statistics
from pathlib import Path

from research_manifest import validate

METRICS = ('p50_ms','p95_ms','p99_ms','total_mbps','background_mbps','jain_index','cpu_seconds',
           'peak_daemon_rss_bytes','adjustments_per_minute','event_loss','response_seconds',
           'first_verdict_seconds','convergence_seconds','failure_ratio','sampled_retransmit_segments',
           'qdisc_drop_delta','netem_drop_delta','peak_backlog_bytes','oscillation_guard')
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
    if not isinstance(data,dict) or data.get('version')!=1 or not isinstance(data.get('environment'),dict):
        raise ValueError('invalid research summary header')
    data['manifest'] = validate(data.get('manifest'))
    rows = data.get('runs')
    if not isinstance(rows,list) or len(rows)>210:
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
        seen.add((rep,mode))
    return data


def value(row, key):
    if key=='failure_ratio':
        return row['failed']/row['offered'] if row.get('offered') else None
    v = row.get(key)
    return v if type(v) in (float,int) and math.isfinite(v) else None


def median_ci(values):
    """Distribution-free median interval from order statistics."""
    n = len(values)
    values = sorted(values)
    result = None
    for k in range(1,n//2+1):
        coverage = 1-2*sum(math.comb(n,j) for j in range(k))/2**n
        if coverage < .95:
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


def compare(base,candidate,base_mode=None,candidate_mode=None):
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
