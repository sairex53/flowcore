"""Privileged end-to-end research controls on disposable veth links."""
import copy
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from benchmark import owned_directory
from cli import request
from daemon import SOCKET
from research_manifest import load_manifest
from research_runner import MARKER, RESULTS, save

BIN = ROOT/'build/flowcore'


def call(*args,timeout=120):
    proc = subprocess.run([BIN,*args],capture_output=True,text=True,timeout=timeout)
    if proc.returncode:
        raise RuntimeError(f'{args} failed: {proc.returncode}\n{proc.stderr}\n{proc.stdout}')
    return json.loads(proc.stdout)


def require(value,message):
    if not value:
        raise AssertionError(message)


def main():
    require(os.geteuid()==0 and not SOCKET.exists(),'requires sudo and a stopped daemon')
    RESULTS.mkdir(exist_ok=True)
    output = RESULTS/f'research-integration-{time.time_ns()}'
    owned_directory(output)
    manifest = load_manifest(ROOT/'scenarios/chaos-smoke.json')
    manifest.update(modes=['fq_codel'],repetitions=1,duration_seconds=6)
    manifest['workload']['phases'] = [dict(seconds=6,requests_per_second=55,background_streams=2)]
    manifest['faults'] = [dict(at_seconds=.4,jitter_ms=1.5),dict(at_seconds=1.25,link_up=False),
                          dict(at_seconds=3.25,link_up=True)]
    manifest['controller']['bpf'] = 'auto'
    original = output/'input.json'
    save(original,manifest)
    first = Path(call('chaos','run',original)['experiments'][0]['directory'])
    second = Path(call('bench','replay',first/'experiment.json')['experiments'][0]['directory'])
    require(json.loads((second/'experiment.json').read_text())==manifest,'replay changed manifest')
    save(output/'runs.json',dict(chaos=str(first),replay=str(second)))
    for path in (first,second):
        summary = json.loads((path/'summary.json').read_text())
        row = summary['runs'][0]
        require(row['valid'] and row['failed']>0,'live link fault produced no measured failure')
        applied = [json.loads(line) for line in (path/row['artifact']/'transitions.jsonl').read_text().splitlines()]
        require([item['link_up'] for item in applied]==[True,True,False,True],'fault readback sequence mismatch')
        require(all((path/name).is_file() for name in ('report.html','report.json','report.csv')),
                'missing report artifact')
    comparison = call('bench','compare',first/'summary.json',second/'summary.json')
    require(all(row['status']=='inconclusive' for row in comparison['findings']),'single replay overclaimed')
    save(output/'comparison.json',comparison)
    result = call('bench','minimize',original,'--mode','fq_codel','--failure-ratio','.1','--budget','6')
    require(result['seed']==manifest['seed'] and result['faults']<len(manifest['faults']),
            'failure minimization did not shrink the schedule')
    save(output/'minimization.json',result)

    extended = copy.deepcopy(manifest)
    extended['duration_seconds'] = extended['workload']['phases'][0]['seconds'] = 40
    long_run = output/'interrupt.json'
    save(long_run,extended)
    proc = None
    pids = []
    with (output/'interrupt.log').open('w') as log:
        try:
            proc = subprocess.Popen([BIN,'chaos','run',long_run],stdout=log,stderr=log)
            deadline = time.monotonic()+10
            while time.monotonic()<deadline and proc.poll() is None:
                if MARKER.exists() and SOCKET.exists():
                    try:
                        pids = request('inspect')['namespaces']
                    except (OSError,ValueError):
                        pass
                    if pids:
                        break
                time.sleep(.1)
            require(pids,'could not observe active lab before cancellation')
            require(call('chaos','stop')['stopping'],'chaos stop did not signal runner')
            require(proc.wait(timeout=12)==130,'interruption exit status')
        finally:
            if proc and proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=12)
    require(not SOCKET.exists() and not MARKER.exists(),'interrupted lab ownership leaked')
    require(all(not Path(f'/proc/{pid}/ns/net').exists() for pid in pids),'namespace holder leaked')
    require(call('chaos','stop')=={'stopping':False},'idle stop not idempotent')
    print(f'PASS: chaos, measured replay, report, comparison, minimization and scoped cancellation. {output}')


if __name__=='__main__':
    main()
