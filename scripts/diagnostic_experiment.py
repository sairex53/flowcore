"""Live incident validation and reversible interventions in disposable namespaces."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import json
import os
from queue import Empty, Queue
import random
import socket
import statistics
from threading import Event
import time

from benchmark import RESULTS, ROOT, TC, owned_directory
from bottleneck_experiment import receive
from causal_diagnostics import NS, WINDOW, decode_bundle, replay
from daemon import Service
from kernel_api_experiment import host_identity
from research_faults import resize_htb
from research_runner import exclusive, metadata
from service_config import load
from service_lab import ServiceLab
from workload import percentile

CASES = {
    'queue': 'QUEUE_SATURATION',
    'path': 'NETWORK_PATH_DEGRADATION',
    'application': 'APPLICATION_PROCESSING_DELAY',
    'opaque': None,
    'stable': None,
}
PERIOD = .067
REQUEST = bytes(512)
REPLY = bytes(64)
DEADLINE_US = 15_000


def exact(sock, size):
    data = bytearray()
    while len(data) < size:
        chunk = sock.recv(size-len(data))
        if not chunk:
            raise EOFError('probe closed')
        data.extend(chunk)
    return data


def peer_loop(sock, stop, processing):
    sock.settimeout(.5)
    while not stop.is_set():
        try:
            exact(sock, len(REQUEST))
        except TimeoutError:
            continue
        except EOFError:
            return
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_QUICKACK, 1)
        started = time.monotonic_ns()
        if processing.is_set():
            time.sleep(.021)
        duration = (time.monotonic_ns()-started)/1000
        sock.sendall(REPLY[:-8]+int(duration).to_bytes(8,'big'))


def probe(sock, stop, samples, planned, identity):
    sock.settimeout(.35)
    deadline = time.monotonic()
    while not stop.is_set():
        deadline += PERIOD
        if stop.wait(max(0,deadline-time.monotonic())):
            return
        if time.monotonic()-deadline >= PERIOD:
            planned.append(deadline)
            continue
        start = time.monotonic_ns()
        outcome = 'ok'
        processing = None
        try:
            sock.sendall(REQUEST)
            response = exact(sock,len(REPLY))
            processing = int.from_bytes(response[-8:],'big')
        except TimeoutError:
            outcome = 'timeout'
        except (OSError, EOFError):
            outcome = 'failure'
        end = time.monotonic_ns()
        samples.put_nowait(dict(connection=identity,start=start,stamp=end,outcome=outcome,duration=(end-start)/1000,processing=processing))
        if outcome != 'ok':
            # A failed stream cannot be safely reused without framing recovery.
            # Missing future offered slots are tracked separately.
            return


def socket_pairs(lab, stack, port, count):
    with lab.enter(1):
        listener = stack.enter_context(socket.socket())
        listener.bind(('10.203.0.2',port))
        listener.listen(count)
        listener.settimeout(2)
    pairs = []
    for _ in range(count):
        with lab.enter(0):
            client = stack.enter_context(socket.socket())
            client.settimeout(2)
        client.connect(listener.getsockname())
        peer = stack.enter_context(listener.accept()[0])
        for sock in (client,peer):
            sock.settimeout(.25)
            sock.setsockopt(socket.IPPROTO_TCP,socket.TCP_NODELAY,1)
        pairs.append((client,peer))
    return pairs


def bulk(sock, stop, demand):
    data = bytes(4096)
    while not stop.wait(.01):
        if demand.is_set():
            try:
                sock.send(data)
            except TimeoutError:
                continue


def path_delay(lab, delay):
    lab.command(1,[TC,'qdisc','change','dev','fc1','root','netem','limit','1000','delay',f'{delay}ms'])
    actual = json.loads(lab.command(1,[TC,'-j','qdisc','show','dev','fc1']))
    root = next(row for row in actual if row['kind']=='netem' and row['root'])
    if abs(root['options'].get('delay',{}).get('delay',0)-delay/1000) > .00001:
        raise RuntimeError('netem change was not confirmed')
    return root


def trial(directory, case, variant):
    cfg = load(ROOT/'config/service.json')
    cfg['lab'].update(bandwidth_kbit=4000,delay_ms=0,jitter_ms=0)
    cfg['observability']['bpf'] = 'off'
    cfg['log_level'] = 'error'
    service = Service(cfg)
    lab = ServiceLab(directory)
    app = []
    events = []
    cpu = []
    baseline_cpu = []
    collection_cpu = []
    interventions = []
    planned = []
    processing = Event()
    demand = Event()
    stop = Event()
    raw = Queue(maxsize=1024)
    tc = None
    try:
        lab.start()
        lab.install_policy(cfg['policies']['default'],cfg['lab'])
        service.lab = lab
        service.observer.start(lab)
        service.incident.policy('default',4000)
        for handle in ('1:10','1:20'):
            lab.tc('qdisc','replace','dev','fc0','parent',handle,'pfifo','limit','32')
        # The sender and receiver kernel states are collected on the same local
        # monotonic clock. Peer-reported processing duration is an explicit span.
        with ExitStack() as stack, ThreadPoolExecutor(max_workers=20) as pool:
            pairs = socket_pairs(lab,stack,9000,9 if case == 'queue' else 8)
            probes, bulk_pairs = pairs[:8], pairs[8:]
            if case == 'queue':
                bulk_pairs += socket_pairs(lab,stack,9001,1)
            futures = []
            for source,sink in bulk_pairs:
                futures.extend((pool.submit(bulk,source,stop,demand),pool.submit(receive,stop,sink)))
            identities = []
            for index,(client,server) in enumerate(probes):
                local, peer = client.getsockname(), client.getpeername()
                identities.append((int.from_bytes(client.getsockopt(socket.SOL_SOCKET,57,8),os.sys.byteorder),
                                   (client.family,local[0],local[1],peer[0],peer[1])))
                futures.extend((pool.submit(probe,client,stop,raw,planned,index),
                                pool.submit(peer_loop,server,stop,processing)))
            anchor = ((time.monotonic_ns()+NS)//WINDOW+1)*WINDOW
            service.incident.last_window = anchor
            last = last_seq = 0
            applied = {}
            # Four full measurement windows followed by a two-second ingest grace.
            phases = ('baseline','fault','intervention','restored')
            try:
                for sample in range(73):
                    scheduled = anchor+sample*WINDOW//16
                    time.sleep(max(0,(scheduled-time.monotonic_ns())/NS))
                    now = time.monotonic_ns()
                    phase = min(3,sample//16)
                    if sample in (16,32,48):
                        start = time.monotonic_ns()
                        operation = phases[phase]
                        readback = None
                        if case == 'queue':
                            if phase in (1,3):
                                demand.set()
                                if variant == 'rate' and phase == 3:
                                    resize_htb(service,4000,preserve_windows=True)
                            elif variant == 'rate':
                                resize_htb(service,12000,preserve_windows=True)
                            else:
                                demand.clear()
                            tc = lab.counters()
                            applied_rate = next(row['rate'] for row in tc['class'] if row['handle']=='1:1')/125
                            if applied_rate != service.profile['bandwidth_kbit']:
                                raise RuntimeError('HTB readback mismatch')
                            readback = dict(rate_kbit=applied_rate,bulk_offered=demand.is_set())
                        elif case == 'path':
                            readback = path_delay(lab,0 if phase==2 else 22)
                        elif case in ('application','opaque'):
                            processing.clear() if phase==2 else processing.set()
                        end = time.monotonic_ns()
                        applied[operation] = end
                        interventions.append(dict(phase=operation,scheduled_ns=scheduled,begin_ns=start,verified_ns=end,
                                                  kernel_readback=readback))
                        service.incident.recorder.record('fault',end,action=operation,status='applied',scheduled_ns=scheduled,
                                                         capacity=service.profile['bandwidth_kbit'])
                    while True:
                        try:
                            row = raw.get_nowait()
                        except Empty:
                            break
                        app.append(row)
                        if row['stamp'] < anchor:
                            continue
                        timing = row['processing']
                        stage = {'processing':timing} if timing is not None and timing <= row['duration'] and case != 'opaque' else None
                        start_cpu = time.thread_time_ns()
                        service.observer.ingest(dict(op='request_sample',service='demo',outcome=row['outcome'],
                                                     completed_ns=row['stamp'],duration_us=row['duration'],deadline_ms=15),
                                                os.getuid(),record=False)
                        base_cpu = time.thread_time_ns()
                        cookie, identity = identities[row['connection']] if case != 'opaque' else (None,None)
                        service.incident.request(service='demo',traffic_class='critical',deadline=DEADLINE_US,timings=stage,
                            cookie=cookie,socket_identity=identity,protocol='tcp',identity=f"{row['connection']}:{row['start']}",
                            **{key:row[key] for key in ('stamp','start','outcome','duration')})
                        cpu.append(time.thread_time_ns()-base_cpu)
                        baseline_cpu.append(base_cpu-start_cpu)
                    begin = time.thread_time_ns()
                    if case == 'opaque' and phase in (1,3):
                        service.incident.sample(now,'fc0',{},scope=('lab',1),healthy=False)
                    else:
                        # Existing kernel readback and socket sampling. The timer
                        # below separately measures the diagnostic adapter.
                        service.telemetry = lab.counters()
                        service.observer.sample(lab,service.telemetry,service.profile['bandwidth_kbit'])
                        service.diagnostics.lab_sample(service.telemetry,now/NS)
                        begin = time.thread_time_ns()
                        service.incident.sample(now,'fc0',service.diagnostics.lab.view(),
                            scope=('lab',1),healthy=not (service.observer.errors or service.observer.truncated))
                    service.incident.tick()
                    collection_cpu.append(time.thread_time_ns()-begin)
                    recorder = service.incident.recorder
                    events.extend(sorted((row for ring in (recorder.events,recorder.requests) for row in ring
                                          if row['seq'] > last_seq),key=lambda row:row['seq']))
                    last_seq = recorder.seq
                    if service.incident.last_window > last:
                        last = service.incident.last_window
                        if anchor < last <= anchor+4*WINDOW:
                            report = service.incident.recorder.diagnose('demo')
                            (directory/f'{phases[(last-anchor)//WINDOW-1]}.json').write_text(json.dumps(report,indent=2)+'\n')
            finally:
                stop.set()
                for future in futures:
                    future.result(timeout=3)
        summary = dict(case=case, variant=variant, anchor_ns=anchor, expected=CASES[case], interventions=interventions,
                       attempted=sum(anchor <= row['stamp'] < anchor+4*WINDOW for row in app),
                       skipped_slots=len(planned), final_tc=lab.counters(),
                       adapter_request_cpu_ns=dict(p50=statistics.median(cpu),p99=percentile(cpu,.99)),
                       observer_request_cpu_ns=dict(p50=statistics.median(baseline_cpu),p99=percentile(baseline_cpu,.99)),
                       sample_and_tick_cpu_ns=dict(p50=statistics.median(collection_cpu),p99=percentile(collection_cpu,.99)))
        windows = {phase:json.loads((directory/f'{phase}.json').read_text()) for phase in phases}
        incident_dir = directory/'incidents'
        incident_dir.mkdir()
        for bundle in service.incident.recorder.incidents:
            raw_bundle = json.dumps(bundle).encode()
            assert replay(decode_bundle(raw_bundle)) == replay(bundle)
            (incident_dir/(bundle['id']+'.json')).write_bytes(raw_bundle+b'\n')
        report = windows['fault']
        names = [row['name'] for row in report['candidates']
                 if report['signals'] and row['name'] != 'TELEMETRY_ARTIFACT']
        summary.update(windows=windows, candidate_rank=names.index(CASES[case])+1 if CASES[case] in names else None,
                       incorrect_top=bool(names and names[0] != CASES[case]),
                       detection_seconds=(report['observed_at_ns']-applied['fault'])/NS if report['signals'] else None,
                       incident_bundles=len(service.incident.recorder.incidents))
        return summary
    finally:
        stop.set()
        service.observer.close()
        lab.close()
        (directory/'events.jsonl').write_text(''.join(json.dumps(row)+'\n' for row in events))
        (directory/'requests.jsonl').write_text(''.join(json.dumps(row)+'\n' for row in app))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repetitions',type=int,choices=range(1,6),default=2)
    parser.add_argument('--seed',type=int,default=20261113)
    parser.add_argument('--case',choices=CASES)
    args = parser.parse_args()
    rng = random.Random(args.seed)
    with exclusive():
        before = host_identity()
        out = RESULTS/f'causal-{time.time_ns()}'
        owned_directory(out)
        result = dict(status='running',environment=metadata(),inputs=vars(args),runs=[])
        print(out,flush=True)
        try:
            cases = [args.case] if args.case else list(CASES)
            for repetition in range(args.repetitions):
                for case in rng.sample(cases,len(cases)):
                    directory = out/f'r{repetition}-{case}'
                    owned_directory(directory)
                    variant = 'rate' if repetition%2 else 'load'
                    row = trial(directory,case,variant)
                    result['runs'].append(dict(repetition=repetition,**row))
                    print(json.dumps({key:row[key] for key in ('case','variant','candidate_rank','incorrect_top','detection_seconds')}),flush=True)
            result['status'] = 'complete'
        finally:
            result['host_unchanged'] = host_identity()==before
            (out/'summary.json').write_text(json.dumps(result,indent=2)+'\n')
        if not result['host_unchanged']:
            raise RuntimeError('host link or qdisc identity changed')


if __name__ == '__main__':
    main()
