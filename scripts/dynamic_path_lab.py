"""Bounded path adaptation experiment on an owned routed path and a direct bypass."""
from collections import deque
import json
import os
import random
import resource
import socket
import statistics
import struct
import subprocess
import sys
import time
from pathlib import Path

from benchmark import Lab, TC, owned_directory
from kernel_api_experiment import host_identity
import multipath as mp
from observer_perf import stop
from paths import RESULTS
from path_adaptation import PathController, PathEvidence, PathIntent
from research_runner import child, exclusive, metadata, save
from research_stats import median_ci
from service_lab import ServiceLab

MODES = ('static-qos', 'linux-default', 'latency-preferred', 'path-aware')
ROUTER, LIMIT, BULK_PORT = 2, 12000, 9018
DEV = 'rt1'
POLL_SECONDS = .4


class RoutedLab(ServiceLab):
    command = Lab.command


def qdisc(lab, index, device):
    rows = json.loads(lab.command(index,[TC,'-j','-s','qdisc','show','dev',device]))
    return next(row for row in rows if row.get('root'))


def router_classes(lab):
    rows = json.loads(lab.command(ROUTER,[TC,'-j','-s','class','show','dev',DEV]))
    rows=[row for row in rows if row.get('class')=='htb']
    if {row['handle'] for row in rows} != {'1:1','1:10','1:20'}:
        raise RuntimeError('router class ownership drift')
    return {row['handle']:row for row in rows}


def configure_router(lab, capacity, *, initial=False):
    lab.assert_router()
    if initial:
        lab.command(ROUTER,[TC,'qdisc','add','dev',DEV,'root','handle','1:','htb','default','20'])
    elif qdisc(lab,ROUTER,DEV)['kind']!='htb':
        raise RuntimeError('router qdisc ownership drift')
    previous = capacity if initial else router_classes(lab)['1:1']['rate']/125
    rates = {'1:1':capacity,'1:10':capacity*3//10,'1:20':capacity-capacity*3//10}
    order = ('1:1','1:10','1:20') if capacity>=previous else ('1:10','1:20','1:1')
    for handle in order:
        argv=[TC,'class','add' if initial else 'change','dev',DEV,
              'parent','1:' if handle=='1:1' else '1:1','classid',handle,'htb',
              'rate',f'{rates[handle]}kbit','ceil',f'{capacity}kbit',
              'burst','16k','cburst','16k','quantum','1514']
        if handle!='1:1':
            argv+=['prio','0' if handle=='1:10' else '1']
        lab.command(ROUTER,argv)
    if initial:
        for handle in ('1:10','1:20'):
            lab.command(ROUTER,[TC,'qdisc','add','dev',DEV,'parent',handle,'fq_codel','limit','256'])
        lab.command(ROUTER,[TC,'filter','add','dev',DEV,'parent','1:','protocol','ip','pref','10',
                            'flower','ip_proto','tcp','dst_port',mp.PORT,'classid','1:10'])
    rows=router_classes(lab)
    if any(row['rate']!=rates[key]*125 or row['ceil']!=capacity*125 for key,row in rows.items()):
        raise RuntimeError('router capacity readback disagrees')
    return rows


def router_policy_readback(lab):
    queues=json.loads(lab.command(ROUTER,[TC,'-j','-s','qdisc','show','dev',DEV]))
    filters=json.loads(lab.command(ROUTER,[TC,'-j','filter','show','dev',DEV,'parent','1:']))
    root=next((row for row in queues if row.get('root')),{})
    leaves={row.get('parent'):row.get('kind') for row in queues if not row.get('root')}
    classified=[row['options'] for row in filters if row.get('kind')=='flower' and 'options' in row]
    if (root.get('kind')!='htb' or root.get('handle')!='1:' or root.get('options',{}).get('default')!='0x20'
            or leaves!={'1:10':'fq_codel','1:20':'fq_codel'} or len(classified)!=1
            or classified[0].get('classid')!='1:10'
            or classified[0].get('keys',{}).get('ip_proto')!='tcp'
            or str(classified[0].get('keys',{}).get('dst_port'))!=str(mp.PORT)):
        raise RuntimeError(f'router queue or classification readback disagrees: {filters}')
    return dict(qdiscs=queues,filters=filters,classes=router_classes(lab))


def topology(lab, seed):
    lab.start_holders(3)
    for index in range(3):
        lab.command(index,['ip','link','set','lo','up'])
    def link(a, left, b, right):
        lab.command(a,['ip','link','add',left,'type','veth','peer','name',right,
                       'netns',lab.namespace_path(b)])
    link(0,'fc0',ROUTER,'rt0')
    link(ROUTER,DEV,1,'fc1')
    for index,iface,address in ((0,'fc0','172.30.1.1/30'),(ROUTER,'rt0','172.30.1.2/30'),
                                (ROUTER,DEV,'172.30.2.2/30'),(1,'fc1','172.30.2.1/30')):
        lab.command(index,['ip','addr','add',address,'dev',iface])
        lab.command(index,['ip','link','set',iface,'mtu','1500','up'])
        lab.command(index,['ethtool','-K',iface,'tso','off','gso','off','gro','off'])
    lab.command(ROUTER,['sysctl','-w','net.ipv4.ip_forward=1'])
    for i,iface,address,remote,via in (
            (0,'fc0','10.203.0.1','10.203.0.2','172.30.1.2'),
            (1,'fc1','10.203.0.2','10.203.0.1','172.30.2.2')):
        lab.command(i,['ip','addr','add',address+'/32','dev',iface])
        lab.command(i,['ip','route','add',remote+'/32','via',via,'dev',iface,'src',address])
    for address,via,iface in (('10.203.0.1','172.30.1.1','rt0'),('10.203.0.2','172.30.2.1',DEV)):
        lab.command(ROUTER,['ip','route','add',address+'/32','via',via,'dev',iface])
    paths=mp._topology(2,'asymmetric')
    paths[0]['profile']=dict(rtt_ms=4,bandwidth_kbit=50000,loss_pct=0)
    paths[1]['profile']=dict(rtt_ms=20,bandwidth_kbit=8000,loss_pct=.1)
    mp._add_path_links(lab,paths)
    for path in paths:
        mp._route_check(lab,path)
        path['route_identity']=route_identity(path['route_readback'])
        mp._set_path_profile(lab,path,seed)
    expected = {(index, row['name']):row['index'] for index in range(3)
                for row in lab.network(index)['interfaces'] if row.get('kind')=='veth'}
    identities = tuple(lab.namespace_identity(index) for index in range(3))
    def assert_router():
        if tuple(lab.namespace_identity(index) for index in range(3))!=identities:
            raise RuntimeError('namespace ownership drift')
        for index in range(3):
            links={row['name']:row['index'] for row in lab.network(index)['interfaces'] if row.get('kind')=='veth'}
            if links!={name:identity for (i,name),identity in expected.items() if i==index}:
                raise RuntimeError('owned path link identity drift')
    lab.assert_router=assert_router
    configure_router(lab,LIMIT,initial=True)
    return paths


def route_identity(route):
    return tuple(route.get(key) for key in ('dst','dev','gateway','from','prefsrc'))


def valid_route(lab, path):
    try:
        mp._route_check(lab,path)
        row=path['route_readback']
        return route_identity(row)==path['route_identity'] and 'linkdown' not in row.get('flags',[])
    except (subprocess.CalledProcessError, RuntimeError):
        return False


def echo_probe(lab, path):
    try:
        with lab.enter(0), socket.socket(socket.AF_INET,socket.SOCK_STREAM) as sock:
            sock.settimeout(.20)
            sock.setsockopt(socket.IPPROTO_TCP,socket.TCP_NODELAY,1)
            sock.bind((path['client_address'],0))
            sock.connect((path['server_address'],mp.PORT))
            start=time.monotonic_ns()
            sock.sendall(b'p'*256)
            if mp._read_exact(sock,256)!=b'p'*256:
                raise RuntimeError('path probe payload mismatch')
            return (time.monotonic_ns()-start)/1e6
    except OSError:
        return None


def sample(lab, paths, evidence):
    views=[lab.network(index) for index in range(3)]
    links=[{row['name']:row for row in view['interfaces']} for view in views]
    frames={}
    for path in paths:
        ident=path['id']
        client=links[0][path['client_interface']]
        server=links[1][path['server_interface']]
        hops=[client,server] + ([links[2]['rt0'],links[2][DEV]] if ident==1 else [])
        available=all(row.get('up') and row.get('carrier') for row in hops)
        route=valid_route(lab,path) if available else False
        observed = qdisc(lab,ROUTER,DEV) if ident==1 else qdisc(lab,0,path['client_interface'])
        classes = router_classes(lab) if ident==1 else None
        if ident==1 and observed['kind']!='htb' or ident==2 and observed['kind']!='netem':
            raise RuntimeError('path queue ownership changed')
        configured = observed.get('options',{}).get('rate')
        if isinstance(configured,dict):
            configured=configured.get('rate')
        cap = classes['1:1']['rate']/125 if classes else configured/125 if configured else None
        captured=time.monotonic_ns()
        probe=echo_probe(lab,path) if available and route else None
        route_key=route_identity(path['route_readback']) if route else None
        frame=dict(path_id=ident,sample_ns=captured,identity=(tuple(row['index'] for row in hops),route_key),
                   available=bool(available),route_valid=route,route=path['route_readback'] if route else None,
                   interfaces=[{k:row.get(k) for k in ('name','index','up','carrier','counters')} for row in hops],
                   location='MANAGED_INTERMEDIATE' if ident==1 else 'LOCAL_EGRESS',
                   capacity_kbit=cap,departed_bytes=observed.get('bytes',0),
                   backlog_bytes=observed.get('backlog',0),queue_drops=observed.get('drops',0),
                   competing_bytes=classes['1:20'].get('stats',{}).get('bytes') if classes else None,
                   probe_ms=probe, configured_loss_pct=path['profile']['loss_pct'])
        frames[ident]=evidence[ident].observe(frame)
    return frames


class EndpointWriter:
    def __init__(self, lab, paths, owned):
        self.lab,self.paths,self.owned=lab,{row['id']:row for row in paths},owned
        self.expected={path:False for path in self.paths}

    def read(self):
        rows=json.loads(self.lab.command(1,['ip','-j','mptcp','endpoint','show']))
        if {row.get('id') for row in rows}!=set(self.owned):
            raise RuntimeError('endpoint set changed outside the controller')
        return {ident:bool(mp.require_endpoint(self.lab,ident,self.owned).get('backup'))
                for ident in self.paths}

    def apply(self, primary):
        self.lab.assert_router()
        candidate=self.paths[primary]
        if not valid_route(self.lab,candidate) or not mp._link_up(self.lab,candidate,0) or not mp._link_up(self.lab,candidate,1):
            raise RuntimeError('candidate path failed transition preflight')
        before=self.read()
        if before!=self.expected:
            raise RuntimeError('endpoint flags changed since the last verified decision')
        target={ident:ident!=primary for ident in self.paths}
        changed=[]
        started=time.monotonic_ns()
        try:
            for ident in (primary,*(ident for ident in self.paths if ident!=primary)):
                if before[ident]!=target[ident]:
                    # Include an attempted update even when post-write readback throws.
                    changed.append(ident)
                    mp.set_endpoint_backup(self.lab,self.paths[ident],target[ident],self.owned)
            if self.read()!=target:
                raise RuntimeError('endpoint flag map did not verify')
        except (OSError,ValueError,RuntimeError,subprocess.SubprocessError) as error:
            rollback='verified'
            try:
                for ident in reversed(changed):
                    mp.set_endpoint_backup(self.lab,self.paths[ident],before[ident],self.owned)
                if self.read()!=before:
                    raise RuntimeError('compensation did not verify')
            except (OSError,ValueError,RuntimeError,subprocess.SubprocessError):
                rollback='uncertain'
            return dict(status='rejected',rollback=rollback,error=str(error)[:256],started_ns=started)
        self.expected=target
        return dict(status='applied',before=before,after=target,changed=changed,
                    started_ns=started,kernel_confirmed_ns=time.monotonic_ns())


def burst_worker(kind, output, start, duration, dynamic):
    payload=b'b'*996
    end=start+int(duration*1e9)
    if kind=='sink':
        counts={9100:set(),9101:set()}
        with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sock:
            sock.bind(('10.203.0.2',BULK_PORT))
            sock.settimeout(.1)
            while time.monotonic_ns()<end+500_000_000:
                try:
                    data,(_,port)=sock.recvfrom(1000)
                except TimeoutError:
                    continue
                if port in counts and len(data)==1000:
                    seq,=struct.unpack_from('!I',data)
                    if seq<duration*1000:
                        counts[port].add(seq)
        save(output,{port:len(rows)*1000 for port,rows in counts.items()})
        return
    offered=[0,0]
    with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as left, socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as right:
        for port,sock in zip((9100,9101),(left,right)):
            sock.bind(('10.203.0.1',port))
            sock.setblocking(False)
        slot=0
        while time.monotonic_ns()<end:
            now=time.monotonic_ns()
            if now<start+slot*4_000_000:
                time.sleep(min(.01,(start+slot*4_000_000-now)/1e9))
                continue
            # 4 Mbit/s normally, 8 Mbit/s under the competing-traffic burst.
            for index,sock in enumerate((left,right)):
                repeat=2 if dynamic and duration*.15 <= (now-start)/1e9 < duration*.76 else 1
                for _ in range(repeat):
                    try:
                        sock.sendto(struct.pack('!I',offered[index])+payload,('10.203.0.2',BULK_PORT))
                    except OSError:
                        pass
                    offered[index]+=1
            slot=max(slot+1,(now-start)//4_000_000)
    save(output,offered)


def read_rows(path, offset=0):
    rows=[]
    if path.exists():
        with path.open('rb') as stream:
            stream.seek(offset)
            while line:=stream.readline():
                if not line.endswith(b'\n'):
                    break
                rows.append(json.loads(line))
                offset=stream.tell()
    return rows,offset


def subflow_totals(rows, paths):
    previous={}
    delivered={path['id']:0 for path in paths}
    retrans={key:0 for key in delivered}
    addresses={(path['client_address'],path['server_address']):path['id'] for path in paths}
    samples=0
    for row in rows:
        flows=row.get('connection',{}).get('full_subflows')
        if not flows:
            continue
        samples+=1
        current={}
        for flow in flows:
            path=addresses.get((flow['local'],flow['remote']))
            info=flow['tcp_info']
            key=(path,flow['id'],flow['local_port'],flow['remote_port'])
            if path is None or 'bytes_acked' not in info or 'total_retrans_segments' not in info:
                continue
            counters=(info['bytes_acked'],info['total_retrans_segments'])
            current[key]=counters
            before=previous.get(key)
            if before and all(value>=old for value,old in zip(counters,before)):
                delivered[path]+=counters[0]-before[0]
                retrans[path]+=counters[1]-before[1]
        previous=current
    return dict(samples=samples,matched_acknowledged_bytes=delivered if samples else None,
                matched_retransmitted_segments=retrans if samples else None)


def fault_schedule(duration):
    return [(duration*offset, name, value) for offset,name,value in (
        (.20,'capacity',10800),(.31,'capacity',9600),(.43,'capacity',1500),
        (.57,'link',False),(.70,'link',True),(.80,'capacity',LIMIT))]


def trial(directory, mode, scenario, duration, seed, intent):
    owned_directory(directory)
    lab=RoutedLab(directory)
    processes=[]
    owned={}
    cleanup=[]
    try:
        paths=topology(lab,seed)
        mp._mptcp_limits(lab,2)
        if any(mp._endpoint_snapshot(lab)):
            raise RuntimeError('fresh namespaces contain MPTCP endpoints')
        for path in paths:
            mp.add_owned_endpoint(lab,path,owned)
        writer=EndpointWriter(lab,paths,owned)
        initial_policy=router_policy_readback(lab)
        save(directory/'topology.json',dict(paths=paths,router_policy=initial_policy,networks=[lab.network(i) for i in range(3)],
             namespaces=[lab.namespace_identity(i) for i in range(3)]))
        def launch(index, script, *args):
            with (directory/f'{args[0]}.log').open('w') as log:
                process=child(lab.ns(index,[sys.executable,'-B',script,*map(str,args)]),stdout=log,stderr=log)
            processes.append(process)
            return process
        ready=directory/'ready'
        server=launch(1,mp.__file__,'server',ready)
        until=time.monotonic()+4
        while not ready.exists():
            if server.poll() is not None or time.monotonic()>until:
                raise RuntimeError('owned echo server failed readiness')
            time.sleep(.025)
        evidence={path['id']:PathEvidence() for path in paths}
        if mode=='latency-preferred':
            initial=mp.choose_path('latency-preferred',[mp.probe_path(lab,path,3) for path in paths])
            fixed=initial['chosen_path_id']
            save(directory/'initial-probes.json',initial)
        else:
            fixed=1
        if mode!='linux-default' and writer.apply(fixed)['status']!='applied':
            raise RuntimeError('initial endpoint policy failed')
        controller=PathController(intent,primary=fixed)
        events=[]; decisions=[]; recent=deque(maxlen=12); offset=0
        start=time.monotonic_ns()+300_000_000
        sink=launch(1,__file__,'sink',directory/'bulk-received.json',start,duration,int(scenario=='dynamic'))
        source=launch(0,__file__,'source',directory/'bulk-offered.json',start,duration,int(scenario=='dynamic'))
        path=paths[fixed-1]
        transfer=directory/'transfer.jsonl'
        client=launch(0,mp.__file__,'client-full',transfer,duration,'mptcp',path['server_address'],path['client_address'])
        schedule=deque(fault_schedule(duration) if scenario=='dynamic' else [])
        poll_cpu=0
        child_cpu=0.0
        next_poll=start
        link_changed=False
        with (directory/'path-samples.jsonl').open('w') as stream:
            while time.monotonic_ns()<start+int(duration*1e9):
                now=time.monotonic_ns()
                while schedule and now>=start+int(schedule[0][0]*1e9):
                    delay,kind,value=schedule.popleft()
                    lab.assert_router()
                    begun=time.monotonic_ns()
                    if kind=='capacity':
                        readback=configure_router(lab,value)
                    else:
                        lab.command(0,['ip','link','set','dev','fc0','up' if value else 'down'])
                        client_up=mp._link_up(lab,paths[0],0)
                        router_up=next(row for row in lab.network(ROUTER)['interfaces'] if row['name']=='rt0')['carrier']
                        if client_up!=value or bool(router_up)!=value:
                            raise RuntimeError('routed link change did not verify')
                        readback=dict(client=client_up,router=router_up)
                        link_changed=True
                    events.append(dict(event=kind,value=value,planned_ns=start+int(delay*1e9),started_ns=begun,
                                       confirmed_ns=time.monotonic_ns(),readback=readback))
                    save(directory/'events.json',events)
                    if events[-1]['confirmed_ns']-events[-1]['planned_ns']>750_000_000:
                        raise RuntimeError('fault application exceeded the 750 ms validity bound')
                    now=time.monotonic_ns()
                if now>=next_poll:
                    cpu=time.process_time_ns()
                    commands_before=resource.getrusage(resource.RUSAGE_CHILDREN)
                    frames=sample(lab,paths,evidence)
                    new,offset=read_rows(transfer,offset)
                    recent.extend(new)
                    now=time.monotonic_ns()
                    fresh=[row for row in recent if 0<=now-row['completed_ns']<=2_000_000_000]
                    app=dict(samples=len(fresh),deadline_misses=sum(row.get('outcome')!='ok'
                        or row['latency_ms']>intent.deadline_ms for row in fresh),
                        p99_ms=mp._request_metrics(fresh,intent.deadline_ms)['p99_ms'])
                    latest=next((row['connection'] for row in reversed(fresh) if row.get('connection')), {})
                    active={(row['local'],row['remote']) for row in latest.get('full_subflows',[])
                            if row.get('tcp_info',{}).get('state_code')==1}
                    for candidate in paths:
                        key=(candidate['client_address'],candidate['server_address'])
                        frames[candidate['id']]['subflow_observed']=bool(
                            latest.get('is_mptcp') is True and not latest.get('fallback') and key in active)
                    decision=controller.evaluate(now,frames,app) if mode=='path-aware' else None
                    if mode=='latency-preferred' and link_changed:
                        selected=fixed if frames[fixed]['available'] else 3-fixed
                        if selected!=controller.primary:
                            decision=dict(action='MPTCP_BACKUP_PREFERENCE',chosen_path_id=selected,
                                          reason='legacy carrier event and initial probe ranking')
                    link_changed=False
                    if decision and decision['action']=='MPTCP_BACKUP_PREFERENCE':
                        selected=decision['chosen_path_id']
                        try:
                            outcome=writer.apply(selected)
                        except (OSError,ValueError,RuntimeError,subprocess.SubprocessError) as error:
                            outcome=dict(status='rejected',rollback='no_write',error=str(error)[:256])
                        decisions.append(dict(decision,decision_ns=now,outcome=outcome))
                        save(directory/'decisions.json',decisions)
                        if outcome['status']=='applied':
                            controller.commit(selected,outcome['kernel_confirmed_ns'])
                        else:
                            controller.frozen=True
                            if outcome['rollback']=='uncertain':
                                raise RuntimeError('endpoint transaction failed with uncertain compensation')
                    stream.write(json.dumps(dict(time_ns=now,paths=frames,application=app,decision=decision),
                                            allow_nan=False)+'\n')
                    stream.flush()
                    poll_cpu+=time.process_time_ns()-cpu
                    commands_after=resource.getrusage(resource.RUSAGE_CHILDREN)
                    child_cpu+=commands_after.ru_utime+commands_after.ru_stime-(
                        commands_before.ru_utime+commands_before.ru_stime)
                    next_poll=now+int(POLL_SECONDS*1e9)
                next_event=start+int(schedule[0][0]*1e9) if schedule else start+int(duration*1e9)
                time.sleep(max(0,min(.025,(min(next_poll,next_event)-time.monotonic_ns())/1e9)))
        for process in (client,source,sink):
            process.wait(timeout=14)
        if client.returncode not in (0,2) or source.returncode or sink.returncode:
            raise RuntimeError('experiment worker exited unexpectedly')
        rows,_=read_rows(transfer)
        infos=[row.get('connection',{}) for row in rows]
        if (not any(info.get('is_mptcp') is True and info.get('total_subflows',0)>=2 for info in infos)
                or any(info.get('fallback') for info in infos)):
            raise RuntimeError('workload did not prove negotiated multipath')
        final_policy=router_policy_readback(lab)
        packet_delta={name:final_policy['classes'][name]['stats']['packets']-
                      initial_policy['classes'][name]['stats']['packets'] for name in ('1:10','1:20')}
        save(directory/'router-readback.json',dict(final=final_policy,packet_delta=packet_delta))
        if any(value<=0 for value in packet_delta.values()):
            raise RuntimeError('router class counters did not demonstrate both traffic classes')
        bulk=json.loads((directory/'bulk-received.json').read_text())
        counts=list(bulk.values())
        up=next((event['confirmed_ns'] for event in events if event['event']=='link' and event['value']),None)
        first=next((row['completed_ns'] for row in rows if up and row['issued_ns']>=up and row['outcome']=='ok'
                    and row['latency_ms']<=intent.deadline_ms),None)
        metrics=mp._request_metrics(rows,intent.deadline_ms)
        elapsed=json.loads((directory/'client-summary.json').read_text())['elapsed_seconds']
        result=dict(metrics,status='MEASURED',connection_survived=client.returncode==0,
                    goodput_mbps=sum(row.get('verified_payload_bytes',0) for row in rows)*8/elapsed/1e6,
                    bulk_mbps=sum(counts)*8/duration/1e6, bulk_fairness=sum(counts)**2/(2*sum(n*n for n in counts)) if any(counts) else None,
                    recovery_ms=(first-up)/1e6 if first else None, subflows=subflow_totals(rows,paths),
                    controller_parent_cpu_ms=poll_cpu/1e6, command_cpu_ms=child_cpu*1000,
                    policy_changes=controller.changes,
                    frozen=controller.frozen,events=events, decisions=decisions)
        save(directory/'summary.json',result)
        return result
    finally:
        try:
            for process in reversed(processes):
                stop(process)
            mp._remove_owned_endpoints(lab,owned,cleanup)
        finally:
            lab.close()
            holders_exited=all(proc.poll() is not None for proc in lab.holders)
            save(directory/'cleanup.json',dict(errors=cleanup,remaining_endpoints=sorted(owned),
                holders_exited=holders_exited))
            if cleanup or owned or not holders_exited:
                raise RuntimeError('owned path cleanup did not verify')


def experiment(*, modes=MODES, scenario='dynamic', repetitions=10, duration=10, seed=44139,
               preference='latency', deadline_ms=100, service=None, multipath=True):
    if (not 1<=repetitions<=20 or not 8<=duration<=60 or not 0<=seed<2**32 or
            scenario not in ('stable','dynamic') or not modes or len(set(modes))!=len(modes) or
            set(modes)-set(MODES)):
        raise ValueError('invalid path experiment configuration')
    intent=PathIntent(preference,deadline_ms,multipath=multipath)
    if not intent.multipath:
        raise ValueError('the path lab requires explicit MPTCP participation')
    with exclusive():
        output=RESULTS/f'dynamic-path-{time.time_ns()}'
        owned_directory(output)
        print(f'Evidence: {output}',file=sys.stderr,flush=True)
        host_before=host_identity()
        mp_before=mp.capabilities()
        rng=random.Random(seed)
        blocks=[dict(repetition=rep,seed=value,order=rng.sample(list(modes),len(modes)))
                for rep,value in enumerate(rng.sample(range(1,2**32-64),repetitions))]
        summary=dict(schema=1,artifact=str(output),environment=metadata(),blocks=blocks,runs=[],
                     scenario=scenario,duration=duration,intent=vars(intent),service=service)
        try:
            for block in blocks:
                for mode in block['order']:
                    rep=block['repetition']
                    path=output/f'r{rep:02d}-{mode}'
                    try:
                        result=trial(path,mode,scenario,duration,block['seed'],intent)
                    except (OSError,ValueError,RuntimeError,subprocess.SubprocessError) as error:
                        result=dict(status='INVALID_EXPERIMENT',error=(getattr(error,'stderr',None) or str(error))[:400])
                    summary['runs'].append(dict(result,mode=mode,repetition=rep,seed=block['seed']))
                    save(output/'summary.json',summary)
                    print(f"{path.name}: {result['status']} {result.get('p99_ms',result.get('error',''))}",file=sys.stderr,flush=True)
            rows={(row['mode'],row['repetition']):row for row in summary['runs'] if row['status']=='MEASURED'}
            fields=('p99_ms','deadline_attainment','failure_ratio','goodput_mbps','bulk_mbps','recovery_ms')
            comparisons={}
            for mode in modes:
                if mode=='path-aware':
                    continue
                pairs=[(rows['path-aware',rep],rows[mode,rep]) for rep in range(repetitions)
                       if ('path-aware',rep) in rows and (mode,rep) in rows]
                comparisons[mode]={}
                for field in fields:
                    values=[a[field]-b[field] for a,b in pairs if a[field] is not None and b[field] is not None]
                    comparisons[mode][field]=dict(pairs=len(values),difference=statistics.median(values) if values else None,
                                                   interval=median_ci(values))
            summary['path_aware_minus_reference']=comparisons
        finally:
            mp_after=mp.capabilities()
            summary['host_unchanged']=host_identity()==host_before and all(mp_before.get(key)==mp_after.get(key)
                for key in ('namespace','endpoints','limits','path_manager','scheduler'))
            save(output/'summary.json',summary)
        if not summary['host_unchanged']:
            raise RuntimeError('host network fingerprint changed')
        return summary


if __name__=='__main__':
    role,path,start,duration,dynamic=sys.argv[1:]
    if role not in ('sink','source'):
        raise SystemExit('worker role must be source or sink')
    burst_worker(role,Path(path),int(start),float(duration),bool(int(dynamic)))
