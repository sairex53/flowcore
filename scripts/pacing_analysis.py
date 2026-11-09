"""Reproduce pacing phase goodput, paired differences and monotonic lock checks."""
import argparse
from collections import Counter
import json
from pathlib import Path
from statistics import median

from benchmark import receiver_metrics
from nextgen_report import read_records, report
from research_stats import load_summary, median_ci


def details(path):
    summary = load_summary(path)
    result = report(path)
    baseline = summary['manifest'].get('gates', {}).get('baseline')
    valid = [r for r in summary['runs'] if r['valid']]
    for mode, stats in result['by_mode'].items():
        rows = [r for r in valid if r['mode'] == mode]
        times = {name: [] for name in ('lock_wait_us', 'pin_us', 'precheck_us', 'setsockopt_us',
                                       'readback_us', 'htb_resize_ms', 'queue_age_ms', 'planning_us')}
        reasons, phase_rates = Counter(), {}
        overlap = writes = 0
        for row in rows:
            directory = path.parent/row['artifact']
            if (ledger := directory/'receiver.jsonl').is_file():
                start = row['start']
                for index, phase in enumerate(summary['manifest']['workload']['phases']):
                    end = start + phase['seconds']
                    phase_rates.setdefault(index, []).append(receiver_metrics(ledger, start, end)['background_mbps'])
                    start = end
            class_intervals = []
            for transition in read_records(directory/'transitions.jsonl'):
                lock = transition.get('actuator_lock') or {}
                if 'release_begin_ns' in lock:
                    class_intervals.append((lock['acquired_ns'], lock['release_begin_ns']))
                if resize := transition.get('htb_resize'):
                    times['htb_resize_ms'].append((resize['verified_ns']-resize['started_ns'])/1e6)
            for sample in read_records(directory/'socket-pacing.jsonl'):
                if reason := sample.get('decision_reason'):
                    reasons[reason] += 1
                timing = sample.get('timing') or {}
                if timing.get('queue_sample_ns'):
                    times['queue_age_ms'].append((timing['feedback_read_ns']-timing['queue_sample_ns'])/1e6)
                if timing.get('plan_end_ns'):
                    times['planning_us'].append((timing['plan_end_ns']-timing['plan_begin_ns'])/1e3)
            for action in read_records(directory/'socket-actuation.jsonl'):
                if lock := action.get('lock'):
                    times['lock_wait_us'].append((lock['acquired_ns']-lock['requested_ns'])/1e3)
                for step in action['steps']:
                    if not step['write_attempted']:
                        continue
                    writes += 1
                    overlap += any(lo < step['completed_ns'] and step['started_ns'] < hi
                                   for lo, hi in class_intervals)
                    for key in ('pin', 'precheck', 'setsockopt', 'readback'):
                        times[key+'_us'].append(step[key+'_ns']/1e3)
        stats['pacing_audit'] = dict(
            socket_writes=writes, socket_writes_overlapping_class_lock=overlap, decision_reasons=dict(reasons),
            median_timing={key: median(values) if values else None for key, values in times.items()},
            median_background_mbps_by_phase={index: median(values) for index, values in phase_rates.items()})
        if baseline and mode != baseline:
            reference = {(r['repetition'], r.get('workload_seed')): r for r in valid if r['mode'] == baseline}
            pairs = [(reference[key], r) for r in rows
                     if (key := (r['repetition'], r.get('workload_seed'))) in reference]
            metrics = {}
            for key in ('background_mbps', 'jain_index', 'cpu_seconds'):
                differences = [b[key]-a[key] for a, b in pairs if a.get(key) is not None and b.get(key) is not None]
                metrics[key] = dict(pairs=len(differences), median=median(differences) if differences else None,
                                    median_ci_95=median_ci(differences))
            stats['exploratory_paired_differences'] = metrics
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('summary', type=Path)
    print(json.dumps(details(parser.parse_args().summary), indent=2))
