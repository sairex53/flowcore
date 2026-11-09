"""Summarize integrated control costs with measured benchmark outputs."""
import json
from statistics import median

from research_stats import aggregate, compare_v2, load_summary


def read_records(path):
    try:
        with path.open('rb') as stream:
            raw = stream.read(4*1024*1024+1)
    except FileNotFoundError:
        return []
    if len(raw) > 4*1024*1024:
        raise ValueError(f'oversize measurement artifact: {path.name}')
    return [json.loads(line) for line in raw.splitlines()]


def report(path):
    summary = load_summary(path)
    manifest = summary['manifest']
    baseline = manifest.get('gates', {}).get('baseline')
    by_mode = aggregate(summary)
    for mode, stats in by_mode.items():
        rows = [r for r in summary['runs'] if r['mode'] == mode and r['valid']]
        pacing = [r['socket_pacing'] for r in rows if r.get('socket_pacing')]
        times = []
        cohort_times = []
        ipc = []
        observer = []
        for run in rows:
            directory = path.parent/run['artifact']
            timeline = read_records(directory/'timeline.jsonl')
            ipc.extend(sample['runner_ipc_ns']/1e6 for sample in timeline if 'runner_ipc_ns' in sample)
            observer.extend(sample['telemetry_timing']['observer_ns']/1e6
                            for sample in timeline if 'observer_ns' in sample.get('telemetry_timing', {}))
            if mode != 'integrated':
                continue
            cohort = read_records(directory/'socket-cohort.jsonl')
            if cohort:
                start_ns, end_ns = round(run['start']*1e9), round(run['end']*1e9)
                cohort_times.extend((event['completed_ns']-event['started_ns'])/1e6
                                    for event in cohort if event.get('verified') is True
                                    and start_ns <= event['started_ns'] < end_ns)
            transitions = read_records(directory/'transitions.jsonl')
            pacing_rows = read_records(directory/'socket-pacing.jsonl')
            for generation, transition in enumerate(transitions[1:], 1):
                start = transition.get('kernel_confirmed_ns')
                if not start:
                    continue
                verified = next((row['verified_ns'] for row in pacing_rows
                                 if row.get('verified') is True and row['decision'] == 'fallback'
                                 and (row.get('integrated_scope') or {}).get('generation') == generation
                                 and row['verified_ns'] >= start), None)
                if verified is not None:
                    times.append((verified-start)/1e6)
        ratios = [item['measured_to_configured_ratio'] for run in rows
                  for item in run.get('observed_rate_windows', [])
                  if item.get('measured_to_configured_ratio') is not None]
        def pacing_median(key, scale=1):
            values = [sample[key]/scale for sample in pacing if key in sample]
            return median(values) if values else None

        stats['control_cost'] = dict(
            socket_updates=sum(p['socket_actions'] for p in pacing),
            socket_rollbacks=sum(p['rollbacks'] for p in pacing),
            socket_fallbacks=sum(p['fallbacks'] for p in pacing),
            failed_socket_updates=sum(p.get('apply_failures', 0) for p in pacing),
            decision_cpu_median_ms=pacing_median('decision_cpu_ns', 1e6),
            actuation_wall_median_ms=pacing_median('actuation_wall_ns', 1e6),
            sample_cpu_median_ms=pacing_median('sampler_cpu_ms'),
            sampling_rpc_median_ms=median(ipc) if ipc else None,
            observer_median_ms=median(observer) if observer else None,
            link_utilization_ratio_median=median(ratios) if ratios else None,
            cohort_compensation_samples=len(cohort_times),
            cohort_compensation_median_ms=median(cohort_times) if cohort_times else None,
            transition_compensation_samples=len(times),
            transition_compensation_median_ms=median(times) if times else None,
        )
    return dict(schema=1, experiment_id=summary.get('experiment_id'), status=summary['status'],
                environment=summary['environment'], by_mode=by_mode,
                comparisons={mode: compare_v2(summary, summary, baseline, mode)
                             for mode in by_mode if baseline is not None and mode != baseline
                             and summary['version'] >= 2})
