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
from paths import TC

BIN = ROOT/'build/flowcore'


def call(*args,timeout=120):
    proc = subprocess.run([BIN,*args],capture_output=True,text=True,timeout=timeout)
    if proc.returncode:
        raise RuntimeError(f'{args} failed: {proc.returncode}\n{proc.stderr}\n{proc.stdout}')
    return json.loads(proc.stdout)


def require(value,message):
    if not value:
        raise AssertionError(message)


def host_network_snapshot():
    def command(*args):
        return json.loads(subprocess.run(args,check=True,capture_output=True,text=True).stdout)
    links = command('ip','-j','link','show')
    normalized_links = [{key: item[key] for key in ('ifname','mtu','address','master','link_index')
                         if key in item} for item in links]
    addresses = command('ip','-j','address','show')
    normalized_addresses = [{
        'ifname': item['ifname'],
        'addresses': sorted((entry['family'],entry['local'],entry.get('prefixlen'),entry.get('scope'))
                            for entry in item.get('addr_info',[]))}
        for item in addresses]
    qdiscs = command(str(TC),'-j','qdisc','show')
    return dict(netns=os.stat('/proc/self/ns/net').st_ino,links=normalized_links,
                addresses=normalized_addresses,qdiscs=qdiscs)


def main():
    require(os.geteuid()==0 and not SOCKET.exists(),'requires sudo and a stopped daemon')
    host_network_before=host_network_snapshot()
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
        require(all('rate' not in item.get('netem',{}) for item in applied),
                'legacy scenario unexpectedly gained a reverse-path rate limit')
        require(all((path/name).is_file() for name in ('report.html','report.json','report.csv')),
                'missing report artifact')
    comparison = call('bench','compare',first/'summary.json',second/'summary.json')
    require(all(row['status']=='inconclusive' for row in comparison['findings']),'single replay overclaimed')
    save(output/'comparison.json',comparison)

    schedule_v3 = load_manifest(ROOT/'scenarios/chaos-overlap-v3.json')
    schedule_v3['repetitions'] = 1
    input_v3 = output/'chaos-v3.json'
    save(input_v3,schedule_v3)
    replayed = []
    for command in (('chaos','run',input_v3),):
        replayed.append(Path(call(*command,timeout=180)['experiments'][0]['directory']))
    replayed.append(Path(call('bench','replay',replayed[0]/'experiment.json',timeout=180)['experiments'][0]['directory']))
    offsets = []
    expected_events = {'capacity-reduction-1','capacity-reduction-2','capacity-collapse',
                       'reverse-path-collapse','correlated-loss-jitter','interface-flap'}
    for path in replayed:
        summary = json.loads((path/'summary.json').read_text())
        row = summary['runs'][0]
        require(row['valid'] and row['offered']==row['completed']+row['failed'],
                'IPv6 fault scenario produced an invalid workload ledger')
        require(row['workload_seed']==summary['block_seeds'][0],'replay changed workload seed')
        require(row['udp_mbps']>0,'receiver did not account for generated IPv6 UDP background traffic')
        info = json.loads((path/row['artifact']/'own-tcp-info.json').read_text())
        require(info['local'][0].startswith('fd42:203::'),'IPv6 TCP request did not use the IPv6 path')
        transitions = [json.loads(line) for line in
                       (path/row['artifact']/'transitions.jsonl').read_text().splitlines()]
        batches = [item for item in transitions if not item.get('initial')]
        seen = {op['id'] for item in batches for op in item.get('operations',[])}
        require(seen==expected_events,'scheduled event start/end set differs')
        correlated = next(item for item in batches if any(
            op['id']=='correlated-loss-jitter' and op['kind']=='event_start'
            for op in item['operations']))
        netem = correlated['kernel_readback']['netem']
        require(abs(netem['loss-random']['loss']-.08)<1e-6
                and abs(netem['loss-random']['correlation']-.55)<1e-6
                and abs(netem['duplicate']['duplicate']-.01)<1e-6,
                'correlated loss or duplicate netem readback mismatch')
        reverse = next(item for item in batches if any(
            op['id']=='reverse-path-collapse' and op['kind']=='event_start'
            for op in item['operations']))
        require(reverse['kernel_readback']['netem']['rate']['rate']==2000*125,
                'asymmetric reverse capacity readback mismatch')
        links = [item['link_up'] for item in batches]
        require(False in links and links[-1] is True,'interface flap recovery readback mismatch')
        restored = next(item for item in batches if any(
            op['id']=='interface-flap' and op['kind']=='event_end' for op in item['operations']))
        require('fd42:203::1' in restored['kernel_readback']['sender_addresses'],
                'IPv6 sender address was not restored after interface flap')
        ledger = list(__import__('csv').DictReader(open(path/row['artifact']/'requests.csv')))
        recovered=[request for request in ledger if float(request['scheduled'])>row['start']+10]
        require(any(request['outcome']=='ok' for request in recovered),
                'IPv6 application requests did not recover after faults cleared')
        require({'critical','interactive','standard'} <= {request['traffic_class'] for request in recovered},
                'application request classes were not all represented after recovery')
        stages = [json.loads(line)['stage'] for line in
                  (path/row['artifact']/'stages.jsonl').read_text().splitlines()
                  if json.loads(line)['kind']=='stage']
        require(stages==['warmup','baseline','congestion','fault_injection','recovery',
                         'post_recovery_validation'],'scenario stage timeline mismatch')
        offsets.append([item['scheduled_ns']-round(row['start']*1e9) for item in batches])
        for holder in json.loads((path/row['artifact']/'network.json').read_text())['namespaces']:
            require(not Path(f'/proc/{holder}/ns/net').exists(),'scenario left a namespace holder')
        require((path/'report.html').is_file() and (path/'report.csv').is_file()
                and (path/row['artifact']/'transitions.csv').is_file(),
                'scenario report artifacts are incomplete')
    require(offsets[0]==offsets[1],'replay changed scheduled network stimulus')
    save(output/'replay-v3.json',dict(first=str(replayed[0]),second=str(replayed[1]),
                                     schedule_offsets_ns=offsets))
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
    require(host_network_snapshot()==host_network_before,'host network state changed during isolated lab tests')
    print(f'PASS: chaos, replay, UDP, reports, minimization, cleanup, host network isolation and cancellation. {output}')


if __name__=='__main__':
    main()
