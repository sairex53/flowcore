"""Paired synthetic migration gate; raw observations remain separate runs."""
import argparse
import json
import os
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('output',type=Path)
    parser.add_argument('--pairs',type=int,default=10)
    parser.add_argument('--max-regression',type=float,default=.10)
    args=parser.parse_args()
    if not 1<=args.pairs<=50 or not 0<=args.max_regression<=1:
        parser.error('pairs must be 1..50 and max-regression 0..1')
    reference=subprocess.run(['git','show','01959b2:scripts/telemetry_windows.py'],
                             cwd=ROOT,capture_output=True,text=True,check=True).stdout
    rows=[]
    with tempfile.TemporaryDirectory(dir=ROOT/'build',prefix='native-bench-') as temp:
        source=Path(temp)/'reference.py';source.write_text(reference)
        for pair in range(args.pairs):
            for mode in (('reference','native') if pair%2==0 else ('native','reference')):
                code="import sys,json;sys.path.insert(0,'scripts');"
                if mode=='reference':
                    code+=("import importlib.util;spec=importlib.util.spec_from_file_location("
                           f"'telemetry_windows',{str(source)!r});module=importlib.util.module_from_spec(spec);"
                           "spec.loader.exec_module(module);sys.modules['telemetry_windows']=module;")
                code+="from runtime_profile import aggregation;print(json.dumps(aggregation()))"
                proc=subprocess.run([sys.executable,'-B','-c',code],cwd=ROOT,
                    env=dict(os.environ,FLOWCORE_HISTOGRAM_BACKEND='native' if mode=='native' else 'python'),
                    capture_output=True,text=True,check=True,timeout=30)
                rows.append(dict(pair=pair,mode=mode,**json.loads(proc.stdout)))
    ratios=[next(r['cpu_ns'] for r in rows if r['pair']==p and r['mode']=='native')/
            next(r['cpu_ns'] for r in rows if r['pair']==p and r['mode']=='reference')
            for p in range(args.pairs)]
    median=statistics.median(ratios)
    result=dict(baseline='01959b2',kernel=os.uname().release,architecture=os.uname().machine,
                max_regression=args.max_regression,pairs=args.pairs,runs=rows,
                paired_ratio_median=median,paired_ratio_min=min(ratios),paired_ratio_max=max(ratios),
                outcome='REGRESSION' if median>1+args.max_regression else 'PASS',
                scope='synthetic CPU budget only; no application latency inference')
    args.output.write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({k:v for k,v in result.items() if k!='runs'}))
    return int(result['outcome']=='REGRESSION')

if __name__=='__main__':raise SystemExit(main())
