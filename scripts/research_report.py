"""Portable HTML, CSV and JSON reports from recorded experiments."""
import csv
import html
import io
import json
import tempfile
from pathlib import Path

from research_stats import METRICS, aggregate, compare, investigations, load_summary, value
from workload import percentile

COLORS = ('#2563eb','#15803d','#7c3aed','#b45309','#be185d')


def read_lines(path):
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def chart(title, series, start, end, faults=(), actions=(), unit='s'):
    width,height = 760,185
    horizon = max(end-start,.01)
    limit = max((v for _,rows in series for _,v in rows),default=0) or 1
    def xy(t,v):
        return 53+min(1,max(0,(t-start)/horizon))*690, 152-min(1,max(0,v/limit))*126
    body = ['<path d="M53 26 V152 H743" fill="none" stroke="#94a3b8"/>',
            f'<text x="8" y="31">{limit:.3g}</text>',
            '<text x="30" y="155">0</text>',
            f'<text x="690" y="174">{horizon:.1f} {unit}</text>']
    for marks,color,label in ((faults,'#d97706','network transition'),(actions,'#a21caf','controller action')):
        for t in marks:
            x,_ = xy(t,0)
            body.append(f'<path d="M{x:.1f} 26 V152" stroke="{color}" stroke-dasharray="4 3">'
                        f'<title>{label} at {t-start:.3f}s</title></path>')
    labels = []
    for i,(name,rows) in enumerate(series):
        if not rows:
            continue
        color = COLORS[i%len(COLORS)]
        coords = ' '.join(f'{x:.2f},{y:.2f}' for x,y in (xy(t,v) for t,v in rows))
        body.append(f'<polyline points="{coords}" fill="none" stroke="{color}" stroke-width="2"/>')
        for t,v in rows:
            x,y = xy(t,v)
            body.append(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="2" fill="{color}"><title>'
                        f'{html.escape(name)} at {t-start:.2f} {unit}: {v:.4g}</title></circle>')
        labels.append(f'<span style="color:{color}">{html.escape(name)}</span>')
    return (f'<figure><figcaption>{html.escape(title)}</figcaption><svg viewBox="0 0 {width} {height}" '
            f'role="img" aria-label="{html.escape(title,quote=True)}">{"".join(body)}</svg>'
            f'<div class="legend">{" · ".join(labels)} · <b>orange</b> fault · <b>purple</b> controller</div></figure>')


def run_charts(path, row):
    directory = (path/row['artifact']).resolve()
    if directory.parent != path.resolve() or any(
            (directory/name).resolve().parent != directory for name in
            ('timeline.jsonl','transitions.jsonl','decisions.json','requests.csv','receiver.jsonl')):
        raise ValueError('raw artifact path escapes its experiment')
    start,end = row['start'],row['end']
    trace = read_lines(directory/'timeline.jsonl')
    fault_rows = read_lines(directory/'transitions.jsonl')
    faults = [item['applied_ns']/1e9 for item in fault_rows if not item.get('initial')]
    decisions = json.loads((directory/'decisions.json').read_text())
    actions = [item['time_ns']/1e9 for item in decisions if item['result'] in ('pending','rolled_back','released','verified')]
    seconds = {}
    with (directory/'requests.csv').open() as stream:
        for item in csv.DictReader(stream):
            second = int(max(0,float(item['scheduled'])-start))
            bucket = seconds.setdefault(second,{'ok':[],'failed':0})
            if item['outcome']=='ok':
                bucket['ok'].append(float(item['latency_ms']))
            else:
                bucket['failed'] += 1
    latency = [(start+i+.5,percentile(sorted(bucket['ok']),.99)) for i,bucket in sorted(seconds.items()) if bucket['ok']]
    failures = [(start+i+.5,bucket['failed']) for i,bucket in sorted(seconds.items())]
    rx = read_lines(directory/'receiver.jsonl')
    goodput,fairness = [],[]
    first = next((i for i,item in enumerate(rx) if item['time']>=start),len(rx))
    for a,b in zip(rx[first::10],rx[first+10::10]):
        if b['time']>end:
            break
        rates = [(y-x)*8/(b['time']-a['time'])/1e6 for x,y in zip(a['bytes'],b['bytes'])]
        goodput.append((b['time'],sum(rates)))
        active = [rate for rate in rates if rate>0]
        if active:
            fairness.append((b['time'],sum(active)**2/(len(active)*sum(rate*rate for rate in active))))
    backlog,loss = [],[]
    previous = None
    for item in trace:
        stamp = item['time_ns']/1e9
        q = next((q for q in item['tc'].get('qdisc',[]) if q.get('root')), {})
        netem = next((q for q in item['tc'].get('receiver_qdisc',[]) if q.get('root')), {})
        backlog.append((stamp,q.get('backlog',0)))
        drops = [q.get('drops',0),netem.get('drops',0)]
        if previous:
            loss.append((stamp,*(max(0,y-x) for x,y in zip(previous,drops))))
        previous = drops
    graphs = [('Application P99 by issue second (ms)', [('P99',latency)]),
              ('Background goodput (Mbit/s)', [('receiver payload',goodput)]),
              ('Sender root backlog (bytes)', [('backlog',backlog)]),
              ('Observed drops between samples (packets)',
               [('sender qdisc',[(t,q) for t,q,_ in loss]), ('receiver netem',[(t,n) for t,_,n in loss])]),
              ('Request failures by issue second', [('failed',failures)]),
              ('Background Jain index across positive-rate flows', [('Jain',fairness)])]
    rendered = ''.join(chart(title,series,start,end,faults,actions) for title,series in graphs)
    rows = ''.join(f'<li>{item["applied_ns"]/1e9-start:.3f} s · '
                   f'{html.escape(json.dumps(item["requested"]))}</li>' for item in fault_rows if not item.get('initial'))
    policy_rows = ''.join(f'<li>{item["time_ns"]/1e9-start:.3f} s · {html.escape(item["result"])}</li>' for item in decisions)
    return (f'<details><summary>{html.escape(row["artifact"])} · P99 {row["p99_ms"]!s} ms · '
            f'{row["failed"]} failed</summary><div class="charts">{rendered}</div>'
            f'<p>Applied network conditions</p><ul>{rows}</ul><p>Policy decisions</p><ul>{policy_rows}</ul>'
            f'<p><a href="{row["artifact"]}/requests.csv">Requests CSV</a> · '
            f'<a href="{row["artifact"]}/timeline.jsonl">Kernel timeline</a> · '
            f'<a href="{row["artifact"]}/transitions.jsonl">Transition readbacks</a></p></details>')


def write_artifact(path, content):
    # The experiment directory belongs to the operator even if the old report
    # was generated by the privileged runner. Publish without truncating it.
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode='w',dir=path.parent,prefix='.report-',delete=False) as stream:
            name = Path(stream.name)
            stream.write(content)
        name.chmod(0o644)
        name.replace(path)
    finally:
        if name is not None:
            name.unlink(missing_ok=True)


def report(summary_path, baseline=None):
    summary_path = Path(summary_path)
    data = load_summary(summary_path)
    path = summary_path.parent
    stats = aggregate(data)
    comparisons = []
    if data['version'] == 2:
        baseline_mode = data['manifest']['gates']['baseline']
        if baseline_mode:
            base = load_summary(baseline) if baseline else data
            comparisons = [compare(base,data,baseline_mode,mode) for mode in data['manifest']['modes']
                           if mode != baseline_mode]
    elif baseline:
        comparisons.append(compare(load_summary(baseline),data))
    elif 'static' in data['manifest']['modes']:
        for mode in ('threshold','slo'):
            if mode in data['manifest']['modes']:
                comparisons.append(compare(data,data,'static',mode))
    outcomes = {comparison['outcome'] for comparison in comparisons if 'outcome' in comparison}
    outcome = ('INVALID EXPERIMENT' if any(not row['valid'] for row in data['runs']) or
               'INVALID EXPERIMENT' in outcomes else 'REGRESSION' if 'REGRESSION' in outcomes
               else 'PASS' if outcomes == {'PASS'} else 'INCONCLUSIVE')
    flags = investigations(data) if data['version']==2 else {}
    artifact = dict(version=data['version'], outcome=outcome, investigations=flags, environment=data['environment'],manifest=data['manifest'],
                    aggregate=stats,regression=comparisons,runs=data['runs'],raw_summary=summary_path.name)
    write_artifact(path/'report.json',json.dumps(artifact,indent=2,allow_nan=False)+'\n')
    stream = io.StringIO()
    writer = csv.writer(stream)
    writer.writerow(['mode','repetition','valid','offered','completed','failed',*METRICS,'artifact'])
    for row in data['runs']:
        writer.writerow([*(row.get(key) for key in ('mode','repetition','valid','offered','completed','failed')),
                         *(value(row,key) for key in METRICS),row['artifact']])
    write_artifact(path/'report.csv',stream.getvalue())
    if data['version'] == 2:
        stream = io.StringIO()
        writer = csv.writer(stream)
        writer.writerow(['mode','metric','runs','median','mean','variance','median_ci_low','median_ci_high'])
        for mode,row in stats.items():
            for key,metric in row['metrics'].items():
                interval = metric['median_ci_95'] or {}
                writer.writerow([mode,key,metric['n'],metric['median'],metric['mean'],metric['variance'],
                                 interval.get('low'),interval.get('high')])
        write_artifact(path/'aggregate.csv',stream.getvalue())
    def text(value):
        return '—' if value is None else f'{value:.3g}' if isinstance(value,float) else html.escape(str(value))
    table = ''.join(f'<tr><th>{html.escape(mode)}</th><td>{row["valid"]}/{row["valid"]+row["invalid"]}</td>'+
                    ''.join(f'<td>{text(row["metrics"][key]["median"])}</td>' for key in
                            ('p99_ms','total_mbps','jain_index','cpu_seconds','adjustments_per_minute','failure_ratio','deadline_attainment'))+'</tr>'
                    for mode,row in stats.items())
    tails = [(mode,[(i+1,value(row,'p99_ms')) for i,row in enumerate(data['runs'])
                    if row['mode']==mode and row['valid'] and value(row,'p99_ms') is not None])
             for mode in stats]
    comparison_chart = chart('P99 comparison by execution order (ms)',tails,0,max(1,len(data['runs'])),unit='runs')
    reg = ''.join(f'<tr><td>{item["baseline"]} → {item["candidate"]}</td><td>{item["metric"]}</td>'
                  f'<td>{item["pairs"]}</td><td>{text(item["median_regression"])}</td>'
                  f'<td>{text(item.get("simultaneous_median_ci", item.get("adjusted_p")))}</td><td>{item["status"]}</td></tr>'
                  for result in comparisons for item in result['findings'])
    details = ''.join(run_charts(path,row) if row['valid'] else
                      f'<p>{html.escape(row["artifact"])}: {html.escape(row["error"])}</p>' for row in data['runs'])
    flags_html = ''.join(f'<li>{html.escape(name)} · {html.escape("; ".join(notes))}</li>'
                         for name, notes in flags.items())
    extra_links = (' · <a href="aggregate.csv">Aggregate CSV</a> · <a href="reproduce.sh">Reproduction command</a>'
                   if data['version'] == 2 else '')
    page = f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>FLOWCORE Research Lab</title><style>
body{{font:15px system-ui;color:#0f172a;background:#f8fafc;max-width:1150px;margin:32px auto;padding:0 18px}}
h1,h2{{letter-spacing:-.02em}}table{{border-collapse:collapse;width:100%;background:white}}th,td{{padding:8px;text-align:left;border-bottom:1px solid #e2e8f0}}
details,figure{{padding:12px;background:white;border:1px solid #e2e8f0;border-radius:8px;margin:12px 0}}
summary{{cursor:pointer;font-weight:650}}svg{{width:100%;max-width:100%;font-size:11px}}figcaption{{font-weight:650}}
.charts{{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(380px,100%),1fr));gap:12px}}.legend{{font-size:12px}}
pre{{white-space:pre-wrap}}a{{color:#1d4ed8}}</style><h1>FLOWCORE Research Lab</h1>
<p>{text(data['environment']['kernel'])} · {text(data['environment']['architecture'])} · Git {text(data['environment']['git_commit'])}</p>
<p>Outcome {outcome}. Per-run medians below. Request P99 is conditional on success. Confidence intervals and variance are in JSON.</p>
<p><a href="report.json">JSON</a> · <a href="report.csv">CSV</a> · <a href="experiment.json">Replay manifest</a>{extra_links}</p>
<table><thead><tr><th>Mode</th><th>Valid</th><th>P99 ms</th><th>Total Mbit/s</th><th>Jain</th><th>CPU s</th><th>Adjustments/min</th><th>Failure ratio</th><th>Deadline attainment</th></tr></thead><tbody>{table}</tbody></table>
{comparison_chart}<h2>Run-level regression checks</h2><table><tr><th>Pair</th><th>Metric</th><th>Runs</th><th>Median change</th><th>Interval or adjusted p</th><th>Result</th></tr>{reg}</table>
<h2>Investigation flags</h2><ul>{flags_html}</ul>
<h2>Timelines and raw artifacts</h2>{details}
<details><summary>Experiment manifest</summary><pre>{html.escape(json.dumps(data['manifest'],indent=2))}</pre></details></html>'''
    write_artifact(path/'report.html',page)
    return path/'report.html'
