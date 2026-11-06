"""Privileged fault exercise with the queue-aware controller on the owned veth lab."""
import csv
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
sys.path.insert(0,str(ROOT/'tests'))
from paths import RESULTS
from research_integration import host_network_snapshot, require
from research_manifest import load_manifest
from research_runner import owned_directory, save

BIN = ROOT/'build/flowcore'


def main():
    require(os.geteuid()==0,'requires root on the trusted isolated lab runner')
    host_before=host_network_snapshot()
    manifest=load_manifest(ROOT/'scenarios/chaos-overlap-v3.json')
    manifest['modes']=['queue-aware']
    manifest['repetitions']=1
    output=RESULTS/f'controller-chaos-{time.time_ns()}'
    owned_directory(output)
    input_path=output/'scenario.json'
    save(input_path,manifest)
    proc=subprocess.run([BIN,'chaos','run',input_path],check=True,capture_output=True,
                        text=True,timeout=180)
    response=json.loads(proc.stdout)
    run_path=Path(response['experiments'][0]['directory'])
    summary=json.loads((run_path/'summary.json').read_text())
    row=summary['runs'][0]
    require(row['valid'],'queue-aware chaos workload was invalid')
    require(row['offered']==row['completed']+row['failed'],'request ledger did not reconcile')
    require(row['background_mbps']>0,'background TCP traffic was starved')
    require(row['jain_index'] is not None and row['jain_index']>=.5,
            'background TCP streams were severely unfair')
    config=json.loads((run_path/row['artifact']/'config.json').read_text())
    history=json.loads((run_path/row['artifact']/'decisions.json').read_text())
    changes=[item for item in history if item['result'] in
             ('pending','verified','released','rolled_back','inconclusive','restored_baseline')]
    require(len(changes)==row['adjustments'],'decision history and action accounting differ')
    require(row['adjustments']<=config['adaptive']['maximum_actions'],
            'controller exceeded configured per-run action budget')
    require(row['rollbacks']<=row['adjustments'],'more rollbacks than policy actions')
    require(row['controller_error'] is None,'controller entered an error state')
    with (run_path/row['artifact']/'requests.csv').open() as stream:
        ledger=list(csv.DictReader(stream))
    recovered=[request for request in ledger if float(request['scheduled'])>row['start']+10]
    require(any(request['outcome']=='ok' for request in recovered),
            'critical requests did not complete after fault recovery')
    require({'critical','interactive','standard'} <= {request['traffic_class'] for request in recovered},
            'a request class received no post-recovery service')
    network=json.loads((run_path/row['artifact']/'network.json').read_text())
    for holder in network['namespaces']:
        require(not Path(f'/proc/{holder}/ns/net').exists(),'controller chaos left a namespace holder')
    require(host_network_snapshot()==host_before,'controller chaos changed host network state')
    save(output/'verification.json',dict(outcome='PASS',experiment=str(run_path),
         adjustments=row['adjustments'],rollbacks=row['rollbacks'],
         oscillation_guard=row['oscillation_guard'],p99_ms=row['p99_ms'],
         offered=row['offered'],completed=row['completed'],failed=row['failed']))
    print(f"PASS: queue-aware controller stayed within its action budget; {output}")


if __name__=='__main__':
    main()
