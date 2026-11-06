"""Paired short exploratory comparison: HTB default, existing flower, full TCX engine."""
import argparse
import csv
import hashlib
import json
import os
import random
import statistics
import subprocess
import sys
import time
from pathlib import Path
from benchmark import owned_directory
from benchmark_v2 import host_metadata,host_sample
from research_stats import median_ci
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tests'))
from flow_engine_integration import setup,serve,traffic,RULES


def process(pid):
    try:
        fields=Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()
        status=Path(f'/proc/{pid}/status').read_text().splitlines()
        memory={line.split(':')[0]:int(line.split()[1])*1024 for line in status
                if line.startswith(('VmRSS:','VmHWM:'))}
        return dict(cpu_seconds=(int(fields[11])+int(fields[12]))/os.sysconf('SC_CLK_TCK'),**memory)
    except OSError:
        return {}


def run(output,mode,rep,duration):
    owned_directory(output)
    lab=setup(output)
    try:
        serve(lab)
        engine=None
        if mode=='flower':
            for rule in RULES:
                for protocol in ('ip','ipv6'):
                    lab.tc('filter','add','dev','fc0','parent','1:','protocol',protocol,'flower',
                           'ip_proto','tcp' if rule['protocol']==6 else 'udp','dst_port',rule['dport'],
                           'classid',f"1:{rule['classid']&0xffff:x}")
        elif mode=='bpf':
            if not lab.start_flow_engine(): raise RuntimeError(lab.flow_engine.reason)
            engine=lab.flow_engine
            engine.set_policy(2,RULES)
        before=host_sample()
        cpu0=process(engine.proc.pid) if engine else {}
        result=traffic(lab,'benchmark',duration=duration,bulk=True)
        after=host_sample()
        cpu1=process(engine.proc.pid) if engine else {}
        rows=engine.dump() if engine else []
        stats=engine.stats if engine else {}
        correct=sum(any(row['protocol']==r['protocol'] and row['dport']==r['dport'] and
                    row['classid']==r['classid'] and row['service']==r['service'] for r in RULES) for row in rows)
        counters=lab.counters()
        host_cpu=sum(b-a for i,(a,b) in enumerate(zip(before['cpu_ticks'],after['cpu_ticks'])) if i not in (3,4))/os.sysconf('SC_CLK_TCK')
        summary=dict(mode=mode,repetition=rep,p99_ms=result['p99_ms'],
            failed=result['failed'],background_failed=result['background_failed'],offered=result['offered'],completion_ratio=result['completion_ratio'],
            deadline_attainment=result['deadline_attainment'],total_mbps=result['total_mbps'],
            background_mbps=result['background_mbps'],actual_duration=result['duration'],
            host_busy_cpu_seconds=host_cpu,engine_cpu_seconds=cpu1.get('cpu_seconds',0)-cpu0.get('cpu_seconds',0),
            engine_peak_rss_bytes=cpu1.get('VmHWM'),engine_stats=stats,
            retained_flow_accuracy=correct/len(rows) if rows else None,retained_flows=len(rows),
            configured_rate_kbit=20000,classification='HTB skb priority' if engine else mode,
            outcome='VALID' if result['offered'] and result['p99_ms'] is not None else 'INVALID EXPERIMENT')
        (output/'result.json').write_text(json.dumps(dict(summary=summary,raw=result,
            host_before=before,host_after=after,flows=rows,counters=counters),indent=2))
        return summary
    finally:
        lab.close()


def aggregate(rows):
    grouped={}
    for mode in ('none','flower','bpf'):
        subset=[row for row in rows if row['mode']==mode]
        grouped[mode]={metric:dict(median=statistics.median(values),mean=statistics.mean(values),
            sd=statistics.stdev(values) if len(values)>1 else None,median_ci=median_ci(values))
            for metric in ('p99_ms','total_mbps','background_mbps','host_busy_cpu_seconds','engine_cpu_seconds')
            if (values:=[row[metric] for row in subset if row[metric] is not None])}
        grouped[mode]['offered']=sum(row['offered'] for row in subset)
        grouped[mode]['failed']=sum(row['failed'] for row in subset)
    paired={}
    for baseline in ('none','flower'):
        for metric in ('p99_ms','total_mbps','host_busy_cpu_seconds'):
            differences=[]
            for a in rows:
                if a['mode']!='bpf': continue
                b=next(row for row in rows if row['mode']==baseline and row['repetition']==a['repetition'])
                if b[metric]: differences.append((a[metric]/b[metric]-1)*100)
            paired[baseline+'_'+metric]=dict(median_percent=statistics.median(differences),
                                           median_ci=median_ci(differences))
    return dict(modes=grouped,paired=paired,outcome='INCONCLUSIVE',
        reason='Short VM runs compare whole TCX intelligence cost; no predeclared speedup claim or universal gate.')


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--repetitions',type=int,default=10)
    parser.add_argument('--duration',type=float,default=2)
    parser.add_argument('--seed',type=int,default=42631)
    args=parser.parse_args()
    if os.geteuid()!=0 or not 1<=args.repetitions<=50 or not .5<=args.duration<=30:
        parser.error('root isolated runner, 1..50 repetitions, .5..30 seconds')
    directory=ROOT/'results'/f'flow-benchmark-{time.time_ns()}'
    owned_directory(directory)
    rows=[]
    metadata=dict(kernel=os.uname().release,architecture=os.uname().machine,host=host_metadata(),
                  git=subprocess.check_output(['git','-c',f'safe.directory={ROOT}','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
                  source_sha256={name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest()
                      for name in ('src/flow_engine.cpp','bpf/flow_engine.bpf.c','bpf/flow_engine.h',
                                   'scripts/flow_engine.py','tests/flow_traffic.py')},
                  dirty=bool(subprocess.check_output(['git','-c',f'safe.directory={ROOT}',
                      'status','--porcelain'],cwd=ROOT,text=True).strip()),boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip(),configuration=vars(args))
    (directory/'manifest.json').write_text(json.dumps(metadata,indent=2))
    rng=random.Random(args.seed)
    for rep in range(args.repetitions):
        order=['none','flower','bpf'];rng.shuffle(order)
        for mode in order:
            row=run(directory/f'r{rep}-{mode}',mode,rep,args.duration)
            rows.append(row)
            (directory/'runs.json').write_text(json.dumps(rows,indent=2))
            print(json.dumps(row),flush=True)
    report=aggregate(rows)
    (directory/'summary.json').write_text(json.dumps(report,indent=2))
    fields=['mode','repetition','p99_ms','failed','offered','total_mbps','background_mbps',
            'host_busy_cpu_seconds','engine_cpu_seconds','engine_peak_rss_bytes']
    with (directory/'runs.csv').open('w') as stream:
        writer=csv.DictWriter(stream,fieldnames=fields,extrasaction='ignore')
        writer.writeheader();writer.writerows(rows)
    print(json.dumps(dict(output=str(directory),report=report)),flush=True)


if __name__=='__main__':
    main()
