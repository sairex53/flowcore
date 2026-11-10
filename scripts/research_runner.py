"""Execute validated manifests using the existing daemon and TCP workloads."""
from paths import BINDIR, RESULTS, RUNTIME
import contextlib
import csv
import fcntl
import hashlib
import json
import os
import random
import shlex
import signal
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from adaptive_experiment import await_ready
from benchmark import ROOT, owned_directory, receiver_metrics, run, TC
from cli import request
import benchmark_v2 as v2
import measurement
import ecn_policy
import deadline_runtime
from ecn_feedback import aqm_counters
from ipc_security import private_directory, private_lock
from daemon import SOCKET
from observer_perf import await_service, command, stop, cpu_ticks
from research_manifest import FAULTS, PACING_HTB_MODES, PACING_MODES, net_profile, validate
from service_config import load
from workload import percentile

MARKER = RUNTIME/'research-active.json'
LOCK = RUNTIME/'research.lock'
NANO = 1_000_000_000


def process_start(pid):
    return Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[19]


@contextlib.contextmanager
def exclusive():
    if os.geteuid() != 0:
        raise ValueError('research networking requires sudo')
    RESULTS.mkdir(exist_ok=True)
    private_directory(RUNTIME)
    with private_lock(LOCK) as lock:
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
            with tempfile.NamedTemporaryFile(mode='w', dir=RUNTIME, delete=False) as stream:
                temporary = Path(stream.name)
                try:
                    json.dump(dict(pid=os.getpid(), start=process_start(os.getpid())), stream)
                    stream.close()
                    temporary.replace(MARKER)
                finally:
                    temporary.unlink(missing_ok=True)
            yield
        finally:
            for sig, handler in old.items():
                signal.signal(sig, handler)
            MARKER.unlink(missing_ok=True)


def cancel():
    if os.geteuid() != 0:
        raise ValueError('research stop requires sudo')
    try:
        private_directory(RUNTIME)
        row = json.loads(MARKER.read_text())
        with contextlib.ExitStack() as stack:
            fd = os.pidfd_open(row['pid'])
            stack.callback(os.close, fd)
            if process_start(row['pid']) != row['start']:
                raise ValueError('stale research runner identity')
            target = LOCK.stat()
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
            with private_lock(LOCK) as lock:
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
                      *ROOT.glob('include/flowcore/*.hpp'), *ROOT.glob('include/flowcore/*.h'), ROOT/'config/service.json', ROOT/'CMakeLists.txt'])
    build_keys = ('CMAKE_BUILD_TYPE:', 'CMAKE_CXX_COMPILER:', 'CMAKE_CXX_FLAGS:', 'FLOWCORE_BPF:')
    cache = v2.read(BINDIR/'CMakeCache.txt') or ''
    settings = {line.split(':',1)[0]: line.split('=',1)[1] for line in cache.splitlines()
                if line.startswith(build_keys) and '=' in line}
    return dict(kernel=os.uname().release, architecture=os.uname().machine, cpus=os.cpu_count(),
                monotonic_resolution_ns=round(time.get_clock_info('monotonic').resolution*1e9),
                build_configuration=settings,
                boot_fingerprint=hashlib.sha256((v2.read('/proc/sys/kernel/random/boot_id') or '').encode()).hexdigest(),
                git_commit=git.stdout.strip() if git.returncode==0 else None,
                git_dirty=bool(dirty.stdout) if dirty.returncode==0 else None,
                flowcore=run([BINDIR/'flowcore','--version']).strip(),
                binary_sha256=hashlib.sha256((BINDIR/'flowcore').read_bytes()).hexdigest(),
                host=v2.host_metadata(),
                network_sysctls={key:v2.read('/proc/sys/'+path) for key,path in {
                    'net.core.default_qdisc':'net/core/default_qdisc',
                    'net.core.rmem_max':'net/core/rmem_max',
                    'net.core.wmem_max':'net/core/wmem_max',
                    'net.ipv4.tcp_ecn':'net/ipv4/tcp_ecn',
                    'net.ipv4.tcp_congestion_control':'net/ipv4/tcp_congestion_control',
                    'net.ipv4.tcp_sack':'net/ipv4/tcp_sack',
                    'net.ipv4.tcp_mtu_probing':'net/ipv4/tcp_mtu_probing',
                }.items()},
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
    if mode not in ('threshold','slo','queue-aware','optimizer'):
        return None
    confirmed = (item.get('kernel_confirmed_ns',item.get('applied_ns')) for item in transitions
                 if not item.get('initial') and item.get('status') not in ('failed','invalid'))
    anchor = max([start, *(stamp/NANO for stamp in confirmed if type(stamp) is int)])
    width = m['controller']['window_seconds']
    last_change = max([anchor, *(row['time_ns']/NANO for row in history
        if row['result'] in ('pending','released','rolled_back','verified','restored_baseline','inconclusive','research_rebase'))])
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
    changes = [r for r in history if r['result'] in ('pending','verified','released','rolled_back','inconclusive','restored_baseline')]
    probes = [r for r in history if r['result']=='pending']
    verdicts = [r for r in history if r['result'] in ('kept','rolled_back','inconclusive')]
    first_response = (probes[0]['time_ns']/NANO-start) if probes else None
    verdict_time = ((verdicts[0]['time_ns']-probes[0]['time_ns'])/NANO) if probes and verdicts else None
    ticks, idle = host_ticks
    successes = data['completed']
    payload = successes*m['workload']['payload_bytes']*8/duration/1e6
    data.update(mode=mode, repetition=rep, valid=True, total_mbps=payload+data['background_mbps']+data['udp_mbps'],
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


def session_history(rows,session,start_ns):
    """Monotonic clocks are not comparable across daemon boots/sessions."""
    return [row for row in rows if row.get('controller_session')==session and
            row.get('time_ns',0)>=start_ns]


def compose_scenario_network(base, active_events):
    """Compose overlapping faults with explicit conservative semantics."""
    result = dict(base)
    bandwidths = [base['bandwidth_kbit']]
    reverse = [base.get('reverse_bandwidth_kbit', base['bandwidth_kbit'])]
    delays = [base['base_rtt_ms']]
    jitters = [base['jitter_ms']]
    losses = [base['loss_pct']/100]
    reorder = [base['reorder_pct']/100]
    duplicate = [base.get('duplicate_pct',0)/100]
    correlations = [base.get('loss_correlation_pct',0)]
    for event in active_events:
        effects = event['effects']
        bandwidths.append(effects.get('bandwidth_kbit', base['bandwidth_kbit']))
        reverse.append(effects.get('reverse_bandwidth_kbit', base.get('reverse_bandwidth_kbit', base['bandwidth_kbit'])))
        delays.append(effects.get('base_rtt_ms', base['base_rtt_ms']))
        jitters.append(effects.get('jitter_ms', base['jitter_ms']))
        losses.append(effects.get('loss_pct',0)/100)
        reorder.append(effects.get('reorder_pct',0)/100)
        duplicate.append(effects.get('duplicate_pct',0)/100)
        correlations.append(effects.get('loss_correlation_pct',0))
    result['bandwidth_kbit'] = min(bandwidths)
    result['reverse_bandwidth_kbit'] = min(reverse)
    result['base_rtt_ms'] = max(delays)
    result['jitter_ms'] = max(jitters)
    result['loss_pct'] = (1-__import__('math').prod(1-item for item in losses))*100
    result['reorder_pct'] = (1-__import__('math').prod(1-item for item in reorder))*100
    result['duplicate_pct'] = (1-__import__('math').prod(1-item for item in duplicate))*100
    result['loss_correlation_pct'] = max(correlations)
    return result


def recent_receiver(path):
    # Read only the end of an append-only receiver ledger. Ignore a partial
    # line while the other process is publishing its latest counter sample.
    with path.open('rb') as stream:
        end = stream.seek(0, os.SEEK_END)
        stream.seek(max(0, end-16384))
        raw = stream.read(16384)
    records = raw.split(b'\n')[:-1]
    if end > 16384:
        records = records[1:]
    if not records:
        return {}
    latest = json.loads(records[-1])
    return dict(time_ns=round(latest['time']*NANO), bytes=sum(latest['bytes']), flows=latest['bytes'])


def trial(directory, m, rep, mode, seed=None):
    owned_directory(directory)
    cfg = load(ROOT/'config/service.json')
    network = dict(m['network'])
    cfg['lab'] = net_profile(network)
    cfg['telemetry_seconds'] = 1
    cfg['observability']['bpf'] = m['controller']['bpf']
    cfg['adaptive'].update(window_seconds=m['controller']['window_seconds'],
                           minimum_samples=m['controller']['minimum_samples'])
    if mode == 'slo-single':
        cfg['adaptive']['maximum_actions'] = 1
    if mode == 'slo-long':
        cfg['adaptive']['window_seconds'] = min(30,2*m['controller']['window_seconds'])
        cfg['adaptive']['cooldown_seconds'] = max(8,2*cfg['adaptive']['window_seconds'])
    online = m.get('online')
    if set(m['modes']) & {'integrated', *PACING_HTB_MODES}:
        cfg['observability']['max_sockets'] = 1024
    elif online:
        cfg['observability']['max_sockets'] = 1024
    if mode == 'hybrid-v3':
        cfg['adaptive']['background_floor_pct'] = m['hybrid_v3']['background_floor_pct']
    configured_reserve = ('static', 'pacing-hybrid', *PACING_HTB_MODES, *ecn_policy.CLASSIFIED, *deadline_runtime.MODES[1:])
    if online or mode in configured_reserve:
        reserve = m['static_critical_pct']
    elif mode.startswith('static') and mode != 'static-step':
        reserve = int(mode.removeprefix('static'))
    else:
        reserve = None
    if reserve is not None:
        cfg['policies']['default']['classes']['critical']['min_pct'] = reserve
        cfg['policies']['default']['classes']['background']['min_pct'] = 60-reserve
    if mode in ('cake','ecn-cake','cake-diffserv'):
        cfg['policies']['default'] = cfg['policies']['fair']
    config = directory/'config.json'
    save(config,cfg)
    daemon = server = client = None
    seed = seed if seed is not None else (m['seed']+rep) % (2**32-1)+1
    version2 = m['version'] >= 2
    with (directory/'daemon.log').open('w') as dl, (directory/'server.log').open('w') as sl, \
         (directory/'client.log').open('w') as cl, (directory/'timeline.jsonl').open('w') as timeline, \
         (directory/'transitions.jsonl').open('w') as transitions, \
         (directory/'stages.jsonl').open('w') as stage_log, contextlib.ExitStack() as resources:
        try:
            daemon = child([BINDIR/'flowcored','--research','--config',config],stdout=dl,stderr=dl)
            await_service(daemon)
            controller_session = request('controller_status')['controller_session']
            request('lab_up')
            sender, receiver = request('inspect')['namespaces']
            workload = m['workload']
            services = {cls: [cls] for cls in ('critical', 'interactive', 'standard')}
            if workload.get('critical_services', 1) > 1:
                services['critical'] += [f'critical-{i}' for i in range(2, workload['critical_services']+1)]
            for cls in services:
                request('intent_register', intent=dict(name=cls, priority=cls,
                    target_p99_ms=m['controller']['target_p99_ms'],
                    ttl_seconds=m['duration_seconds']+workload.get('warmup_seconds', 0)+60))
            ecn_profile = m.get('ecn')
            ecn_options = {'ecn': ecn_profile} if ecn_profile else {}
            applied = request('research_conditions',network=network,mode=mode,seed=seed,link_up=True,**ecn_options)
            transitions.write(json.dumps(dict(at_seconds=0,initial=True,requested=network,**applied))+'\n')
            transitions.flush()
            expected_capacity = applied['capacity_kbit']
            ports = {name: cls['port'] for name, cls in load(ROOT/'config/service.json')['policies']['default']['classes'].items()}
            snapshots = []
            initial_namespace_ids = None
            def snapshot(label, setup=False):
                nonlocal initial_namespace_ids
                value = request('research_snapshot', setup=setup)
                value['label'] = label
                value['configured_kbit'] = expected_capacity
                value['readback'] = v2.verify(value, mode, expected_capacity, ports)
                if ecn_profile:
                    ecn_policy.verify(value['tc']['qdisc'], ecn_profile, mode)
                if initial_namespace_ids is None:
                    initial_namespace_ids = value['namespaces']
                elif value['namespaces'] != initial_namespace_ids:
                    raise RuntimeError('owned namespace inode changed during experiment')
                snapshots.append(value)
                save(directory/'topology.json', snapshots)
                return value

            handles = []
            if version2:
                initial = snapshot('setup', setup=True)
                if m['version'] == 3 and workload['address_family']=='ipv6':
                    addresses = [{address['address'] for link in links for address in link['addresses']}
                                 for links in initial['links']]
                    if not {'fd42:203::1','fd42:203::2'} <= addresses[0] | addresses[1]:
                        raise RuntimeError('IPv6 veth addresses are not configured')
                for index, pid in enumerate((sender, receiver)):
                    fd = resources.enter_context(open(f'/proc/{pid}/ns/net', 'rb'))
                    if os.fstat(fd.fileno()).st_ino != initial['namespaces'][index]:
                        raise RuntimeError('namespace holder identity changed before workload launch')
                    handles.append(fd.fileno())

            def worker(index, role, target, options, log):
                argv = command((sender, receiver)[index], role, target, options)
                if not version2:
                    return child(argv, stdout=log, stderr=log)
                fd = handles[index]
                return child(['nsenter', f'--net=/proc/self/fd/{fd}', '--', *argv[4:]],
                             pass_fds=(fd,), stdout=log, stderr=log)

            if mode == 'deadline-existing':
                request('controller_start',strategy='hybrid')
            elif mode == 'ecn-queue':
                request('controller_start',strategy='queue-aware')
            elif mode == 'predictive-shadow':
                request('controller_shadow',strategy='predictive')
            elif mode == 'slo-shadow':
                request('controller_shadow',strategy='slo')
            elif mode == 'optimizer-overload':
                request('controller_start', strategy='optimizer')
            elif mode in ('threshold','queue-aware','slo-aware','hybrid','predictive','optimizer') or mode.startswith('slo'):
                request('controller_start',strategy=mode if mode in ('threshold','queue-aware','slo-aware','hybrid','predictive','optimizer') else 'slo')
            control_dir = (Path(resources.enter_context(tempfile.TemporaryDirectory(
                dir=RUNTIME, prefix='hybrid-v3-'))) if mode == 'hybrid-v3' else directory)
            plan = control_dir/'workload.json'
            receiver_log = control_dir/'receiver.jsonl'
            settings = dict(**workload, seed=seed, start_monotonic=0,
                            ports={cls: ports[cls] for cls in services}, services=services,
                            server_address=('fd42:203::2' if workload.get('address_family')=='ipv6' else '10.203.0.2'))
            integrity = m.get('integrity')
            if integrity:
                settings['integrity'] = integrity
            if 'cake-diffserv' in m['modes']:
                settings['dscp'] = dict(critical=46, interactive=34, standard=0, background=8)
            deadline_profile = m.get('deadline')
            pacing = deadline_profile or ecn_profile or any(mode in PACING_MODES for mode in m['modes'])
            if deadline_profile:
                settings['deadline_admission'] = deadline_profile
                settings['deadline_mode'] = mode
                settings['deadline_pacing_floor'] = deadline_runtime.pacing_floor(m)
            pacing_lock = None
            if mode == 'ecn-dctcp':
                settings['ecn_socket_algorithm'] = 'dctcp'
            if pacing:
                settings['socket_pacing_mode'] = mode
                if mode == 'hybrid-v3':
                    settings['hybrid_control_directory'] = str(control_dir)
                    settings['hybrid_cap'] = m['hybrid_v3']['initial_cap_bytes_s']
                settings['pacing_trace'] = bool(set(m['modes']) & set(PACING_HTB_MODES))
                if mode in ('integrated', 'pacing-guarded'):
                    settings['pacing_target_us'] = round(m['controller']['target_p99_ms']*1000)
                settings['socket_shadows'] = m.get('socket_shadows', [])
                pacing_lock = resources.enter_context(private_lock(control_dir/'pacing.lock'))
            save(plan,settings)
            options = ['--duration',str(m['duration_seconds']), '--payload',str(workload['payload_bytes']),
                       '--streams',str(max(p['background_streams'] for p in workload['phases'])),
                       '--research-plan',plan]
            server = worker(1, 'server', receiver_log, options, sl)
            await_ready(server,directory/'server.log')
            start = time.monotonic()+.5+workload.get('warmup_seconds', 0)
            end = start+m['duration_seconds']
            settings['start_monotonic'] = start
            save(plan, settings)
            if mode == 'hybrid-v3':
                save(directory/'workload.json', {key: item for key, item in settings.items()
                                                if key != 'hybrid_control_directory'})
            client = worker(0, 'client', directory, options, cl)
            samples = []
            begin_ticks = cpu_ticks()
            generation = 0
            hybrid = None
            if mode == 'hybrid-v3':
                from hybrid_runtime import Coordinator, sockets
                hybrid = Coordinator(directory, m['hybrid_v3'], round(m['controller']['target_p99_ms']*1000),
                                     cfg['adaptive']['window_seconds'])

            def pacing_feedback(value, transition=False, action=None, dry=False):
                if not pacing:
                    return
                row = dict(time_ns=time.monotonic_ns(), capacity_bytes_s=(expected_capacity or 0)*125,
                           backlog_bytes=root(value.get('tc', {})).get('backlog', 0),
                           class_active=value.get('state', {}).get('mode', 'off') != 'off',
                           transition=transition)
                if mode in ('integrated', 'pacing-guarded', 'hybrid-v3'):
                    from nextgen_scope import feedback
                    row['integrated'] = feedback(
                        value.get('tc', {}), cfg['policies']['default']['classes'],
                        expected_capacity or 0, m['network']['bandwidth_kbit'],
                        generation=generation, sampled_ns=value.get('sample_ns'),
                        healthy=value.get('integrated_quality')
                        and value.get('state', {}).get('mode') == 'off',
                        require_ecn=mode == 'integrated',
                        allocation=value.get('hybrid', {}).get('allocation'))
                    if hybrid:
                        row['integrated']['revision'] = value.get('hybrid', {}).get('revision')
                        row['hybrid_action'] = action
                    if mode == 'pacing-guarded':
                        row['integrated']['background_ceiling'] = ((expected_capacity or 0)
                            * cfg['policies']['default']['classes']['background']['max_pct']//100*125)
                if settings.get('pacing_trace') and not transition:
                    row['receiver'] = recent_receiver(receiver_log)
                    row['sample_ns'] = value.get('sample_ns')
                    leaves = {q.get('parent'): q.get('backlog', 0)
                              for q in value.get('tc', {}).get('qdisc', []) if q.get('parent')}
                    row['critical_backlog_bytes'] = leaves.get('1:10', 0)
                if deadline_profile:
                    row['deadline_evidence'] = value.get('deadline_evidence', {})
                if ecn_profile:
                    scope = next((q for q in value.get('tc', {}).get('qdisc', [])
                                  if q.get('parent') == '1:20' or mode == 'ecn-cake' and q.get('root')), {})
                    row['ecn_aqm'] = aqm_counters(scope)
                    row['ecn_sample_ns'] = value.get('sample_ns')
                if dry:
                    return row
                with tempfile.NamedTemporaryFile(mode='w', dir=control_dir, prefix='.pacing-', delete=False) as stream:
                    temporary = Path(stream.name)
                    try:
                        json.dump(row, stream)
                        stream.close()
                        temporary.replace(control_dir/'pacing-input.json')
                    finally:
                        temporary.unlink(missing_ok=True)

            @contextlib.contextmanager
            def class_update():
                nonlocal generation
                if pacing_lock is None:
                    yield {}
                    return
                requested = time.monotonic_ns()
                deadline = requested/NANO+1
                while True:
                    try:
                        fcntl.flock(pacing_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise RuntimeError('socket actuator did not release class transition lock')
                        time.sleep(.01)
                try:
                    generation += 1
                    timing = dict(requested_ns=requested, acquired_ns=time.monotonic_ns(), generation=generation)
                    pacing_feedback({}, transition=True)
                    yield timing
                finally:
                    timing['release_begin_ns'] = time.monotonic_ns()
                    fcntl.flock(pacing_lock, fcntl.LOCK_UN)

            def commit_hybrid(action, revision, stamp, expected_sockets):
                with class_update():
                    if expected_sockets is not None and sockets(control_dir) != expected_sockets:
                        hybrid.record('stale_socket_snapshot', action=action)
                        return None
                    try:
                        return request('hybrid_apply', action=action, revision=revision,
                                       capacity_kbit=expected_capacity, sample_ns=stamp)
                    except ValueError as error:
                        if action == 'shift' and 'stale Hybrid V3' in str(error):
                            hybrid.record('stale_class_snapshot', action=action)
                            return None
                        raise

            def sample(allow=True):
                begin = time.monotonic_ns()
                value = request('research_sample', **({'deadline': True} if deadline_profile else {}))
                if mode == 'online-select':
                    status = request('strategy_inspect')
                    value['online_strategy'] = {key: status[key] for key in ('state','active','switches','regressions','shadow')}
                value['runner_ipc_ns'] = time.monotonic_ns()-begin
                samples.append(value)
                action = None
                if hybrid:
                    preview = pacing_feedback(value, dry=True)
                    before_generation = generation
                    now = time.monotonic()
                    boundary = min((start+fault['at_seconds'] for fault in m['faults']
                                    if start+fault['at_seconds'] > now), default=end)
                    action = hybrid.tick(value, preview['integrated'], preview.get('receiver', {}),
                                         sockets(control_dir), expected_capacity, commit_hybrid,
                                         allow=allow, start_new=boundary-now >= hybrid.evaluation_seconds)
                    # A class commit invalidates this sample. Leave the invalidation
                    # frame in place until the daemon publishes its new readback.
                    if generation != before_generation:
                        timeline.write(json.dumps(value)+'\n')
                        timeline.flush()
                        return
                pacing_feedback(value, action=action)
                timeline.write(json.dumps(value)+'\n')
                timeline.flush()
            if version2:
                time.sleep(max(0, start-time.monotonic()))
                snapshot('measurement_start')
                host_before = v2.host_sample()
            runner_before = measurement.process_sample() if integrity else None
            if mode == 'online-select':
                request('strategy_shadow',names=online['shadows'],seconds=online['seconds']+5)
                request('strategy_activate',name=online['candidate'],scope='owned-fc0',
                        seconds=online['seconds'],guards=online['guards'],confirm=True)
            sample()
            next_sample = start+1
            link_up = True
            if m['version'] == 3:
                stage_boundaries = []
                cursor = 0
                for stage in m['stages']:
                    stage_boundaries.append((cursor, stage['name']))
                    cursor += stage['seconds']
                stage_log.write(json.dumps(dict(kind='stage', stage=stage_boundaries[0][1],
                    scheduled_ns=round(start*NANO), actual_ns=time.monotonic_ns()))+'\n')
                stage_log.flush()
                points = {}
                for offset, name in stage_boundaries[1:]:
                    points.setdefault(float(offset), []).append(dict(kind='stage', name=name))
                for event in m['events']:
                    points.setdefault(float(event['at_seconds']), []).append(
                        dict(kind='event_start', event=event))
                    points.setdefault(float(event['at_seconds']+event['duration_seconds']), []).append(
                        dict(kind='event_end', event=event))
                points.setdefault(float(m['duration_seconds']), []).append(dict(kind='finish'))
                active = {}
                lifecycle = {}
                for index, (offset, operations) in enumerate(sorted(points.items()), 1):
                    scheduled_ns = round(start*NANO)+round(offset*NANO)
                    deadline = scheduled_ns/NANO
                    while next_sample < deadline:
                        time.sleep(max(0,next_sample-time.monotonic()))
                        sample()
                        next_sample += 1
                    time.sleep(max(0,deadline-time.monotonic()))
                    sample()
                    if any(operation['kind'] == 'finish' for operation in operations):
                        if version2:
                            snapshot('measurement_end')
                        stage_log.write(json.dumps(dict(kind='stage_end', stage=m['stages'][-1]['name'],
                            scheduled_ns=round(deadline*NANO), actual_ns=time.monotonic_ns()))+'\n')
                        stage_log.flush()
                        break
                    stage_ops = [item for item in operations if item['kind']=='stage']
                    for operation in stage_ops:
                        actual_stage = time.monotonic_ns()
                        record = dict(kind='stage', stage=operation['name'],
                                      scheduled_ns=round(deadline*NANO), actual_ns=actual_stage,
                                      schedule_lag_ns=max(0,actual_stage-round(deadline*NANO)))
                        stage_log.write(json.dumps(record)+'\n')
                    if stage_ops:
                        stage_log.flush()
                    event_ops = [item for item in operations if item['kind'].startswith('event_')]
                    if not event_ops:
                        continue
                    if version2:
                        snapshot('pre_transition')
                    for operation in event_ops:
                        event = operation['event']
                        if operation['kind']=='event_start':
                            active[event['id']] = event
                            lifecycle[event['id']] = dict(scheduled_start_ns=round(deadline*NANO))
                        else:
                            active.pop(event['id'], None)
                    target = compose_scenario_network(m['network'], list(active.values()))
                    target_link = all(item['effects'].get('link_up', True) for item in active.values())
                    begun = time.monotonic_ns()
                    try:
                        with class_update():
                            begun = time.monotonic_ns()
                            applied = request('research_conditions', network=target, mode=mode,
                                              seed=(seed+index-1)%(2**32-1)+1, link_up=target_link, **ecn_options)
                    except (OSError,RuntimeError,ValueError,subprocess.SubprocessError) as error:
                        failed = dict(scheduled_ns=round(deadline*NANO), actual_application_ns=begun,
                                      operations=[dict(kind=item['kind'], id=item['event']['id']) for item in event_ops],
                                      active_event_ids=sorted(active), requested=target,
                                      status='failed', failed_operation='research_conditions',
                                      error_type=type(error).__name__, error=str(error))
                        transitions.write(json.dumps(failed)+'\n')
                        transitions.flush()
                        raise
                    confirmed = applied.get('kernel_confirmed_ns', applied['applied_ns'])
                    expected_capacity = applied['capacity_kbit']
                    readback = snapshot('post_transition') if version2 else applied
                    completed = []
                    for operation in event_ops:
                        event = operation['event']
                        state = lifecycle.setdefault(event['id'], {})
                        if operation['kind']=='event_start':
                            state.update(actual_application_start_ns=begun,
                                         kernel_confirmed_start_ns=confirmed)
                        else:
                            state.update(scheduled_end_ns=round(deadline*NANO),
                                         actual_application_end_ns=begun,
                                         kernel_confirmed_end_ns=confirmed,
                                         scheduled_duration_ns=round(event['duration_seconds']*NANO),
                                         applied_duration_ns=max(0,confirmed-state.get('kernel_confirmed_start_ns',confirmed)),
                                         recovery_lag_ns=max(0,confirmed-round(deadline*NANO)))
                            completed.append(dict(id=event['id'], **state))
                    sender_addresses = []
                    if readback.get('links'):
                        sender_link = next(row for row in readback['links'][0] if row['name']=='fc0')
                        sender_addresses = sorted(row['address'] for row in sender_link['addresses'])
                    record = dict(scheduled_ns=round(deadline*NANO), actual_application_ns=begun,
                                  schedule_lag_ns=max(0,begun-round(deadline*NANO)),
                                  kernel_confirmed_ns=confirmed, application_latency_ns=max(0,confirmed-begun),
                                  operations=[dict(kind=item['kind'], id=item['event']['id']) for item in event_ops],
                                  active_event_ids=sorted(active), requested=target,
                                  kernel_readback=dict(topology=readback.get('readback'),
                                      netem=applied.get('netem'), capacity_kbit=applied.get('capacity_kbit'),
                                      reverse_capacity_kbit=applied.get('reverse_capacity_kbit'),
                                      link_up=applied.get('link_up'), sender_addresses=sender_addresses),
                                  link_up=applied.get('link_up'), status='applied', completed_events=completed)
                    transitions.write(json.dumps(record)+'\n')
                    transitions.flush()
            else:
                events = m['faults'] if mode != 'static-step' else [dict(at_seconds=4, reservation_step=True)]
                for index,fault in enumerate([*events,{'at_seconds':m['duration_seconds']}],1):
                    deadline = start+fault['at_seconds']
                    while next_sample < deadline:
                        time.sleep(max(0,next_sample-time.monotonic()))
                        sample()
                        next_sample += 1
                    time.sleep(max(0,deadline-time.monotonic()))
                    sample(allow=False)
                    if version2:
                        snapshot('measurement_end' if len(fault)==1 else 'pre_transition')
                    if len(fault)==1:
                        break
                    if fault.get('reservation_step'):
                        step = request('research_reservation_step')
                        transitions.write(json.dumps(dict(scheduled_ns=round(deadline*NANO),
                                                          requested=fault, **step))+'\n')
                        transitions.flush()
                        if version2:
                            snapshot('post_transition')
                        continue
                    network.update({key:fault[key] for key in FAULTS[:-1] if key in fault})
                    link_up = fault.get('link_up',link_up)
                    if hybrid:
                        hybrid.reset()
                    with class_update() as lock_timing:
                        begun = time.monotonic_ns()
                        applied = request('research_conditions',network=network,mode=mode,
                                          seed=(seed+index-1)%(2**32-1)+1,link_up=link_up,**ecn_options)
                    transitions.write(json.dumps(dict(scheduled_ns=round(deadline*NANO),begun_ns=begun,
                                                      requested=fault, actuator_lock=lock_timing, **applied))+'\n')
                    transitions.flush()
                    if version2:
                        expected_capacity = applied['capacity_kbit']
                        snapshot('post_transition')
            if version2:
                host_after = v2.host_sample()
                save(directory/'host-noise.json', dict(start=host_before, end=host_after))
            finish_ticks = cpu_ticks()
            runner_usage = measurement.process_delta(runner_before, measurement.process_sample()) if integrity else None
            online_status = request('strategy_rollback') if mode=='online-select' else None
            if online_status:
                save(directory/'strategy.json', online_status)
                if online_status['state'] == 'FAULTED':
                    raise RuntimeError('online canary rollback could not be verified')
            code = client.wait(timeout=12)
            if code not in (0,2):
                raise measurement.WorkloadExit(f'client exited {code} before successful accounting')
            if server.poll() is not None:
                raise RuntimeError('receiver exited during measurement')
            time.sleep(.15)
            if hybrid:
                stop(server)
                for artifact in (receiver_log, control_dir/'hybrid-sockets.json'):
                    if not artifact.exists():
                        continue
                    with artifact.open('rb') as source, tempfile.NamedTemporaryFile(
                            dir=directory, prefix='.evidence-', delete=False) as stream:
                        temporary = Path(stream.name)
                        try:
                            shutil.copyfileobj(source, stream)
                            stream.close()
                            temporary.chmod(0o644)
                            temporary.replace(directory/artifact.name)
                        finally:
                            temporary.unlink(missing_ok=True)
            if mode == 'ecn-dctcp':
                records = (directory/'receiver.jsonl').read_text().splitlines()
                if not records or json.loads(records[-1])['algorithm_fallbacks']:
                    raise RuntimeError('DCTCP server observed congestion-control fallback')
            history = session_history(request('policy_history'),controller_session,round(start*NANO))
            save(directory/'decisions.json',history)
            final_inspect = request('inspect')
            final_inspect['expected_namespace_holders'] = [sender,receiver]
            save(directory/'network.json',final_inspect)
            if final_inspect['namespaces'] != [sender,receiver]:
                raise RuntimeError(f'namespace holder identity changed: expected {[sender,receiver]}, got {final_inspect["namespaces"]}')
            result = summarize(directory,m,rep,mode,samples,history,start,end,
                               tuple(b-a for a,b in zip(begin_ticks,finish_ticks)))
            if hybrid:
                result['hybrid_v3'] = dict(actions=hybrid.actions, rollbacks=hybrid.rollbacks, frozen=hybrid.frozen)
            if online_status:
                result['online_strategy'] = {key: online_status[key] for key in ('switches','regressions','shadow_evaluations','shadow_cpu_ns')}
            if pacing:
                report = result.get('socket_pacing', {})
                if report.get('mode') != mode or not report.get('background_attaches'):
                    raise RuntimeError('pacing workload instrumentation did not attach any background socket')
                result['socket_updates_per_minute'] = report['socket_actions']*60/(end-start)
            if version2:
                recovery_start = None
                if m['version'] == 2:
                    capacity = m['network']['bandwidth_kbit']
                    previous = capacity
                    with (directory/'transitions.jsonl').open() as stream:
                        for line in stream:
                            transition = json.loads(line)
                            current = transition.get('capacity_kbit')
                            if current == capacity and previous is not None and previous < capacity:
                                recovery_start = transition['kernel_confirmed_ns']/NANO
                            if current != capacity:
                                recovery_start = None
                            previous = current
                metrics = v2.account(directory/'requests.csv', result['offered'], start, end,
                                     workload['deadline_ms'], workload['payload_bytes'],
                                     recovery_start=recovery_start)
                if metrics['failed_requests'] != result['failed']:
                    raise RuntimeError('workload summary disagrees with raw ledger')
                result.update(metrics)
                result['total_mbps'] = result['background_mbps'] + result['udp_mbps'] + metrics['request_payload_mbps']
                result['workload_seed'] = seed
                observed_snapshots = snapshots[1:]
                result['observed_rate_windows'] = [v2.observed(a, b, a['configured_kbit'])
                    for a, b in zip(observed_snapshots[::2], observed_snapshots[1::2])]
                if mode == 'cake-diffserv':
                    tins = root(observed_snapshots[-1]['tc']).get('tins', [])
                    if len(tins) != 4 or not all(tins[i].get('sent_packets', 0) > 0 for i in (0, 3)):
                        raise RuntimeError('CAKE DiffServ bulk and voice tins did not both transmit')
                    result['cake_diffserv_tins'] = tins
                class_changes = sum(row['result'] in ('pending', 'verified', 'released', 'restored_baseline')
                                    or bool(row.get('rollback')) for row in history)
                socket_changes = (result.get('socket_pacing') or {}).get('socket_actions', 0)
                result['slo_policy_updates'] = class_changes + (
                    hybrid.actions+hybrid.rollbacks if hybrid else socket_changes)
                result['slo_harmful_actions'] = (sum(row['result']=='rolled_back' and bool(row.get('degraded'))
                                                      for row in history) + (hybrid.adverse if hybrid else 0))
                result['warnings'] = v2.noise(host_before, host_after, metrics, result['observed_rate_windows'])
                if integrity:
                    result['measurement'] = measurement.classify(directory, result, m,
                        dict(start=host_before, end=host_after), runner_usage)
                    result['valid'] = result['measurement']['state'] != 'INVALID_INFRASTRUCTURE'
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
    rng = random.Random(m['seed'])
    seeds = rng.sample(range(1,2**32), m['repetitions']) if m['version']>=2 else []
    integrity = m.get('integrity')
    mode_order = measurement.block_order(m['modes'], m['repetitions'], rng,
                                         integrity['ordering'] if integrity else 'randomized') if seeds else [m['modes']]*m['repetitions']
    order = [(rep, mode) for rep, modes in enumerate(mode_order) for mode in modes]
    if not seeds:
        rng.shuffle(order)
    environment = metadata()
    if integrity:
        environment['analysis_version'] = measurement.VERSION
        environment['experiment_manifest_sha256'] = hashlib.sha256(
            json.dumps(m,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
    data = dict(version=m['version'], status='running', manifest=m, environment=environment, order=order, runs=[],
                block_seeds=seeds, experiment_id=path.name)
    if seeds:
        reproduction = shlex.join(['sudo', str(BINDIR/'flowcore'), 'bench', 'replay', str(path/'experiment.json')])
        (path/'reproduce.sh').write_text('#!/bin/sh\nexec '+reproduction+'\n')
    save(path/'experiment.json',m)
    save(path/'summary.json',data)
    print(f'Evidence: {path}',flush=True,file=sys.stderr)
    for rep, mode in order:
        name = f'r{rep}-{mode}'
        caught = None
        try:
            result = trial(path/name,m,rep,mode,seeds[rep] if seeds else None)
        except KeyboardInterrupt:
            data['status'] = 'interrupted'
            if integrity:
                interrupted = dict(mode=mode, repetition=rep, artifact=name, workload_seed=seeds[rep],
                                   valid=False, error='operator interrupted trial',
                                   measurement=dict(version=measurement.VERSION, state='INVALID_INFRASTRUCTURE',
                                                    issues=[dict(code='INTERRUPTED')]))
                save(path/name/'result.json', interrupted)
                interrupted['evidence_sha256'] = measurement.seal(path/name)
                data['runs'].append(interrupted)
            save(path/'summary.json',data)
            raise
        except (OSError,RuntimeError,ValueError,KeyError,StopIteration,subprocess.SubprocessError) as error:
            caught = error
            result = dict(mode=mode,repetition=rep,valid=False,error=str(error))
            save(path/name/'result.json',result)
        result['artifact'] = name
        if seeds:
            result['workload_seed'] = seeds[rep]
        if integrity:
            if 'measurement' not in result:
                failed_workload = isinstance(caught, measurement.WorkloadExit)
                state = 'FAILED_WORKLOAD' if failed_workload else 'INVALID_INFRASTRUCTURE'
                issue = 'CLIENT_EXIT' if failed_workload else 'TRIAL_FAILED'
                result['measurement'] = dict(version=measurement.VERSION,state=state,
                    issues=[dict(code=issue, detail=result['error'])])
                if failed_workload:
                    try:
                        result['measurement']['partial_ledger'] = measurement.partial_timing_ledger(path/name)
                    except (OSError,ValueError,UnicodeError) as ledger_error:
                        result['measurement']['partial_ledger'] = dict(available=False,valid=False,
                                                                       error=str(ledger_error))
            save(path/name/'result.json', result)
            result['evidence_sha256'] = measurement.seal(path/name)
        data['runs'].append(result)
        save(path/'summary.json',data)
        print(f"{name}: {result.get('p99_ms')} ms, failures={result.get('failed')}, {result.get('error','measured')}",
              flush=True,file=sys.stderr)
        if integrity:
            time.sleep(integrity['cooldown_seconds'])
    data['status'] = 'complete'
    save(path/'summary.json',data)
    return path,data
