"""Frozen predictive evaluation: separate static pilot, shadow, paired trials, unseen stress."""
import argparse
import hashlib
import json
import os
import random
import time
from pathlib import Path
from benchmark import ROOT, RESULTS, owned_directory
from research_manifest import load_manifest
from research_runner import exclusive, execute, save
from research_report import report
from research_stats import load_summary
from predictive_report import analyze


def select_static(data,evaluation):
    pilot=data['manifest']
    if data.get('status')!='complete' or set(pilot['modes'])!={'static20','static30','static40'}:
        raise ValueError('complete static20/30/40 pilot required')
    if pilot['repetitions']<2 or pilot['seed']==evaluation['seed']:
        raise ValueError('separate pilot blocks required')
    for field in ('network','workload','faults','duration_seconds','controller'):
        if pilot[field]!=evaluation[field]: raise ValueError('pilot scenario mismatch')
    eval_seeds=random.Random(evaluation['seed']).sample(range(1,2**32),evaluation['repetitions'])
    if set(data['block_seeds']) & set(eval_seeds): raise ValueError('pilot/evaluation seed overlap')
    active=sum(p['seconds'] for p in pilot['workload']['phases'] if p['background_streams'])
    floor=.2*pilot['network']['bandwidth_kbit']/1000*active/pilot['duration_seconds']
    import statistics
    candidates=[]
    for mode in pilot['modes']:
        rows=[row for row in data['runs'] if row['mode']==mode and row['valid']]
        if len(rows)!=pilot['repetitions'] or any(row['failed'] for row in rows): continue
        background=statistics.median(row['background_mbps'] for row in rows)
        if background<floor: continue
        candidates.append(dict(mode=mode,p99_ms=statistics.median(row['p99_ms'] for row in rows),
            deadline_attainment=statistics.median(row['deadline_attainment'] for row in rows),
            background_mbps=background))
    if not candidates: raise ValueError('no static pilot meets declared failure/background gates')
    winner=min(candidates,key=lambda row:(-row['deadline_attainment'],row['p99_ms'],row['mode']))
    return dict(critical_pct=int(winner['mode'].removeprefix('static')),candidates=candidates,
                background_floor_mbps=floor,rule='zero failures; active-duty 20% background floor; '
                    'highest median deadline attainment, then lowest median P99, then mode name')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repetitions',type=int,default=10)
    parser.add_argument('--pilot',type=Path,help='reuse a complete, separately seeded local static-only pilot directory')
    args=parser.parse_args()
    if os.geteuid()!=0 or not 1<=args.repetitions<=30: parser.error('isolated root runner; 1..30 repetitions')
    with exclusive():
        output=RESULTS/f'predictive-suite-{time.time_ns()}';owned_directory(output)
        index=dict(directory=str(output),status='running',experiments=[])
        def experiment(label,manifest):
            path,data=execute(manifest,label=label,parent=output)
            report(path/'summary.json')
            save(path/'predictions.json',analyze(path))
            index['experiments'].append(dict(label=label,directory=str(path),
                valid=sum(row['valid'] for row in data['runs']),invalid=sum(not row['valid'] for row in data['runs'])))
            save(output/'index.json',index)
            return path,data
        # Model coefficients and warning thresholds never depend on any of these results.
        experiment('shadow',load_manifest(ROOT/'scenarios/predictive-shadow.json'))
        if args.pilot:
            if not args.pilot.resolve().is_relative_to(RESULTS.resolve()):
                raise ValueError('pilot must be in the local results tree')
            pilot=load_summary(args.pilot/'summary.json')
            index['reused_pilot']=dict(directory=str(args.pilot),
                sha256=hashlib.sha256((args.pilot/'summary.json').read_bytes()).hexdigest())
        else:
            _,pilot=experiment('pilot',load_manifest(ROOT/'scenarios/predictive-static-pilot.json'))
        dynamic=load_manifest(ROOT/'scenarios/predictive-buildup.json');dynamic['repetitions']=args.repetitions
        selected=select_static(pilot,dynamic);save(output/'static-selection.json',selected)
        for label,manifest in [('buildup',dynamic),
                ('stable',load_manifest(ROOT/'scenarios/predictive-stable.json')),
                ('bursts',load_manifest(ROOT/'scenarios/predictive-bursts.json'))]:
            manifest['static_critical_pct']=selected['critical_pct']
            if label!='bursts': manifest['repetitions']=args.repetitions
            experiment(label,manifest)
        index['status']='complete';save(output/'index.json',index)
        print(json.dumps(index),flush=True)


if __name__=='__main__': main()
