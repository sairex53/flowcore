"""Frozen maintainer-run held-out evaluation; no policy fitting on evaluation data."""
import copy
import hashlib
import json
import random
import statistics
import time
from pathlib import Path
from benchmark import ROOT, RESULTS, owned_directory
from research_manifest import validate
from research_runner import exclusive, execute, save
from research_stats import aggregate
from research_report import report


def main():
    plan=json.loads((ROOT/'config/release-evaluation-plan.json').read_text())
    template=json.loads((ROOT/'scenarios/v2-heavy-paired.json').read_text())
    primary=copy.deepcopy(template)
    primary.update(duration_seconds=12,repetitions=plan['evaluation_repetitions'],
                   seed=plan['evaluation_seed'],modes=plan['primary_modes'])
    primary['network'].update(bandwidth_kbit=12000,base_rtt_ms=6)
    primary['workload'].update(payload_bytes=8192,deadline_ms=40,critical_services=2,
        priority_weights=dict(critical=60,interactive=25,standard=15),
        phases=[dict(seconds=s,requests_per_second=r,background_streams=b)
                for s,r,b in ((2,40,1),(3,100,4),(4,150,8),(3,60,2))])
    primary['controller'].update(target_p99_ms=30)
    primary['faults']=[]
    pilot=copy.deepcopy(primary)
    pilot.update(seed=plan['pilot_seed'],repetitions=plan['pilot_repetitions'],
                 modes=['static20','static30','static40'])
    pilot['gates']['baseline']=None
    baseline=copy.deepcopy(template)
    baseline.update(seed=plan['baselines_seed'],repetitions=plan['exploratory_repetitions'],
                    modes=plan['baseline_modes'])
    baseline['network'].update(bandwidth_kbit=5000,base_rtt_ms=10)
    baseline['workload'].update(payload_bytes=4096,deadline_ms=50,
        phases=[dict(seconds=3,requests_per_second=r,background_streams=b) for r,b in ((40,2),(80,6))])
    low=copy.deepcopy(template)
    low.update(seed=plan['low_seed'],repetitions=plan['exploratory_repetitions'],
               modes=['static','hybrid','queue-aware','predictive'])
    low['workload'].update(payload_bytes=2048,deadline_ms=40,
                          phases=[dict(seconds=6,requests_per_second=20,background_streams=0)])
    linux=copy.deepcopy(low)
    linux.update(seed=plan['linux_seed'],modes=['linux-default'])
    linux['network'].update(base_rtt_ms=0)
    linux['gates']['baseline']=None
    manifests=dict(pilot=pilot,primary=primary,baselines=baseline,low=low,linux_default=linux)
    for manifest in manifests.values():validate(manifest)
    seeds={name:set(random.Random(m['seed']).sample(range(1,2**32),m['repetitions']))
           for name,m in manifests.items()}
    for a in seeds:
        for b in seeds:
            if a!=b and seeds[a]&seeds[b]:raise RuntimeError('overlapping data partitions')
    with exclusive():
        output=RESULTS/f'release-review-{time.time_ns()}';owned_directory(output)
        save(output/'frozen-plan.json',dict(plan=plan,manifests=manifests))
        index=dict(status='running',scope='maintainer reproduction; no third-party certification',groups={},
            plan_sha256=hashlib.sha256((output/'frozen-plan.json').read_bytes()).hexdigest())
        save(output/'index.json',index)
        print('Review evidence: '+str(output),flush=True)
        path,data=execute(pilot,label='review-pilot')
        index['groups']['pilot']=str(path)
        if any(not row['valid'] for row in data['runs']):raise RuntimeError('invalid static pilot; evaluation not selected')
        def score(mode):
            rows=[r for r in data['runs'] if r['mode']==mode]
            return (statistics.median(r['failure_ratio'] for r in rows),
                    statistics.median(r['p99_ms'] for r in rows),
                    -statistics.median(r['background_mbps'] for r in rows))
        selected=min(pilot['modes'],key=score)
        reserve=int(selected.removeprefix('static'))
        index['static_selection']=dict(mode=selected,reservation_pct=reserve,
                                       scores={mode:score(mode) for mode in pilot['modes']})
        save(output/'index.json',index)
        for name in ('primary','baselines','low','linux_default'):
            m=copy.deepcopy(manifests[name]);m['static_critical_pct']=reserve
            path,data=execute(m,label='review-'+name)
            index['groups'][name]=str(path)
            index.setdefault('aggregates',{})[name]=aggregate(data)
            report(path/'summary.json')
            save(output/'index.json',index)
        index['status']='complete';save(output/'index.json',index)
        print(json.dumps(dict(output=str(output),selection=index['static_selection'])),flush=True)
if __name__=='__main__':main()
