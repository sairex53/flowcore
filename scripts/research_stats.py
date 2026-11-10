"""Run-level summaries and conservative paired regression tests."""
import hashlib
import json
import math
import statistics
from pathlib import Path

from research_manifest import validate

METRICS = ('p50_ms','p95_ms','p99_ms','total_mbps','background_mbps','jain_index','cpu_seconds',
           'peak_daemon_rss_bytes','adjustments_per_minute','event_loss','response_seconds',
           'first_verdict_seconds','convergence_seconds','failure_ratio','sampled_retransmit_segments',
           'qdisc_drop_delta','netem_drop_delta','peak_backlog_bytes','oscillation_guard',
           'offered_rps','completion_ratio','deadline_attainment','deadline_misses','timeout_requests','udp_mbps',
           'request_payload_mbps','dispatch_lag_p99_ms')
RULES = dict(p99_ms=('higher','relative'), total_mbps=('lower','relative'),
             jain_index=('lower','absolute'), cpu_seconds=('higher','relative'),
             adjustments_per_minute=('higher','absolute'), event_loss=('higher','absolute'),
             failure_ratio=('higher','absolute'), oscillation_guard=('higher','absolute'))


def load_summary(path):
    path = Path(path)
    with path.open('rb') as stream:
        payload = stream.read(16*1024*1024+1)
    if len(payload)>16*1024*1024:
        raise ValueError('summary exceeds 16 MiB')
    data = json.loads(payload)
    if not isinstance(data,dict) or data.get('version') not in (1,2,3) or not isinstance(data.get('environment'),dict):
        raise ValueError('invalid research summary header')
    data['manifest'] = validate(data.get('manifest'))
    if data['manifest'].get('integrity'):
        from measurement import read_bounded
        expected = data['environment'].get('experiment_manifest_sha256')
        manifest_bytes = read_bounded(path.parent/'experiment.json', 65536)
        recorded = validate(json.loads(manifest_bytes))
        canonical = json.dumps(recorded,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
        if recorded != data['manifest']:
            raise ValueError('experiment manifest differs from integrity summary')
        if expected is not None and (not isinstance(expected,str)
                or hashlib.sha256(canonical).hexdigest()!=expected):
            raise ValueError('experiment manifest checksum differs from integrity summary')
        data['experiment_manifest_hash_verified'] = expected is not None
    if data['manifest']['version'] != data['version']:
        raise ValueError('summary and manifest versions disagree')
    if data['version'] >= 2:
        seeds = data.get('block_seeds')
        if (not isinstance(data.get('experiment_id'), str) or not isinstance(seeds, list)
                or len(seeds) != data['manifest']['repetitions']
                or any(type(seed) is not int or not 1<=seed<2**32 for seed in seeds)
                or len(set(seeds)) != len(seeds)):
            raise ValueError('invalid benchmark block identities')
    rows = data.get('runs')
    if not isinstance(rows,list) or len(rows)>data['manifest']['repetitions']*len(data['manifest']['modes']):
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
        if data['version'] >= 2:
            if row.get('workload_seed') != seeds[rep]:
                raise ValueError('run seed differs from its block')
            if row['valid']:
                counts = [row.get(key) for key in ('offered', 'completed', 'failed')]
                if (any(type(count) is not int or count < 0 for count in counts)
                        or not counts[0] or counts[0] != counts[1]+counts[2]):
                    raise ValueError('invalid offered, completed or failed accounting')
                if any(row.get(key) is not None and (type(row[key]) not in (int, float)
                        or not math.isfinite(row[key]) or row[key]<0) for key in METRICS):
                    raise ValueError('invalid numeric run metric')
                deadline = row.get('deadline_attainment')
                if deadline is not None and deadline > 1:
                    raise ValueError('deadline attainment outside unit interval')
        seen.add((rep,mode))
    if data['manifest'].get('integrity'):
        from measurement import verify
        for row in rows:
            try:
                row['artifact_integrity'] = verify(path.parent/row['artifact'], row)
            except (OSError, ValueError, TypeError, KeyError) as error:
                row['valid'] = False
                row['artifact_integrity'] = dict(verified=False, error=str(error))
                measurement = row.setdefault('measurement', dict(version=3, state='INVALID_INFRASTRUCTURE',
                    issues=[]))
                measurement['state'] = 'INVALID_INFRASTRUCTURE'
                measurement.setdefault('issues', []).append(dict(code='ARTIFACT_INTEGRITY', detail=str(error)))
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
    equal = (base.get('version') == candidate.get('version') and candidate.get('version') >= 2
             and base.get('experiment_id') == candidate.get('experiment_id')
             and base.get('block_seeds') == candidate.get('block_seeds')
             and gates['baseline'] == baseline and comparable(base, candidate))
    left = {row['repetition']: row for row in base['runs'] if row['mode'] == baseline}
    right = {row['repetition']: row for row in candidate['runs'] if row['mode'] == contender}
    complete = (base.get('status') == candidate.get('status') == 'complete'
                and len(left) == base['manifest']['repetitions']
                and len(right) == candidate['manifest']['repetitions'])
    bad = any(not row['valid'] for row in (*left.values(), *right.values()))
    invalid = not equal or not complete or bad
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


INTEGRITY_DIRECTIONS = {
    'p99_ms': ('higher', 'relative'), 'deadline_attainment': ('lower', 'absolute'),
    'failure_ratio': ('higher', 'absolute'), 'total_mbps': ('lower', 'relative'),
    'background_mbps': ('lower', 'relative'), 'jain_index': ('lower', 'absolute'),
    'cpu_seconds': ('higher', 'relative'),
}


def _bootstrap_median(values, resamples, alpha, seed_text):
    if not values or resamples <= 0:
        return None
    import hashlib
    import random
    rng = random.Random(int.from_bytes(hashlib.sha256(seed_text.encode()).digest()[:8], 'big'))
    n = len(values)
    samples = sorted(statistics.median(values[rng.randrange(n)] for _ in range(n))
                     for _ in range(resamples))
    low = max(0, min(resamples-1, math.floor((alpha/2)*resamples)))
    high = max(0, min(resamples-1, math.ceil((1-alpha/2)*resamples)-1))
    return dict(low=samples[low], high=samples[high], confidence=1-alpha,
                method='paired bootstrap percentile interval', resamples=resamples)


def _sign_power(n, assumed_probability, alpha):
    if n <= 0:
        return dict(power=None, minimum_pairs_for_target=None, assumed_direction_probability=assumed_probability)
    cutoff = next((k for k in range(1,n+1)
                   if sum(math.comb(n,j) for j in range(k,n+1))/2**n <= alpha), None)
    power = (sum(math.comb(n,j)*assumed_probability**j*(1-assumed_probability)**(n-j)
                 for j in range(cutoff,n+1)) if cutoff is not None else 0.0)
    minimum = None
    for size in range(1,257):
        threshold = next((k for k in range(1,size+1)
                          if sum(math.comb(size,j) for j in range(k,size+1))/2**size <= alpha), None)
        achieved = (sum(math.comb(size,j)*assumed_probability**j*(1-assumed_probability)**(size-j)
                        for j in range(threshold,size+1)) if threshold is not None else 0.0)
        if achieved >= .8:
            minimum = size
            break
    return dict(power=power, minimum_pairs_for_target=minimum,
                assumed_direction_probability=assumed_probability, significance_alpha=alpha)


def _integrity_compatible(base, candidate):
    ignored = {'modes', 'repetitions', 'seed', 'regression', 'gates'}
    left = {k:v for k,v in base['manifest'].items() if k not in ignored}
    right = {k:v for k,v in candidate['manifest'].items() if k not in ignored}
    # Paired revision comparisons must be from the same boot, kernel and host
    # resource envelope. Build identity may differ intentionally.
    env = ('kernel','architecture','cpus','tc','boot_fingerprint','host',
           'build_configuration','network_sysctls')
    same_campaign = (base.get('experiment_id') == candidate.get('experiment_id')
                     and base.get('environment') == candidate.get('environment'))
    fingerprints_match = (all(base['environment'].get(k) == candidate['environment'].get(k) for k in env)
                          and all(base['environment'].get(k) is not None for k in env))
    return (left == right and base.get('version') == candidate.get('version') in (2,3)
            and (same_campaign or fingerprints_match)
            and base.get('block_seeds') == candidate.get('block_seeds'))


def compare_integrity(base, candidate, baseline, contender):
    """Paired run-level V3 gates. Invalid blocks invalidate the comparison, never disappear."""
    planned = max(1, len(candidate['manifest']['modes'])-1)
    margins = dict(candidate['manifest']['gates']['margins'])
    margins.update(candidate['manifest']['integrity']['extra_gates'])
    tests = max(1, planned*len(margins))
    alpha = .05/tests
    compatible = _integrity_compatible(base, candidate)
    left_rows = [row for row in base['runs'] if row['mode'] == baseline]
    right_rows = [row for row in candidate['runs'] if row['mode'] == contender]
    left = {row['repetition']:row for row in left_rows}
    right = {row['repetition']:row for row in right_rows}
    seeds_match = (set(left) == set(right) and
                   all(left[i].get('workload_seed') == right[i].get('workload_seed') for i in left))
    complete = (base.get('status') == candidate.get('status') == 'complete'
                and len(left) == base['manifest']['repetitions']
                and len(right) == candidate['manifest']['repetitions'])
    selected_rows = (*left_rows,*right_rows)
    failed_workloads = [row['artifact'] for row in selected_rows
                        if row.get('measurement',{}).get('state') == 'FAILED_WORKLOAD']
    invalid_rows = [row['artifact'] for row in selected_rows
                    if (not row.get('valid') and row.get('measurement',{}).get('state') != 'FAILED_WORKLOAD')
                    or row.get('measurement',{}).get('state') == 'INVALID_INFRASTRUCTURE'
                    or row.get('artifact_integrity',{}).get('verified') is False]
    schedule_mismatches = []
    if candidate['manifest']['integrity']['load'] == 'open':
        for repetition in sorted(left.keys() & right.keys()):
            a,b = left[repetition],right[repetition]
            if any(not row.get('valid') or row.get('measurement',{}).get('state') == 'FAILED_WORKLOAD'
                   for row in (a,b)):
                continue
            a_hash = a.get('measurement',{}).get('timing',{}).get('scheduled_workload_sha256')
            b_hash = b.get('measurement',{}).get('timing',{}).get('scheduled_workload_sha256')
            if not a_hash or a_hash != b_hash:
                schedule_mismatches.append(f'{a["artifact"]}<->{b["artifact"]}')
    invalid_rows.extend(f'{pair}: OPEN_SCHEDULE_FINGERPRINT_MISMATCH' for pair in schedule_mismatches)
    insufficient_runs = [row['artifact'] for row in selected_rows
                         if row.get('measurement',{}).get('state') == 'INCONCLUSIVE']
    invalid = not compatible or not seeds_match or not complete or bool(invalid_rows)
    min_pairs = candidate['manifest']['gates']['min_pairs']
    repetitions = candidate['manifest']['integrity']['bootstrap_resamples']
    assumed = candidate['manifest']['integrity']['power_direction_probability']
    findings = []
    for metric, margin in margins.items():
        if metric not in INTEGRITY_DIRECTIONS:
            raise ValueError(f'unsupported integrity gate: {metric}')
        direction, unit = INTEGRITY_DIRECTIONS[metric]
        diffs, pairs, censored, reasons = [], [], [], []
        for rep in sorted(left.keys() & right.keys()):
            a,b = left[rep],right[rep]
            if not a.get('valid') or not b.get('valid'):
                continue
            x,y = value(a,metric),value(b,metric)
            if x is None or y is None or (unit == 'relative' and x <= 0):
                reasons.append(f'{a["artifact"]}: missing {metric}')
                continue
            if metric == 'p99_ms' and (a['failed'] or b['failed']):
                censored.append(rep)
                continue
            # Positive values always mean the candidate regressed.
            change = y-x if direction == 'higher' else x-y
            effect = change/x if unit == 'relative' else change
            diffs.append(effect)
            pairs.append(dict(repetition=rep, seed=a['workload_seed'], baseline=x,
                              candidate=y, regression=effect))
        seed_text = f'{candidate.get("experiment_id")}:{baseline}:{contender}:{metric}'
        interval = (_bootstrap_median(diffs,repetitions,alpha,seed_text)
                    if len(diffs)>=min_pairs else None)
        if invalid:
            status = 'INVALID EXPERIMENT'
        elif failed_workloads:
            status = 'FAILED_WORKLOAD'
        elif insufficient_runs or len(diffs)<min_pairs or interval is None or censored or reasons:
            status = 'INCONCLUSIVE'
        elif interval['low'] > margin:
            status = 'REGRESSION'
        elif interval['high'] <= margin:
            status = 'PASS'
        else:
            status = 'INCONCLUSIVE'
        findings.append(dict(baseline=baseline,candidate=contender,metric=metric,unit=unit,
            threshold=margin,pairs=len(diffs),required_pairs=min_pairs,status=status,
            median_regression=statistics.median(diffs) if diffs else None,
            variance=statistics.variance(diffs) if len(diffs)>1 else None,
            simultaneous_median_ci=interval,paired_effects=[item['regression'] for item in pairs],
            paired_values=pairs,censored_p99_repetitions=censored,missing_data=reasons,
            power=_sign_power(len(diffs),assumed,alpha)))
    states = {item['status'] for item in findings}
    outcome = ('INVALID EXPERIMENT' if invalid else 'FAILED_WORKLOAD' if failed_workloads
               else 'REGRESSION' if 'REGRESSION' in states else
               'PASS' if states == {'PASS'} else 'INCONCLUSIVE')
    return dict(outcome=outcome,
        state={'INVALID EXPERIMENT':'INVALID_INFRASTRUCTURE','FAILED_WORKLOAD':'FAILED_WORKLOAD',
               'REGRESSION':'COMPLETED_WITH_REGRESSION','PASS':'VALID',
               'INCONCLUSIVE':'INCONCLUSIVE'}[outcome],
        comparable_manifest_and_environment=compatible,
        complete_paired_blocks=complete and seeds_match,invalid_blocks=invalid_rows,
        failed_workload_blocks=failed_workloads,insufficient_data_blocks=insufficient_runs,
        schedule_mismatches=schedule_mismatches,
        method='paired bootstrap of independent run-level effects; family-wise Bonferroni percentile intervals',
        planned_tests=tests,confidence_family=1-.05/tests,minimum_pairs=min_pairs,
        lower_p99_supported=outcome=='PASS' and next(
            (item['simultaneous_median_ci']['high']<0 for item in findings if item['metric']=='p99_ms'
             and item['simultaneous_median_ci']),False),findings=findings)


def compare(base,candidate,base_mode=None,candidate_mode=None):
    if candidate.get('version') in (2,3):
        if (base_mode is None) != (candidate_mode is None):
            raise ValueError('specify both baseline and candidate mode')
        if not base_mode or not candidate_mode:
            raise ValueError('version 2 comparisons require baseline and candidate modes')
        if base_mode not in base['manifest']['modes'] or candidate_mode not in candidate['manifest']['modes']:
            raise ValueError('unknown comparison mode')
        if base['manifest'].get('integrity') or candidate['manifest'].get('integrity'):
            if not base['manifest'].get('integrity') or not candidate['manifest'].get('integrity'):
                raise ValueError('integrity comparisons require instrumentation on both summaries')
            return compare_integrity(base, candidate, base_mode, candidate_mode)
        if base_mode == candidate_mode:
            raise ValueError('version 2 comparisons require two distinct modes in a randomized experiment')
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



def investigations(summary):
    """Annotate runs without deleting them or changing gate inputs."""
    flagged = {}
    for row in summary['runs']:
        if row.get('warnings'):
            flagged[row['artifact']] = list(row['warnings'])
    for mode in summary['manifest']['modes']:
        rows = [row for row in summary['runs'] if row['mode']==mode and row['valid']]
        for metric in ('p99_ms','failure_ratio','total_mbps','dispatch_lag_p99_ms'):
            usable = [(row, v) for row in rows if (v := value(row,metric)) is not None]
            if len(usable) < 6:
                continue
            first, _, third = statistics.quantiles([v for _,v in usable],method='inclusive')
            width = third-first
            if width:
                for row, v in usable:
                    if v < first-1.5*width or v > third+1.5*width:
                        flagged.setdefault(row['artifact'],[]).append(f'{metric} outside Tukey fences')
    return flagged
