"""Owned-namespace Linux MPTCP path manager experiments and telemetry."""
import ctypes
import html
import ipaddress
import json
import math
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

from benchmark import TC, owned_directory, run
from multipath_engine import STRATEGIES, choose_path, summarize_probe, validate_plan
from observer_perf import stop
from paths import RESULTS, ROOT
from research_runner import child, exclusive, save
from service_lab import ServiceLab
from tcp_diag import tcp_info

PROTOCOL = 262
SOL_MPTCP = 284
TCP_IS_MPTCP = getattr(socket,'TCP_IS_MPTCP',43)
MAX_PATHS = 4
PORT = 9017

PATH_PROFILES = {
    'stable': ((4,16000,0.0),(18,8000,0.05),(42,4000,0.15),(75,2000,0.3)),
    'asymmetric': ((4,20000,0.0),(35,3000,0.5),(18,6000,0.1),(60,2500,0.3)),
    'lossy': ((8,12000,1.0),(15,8000,0.1),(38,4000,0.5),(70,2000,1.5)),
    'primary-failover': ((4,12000,0.0),(20,6000,0.05),(45,3000,0.2),(75,1500,0.5)),
    'multiple-failures': ((5,15000,0.0),(18,8000,0.1),(45,4000,0.3),(75,2000,0.5)),
}
SCENARIOS=tuple(PATH_PROFILES)
COMPARE_STRATEGIES=('single-path-tcp','linux-default','static-endpoints',
                    'latency-preferred','reliability-preferred',
                    'throughput-preferred','slo-aware')


def _namespace_identity():
    stat=os.stat('/proc/self/ns/net')
    return dict(device=stat.st_dev,inode=stat.st_ino)


def _sysctl_values():
    values={}
    for key in ('enabled','path_manager','pm_type','available_path_managers',
                'scheduler','available_schedulers','syn_retrans_before_tcp_fallback',
                'stale_loss_cnt','add_addr_timeout','allow_join_initial_addr_port'):
        try:
            values[key]=Path('/proc/sys/net/mptcp',key).read_text().strip()
        except OSError:
            pass
    return values


def _limits():
    output=run(['ip','-j','mptcp','limits','show'])
    try:
        value=json.loads(output)
        if isinstance(value,dict):
            return value
    except ValueError:
        pass
    values={name:int(value) for name,value in re.findall(r'([a-z_]+)\s+(\d+)',output)}
    if 'subflows' not in values:
        raise ValueError('unrecognized MPTCP limits output')
    return values


def _monitor_probe():
    proc=None
    try:
        proc=subprocess.Popen(['ip','mptcp','monitor'],stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE,text=True)
        time.sleep(.08)
        if proc.poll() is None:
            return dict(supported=True,source='ip mptcp monitor; exited by capability probe')
        error=(proc.stderr.read() if proc.stderr else '').strip()
        return dict(supported=False,error=error[:240])
    except (OSError,subprocess.SubprocessError) as error:
        return dict(supported=False,error=str(error)[:240])
    finally:
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.communicate(timeout=1)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()


def capabilities():
    """Report only the current network namespace; this function changes no state."""
    sysctls=_sysctl_values()
    result=dict(source='current-network-namespace',namespace=_namespace_identity(),
                kernel_release=os.uname().release,architecture=os.uname().machine,
                protocol=PROTOCOL,socket_supported=False,namespace_enabled=sysctls.get('enabled'),
                path_manager=sysctls.get('path_manager'),scheduler=sysctls.get('scheduler'),
                available_path_managers=(sysctls.get('available_path_managers','').split()
                                         if sysctls.get('available_path_managers') else []),
                available_schedulers=(sysctls.get('available_schedulers','').split()
                                      if sysctls.get('available_schedulers') else []),
                sysctls=sysctls,limits=None,endpoints=None)
    try:
        with socket.socket(socket.AF_INET,socket.SOCK_STREAM,PROTOCOL):
            result['socket_supported']=True
    except OSError as error:
        result['socket_error']=dict(errno=error.errno,message=str(error))
    if sysctls.get('path_manager') is None and sysctls.get('pm_type') is not None:
        result['path_manager']='kernel' if sysctls['pm_type']=='0' else 'userspace' if sysctls['pm_type']=='1' else 'unknown'
    if sysctls.get('enabled') is None:
        result['namespace_enabled']='unknown'
    for key,command in (('endpoints',['ip','-j','mptcp','endpoint','show']),
                        ('limits',['ip','-j','mptcp','limits','show'])):
        try:
            value=run(command)
            result[key]=json.loads(value) if key=='endpoints' else _limits()
        except (OSError,ValueError,subprocess.SubprocessError) as error:
            result[key+'_error']=dict(type=type(error).__name__,message=str(error)[:240])
    try:
        result['iproute2_version']=run(['ip','-Version']).strip()
    except (OSError,subprocess.SubprocessError):
        result['iproute2_version']=None
    result['userspace_manager_daemon_installed']=bool(shutil.which('mptcpd'))
    result['netlink_interfaces']={
        'endpoint_dump':result.get('endpoints') is not None,
        'limits_dump':result.get('limits') is not None,
        'monitor':_monitor_probe()}
    result['netlink_interfaces']['available']=all(
        result['netlink_interfaces'][key] for key in ('endpoint_dump','limits_dump'))
    return result


def _tcp_is_mptcp(sock):
    try:
        raw=sock.getsockopt(socket.IPPROTO_TCP,TCP_IS_MPTCP,4)
    except OSError as error:
        return None,dict(errno=error.errno,message=str(error))
    if len(raw)!=4:
        return None,dict(error='short TCP_IS_MPTCP result')
    return bool(struct.unpack('=i',raw)[0]),None


def subflow_option(sock, option, stride):
    count=MAX_PATHS+2
    data=ctypes.create_string_buffer(16+count*stride)
    struct.pack_into('=IIII',data,0,16,0,0,stride)
    length=ctypes.c_uint(ctypes.sizeof(data))
    if ctypes.CDLL(None,use_errno=True).getsockopt(sock.fileno(),SOL_MPTCP,option,
                                                  data,ctypes.byref(length)):
        raise OSError(ctypes.get_errno(),f'MPTCP socket option {option}')
    header,available,kernel_stride,returned_stride=struct.unpack_from('=IIII',data)
    if header!=16 or not 0<returned_stride<=stride or length.value<header:
        raise ValueError('invalid MPTCP subflow payload')
    n=min(count,available,(length.value-header)//returned_stride)
    return [data.raw[header+i*returned_stride:header+i*returned_stride+
                     min(returned_stride,kernel_stride)] for i in range(n)]


def sockaddr(data):
    if len(data)<2:
        return None
    family=struct.unpack_from('=H',data)[0]
    offset,size=(4,4) if family==socket.AF_INET else (8,16) if family==socket.AF_INET6 else (0,0)
    if not size or len(data)<offset+size:
        return None
    return str(ipaddress.ip_address(data[offset:offset+size]))


def tcp_connection(sock):
    data=sock.getsockopt(socket.IPPROTO_TCP,socket.TCP_INFO,512)
    is_mptcp,error=_tcp_is_mptcp(sock)
    return dict(transport='TCP',source='TCP_INFO',is_mptcp=is_mptcp,
                fallback=False,tcp_is_mptcp_error=error,total_subflows=1,
                tcp_info=tcp_info(data),subflows=[])


def full_subflows(sock):
    """Linux MPTCP_FULL_INFO provides both arrays in the same snapshot."""
    count, tcp_stride, addr_stride = MAX_PATHS+2, 512, 264
    addresses = ctypes.create_string_buffer(count*addr_stride)
    transport = ctypes.create_string_buffer(count*tcp_stride)
    header = ctypes.create_string_buffer(136)
    struct.pack_into('=6I2Q', header, 0, 0,tcp_stride,0,addr_stride,0,count,
                     ctypes.addressof(addresses),ctypes.addressof(transport))
    length = ctypes.c_uint(ctypes.sizeof(header))
    if ctypes.CDLL(None,use_errno=True).getsockopt(sock.fileno(),SOL_MPTCP,4,header,ctypes.byref(length)):
        raise OSError(ctypes.get_errno(),'MPTCP_FULL_INFO')
    tcp_kernel,tcp_user,addr_kernel,addr_user,available,capacity=struct.unpack_from('=6I',header)
    # Linux clamps userspace strides to the kernel record widths and
    # packs returned records using those smaller strides.
    if (not 40<=length.value<=ctypes.sizeof(header) or not 1<=tcp_user<=min(tcp_kernel,tcp_stride)
            or addr_user!=addr_stride or addr_kernel<addr_user or capacity!=count or available>count):
        raise ValueError('invalid or truncated MPTCP_FULL_INFO')
    result=[]
    for index in range(available):
        offset=index*addr_user
        row=addresses.raw[offset:offset+addr_user]
        info=transport.raw[index*tcp_user:(index+1)*tcp_user]
        result.append(dict(id=struct.unpack_from('=I',row)[0],local=sockaddr(row[8:136]),
                           remote=sockaddr(row[136:264]),
                           local_port=struct.unpack_from('!H',row,10)[0],
                           remote_port=struct.unpack_from('!H',row,138)[0],tcp_info=tcp_info(info)))
    return result


def connection(sock, full=False):
    raw=sock.getsockopt(SOL_MPTCP,1,256)
    is_mptcp,is_mptcp_error=_tcp_is_mptcp(sock)
    result=dict(transport='MPTCP',extra_subflows=raw[0],source='SOL_MPTCP/MPTCP_INFO',
                is_mptcp=is_mptcp,tcp_is_mptcp_error=is_mptcp_error)
    for name,offset,form in (('flags',8,'I'),('bytes_retrans',48,'Q'),('bytes_sent',56,'Q'),
        ('bytes_received',64,'Q'),('bytes_acked',72,'Q'),('total_subflows',80,'B')):
        if len(raw)>=offset+struct.calcsize(form):
            result[name]=struct.unpack_from('='+form,raw,offset)[0]
    result['fallback']=bool(result.get('flags',0)&1) or is_mptcp is False
    try:
        addresses=subflow_option(sock,3,256)
        result['subflows']=[dict(local=sockaddr(row),remote=sockaddr(row[128:])) for row in addresses]
        # MPTCP_TCPINFO and MPTCP_SUBFLOW_ADDRS are separate getsockopt snapshots;
        # preserve TCP_INFO as an independent list and never zip it to address order.
        result['subflow_tcp_info']=[tcp_info(row) for row in subflow_option(sock,2,512)]
    except (OSError,ValueError,struct.error) as error:
        result['subflow_error']=dict(errno=getattr(error,'errno',None),message=str(error)[:240])
    if full:
        try:
            result['full_subflows']=full_subflows(sock)
            result['full_subflow_source']='MPTCP_FULL_INFO'
        except (OSError,ValueError,struct.error) as error:
            result['full_subflow_error']=str(error)[:160]
    return result


def _percentile(values,fraction):
    ordered=sorted(values)
    return ordered[max(0,math.ceil(len(ordered)*fraction)-1)] if ordered else None


def _topology(path_count,scenario):
    if type(path_count) is not int or not 2<=path_count<=MAX_PATHS:
        raise ValueError(f'path count must be 2..{MAX_PATHS}')
    if scenario not in SCENARIOS:
        raise ValueError('unsupported multipath scenario')
    if scenario=='multiple-failures' and path_count<3:
        raise ValueError('multiple-failures requires at least three independent paths')
    profiles=PATH_PROFILES[scenario]
    result=[]
    for index in range(path_count):
        local=203+index
        client_if='fc0' if index==0 else f'mp{index-1}'
        server_if='fc1' if index==0 else f'mp{index-1}'
        result.append(dict(id=index+1,index=index,client_interface=client_if,
            server_interface=server_if,client_address=f'10.{local}.0.1',
            server_address=f'10.{local}.0.2',profile=dict(zip(('rtt_ms','bandwidth_kbit','loss_pct'),profiles[index]))))
    return result


def _add_path_links(lab,paths):
    for path in paths[1:]:
        client_if=path['client_interface']
        server_if=path['server_interface']
        lab.command(0,['ip','link','add',client_if,'type','veth','peer','name',server_if,
                       'netns',lab.namespace_path(1)])
        for index,interface,address in ((0,client_if,path['client_address']+'/30'),
                                        (1,server_if,path['server_address']+'/30')):
            lab.command(index,['ip','addr','add',address,'dev',interface])
            lab.command(index,['ip','link','set',interface,'mtu','1500','up'])
            lab.command(index,['ethtool','-K',interface,'tso','off','gso','off','gro','off'])


def _tc_options(lab,index,interface):
    rows=json.loads(lab.command(index,[TC,'-j','-d','qdisc','show','dev',interface]))
    root=next((row for row in rows if row.get('root')),None)
    if not root or root.get('kind')!='netem':
        raise RuntimeError(f'expected owned netem qdisc on {interface}')
    return root.get('options',{})


def _set_path_profile(lab,path,seed):
    profile=path['profile']
    one_way=profile['rtt_ms']/2
    observed=[]
    for side,index,interface in (('forward',0,path['client_interface']),
                                 ('reverse',1,path['server_interface'])):
        argv=[TC,'qdisc','add','dev',interface,'root','netem','limit','1000',
              'delay',f'{one_way:.3f}ms','rate',f"{profile['bandwidth_kbit']}kbit"]
        if profile['loss_pct']:
            argv.extend(['loss','random',f"{profile['loss_pct']}%"])
        argv.extend(['seed',str(seed+path['id']*10+index)])
        lab.command(index,argv)
        options=_tc_options(lab,index,interface)
        delay=options.get('delay',{})
        rate=options.get('rate',0)
        rate=rate.get('rate',0) if isinstance(rate,dict) else rate
        loss=options.get('loss-random',{}).get('loss',0)
        if (abs(delay.get('delay',0)-one_way/1000)>.001 or
            abs(rate-profile['bandwidth_kbit']*125)>1 or
            abs(loss-profile['loss_pct']/100)>1e-6):
            raise RuntimeError(f'path qdisc readback mismatch on {interface}')
        observed.append(dict(direction=side,interface=interface,options=options))
    path['qdisc_readback']=observed


def _route_check(lab,path):
    rows=json.loads(lab.command(0,['ip','-j','route','get',path['server_address'],
                                  'from',path['client_address']]))
    if len(rows)!=1 or rows[0].get('dev')!=path['client_interface'] or rows[0].get('prefsrc',rows[0].get('src',rows[0].get('from')))!=path['client_address']:
        raise RuntimeError(f'route does not select the owned {path["client_interface"]} path: {rows}')
    path['route_readback']=rows[0]


def _read_exact(sock,size):
    data=bytearray()
    while len(data)<size:
        chunk=sock.recv(size-len(data))
        if not chunk:
            raise ConnectionError('peer closed before the complete echo response')
        data.extend(chunk)
    return bytes(data)


def probe_path(lab,path,attempts=5):
    latencies=[]
    for _ in range(attempts):
        try:
            with lab.enter(0):
                with socket.socket(socket.AF_INET,socket.SOCK_STREAM,socket.IPPROTO_TCP) as sock:
                    sock.settimeout(2)
                    sock.setsockopt(socket.IPPROTO_TCP,socket.TCP_NODELAY,1)
                    sock.bind((path['client_address'],0))
                    sock.connect((path['server_address'],PORT))
                    if sock.getsockname()[0]!=path['client_address']:
                        raise RuntimeError('probe escaped its designated source path')
                    start=time.monotonic_ns()
                    payload=b'P'*1024
                    sock.sendall(payload)
                    if _read_exact(sock,len(payload))!=payload:
                        raise RuntimeError('probe payload mismatch')
                    latencies.append((time.monotonic_ns()-start)/1e6)
        except (OSError,TimeoutError,ConnectionError):
            pass
    sent=0
    elapsed=.001
    try:
        with lab.enter(0):
            with socket.socket(socket.AF_INET,socket.SOCK_STREAM,socket.IPPROTO_TCP) as sock:
                sock.settimeout(3)
                sock.setsockopt(socket.IPPROTO_TCP,socket.TCP_NODELAY,1)
                sock.bind((path['client_address'],0))
                sock.connect((path['server_address'],PORT))
                payload=b'T'*65536
                start=time.monotonic()
                sock.sendall(payload)
                if _read_exact(sock,len(payload))==payload:
                    sent=len(payload)
                elapsed=max(.001,time.monotonic()-start)
    except (OSError,TimeoutError,ConnectionError):
        pass
    return summarize_probe(path,latencies,len(latencies),attempts,sent,elapsed)


def endpoint(lab, endpoint_id):
    rows=json.loads(lab.command(1,['ip','-j','mptcp','endpoint','show']))
    return next((row for row in rows if row.get('id')==endpoint_id),None)


def endpoint_identity(row):
    return (row.get('address'),row.get('dev'),row.get('signal')) if row else None


def require_endpoint(lab, endpoint_id, owned_ids):
    if endpoint_id not in owned_ids:
        raise ValueError('endpoint is outside this lab ownership set')
    try:
        namespace = lab.namespace_identity(1)
        row = endpoint(lab,endpoint_id)
    except (OSError,ValueError,RuntimeError,subprocess.SubprocessError):
        del owned_ids[endpoint_id]
        raise
    if (namespace,endpoint_identity(row))!=owned_ids[endpoint_id]:
        del owned_ids[endpoint_id]
        raise RuntimeError('MPTCP endpoint ownership drift')
    return row


def add_owned_endpoint(lab,path,owned_ids):
    endpoint_id=path['id']
    namespace = lab.namespace_identity(1)
    if endpoint_id in owned_ids or endpoint(lab,endpoint_id):
        raise RuntimeError('refusing to modify a pre-existing MPTCP endpoint')
    lab.command(1,['ip','mptcp','endpoint','add',path['server_address'],'dev',
                   path['server_interface'],'id',str(endpoint_id),'signal'])
    row = endpoint(lab,endpoint_id)
    identity = (path['server_address'],path['server_interface'],True)
    if lab.namespace_identity(1)!=namespace or endpoint_identity(row)!=identity:
        raise RuntimeError('MPTCP endpoint registration failed readback')
    owned_ids[endpoint_id] = (namespace,identity)
    return row


def set_endpoint_backup(lab,path,backup,owned_ids):
    endpoint_id=path['id']
    if type(backup) is not bool:
        raise ValueError('invalid endpoint backup flag')
    require_endpoint(lab,endpoint_id,owned_ids)
    operation='backup' if backup else 'nobackup'
    lab.command(1,['ip','mptcp','endpoint','change','id',str(endpoint_id),operation])
    row = require_endpoint(lab,endpoint_id,owned_ids)
    if bool(row.get('backup',False))!=backup:
        raise RuntimeError('MPTCP endpoint backup readback mismatch')
    return row


def remove_owned_endpoint(lab,endpoint_id,owned_ids):
    require_endpoint(lab,endpoint_id,owned_ids)
    lab.command(1,['ip','mptcp','endpoint','delete','id',str(endpoint_id)])
    del owned_ids[endpoint_id]
    if endpoint(lab,endpoint_id):
        raise RuntimeError('MPTCP endpoint removal readback mismatch')


def _apply_plan(lab,paths,plan,owned_ids,history,trigger):
    validate_plan(plan,[path['id'] for path in paths])
    backup_ids=set(plan['backup_path_ids'])
    for path in paths:
        require_endpoint(lab,path['id'],owned_ids)
    begun=time.monotonic_ns()
    updates=[]
    for path in paths:
        row=set_endpoint_backup(lab,path,path['id'] in backup_ids,owned_ids)
        updates.append(dict(path_id=path['id'],backup=bool(row.get('backup',False)),endpoint=row))
    confirmed=time.monotonic_ns()
    decision=dict(trigger=trigger,strategy=plan['strategy'],chosen_path_id=plan['chosen_path_id'],
                  backup_path_ids=sorted(backup_ids),observations=plan['observations'],
                  rationale=plan['rationale'],probe_target_feasible=plan['probe_target_feasible'],
                  expected_effect=('kernel scheduler may prefer available non-backup subflows; '
                                   'endpoint backup flags do not choose packets'),
                  kernel_updates=updates,started_ns=begun,kernel_confirmed_ns=confirmed,
                  application_latency_ns=confirmed-begun,status='applied')
    history.append(decision)
    return decision


def _link_up(lab,path,namespace_index=0):
    rows=lab.network(namespace_index)['interfaces']
    key='client_interface' if namespace_index==0 else 'server_interface'
    row=next((item for item in rows if item['name']==path[key]),None)
    # Veth peer admin state stays UP when the other end is taken down;
    # carrier is the kernel-confirmed evidence that the path has recovered.
    return bool(row and row.get('up') and row.get('carrier'))


def _set_link(lab,path,up):
    lab.command(0,['ip','link','set','dev',path['client_interface'],'up' if up else 'down'])
    value=_link_up(lab,path,0)
    if value is not up:
        raise RuntimeError(f'link state readback failed for {path["client_interface"]}')
    peer=_link_up(lab,path,1)
    if peer is not up:
        raise RuntimeError(f'peer link state readback failed for {path["server_interface"]}')
    return dict(link_up=value,peer_link_up=peer,kernel_confirmed_ns=time.monotonic_ns())


def _scenario_events(scenario,primary_path_id,paths,duration):
    if scenario in ('stable','asymmetric','lossy'):
        return []
    if scenario in ('primary-failover',):
        return [(duration*.4,primary_path_id,False,'primary-down'),
                (duration*.68,primary_path_id,True,'primary-restored')]
    others=[path['id'] for path in paths if path['id']!=primary_path_id]
    if not others:
        raise ValueError('multiple-failures needs a second path after the selected primary')
    secondary=others[0]
    return [(duration*.3,primary_path_id,False,'primary-down'),
            (duration*.4,secondary,False,'secondary-down'),
            (duration*.62,secondary,True,'secondary-restored'),
            (duration*.74,primary_path_id,True,'primary-restored')]


def _path_state(lab,paths):
    states=[]
    for path in paths:
        client_up=_link_up(lab,path,0)
        server_up=_link_up(lab,path,1)
        states.append(dict(path_id=path['id'],client_interface=path['client_interface'],
            server_interface=path['server_interface'],client_available=client_up,
            server_available=server_up,available=client_up and server_up))
    return states


def _mptcp_limits(lab,path_count):
    before=[]
    for index in (0,1):
        with lab.enter(index):
            before.append(capabilities())
    for index in (0,1):
        lab.command(index,['ip','mptcp','limits','set','subflows',str(path_count),
                           'add_addr_accepted',str(path_count)])
        with lab.enter(index):
            now=capabilities()
        limits=now.get('limits') or {}
        if limits.get('subflows')!=path_count or limits.get('add_addr_accepted')!=path_count:
            raise RuntimeError('MPTCP subflow/address limits did not read back')
        if now.get('namespace_enabled') not in ('1','true'):
            raise RuntimeError('MPTCP is disabled in an owned experiment namespace')
        manager=now.get('path_manager')
        available=now.get('available_path_managers',[])
        if manager!='kernel' and 'kernel' not in available:
            raise RuntimeError('kernel MPTCP path manager is unavailable in namespace')
        if manager!='kernel':
            lab.command(index,['sysctl','-w','net.mptcp.path_manager=kernel'])
            with lab.enter(index):
                now=capabilities()
            if now.get('path_manager')!='kernel':
                raise RuntimeError('private lab did not select the kernel MPTCP path manager')
        schedulers=now.get('available_schedulers',[])
        if schedulers and now.get('scheduler') not in schedulers:
            raise RuntimeError('namespace MPTCP scheduler readback is not registered')
        before[index]['limits_after']=now.get('limits')
        before[index]['path_manager_after']=now.get('path_manager')
        before[index]['scheduler_after']=now.get('scheduler')
        before[index]['netlink_interfaces_after']=now.get('netlink_interfaces')
        if not now.get('netlink_interfaces',{}).get('available'):
            raise RuntimeError('MPTCP endpoint/limits netlink interface unavailable')
    return before


def _configure_topology(lab,paths,seed):
    _add_path_links(lab,paths)
    for path in paths:
        _route_check(lab,path)
        _set_path_profile(lab,path,seed)
    states=[lab.network(i)['interfaces'] for i in (0,1)]
    for path in paths:
        for side,index,interface,address in (('client',0,path['client_interface'],path['client_address']),
                                               ('server',1,path['server_interface'],path['server_address'])):
            row=next((item for item in states[index] if item['name']==interface),None)
            if not row or not row.get('up') or address not in {a['address'] for a in row['addresses']}:
                raise RuntimeError(f'owned multipath interface/address readback failed for {side}:{interface}')


def _endpoint_snapshot(lab):
    return [json.loads(lab.command(index,['ip','-j','mptcp','endpoint','show'])) for index in (0,1)]


def _remove_owned_endpoints(lab,owned_ids,cleanup_errors):
    removed=[]
    for endpoint_id in sorted(tuple(owned_ids),reverse=True):
        try:
            remove_owned_endpoint(lab,endpoint_id,owned_ids)
            removed.append(endpoint_id)
        except (OSError,ValueError,RuntimeError,subprocess.SubprocessError) as error:
            cleanup_errors.append(dict(endpoint_id=endpoint_id,type=type(error).__name__,message=str(error)[:240]))
    return removed


def _request_metrics(rows,target_latency_ms):
    successes=[row['latency_ms'] for row in rows if row.get('outcome')=='ok']
    failures=[row for row in rows if row.get('outcome')!='ok']
    offered=len(rows)
    data=dict(offered=offered,completed=len(successes),failed=len(failures),
        request_disruptions=len(failures),failure_ratio=len(failures)/offered if offered else None,
        completion_ratio=len(successes)/offered if offered else None,
        p50_ms=_percentile(successes,.50),p95_ms=_percentile(successes,.95),
        p99_ms=_percentile(successes,.99),max_ms=max(successes,default=None))
    if target_latency_ms is not None:
        attained=sum(value<=target_latency_ms for value in successes)
        data.update(target_latency_ms=target_latency_ms,deadline_misses=offered-attained,
                    deadline_attainment=attained/offered if offered else None)
    return data


def _recovery_metrics(rows,events,primary_path):
    down=next((event for event in events if event['event']=='path-down' and
               event['path_id']==primary_path['id']),None)
    up=next((event for event in events if event['event']=='path-up' and
             event['path_id']==primary_path['id']),None)
    if not down or not up:
        return dict(primary_outage_completed_requests=None,primary_subflow_address_reobserved_ms=None,
                    first_request_after_outage_ms=None,outage_goodput_mbps=None,
                    post_recovery_goodput_mbps=None)
    down_ns=down['kernel_confirmed_ns']; up_ns=up['kernel_confirmed_ns']
    completed_during=sum(row.get('outcome')=='ok' and down_ns<=row['completed_ns']<up_ns for row in rows)
    after=[row for row in rows if row.get('outcome')=='ok' and row['completed_ns']>=up_ns]
    restored=next((row for row in rows if row['completed_ns']>=up_ns and any(
        subflow.get('local')==primary_path['client_address'] for subflow in row.get('connection',{}).get('subflows',[]))),None)
    def goodput(samples,seconds):
        return sum(row.get('verified_payload_bytes',0) for row in samples if row.get('outcome')=='ok')*8/seconds/1e6 if seconds>0 else None
    outage_seconds=(up_ns-down_ns)/1e9
    post_seconds=max(0,(max((row['completed_ns'] for row in rows),default=up_ns)-up_ns)/1e9)
    outage_rows=[row for row in rows if down_ns<=row.get('completed_ns',0)<up_ns]
    return dict(primary_outage_completed_requests=completed_during,
        primary_subflow_address_reobserved_ms=((restored['completed_ns']-up_ns)/1e6 if restored else None),
        first_request_after_outage_ms=((after[0]['completed_ns']-up_ns)/1e6 if after else None),
        outage_goodput_mbps=goodput(outage_rows,outage_seconds),
        post_recovery_goodput_mbps=goodput(after,post_seconds))


def _trial(parent,strategy,scenario,path_count,duration,seed,target_latency_ms,service_name,repetition):
    trial_path=parent/f'r{repetition:02d}-{strategy}'
    owned_directory(trial_path)
    paths=_topology(path_count,scenario)
    lab=ServiceLab(trial_path)
    server=client=None
    owned_ids={}
    cleanup_errors=[]
    decisions=[]
    events=[]
    from causal_diagnostics import Recorder
    causal = Recorder()
    client_error=None
    host_before=capabilities()
    with (trial_path/'server.log').open('w') as server_log, (trial_path/'client.log').open('w') as client_log:
        try:
            if os.geteuid()!=0:
                raise PermissionError('multipath experiment requires root for owned network namespaces')
            lab.start()
            namespace_caps=_mptcp_limits(lab,path_count)
            _configure_topology(lab,paths,seed)
            endpoints_before=_endpoint_snapshot(lab)
            if any(endpoints_before):
                raise RuntimeError('fresh lab namespace already contains MPTCP endpoints; refusing to overwrite')
            for path in paths:
                row=add_owned_endpoint(lab,path,owned_ids)
                path['endpoint_id']=row['id']
            endpoints_initial=_endpoint_snapshot(lab)
            save(trial_path/'capabilities.json',dict(host=host_before,namespaces=namespace_caps))
            save(trial_path/'topology.json',dict(paths=paths,namespaces=[lab.network(0),lab.network(1)],
                 endpoint_before=endpoints_before,endpoint_initial=endpoints_initial,
                 namespace_ids=[os.fstat(fd).st_ino for fd in lab.namespace_fds]))

            server=child(['nsenter',f'--net={lab.namespace_path(1)}','--',sys.executable,'-B',__file__,
                          'server',str(trial_path/'server-ready')],stdout=server_log,stderr=server_log)
            deadline=time.monotonic()+4
            while not (trial_path/'server-ready').exists():
                if server.poll() is not None or time.monotonic()>deadline:
                    raise RuntimeError('path probe echo server failed to start')
                time.sleep(.03)
            qualities=[probe_path(lab,path) for path in paths]
            save(trial_path/'path-quality.json',qualities)
            if strategy=='single-path-tcp':
                plan=dict(strategy=strategy,chosen_path_id=paths[0]['id'],backup_path_ids=[],
                          all_paths_active=False,target_latency_ms=target_latency_ms,probe_target_feasible=None,
                          rationale='TCP comparator uses one path and does not create MPTCP subflows.',observations=qualities)
                transport='tcp'
            else:
                plan=choose_path(strategy,qualities,target_latency_ms)
                transport='mptcp'
                _apply_plan(lab,paths,plan,owned_ids,decisions,'initial-path-policy')
            chosen=next(path for path in paths if path['id']==plan['chosen_path_id'])
            events_plan=_scenario_events(scenario,chosen['id'],paths,duration)
            start=time.monotonic()
            start_ns=time.monotonic_ns()
            client=child(['nsenter',f'--net={lab.namespace_path(0)}','--',sys.executable,'-B',__file__,
                          'client',str(trial_path/'transfer.jsonl'),str(duration),transport,
                          chosen['server_address'],chosen['client_address']],stdout=client_log,stderr=client_log)
            for offset,path_id,up,name in events_plan:
                when=start+offset
                time.sleep(max(0,when-time.monotonic()))
                path=next(item for item in paths if item['id']==path_id)
                action_start=time.monotonic_ns()
                state=_set_link(lab,path,up)
                event=dict(event='path-up' if up else 'path-down',name=name,path_id=path_id,
                           interface=path['client_interface'],scheduled_ns=start_ns+round(offset*1e9),
                           actual_application_ns=action_start,kernel_confirmed_ns=state['kernel_confirmed_ns'],
                           operation_latency_ns=state['kernel_confirmed_ns']-action_start,
                           kernel_state=state,active_paths=_path_state(lab,paths))
                if strategy in ('latency-preferred','reliability-preferred','throughput-preferred','slo-aware'):
                    unavailable=[item['id'] for item in paths
                                 if not _link_up(lab,item,0) or not _link_up(lab,item,1)]
                    next_plan=choose_path(strategy,qualities,target_latency_ms,unavailable)
                    decision=_apply_plan(lab,paths,next_plan,owned_ids,decisions,
                                         'path-recovery' if up else 'path-failure')
                    event['policy_decision']=decision
                    event['policy_adaptation_ms']=decision['application_latency_ns']/1e6
                elif strategy=='linux-default':
                    event['policy_decision']=dict(status='kernel-default',
                        rationale='No endpoint backup flags change; the kernel path manager and default scheduler remain in control.')
                else:
                    event['policy_decision']=dict(status='static-endpoints',
                        rationale='Keep the initial static endpoint backup map; kernel performs failover.')
                events.append(event)
                if transport == 'mptcp':
                    causal.record('mptcp_path', state['kernel_confirmed_ns'], service_name,
                                  transport='mptcp', source='rtnetlink_lab_readback', path_id=path_id, up=up)
                save(trial_path/'transitions.json',events)
                save(trial_path/'causal-path-events.json',list(causal.events))
            time.sleep(max(0,start+duration-time.monotonic()))
            if client.poll() is None:
                try:
                    client.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    client_error=dict(type='timeout',message='client exceeded bounded transfer deadline')
                    stop(client)
            code=client.poll()
            if code not in (0,2):
                client_error=client_error or dict(type='worker_exit',exit_code=code)
            error_file=trial_path/'client-error.json'
            if error_file.exists():
                client_error=json.loads(error_file.read_text())
            rows=[]
            transfer=trial_path/'transfer.jsonl'
            if transfer.exists():
                for line in transfer.read_text().splitlines():
                    if line.strip():
                        rows.append(json.loads(line))
            rows.sort(key=lambda row:row.get('request_id',0))
            transport_mptcp=transport=='mptcp'
            fallback=transport_mptcp and any(row.get('connection',{}).get('fallback') or
                row.get('connection',{}).get('is_mptcp') is False for row in rows)
            confirmation=transport_mptcp and any(row.get('connection',{}).get('is_mptcp') is True and
                row.get('connection',{}).get('total_subflows',0)>=2 for row in rows)
            max_subflows=max((row.get('connection',{}).get('total_subflows',0) for row in rows),default=0)
            request_summary=_request_metrics(rows,target_latency_ms)
            recovery=_recovery_metrics(rows,events,chosen)
            status='valid'
            invalid_reason=None
            if transport_mptcp and fallback:
                status='invalid_fallback'
                invalid_reason='TCP_IS_MPTCP/MPTCP_INFO identified TCP fallback; no MPTCP success is claimed.'
            elif transport_mptcp and not confirmation:
                status='invalid_no_multipath'
                invalid_reason='No sample proved at least two MPTCP subflows using TCP_IS_MPTCP and MPTCP_INFO.'
            elif not rows:
                status='invalid_no_samples'
                invalid_reason='No application transfer samples were recorded.'
            mptcp_rows=[row for row in rows if row.get('connection',{}).get('transport')=='MPTCP']
            observed_addresses=sorted({flow.get('local') for row in mptcp_rows
                                       for flow in row.get('connection',{}).get('subflows',[])
                                       if flow.get('local')})
            result=dict(version=2,strategy=strategy,scenario=scenario,repetition=repetition,seed=seed,
                service=service_name,transport=transport,status=status,
                valid=status=='valid',invalid_reason=invalid_reason,client_error=client_error,
                connection_survived=(code==0),mptcp_confirmed=confirmation,fallback_observed=fallback,
                configured_path_count=path_count,observed_mptcp_subflows=max_subflows,
                observed_subflow_source_addresses=observed_addresses,
                offered_bulk_requests=len(rows),verified_payload_bytes=sum(
                    row.get('verified_payload_bytes',0) for row in rows if row.get('outcome')=='ok'),
                path_quality=qualities,initial_policy=plan,policy_decisions=decisions,
                transitions=events,request_metrics=request_summary,**request_summary,
                recovery=recovery,primary_subflow_address_reobserved_ms=recovery['primary_subflow_address_reobserved_ms'],
                outage_goodput_mbps=recovery['outage_goodput_mbps'],
                post_recovery_goodput_mbps=recovery['post_recovery_goodput_mbps'],
                namespace_ids=[os.fstat(fd).st_ino for fd in lab.namespace_fds],
                endpoints_before=endpoints_before,endpoints_initial=endpoints_initial,
                endpoints_final=_endpoint_snapshot(lab),host_mptcp_state_before=host_before,
                application_latency_source='monotonic send-to-verified-echo',
                mptcp_info_source='SOL_MPTCP getsockopt MPTCP_INFO',
                fallback_source='TCP_IS_MPTCP plus MPTCP_INFO fallback flag',
                created_monotonic_ns=start_ns)
            with lab.enter(1):
                disabled_before=capabilities()
            lab.command(1,['sysctl','-w','net.mptcp.enabled=0'])
            with lab.enter(1):
                disabled_after=capabilities()
            result['namespace_disable_prevented_mptcp_socket']=not disabled_after['socket_supported']
            result['namespace_mptcp_enabled_before_disable']=disabled_before.get('namespace_enabled')
            if not result['namespace_disable_prevented_mptcp_socket']:
                raise RuntimeError('namespace MPTCP disable did not prevent new MPTCP sockets')
            save(trial_path/'summary.json',result)
            return result
        finally:
            stop(client)
            stop(server)
            removed=_remove_owned_endpoints(lab,owned_ids,cleanup_errors)
            save(trial_path/'cleanup.json',dict(removed_endpoints=removed,remaining_owned_endpoints=sorted(owned_ids),errors=cleanup_errors))
            lab.close()


def _stats(rows,key):
    values=[row[key] for row in rows if row.get('valid') and type(row.get(key)) in (int,float)
            and math.isfinite(row[key])]
    import statistics
    return dict(n=len(values),median=statistics.median(values) if values else None,
                minimum=min(values) if values else None,maximum=max(values) if values else None)


def _render_report(output,summary):
    rows=summary['runs']
    fields=('strategy','repetition','status','mptcp_confirmed','fallback_observed',
            'observed_mptcp_subflows','connection_survived','p50_ms','p95_ms','p99_ms',
            'failure_ratio','deadline_attainment','verified_payload_bytes')
    with (output/'report.csv').open('w') as stream:
        import csv
        writer=csv.DictWriter(stream,fieldnames=fields,extrasaction='ignore')
        writer.writeheader()
        for row in rows:
            writer.writerow(dict(row,**row.get('request_metrics',{})))
    cells=''.join(f'<th>{html.escape(key)}</th>' for key in fields)
    body=[]
    for row in rows:
        values={**row,**row.get('request_metrics',{})}
        body.append('<tr>'+''.join(f'<td>{html.escape(str(values.get(key,"")))}</td>' for key in fields)+'</tr>')
    path_rows=[]
    for path in [dict(path,strategy=trial['strategy'],repetition=trial['repetition'])
                 for trial in rows for path in trial.get('path_quality',[])]:
        path_rows.append('<tr>'+''.join(f'<td>{html.escape(str(path.get(key,"")))}</td>'
            for key in ('strategy','repetition','path_id','interface','rtt_p50_ms','rtt_p95_ms','probe_failure_ratio','throughput_mbps'))+'</tr>')
    page='''<!doctype html><meta charset="utf-8"><title>FLOWCORE Multipath Lab</title>
<style>body{font:15px system-ui;margin:2rem;color:#18212b}table{border-collapse:collapse;margin:1rem 0}td,th{border:1px solid #bbb;padding:.4rem .6rem}th{background:#eef4fa}.note{max-width:75ch}</style>
<h1>FLOWCORE Intelligent Multipath Engine</h1>
<p class="note">Rows marked invalid do not count as MPTCP successes. The engine changes only owned namespace endpoints and leaves packet selection to the kernel scheduler.</p>
<h2>Run outcomes</h2><table><thead><tr>'''+cells+'''</tr></thead><tbody>'''+''.join(body)+'''</tbody></table>
<h2>Per-path probes</h2><table><thead><tr>'''+''.join(f'<th>{key}</th>' for key in
    ('strategy','repetition','path_id','interface','rtt_p50_ms','rtt_p95_ms','probe_failure_ratio','throughput_mbps'))+'''</tr></thead><tbody>'''+''.join(path_rows)+'''</tbody></table>
<h2>Scope</h2><p>Application echo latency is measured separately from MPTCP TCP_INFO and MPTCP_INFO. A successful TCP fallback is never labeled multipath.</p>'''
    (output/'report.html').write_text(page+'\n')


def experiment(strategy='linux-default',scenario='primary-failover',path_count=2,duration=10,
               target_latency_ms=None,service_name=None,repetitions=1,compare=False,seed=20261107,service_intent=None):
    if scenario not in SCENARIOS:
        raise ValueError('unsupported multipath scenario')
    if type(path_count) is not int or not 2<=path_count<=MAX_PATHS:
        raise ValueError(f'path count must be 2..{MAX_PATHS}')
    if type(duration) not in (int,float) or not math.isfinite(duration) or not 6<=duration<=180:
        raise ValueError('duration must be 6..180 seconds')
    if type(repetitions) is not int or not 1<=repetitions<=10:
        raise ValueError('repetitions must be 1..10')
    if type(seed) is not int or not 0<=seed<2**32:
        raise ValueError('seed must be an unsigned 32-bit integer')
    if compare:
        strategies=list(COMPARE_STRATEGIES)
    else:
        strategies=[strategy]
    if strategy not in (*STRATEGIES,'single-path-tcp'):
        raise ValueError('unsupported multipath strategy')
    if any(item not in (*STRATEGIES,'single-path-tcp') for item in strategies):
        raise ValueError('unsupported multipath comparison strategy')
    if 'slo-aware' in strategies and (type(target_latency_ms) not in (int,float) or
            not math.isfinite(target_latency_ms) or target_latency_ms<=0):
        target_latency_ms=25.0
    if target_latency_ms is not None and (type(target_latency_ms) not in (int,float) or
            not math.isfinite(target_latency_ms) or not 0<target_latency_ms<=600000):
        raise ValueError('target latency must be in (0, 600000] milliseconds')
    if scenario=='multiple-failures' and path_count<3:
        raise ValueError('multiple-failures requires --paths >= 3')
    with exclusive():
        output=RESULTS/f'multipath-{time.time_ns()}'
        owned_directory(output)
        host_before=capabilities()
        runs=[]
        order=[]
        for repetition in range(repetitions):
            rotation=repetition%len(strategies)
            current=strategies[rotation:]+strategies[:rotation]
            for offset,current_strategy in enumerate(current):
                actual_seed=(seed+repetition)%(2**32)
                result=_trial(output,current_strategy,scenario,path_count,duration,actual_seed,
                              target_latency_ms,service_name,repetition)
                result['order']=offset
                runs.append(result)
                order.append(dict(repetition=repetition,position=offset,strategy=current_strategy,seed=actual_seed))
        host_after=capabilities()
        host_unchanged=(host_before.get('namespace')==host_after.get('namespace') and
            host_before.get('endpoints')==host_after.get('endpoints') and
            host_before.get('limits')==host_after.get('limits') and
            host_before.get('path_manager')==host_after.get('path_manager') and
            host_before.get('scheduler')==host_after.get('scheduler'))
        summary=dict(version=2,experiment=str(output),scenario=scenario,path_count=path_count,
            duration_seconds=duration,target_latency_ms=target_latency_ms,service=service_name,
            requested_strategies=strategies,repetitions=repetitions,seed=seed,order=order,
            service_intent=service_intent,
            host_mptcp_state_unchanged=host_unchanged,
            host_mptcp_state_before=host_before,host_mptcp_state_after=host_after,runs=runs,
            aggregates={mode:{key:_stats([row for row in runs if row['strategy']==mode],key)
                for key in ('p99_ms','failure_ratio','deadline_attainment','verified_payload_bytes',
                            'observed_mptcp_subflows','primary_subflow_address_reobserved_ms',
                            'outage_goodput_mbps','post_recovery_goodput_mbps')}
                for mode in strategies},
            comparison_status=('INCONCLUSIVE' if repetitions<10 else 'EXPLORATORY_REPEATED'))
        save(output/'summary.json',summary)
        _render_report(output,summary)
        summary['report_html']=str(output/'report.html')
        summary['report_csv']=str(output/'report.csv')
        save(output/'summary.json',summary)
        return summary


def _server(ready_path):
    slots=threading.BoundedSemaphore(12)

    def echo(peer):
        try:
            with peer:
                peer.settimeout(12)
                while data := peer.recv(32768):
                    peer.sendall(data)
        except OSError:
            pass
        finally:
            slots.release()

    with socket.socket(socket.AF_INET,socket.SOCK_STREAM,PROTOCOL) as listener:
        listener.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
        listener.bind(('0.0.0.0',PORT))
        listener.listen(12)
        Path(ready_path).write_text('ready\n')
        while True:
            peer,_=listener.accept()
            if slots.acquire(False):
                threading.Thread(target=echo,args=(peer,),daemon=True).start()
            else:
                peer.close()


def _client(path,duration,transport,destination,source,full=False):
    proto=PROTOCOL if transport=='mptcp' else socket.IPPROTO_TCP
    rows=[]
    error=None
    request_id=0
    started=time.monotonic()
    try:
        with socket.socket(socket.AF_INET,socket.SOCK_STREAM,proto) as sock:
            sock.settimeout(12)
            sock.setsockopt(socket.IPPROTO_TCP,socket.TCP_NODELAY,1)
            sock.connect((destination,PORT))
            if sock.getsockname()[0]!=source:
                raise RuntimeError('transport connection used the wrong initial path')
            payload=b'F'*32768
            with Path(path).open('w') as stream:
                while time.monotonic()-started<duration:
                    request_id+=1
                    issued=time.monotonic_ns()
                    try:
                        sock.sendall(payload)
                        response=_read_exact(sock,len(payload))
                        if response!=payload:
                            raise RuntimeError('MPTCP echo payload verification failed')
                        completed=time.monotonic_ns()
                        info=connection(sock,full=full) if transport=='mptcp' else tcp_connection(sock)
                        row=dict(request_id=request_id,issued_ns=issued,completed_ns=completed,
                                 latency_ms=(completed-issued)/1e6,outcome='ok',
                                 verified_payload_bytes=len(response),connection=info)
                    except (OSError,TimeoutError,ConnectionError,RuntimeError) as exc:
                        completed=time.monotonic_ns()
                        row=dict(request_id=request_id,issued_ns=issued,completed_ns=completed,
                                 latency_ms=(completed-issued)/1e6,outcome=type(exc).__name__,
                                 verified_payload_bytes=0,error=str(exc)[:240],connection={})
                        rows.append(row)
                        stream.write(json.dumps(row,allow_nan=False)+'\n')
                        stream.flush()
                        error=dict(type=type(exc).__name__,message=str(exc)[:240])
                        break
                    rows.append(row)
                    stream.write(json.dumps(row,allow_nan=False)+'\n')
                    stream.flush()
                    if transport=='mptcp' and row['connection'].get('fallback'):
                        error=dict(type='fallback',message='TCP_IS_MPTCP or MPTCP_INFO reported fallback')
                        break
                    time.sleep(.06)
    except (OSError,TimeoutError,ConnectionError,RuntimeError) as exc:
        error=dict(type=type(exc).__name__,message=str(exc)[:240])
    Path(path).with_name('client-error.json').write_text(json.dumps(error,indent=2)+'\n' if error else 'null\n')
    Path(path).with_name('client-summary.json').write_text(json.dumps(dict(
        transport=transport,offered=len(rows),completed=sum(row['outcome']=='ok' for row in rows),
        failed=sum(row['outcome']!='ok' for row in rows),connection_survived=error is None,
        elapsed_seconds=time.monotonic()-started),indent=2)+'\n')
    return 2 if error else 0


def _main():
    if sys.argv[1]=='capabilities':
        print(json.dumps(capabilities()))
        return 0
    if sys.argv[1]=='server':
        _server(sys.argv[2])
        return 0
    if sys.argv[1] in ('client','client-full'):
        return _client(sys.argv[2],float(sys.argv[3]),sys.argv[4],sys.argv[5],sys.argv[6],sys.argv[1]=='client-full')
    raise SystemExit('invalid multipath worker role')


if __name__=='__main__':
    sys.exit(_main())
