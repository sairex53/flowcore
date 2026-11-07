"""Run-level independent review summaries; preserves failures and all valid runs."""
import argparse
import collections
import csv
import json
import statistics
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from research_stats import aggregate, compare_v2, load_summary, median_ci
from workload import percentile

KEYS=('p50_ms','p95_ms','p99_ms','failure_ratio','deadline_attainment','total_mbps',
      'background_mbps','jain_index','cpu_seconds','peak_daemon_rss_bytes',
      'dispatch_lag_p99_ms','host_cpu_busy_ratio','response_seconds','first_verdict_seconds')

def summary(values):
    values=[v for v in values if v is not None]
    return dict(n=len(values),median=statistics.median(values) if values else None,
                minimum=min(values) if values else None,maximum=max(values) if values else None,
                median_ci_95=median_ci(values))

def critical(path,deadline):
    with path.open() as stream:
        rows=[r for r in csv.DictReader(stream) if r['traffic_class']=='critical']
    successes=[float(r['latency_ms']) for r in rows if r['outcome']=='ok']
    return dict(offered=len(rows),failed=len(rows)-len(successes),
        p50_ms=percentile(successes,.50),p95_ms=percentile(successes,.95),p99_ms=percentile(successes,.99),
        failure_ratio=(len(rows)-len(successes))/len(rows) if rows else None,
        deadline_attainment=sum(v<=deadline for v in successes)/len(rows) if rows and deadline is not None else None)

def analyze(index):
    result=dict(groups={},static_selection=index['static_selection'],scope='VM maintainer reproduction')
    for name,directory in index['groups'].items():
        if name=='pilot':continue
        path=Path(directory);data=load_summary(path/'summary.json')
        modes={}
        for mode in data['manifest']['modes']:
            rows=[r for r in data['runs'] if r['mode']==mode]
            valid=[r for r in rows if r['valid']]
            details=[critical(path/r['artifact']/'requests.csv',data['manifest']['workload']['deadline_ms']) for r in valid]
            warnings=collections.Counter(str(item) for r in valid for item in r.get('warnings',[]))
            modes[mode]=dict(valid=len(valid),invalid=len(rows)-len(valid),
                metrics={key:summary([r.get(key) for r in valid]) for key in KEYS},
                critical={key:summary([r.get(key) for r in details]) for key in ('p50_ms','p95_ms','p99_ms','failure_ratio','deadline_attainment')},
                total_offered=sum(r['offered'] for r in valid),total_failed=sum(r['failed'] for r in valid),
                changes=sum(r['adjustments'] for r in valid),warnings=dict(warnings),
                controller_errors=[r['controller_error'] for r in valid if r.get('controller_error')])
        baseline=data['manifest']['gates']['baseline']
        effects={}
        if baseline:
            for mode in modes:
                if mode==baseline:continue
                report=compare_v2(data,data,baseline,mode)
                paired=[]
                for row in data['runs']:
                    if row['mode']!=mode or not row['valid']:continue
                    other=next(r for r in data['runs'] if r['mode']==baseline and r['repetition']==row['repetition'])
                    if not other['valid']:continue
                    assert other['workload_seed']==row['workload_seed']
                    a=critical(path/other['artifact']/'requests.csv',data['manifest']['workload']['deadline_ms'])
                    b=critical(path/row['artifact']/'requests.csv',data['manifest']['workload']['deadline_ms'])
                    if a['p99_ms'] and b['p99_ms'] is not None:paired.append((b['p99_ms']/a['p99_ms']-1)*100)
                effects[mode]=dict(gate=report,critical_p99_percent_effect=summary(paired),
                    critical_note='secondary exploratory endpoint; unadjusted median interval')
        if 'predictive' in modes and 'predictive-shadow' in modes:
            pairs=[]
            for r in data['runs']:
                if r['mode']!='predictive' or not r['valid']:continue
                other=next(x for x in data['runs'] if x['mode']=='predictive-shadow' and x['repetition']==r['repetition'])
                if other['valid'] and other['p99_ms']:pairs.append((r['p99_ms']/other['p99_ms']-1)*100)
            effects['predictive_vs_shadow']=dict(overall_p99_percent_effect=summary(pairs),
                qualification='zero-action series cannot identify the benefit of dynamic actuation')
        result['groups'][name]=dict(path=str(path),status=data['status'],modes=modes,effects=effects,
            environment=data['environment'],manifest=data['manifest'])
    return result

def fmt(value,digits=3):
    return 'N/A' if value is None else f'{value:.{digits}f}'

def render(data):
    lines=['# Independent benchmark report','',
      'Maintainer-run reproduction on Fedora 45 ARM64/QEMU. No external reviewer or physical system participated.',
      'The performance suite was frozen before pilot/evaluation. Static policy selection used disjoint pilot seeds.',
      'The primary gate endpoint is overall successful-request P99 across traffic classes; critical-class metrics are secondary exploratory endpoints.',
      'Every reported latency is offered/scheduled-to-completion application latency, including dispatch lag, TCP establishment, a request payload and 64-byte response. Failures and deadlines are reported separately.','',
      '## Static pilot','',f"Selected {data['static_selection']['mode']}: {data['static_selection']['reservation_pct']}% critical guarantee.",
      'Selection: lower median failure ratio, then lower overall P99, then higher background goodput; two pilot repetitions.','']
    for name,group in data['groups'].items():
        lines.extend(['## '+name,'',f"Evidence: {group['path']}",'',
            '| Mode | Valid/invalid | Overall P50/P95/P99 ms | Critical P99 ms | Failed/offered | Deadline attainment | Total app Mbps | Background Mbps | Background Jain | CPU s | Peak RSS MiB | Changes |',
            '|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|'])
        for mode,row in group['modes'].items():
            m=row['metrics']
            lines.append('| '+mode+' | '+f"{row['valid']}/{row['invalid']}"+' | '+
                '/'.join(fmt(m[k]['median']) for k in ('p50_ms','p95_ms','p99_ms'))+' | '+
                fmt(row['critical']['p99_ms']['median'])+' | '+f"{row['total_failed']}/{row['total_offered']}"+' | '+
                fmt(m['deadline_attainment']['median'])+' | '+fmt(m['total_mbps']['median'])+' | '+fmt(m['background_mbps']['median'])+' | '+
                fmt(m['jain_index']['median'])+' | '+fmt(m['cpu_seconds']['median'])+' | '+
                fmt(m['peak_daemon_rss_bytes']['median']/1024**2 if m['peak_daemon_rss_bytes']['median'] else None)+' | '+str(row['changes'])+' |')
        lines.extend(['','### Run-level variability and uncertainty','',
            '| Mode | Overall P99 min–max ms | 95% distribution-free median interval ms |',
            '|---|---:|---|'])
        for mode,row in group['modes'].items():
            metric=row['metrics']['p99_ms'];interval=metric['median_ci_95']
            lines.append(f"| {mode} | {fmt(metric['minimum'])}–{fmt(metric['maximum'])} | "+
                (f"{fmt(interval['low'])}–{fmt(interval['high'])} (coverage {interval['coverage']:.4f})" if interval else 'Insufficient independent runs')+' |')
        if group['effects']:
            lines.extend(['','### Paired effects',''])
            for mode,effect in group['effects'].items():
                if 'gate' not in effect:
                    lines.append(f"- {mode}: {json.dumps(effect)}")
                    continue
                finding=next(f for f in effect['gate']['findings'] if f['metric']=='p99_ms')
                lines.append(f"- {mode}: overall P99 median relative regression {fmt((finding['median_regression'] or 0)*100)}%; gate {effect['gate']['outcome']}; simultaneous interval {finding['simultaneous_median_ci']}.")
                critical_effect=effect['critical_p99_percent_effect']
                lines.append(f"  Critical P99 median paired effect {fmt(critical_effect['median'])}%; exploratory interval {critical_effect['median_ci_95']}.")
        lines.append('')
    return '\n'.join(lines)+'\n'

def main():
    parser=argparse.ArgumentParser();parser.add_argument('index',type=Path);parser.add_argument('output',type=Path)
    parser.add_argument('--markdown',type=Path)
    args=parser.parse_args();index=json.loads(args.index.read_text())
    if index['status']!='complete':raise ValueError('evaluation index incomplete')
    result=analyze(index);args.output.write_text(json.dumps(result,indent=2))
    if args.markdown:args.markdown.write_text(render(result))
    print(args.output)
if __name__=='__main__':main()
