"""Summarize controller evidence from saved Research Lab runs without touching the network."""
import argparse
import json
import math
import statistics
from pathlib import Path

NANO = 10**9
LEAVES = {'critical': '1:10', 'background': '1:20'}


def read(path):
    return json.loads(path.read_text())


def lines(path):
    with path.open() as stream:
        return [json.loads(line) for line in stream]


def distribution(values, scale=1):
    values = sorted(v/scale for v in values if v is not None)
    if not values:
        return None
    return dict(n=len(values), minimum=values[0], median=statistics.median(values),
                p95=values[math.ceil(.95*len(values))-1], maximum=values[-1])


def span_delta(rows, field):
    first, last = rows[0].get(field), rows[-1].get(field)
    return last-first if type(first) is int and type(last) is int and last >= first else None


def analyze_run(directory, result, manifest):
    trace = lines(directory/'timeline.jsonl')
    decisions = read(directory/'decisions.json')
    start_ns = round(result['start']*NANO)
    phases = [start_ns]
    for phase in manifest['workload']['phases']:
        phases.append(phases[-1]+phase['seconds']*NANO)
    unique = {row['sample_ns']: row['tc'] for row in trace if row['sample_ns'] is not None and row['tc']}
    samples = sorted(unique.items())
    queue = {}
    for name, handle in LEAVES.items():
        leaf = [(stamp, q) for stamp, tc in samples for q in tc['qdisc'] if q.get('parent') == handle]
        if not leaf:
            continue
        stamps, rows = zip(*leaf)
        classes = [tc['traffic_classes'][name] for _,tc in samples if name in tc['traffic_classes']]
        stats = [row.get('stats', {}) for row in classes]
        cfg = classes[0]
        first, last = stamps[0], stamps[-1]
        bytes_sent = span_delta(stats,'bytes')
        queue[name] = dict(samples=len(rows), backlogged=sum(q.get('backlog',0)>0 for q in rows),
                           peak_bytes=max(q.get('backlog',0) for q in rows),
                           empty_observed_after_nonempty=sum(a.get('backlog',0)>0 and b.get('backlog',0)==0
                               for a,b in zip(rows,rows[1:])),
                           sample_gap_ms=distribution([b-a for a,b in zip(stamps,stamps[1:])],1e6),
                           guarantee_kbit=cfg['rate']/125, ceil_kbit=cfg['ceil']/125, priority=cfg['prio'],
                           borrowed=span_delta(stats,'borrowed'), lended=span_delta(stats,'lended'),
                           sampled_class_mbps=(bytes_sent*8*NANO/(last-first)/1e6
                                               if bytes_sent is not None and last>first else None),
                           drops=span_delta(rows,'drops'), ecn_marks=span_delta(rows,'ecn_mark'))
        if queue[name]['peak_bytes'] and cfg['rate']:
            queue[name]['peak_backlog_at_guarantee_ms'] = 1000*queue[name]['peak_bytes']/cfg['rate']
    controller = {row['state'].get('timing',{}).get('time_ns'): row['state']['timing']
                  for row in trace if row['state'].get('timing')}
    polls = {row['telemetry_timing']['time_ns']: row['telemetry_timing']
             for row in trace if row.get('telemetry_timing')}
    timings = {
        'runner_ipc_ms': distribution([row.get('runner_ipc_ns') for row in trace],1e6),
        'tc_counters_ms': distribution([row.get('counters_ns') for row in polls.values()],1e6),
        'observer_sample_ms': distribution([row.get('observer_ns') for row in polls.values()],1e6),
    }
    for key in ('aggregation_ns','policy_ns','telemetry_age_ns'):
        timings[key.removesuffix('_ns')+'_ms'] = distribution([row.get(key) for row in controller.values()],1e6)
    for key in ('pre_read_ns','apply_ns','verify_ns','refresh_ns'):
        timings['actuator_'+key.removesuffix('_ns')+'_ms'] = distribution([
            row['timing'].get(key) for row in decisions if row.get('timing')],1e6)
    for label, key in (('verdict_ms','time_to_evaluate_ns'), ('verdict_lag_ms','verdict_lag_ns'),
                       ('post_aggregation_ms','post_aggregation_ns'), ('post_policy_ms','post_policy_ns')):
        timings[label] = distribution([row.get(key) for row in decisions],1e6)
    probes = []
    for row in decisions:
        if row['result'] not in ('pending','hypothetical','kept','rolled_back','inconclusive','verified','released','oscillation_guard'):
            continue
        stamp = row['time_ns']
        previous = next((tc for when,tc in reversed(samples) if when <= stamp), {})
        critical = next((q for q in previous.get('qdisc',[]) if q.get('parent') == LEAVES['critical']), {})
        probes.append(dict(at_seconds=(stamp-start_ns)/NANO, result=row['result'],
                           latest_sampled_critical_backlog_bytes=critical.get('backlog'),
                           phases_in_post_window=[(when-start_ns)/NANO for when in phases[1:-1]
                               if row.get('post_start_ns',stamp) < when < row.get('post_end_ns',stamp)],
                           service=row.get('service'), timing=row.get('timing'),
                           before=row.get('before'), measured=row.get('measured'),
                           allocation=row.get('allocation',row.get('proposed'))))
    return dict(artifact=result['artifact'], mode=result['mode'], p99_ms=result['p99_ms'],
                deadline_attainment=result.get('deadline_attainment'), failed=result['failed'],
                background_mbps=result['background_mbps'], queue=queue, timing=timings,
                dispatch_p99_ms=result.get('dispatch_lag_p99_ms'),
                app_reporting=result.get('reports_by_class'), decisions=probes,
                controller_ticks=list(controller.values()))


def analyze(path):
    data = read(path/'summary.json')
    return dict(directory=str(path), experiment_id=data.get('experiment_id'), environment=data['environment'],
                runs=[analyze_run(path/row['artifact'],row,data['manifest']) for row in data['runs'] if row['valid']])


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directories', nargs='+', type=Path)
    args = parser.parse_args()
    print(json.dumps([analyze(path) for path in args.directories],indent=2,allow_nan=False))
