"""Aggregate saved causal prediction counters and paired run-level effects; no fitting."""
import argparse
import json
import statistics
from pathlib import Path
from predictive import METHODS
from research_stats import median_ci

COUNTERS=('tp','fp','fn','tn','censored','alerts','warning_samples','early_alerts','early_evaluated',
          'early_hits','onsets','missed_onsets','lead_sum_seconds','lead_count')


def analyze(directory):
    summary=json.loads((directory/'summary.json').read_text())
    results=[]
    for run in summary['runs']:
        if not run['valid']: continue
        path=directory/run['artifact']
        frames=[json.loads(line) for line in (path/'timeline.jsonl').read_text().splitlines()]
        segments=[];last=None
        timing={}
        for row in frames:
            telemetry=row.get('telemetry_timing',{})
            if telemetry.get('prediction_ns') is not None:
                timing[telemetry['time_ns']]=telemetry['prediction_ns']/1e6
            current=row['state'].get('prediction')
            if not current: continue
            if last and current['instance']!=last['instance']: segments.append(last)
            last=current
        if last: segments.append(last)
        counts={method:{key:0 for key in COUNTERS} for method in METHODS}
        bins={method:[dict(n=0,events=0) for _ in range(5)] for method in METHODS}
        for segment in segments:
            for method,values in segment['metrics'].items():
                for key in COUNTERS: counts[method][key]+=values[key]
                for target,source in zip(bins[method],values['bins']):
                    target['n']+=source['n'];target['events']+=source['events']
        for method,value in counts.items():
            value.update(precision=value['tp']/(value['tp']+value['fp']) if value['tp']+value['fp'] else None,
                         recall=value['tp']/(value['tp']+value['fn']) if value['tp']+value['fn'] else None,
                         onset_recall=(value['onsets']-value['missed_onsets'])/value['onsets'] if value['onsets'] else None,
                         bins=bins[method])
        decisions=json.loads((path/'decisions.json').read_text())
        proactive=[row for row in decisions if row['result'] in ('pending','hypothetical') and
                   any(item.get('proactive') for item in row.get('estimator',{}).values())]
        results.append(dict(mode=run['mode'],repetition=run['repetition'],metrics=counts,
                            segments=len(segments),pending_censored_at_end=sum(segment['pending'] for segment in segments),
                            proactive_probes=len(proactive),
                            prediction_collection_ms=dict(median=statistics.median(timing.values()) if timing else None,
                                maximum=max(timing.values(),default=None)),artifact=run['artifact']))
    effects={}
    for baseline in ('static','slo-aware','queue-aware'):
        for metric in ('p99_ms','deadline_attainment','background_mbps','cpu_seconds'):
            changes=[]
            for row in summary['runs']:
                if not row['valid'] or row['mode']!='predictive': continue
                other=next((item for item in summary['runs'] if item['valid'] and item['mode']==baseline
                            and item['repetition']==row['repetition']),None)
                if other and other.get(metric) and row.get(metric) is not None:
                    changes.append((row[metric]/other[metric]-1)*100)
            if changes:
                effects[baseline+'_'+metric]=dict(pairs=len(changes),median_percent=statistics.median(changes),
                                               median_interval=median_ci(changes))
    return dict(source=str(directory),run_predictions=results,paired_effects=effects,
                warning='Forecasts overlap; confusion counts are descriptive, not independent trials. '
                        'Risk bins are empirical checks of an uncalibrated index, not fitted probabilities.')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory',type=Path)
    args=parser.parse_args()
    print(json.dumps(analyze(args.directory),indent=2,allow_nan=False))
