"""Execute validated manifests using the existing daemon and TCP workloads."""
from paths import BINDIR, RESULTS
import contextlib
import csv
import fcntl
import hashlib
import json
import os
import random
import signal
import subprocess
import sys
import time
from pathlib import Path

from adaptive_experiment import await_ready
from benchmark import ROOT, owned_directory, receiver_metrics, run, TC
from cli import request
from daemon import SOCKET
from observer_perf import await_service, command, stop, cpu_ticks
from research_manifest import FAULTS, net_profile, validate
from service_config import load
from workload import percentile

MARKER = RESULTS/'.research-active.json'
NANO = 1_000_000_000


def process_start(pid):
    return Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[19]


@contextlib.contextmanager
def exclusive():
    if os.geteuid() != 0:
        raise ValueError('research networking requires sudo')
    RESULTS.mkdir(exist_ok=True)
    with (RESULTS/'.research.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError('another research runner owns the lab') from error
        if SOCKET.exists():
            raise ValueError('stop the existing flowcored before running experiments')
        old = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
        def interrupt(*_):
            for sig in old:
                signal.signal(sig,signal.SIG_IGN)
            raise KeyboardInterrupt('research stop requested')
        for sig in old:
            signal.signal(sig, interrupt)
        try:
            temporary = MARKER.with_suffix('.tmp')
            temporary.write_text(json.dumps(dict(pid=os.getpid(), start=process_start(os.getpid()))))
            temporary.replace(MARKER)
            yield
        finally:
            for sig, handler in old.items():
                signal.signal(sig, handler)
            MARKER.unlink(missing_ok=True)


def cancel():
    if os.geteuid() != 0:
        raise ValueError('research stop requires sudo')
    try:
        row = json.loads(MARKER.read_text())
        with contextlib.ExitStack() as stack:
            fd = os.pidfd_open(row['pid'])
            stack.callback(os.close, fd)
            if process_start(row['pid']) != row['start']:
                raise ValueError('stale research runner identity')
            target = (RESULTS/'.research.lock').stat()
            owned = False
            for descriptor in Path(f'/proc/{row["pid"]}/fd').iterdir():
                try:
                    state = descriptor.stat()
                except FileNotFoundError:
                    continue
                if (state.st_dev,state.st_ino)==(target.st_dev,target.st_ino):
                    owned = True
                    break
            if not owned:
                raise ValueError('process does not own the research workspace descriptor')
            # A live lock proves the workspace has a runner.
            with (RESULTS/'.research.lock').open('a') as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    signal.pidfd_send_signal(fd, signal.SIGTERM)
                    return dict(stopping=True)
                raise ValueError('no research runner holds the lab lock')
    except (FileNotFoundError, ProcessLookupError):
        return dict(stopping=False)


def child(argv, **kwargs):
    guard = ('import ctypes,os,signal,sys; '
             'assert ctypes.CDLL(None).prctl(1,signal.SIGTERM,0,0,0)==0; '
             'assert os.getppid()==int(sys.argv[1]); os.execvp(sys.argv[2],sys.argv[2:])')
    return subprocess.Popen([sys.executable, '-B', '-c', guard, str(os.getpid()), *argv], **kwargs)


def save(path, data):
    path.write_text(json.dumps(data, indent=2, allow_nan=False)+'\n')


def metadata():
    git = subprocess.run(['git','rev-parse','HEAD'], cwd=ROOT, capture_output=True, text=True)
    dirty = subprocess.run(['git','status','--porcelain'], cwd=ROOT, capture_output=True, text=True)
    sources = sorted([*ROOT.glob('scripts/*.py'), *ROOT.glob('src/*.cpp'), *ROOT.glob('bpf/*'),
                      *ROOT.glob('include/flowcore/*.hpp'), ROOT/'config/service.json', ROOT/'CMakeLists.txt'])
    return dict(kernel=os.uname().release, architecture=os.uname().machine, cpus=os.cpu_count(),
                git_commit=git.stdout.strip() if git.returncode==0 else None,
                git_dirty=bool(dirty.stdout) if dirty.returncode==0 else None,
                tc=run([TC,'-V']).strip(),
                source_sha256={str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                               for p in sources if p.is_file()})


def root(tc, field='qdisc'):
    return next((row for row in tc.get(field, []) if row.get('root')), {})


def counter_deltas(samples, field):
    values = [root(row['tc'], field).get('drops') for row in samples]
    deltas = [b-a for a,b in zip(values,values[1:]) if type(a) is int and type(b) is int and b >= a]
    return sum(deltas) if deltas else None


def convergence(samples, history, transitions, m, start, mode):
    """Two nonoverlapping healthy critical windows at the same allocation after recovery."""
    if mode not in ('threshold','slo'):
        return None
    anchor = max([start, *(item['applied_ns']/NANO for item in transitions if not item.get('initial'))])
    width = m['controller']['window_seconds']
    last_change = max([anchor, *(row['time_ns']/NANO for row in history
        if row['result'] in ('pending','released','rolled_back','verified','restored_baseline','research_rebase'))])
    eligible = None
    for sample in samples:
        stamp = sample['time_ns']/NANO
        if stamp-width < last_change:
            continue
        state = sample['state']
        app = sample['control_window'].get('critical',{}).get('application',{})
        if (state['mode'] != 'active' or state['pending'] or state['error']
                or app.get('samples',0) < m['controller']['minimum_samples']
                or app.get('p99_us') is None or app['p99_us']>1000*m['controller']['target_p99_ms']
                or app.get('failure') or app.get('timeout')):
            eligible = None
            continue
        if eligible and stamp-eligible[0]>=width and state['allocation']==eligible[1]:
            return stamp-anchor
        if eligible is None:
            eligible = stamp,state['allocation']
    return None


def summarize(directory, m, rep, mode, samples, history, start, end, host_ticks):
    data = json.loads((directory/'client.json').read_text())
    if data['completed']+data['failed'] != data['offered']:
        raise RuntimeError('invalid workload accounting')
    duration = end-start
    data.update(receiver_metrics(directory/'receiver.jsonl', start, end))
    phase_fairness = []
    phase_start = start
    for phase in m['workload']['phases']:
        phase_end = phase_start+phase['seconds']
        streams = phase['background_streams']
        rates = receiver_metrics(directory/'receiver.jsonl',phase_start,phase_end)['flow_mbps'][:streams]
        total = sum(rates)
        jain = total**2/(len(rates)*sum(r*r for r in rates)) if total else None
        phase_fairness.append(dict(seconds=phase['seconds'],streams=streams,jain_index=jain))
        phase_start = phase_end
    comparable_phases = [p for p in phase_fairness if p['streams']>=2 and p['jain_index'] is not None]
    data['phase_fairness'] = phase_fairness
    data['jain_index'] = (sum(p['jain_index']*p['seconds'] for p in comparable_phases)/
                          sum(p['seconds'] for p in comparable_phases)) if comparable_phases else None
    before, after = samples[0]['overhead'], samples[-1]['overhead']
    cpu = sum(after[key]-before[key] for key in ('daemon_cpu_seconds','child_cpu_seconds'))
    if before['bpf_cpu_seconds'] is not None and after['bpf_cpu_seconds'] is not None:
        cpu += max(0, after['bpf_cpu_seconds']-before['bpf_cpu_seconds'])
    buckets = {}
    for row in samples:
        buckets.update(row['buckets'])
    # Only complete monotonic seconds fully inside the scheduled load window.
    covered = [v for second,v in buckets.items() if float(second)>=start and float(second)+1<=end]
    bpf = samples[-1]['backend']
    loss = None if not bpf['active'] or bpf['kernel_reserve_drops'] is None or bpf['transport_drops'] is None else sum(
        bpf[key] for key in ('kernel_reserve_drops','transport_drops','parse_drops'))
    changes = [r for r in history if r['result'] in ('pending','verified','released','rolled_back','restored_baseline')]
    probes = [r for r in history if r['result']=='pending']
    verdicts = [r for r in history if r['result'] in ('kept','rolled_back')]
    first_response = (probes[0]['time_ns']/NANO-start) if probes else None
    verdict_time = ((verdicts[0]['time_ns']-probes[0]['time_ns'])/NANO) if probes and verdicts else None
    ticks, idle = host_ticks
    successes = data['completed']
    payload = successes*m['workload']['payload_bytes']*8/duration/1e6
    data.update(mode=mode, repetition=rep, valid=True, total_mbps=payload+data['background_mbps'],
                request_payload_mbps=payload, cpu_seconds=cpu,
                host_cpu_busy_ratio=1-idle/ticks if ticks else None,
                peak_daemon_rss_bytes=max(row['overhead']['daemon_rss_bytes'] or 0 for row in samples),
                peak_bpf_rss_bytes=max(row['overhead']['bpf_rss_bytes'] or 0 for row in samples) if bpf['active'] else None,
                sampled_retransmit_segments=sum(bucket.get('tcp.retrans',0) for bucket in covered)
                    if any(bucket.get('tcp.pairs') for bucket in covered) else None,
                sampled_data_segments=sum(bucket.get('tcp.segments',0) for bucket in covered),
                traced_retransmit_events=sum(bucket.get('bpf.retransmit_events',0) for bucket in covered)
                    if 'tcp_retransmit' in bpf['hooks'] else None,
                complete_counter_seconds=len(covered),
                peak_backlog_bytes=max(root(row['tc']).get('backlog',0) for row in samples),
                qdisc_drop_delta=counter_deltas(samples,'qdisc'), netem_drop_delta=counter_deltas(samples,'receiver_qdisc'),
                event_loss=loss, adjustments=len(changes), adjustments_per_minute=len(changes)*60/duration,
                capacity_rebases=sum(r['result']=='research_rebase' for r in history),
                rollbacks=sum(r['result']=='rolled_back' for r in history),
                oscillation_guard=sum(r['result']=='oscillation_guard' for r in history),
                response_seconds=first_response, first_verdict_seconds=verdict_time,
                convergence_seconds=convergence(samples,history,
                    [json.loads(line) for line in (directory/'transitions.jsonl').read_text().splitlines()],m,start,mode),
                controller_error=samples[-1]['state']['error'])
    class_latency = {}
    with (directory/'requests.csv').open() as stream:
        for row in csv.DictReader(stream):
            item = class_latency.setdefault(row['traffic_class'], {'ok': [], 'failed': 0})
            if row['outcome']=='ok':
                item['ok'].append(float(row['latency_ms']))
            else:
                item['failed'] += 1
    data['classes'] = {name: dict(completed=len(row['ok']),failed=row['failed'],
                                  p99_ms=percentile(sorted(row['ok']),.99)) for name,row in class_latency.items()}
    return data


def trial(directory, m, rep, mode):
    owned_directory(directory)
    cfg = load(ROOT/'config/service.json')
    network = dict(m['network'])
    cfg['lab'] = net_profile(network)
    cfg['telemetry_seconds'] = 1
    cfg['observability']['bpf'] = m['controller']['bpf']
    cfg['adaptive'].update(window_seconds=m['controller']['window_seconds'],
                           minimum_samples=m['controller']['minimum_samples'])
    if mode == 'static':
        cfg['policies']['default']['classes']['critical']['min_pct'] = m['static_critical_pct']
        cfg['policies']['default']['classes']['background']['min_pct'] = 60-m['static_critical_pct']
    if mode == 'cake':
        cfg['policies']['default'] = cfg['policies']['fair']
    config = directory/'config.json'
    save(config,cfg)
    daemon = server = client = None
    seed = (m['seed']+rep) % (2**32-1)+1
    with (directory/'daemon.log').open('w') as dl, (directory/'server.log').open('w') as sl, \
         (directory/'client.log').open('w') as cl, (directory/'timeline.jsonl').open('w') as timeline, \
         (directory/'transitions.jsonl').open('w') as transitions:
        try:
            daemon = child([BINDIR/'flowcored','--research','--config',config],stdout=dl,stderr=dl)
            await_service(daemon)
            request('lab_up')
            sender, receiver = request('inspect')['namespaces']
            for cls in ('critical','interactive','standard'):
                request('intent_register',intent=dict(name=cls,priority=cls,
                    target_p99_ms=m['controller']['target_p99_ms'],ttl_seconds=m['duration_seconds']+60))
            applied = request('research_conditions',network=network,mode=mode,seed=seed,link_up=True)
            transitions.write(json.dumps(dict(at_seconds=0,initial=True,requested=network,**applied))+'\n')
            transitions.flush()
            if mode in ('threshold','slo'):
                request('controller_start',strategy=mode)
            workload = m['workload']
            plan = directory/'workload.json'
            settings = dict(**workload, seed=seed, start_monotonic=0,
                            ports={'critical':9000,'interactive':9002,'standard':9003})
            save(plan,settings)
            options = ['--duration',str(m['duration_seconds']), '--payload',str(workload['payload_bytes']),
                       '--streams',str(max(p['background_streams'] for p in workload['phases'])),
                       '--research-plan',plan]
            server = child(command(receiver,'server',directory/'receiver.jsonl',options),stdout=sl,stderr=sl)
            await_ready(server,directory/'server.log')
            start = time.monotonic()+.5
            end = start+m['duration_seconds']
            settings['start_monotonic'] = start
            save(plan,settings)
            client = child(command(sender,'client',directory,options),stdout=cl,stderr=cl)
            samples = []
            begin_ticks = cpu_ticks()
            def sample():
                value = request('research_sample')
                samples.append(value)
                timeline.write(json.dumps(value)+'\n')
                timeline.flush()
            sample()
            next_sample = start+1
            link_up = True
            for index,fault in enumerate([*m['faults'],{'at_seconds':m['duration_seconds']}],1):
                deadline = start+fault['at_seconds']
                while next_sample < deadline:
                    time.sleep(max(0,next_sample-time.monotonic()))
                    sample()
                    next_sample += 1
                time.sleep(max(0,deadline-time.monotonic()))
                sample()
                if len(fault)==1:
                    break
                network.update({key:fault[key] for key in FAULTS[:-1] if key in fault})
                link_up = fault.get('link_up',link_up)
                begun = time.monotonic_ns()
                applied = request('research_conditions',network=network,mode=mode,
                                  seed=(seed+index-1)%(2**32-1)+1,link_up=link_up)
                transitions.write(json.dumps(dict(scheduled_ns=round(deadline*NANO),begun_ns=begun,
                                                  requested=fault, **applied))+'\n')
                transitions.flush()
            finish_ticks = cpu_ticks()
            code = client.wait(timeout=12)
            if code not in (0,2) or server.poll() is not None:
                raise RuntimeError(f'workload infrastructure failed, exit={code}')
            time.sleep(.15)
            history = [row for row in request('policy_history') if row.get('time_ns',0)>=round(start*NANO)]
            save(directory/'decisions.json',history)
            save(directory/'network.json',request('inspect'))
            if request('inspect')['namespaces'] != [sender,receiver]:
                raise RuntimeError('namespace identity changed mid-experiment')
            result = summarize(directory,m,rep,mode,samples,history,start,end,
                               tuple(b-a for a,b in zip(begin_ticks,finish_ticks)))
            save(directory/'result.json',result)
            return result
        finally:
            stop(client)
            stop(server)
            stop(daemon)


def execute(manifest, label='research', parent=RESULTS):
    m = validate(manifest)
    path = parent/f'{label}-{time.time_ns()}'
    owned_directory(path)
    order = [(rep,mode) for rep in range(m['repetitions']) for mode in m['modes']]
    random.Random(m['seed']).shuffle(order)
    data = dict(version=1, status='running', manifest=m, environment=metadata(), order=order, runs=[])
    save(path/'experiment.json',m)
    save(path/'summary.json',data)
    print(f'Evidence: {path}',flush=True,file=sys.stderr)
    for rep, mode in order:
        name = f'r{rep}-{mode}'
        try:
            result = trial(path/name,m,rep,mode)
        except KeyboardInterrupt:
            data['status'] = 'interrupted'
            save(path/'summary.json',data)
            raise
        except (OSError,RuntimeError,ValueError,subprocess.SubprocessError) as error:
            result = dict(mode=mode,repetition=rep,valid=False,error=str(error))
            save(path/name/'result.json',result)
        result['artifact'] = name
        data['runs'].append(result)
        save(path/'summary.json',data)
        print(f"{name}: {result.get('p99_ms')} ms, failures={result.get('failed')}, {result.get('error','measured')}",
              flush=True,file=sys.stderr)
    data['status'] = 'complete'
    save(path/'summary.json',data)
    return path,data
