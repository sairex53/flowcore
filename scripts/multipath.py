"""Experimental MPTCP support for compatible sockets and owned veth paths."""
import ctypes
import ipaddress
import json
import os
import re
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path

from benchmark import TC, owned_directory, run
from observer_perf import stop
from paths import RESULTS, ROOT
from research_runner import child, exclusive, save
from service_lab import ServiceLab
from tcp_diag import tcp_info

PROTOCOL = 262
SOL_MPTCP = 284


def capabilities():
    result = dict(protocol=PROTOCOL, socket_supported=False, namespace_enabled=None,
                  path_manager=None, endpoints=None, limits=None)
    for key, filename in (('namespace_enabled','enabled'),('path_manager','path_manager')):
        try:
            result[key] = Path('/proc/sys/net/mptcp',filename).read_text().strip()
        except OSError:
            pass
    try:
        with socket.socket(socket.AF_INET,socket.SOCK_STREAM,PROTOCOL):
            result['socket_supported'] = True
    except OSError as error:
        result['socket_error'] = str(error)
    for key, command in (('endpoints','endpoint'),('limits','limits')):
        try:
            output = run(['ip','-j','mptcp',command,'show'])
            if key=='limits' and not output.lstrip().startswith(('{','[')):
                values = dict((name,int(value)) for name,value in re.findall(r'([a-z_]+)\s+(\d+)',output))
                if 'subflows' not in values:
                    raise ValueError('invalid ip mptcp limits output')
                result[key] = values
            else:
                result[key] = json.loads(output)
        except (OSError,ValueError,subprocess.SubprocessError) as error:
            result[f'{key}_error'] = str(error)
    return result


def subflow_option(sock, option, stride):
    count = 8
    data = ctypes.create_string_buffer(16+count*stride)
    struct.pack_into('=IIII',data,0,16,0,0,stride)
    length = ctypes.c_uint(ctypes.sizeof(data))
    if ctypes.CDLL(None,use_errno=True).getsockopt(sock.fileno(),SOL_MPTCP,option,
                                                  data,ctypes.byref(length)):
        raise OSError(ctypes.get_errno(),f'MPTCP socket option {option}')
    header, available, kernel_stride, returned_stride = struct.unpack_from('=IIII',data)
    if header!=16 or not 0<returned_stride<=stride or length.value<header:
        raise ValueError('invalid MPTCP subflow payload')
    n = min(count,available,(length.value-header)//returned_stride)
    return [data.raw[header+i*returned_stride:header+i*returned_stride+min(returned_stride,kernel_stride)]
            for i in range(n)]


def sockaddr(data):
    family = struct.unpack_from('=H',data)[0]
    offset,size = (4,4) if family==socket.AF_INET else (8,16) if family==socket.AF_INET6 else (0,0)
    return str(ipaddress.ip_address(data[offset:offset+size])) if size else None


def connection(sock):
    raw = sock.getsockopt(SOL_MPTCP,1,256)
    result = dict(extra_subflows=raw[0], source='MPTCP_INFO')
    for name, offset, form in (('flags',8,'I'),('bytes_retrans',48,'Q'),('bytes_sent',56,'Q'),
                               ('bytes_received',64,'Q'),('bytes_acked',72,'Q'),('total_subflows',80,'B')):
        if len(raw)>=offset+struct.calcsize(form):
            result[name] = struct.unpack_from('='+form,raw,offset)[0]
    result['fallback'] = bool(result.get('flags',0)&1)
    try:
        addresses = subflow_option(sock,3,256)
        result['subflows'] = [dict(local=sockaddr(row),remote=sockaddr(row[128:])) for row in addresses]
        result['subflow_tcp_info'] = [tcp_info(row) for row in subflow_option(sock,2,512)]
        # The options are independent snapshots. Do not pair rows by position.
    except (OSError,ValueError,struct.error) as error:
        result['subflow_error'] = str(error)
    return result


def socket_worker(role, path, duration):
    with socket.socket(socket.AF_INET,socket.SOCK_STREAM,PROTOCOL) as sock:
        sock.settimeout(12)
        if role=='server':
            sock.bind(('0.0.0.0',9017))
            sock.listen(1)
            print('ready',flush=True)
            peer,_ = sock.accept()
            with peer:
                while data:=peer.recv(32768):
                    peer.sendall(data)
            return
        sock.connect(('10.203.0.2',9017))
        start = time.monotonic()
        payload = b'F'*32768
        exchanged = 0
        with Path(path).open('w') as stream:
            while time.monotonic()-start<duration:
                sock.sendall(payload)
                received = bytearray()
                while len(received)<len(payload):
                    chunk = sock.recv(len(payload)-len(received))
                    if not chunk:
                        raise RuntimeError('MPTCP peer closed during transfer')
                    received.extend(chunk)
                if received!=payload:
                    raise RuntimeError('payload verification failed')
                exchanged += len(received)
                stream.write(json.dumps(dict(time_ns=time.monotonic_ns(),verified_payload_bytes=exchanged,
                                             **connection(sock)))+'\n')
                stream.flush()
                time.sleep(.06)


def experiment(duration=10):
    with exclusive():
        path = RESULTS/f'mptcp-{time.time_ns()}'
        owned_directory(path)
        lab = ServiceLab(path)
        server = client = None
        with (path/'server.log').open('w') as sl,(path/'client.log').open('w') as cl:
            try:
                lab.start()
                initial = [json.loads(lab.command(i,[sys.executable,'-B',__file__,'capabilities'])) for i in (0,1)]
                save(path/'capabilities.json',initial)
                if not all(row['socket_supported'] and row['limits'] is not None for row in initial):
                    raise RuntimeError(f'MPTCP unavailable in lab namespaces: {path}')
                lab.command(0,['ip','link','add','mp0','type','veth','peer','name','mp1','netns',lab.namespace_path(1)])
                for index,interface,ip in ((0,'mp0','10.204.0.1/30'),(1,'mp1','10.204.0.2/30')):
                    lab.command(index,['ip','addr','add',ip,'dev',interface])
                    lab.command(index,['ip','link','set',interface,'up'])
                    lab.command(index,['ip','mptcp','limits','set','subflows','2','add_addr_accepted','2'])
                lab.command(1,['ip','mptcp','endpoint','add','10.204.0.2','dev','mp1','id','2','signal'])
                save(path/'endpoints.json',[json.loads(lab.command(i,['ip','-j','mptcp','endpoint','show'])) for i in (0,1)])
                def worker(index, role, log):
                    fd = lab.namespace_fds[index]
                    return child(['nsenter',f'--net=/proc/self/fd/{fd}','--',sys.executable,'-B',__file__,
                                  role,path/'transfer.jsonl',str(duration)],pass_fds=(fd,),stdout=log,stderr=log)
                server = worker(1,'server',sl)
                deadline = time.monotonic()+4
                while 'ready' not in (path/'server.log').read_text():
                    if server.poll() is not None or time.monotonic()>deadline:
                        raise RuntimeError('MPTCP listener startup failed')
                    time.sleep(.05)
                client = worker(0,'client',cl)
                started = time.monotonic()
                events = []
                for offset, argv, event in (
                    (duration*.2,[TC,'qdisc','add','dev','fc0','root','netem','delay','30ms','loss','5%'],'degrade-primary'),
                    (duration*.4,['ip','link','set','fc0','down'],'primary-down'),
                    (duration*.65,['ip','link','set','fc0','up'],'primary-up'),
                    (duration*.7,[TC,'qdisc','del','dev','fc0','root'],'remove-degradation')):
                    time.sleep(max(0,started+offset-time.monotonic()))
                    if client.poll() is not None:
                        raise RuntimeError('MPTCP client exited before scheduled transitions')
                    lab.command(0,argv)
                    applied = time.monotonic_ns()
                    state = lab.network(0)
                    events.append(dict(event=event,scheduled_seconds=offset,applied_ns=applied,interfaces=state['interfaces']))
                    save(path/'transitions.json',events)
                if client.wait(timeout=18):
                    raise RuntimeError(f'MPTCP transfer failed, see {path}')
                rows = [json.loads(line) for line in (path/'transfer.jsonl').read_text().splitlines()]
                if not rows:
                    raise RuntimeError('no MPTCP samples')
                down,up = (events[i]['applied_ns'] for i in (1,2))
                outage = [row for row in rows if down<=row['time_ns']<up]
                result = dict(path=str(path),verified_payload_bytes=rows[-1]['verified_payload_bytes'],
                              peak_reported_subflows=max(row.get('total_subflows',row['extra_subflows']+1) for row in rows),
                              fallback_observed=any(row['fallback'] for row in rows),
                              completed_during_primary_outage=len(outage),
                              completed_after_primary_link_up=sum(row['time_ns']>=up for row in rows))
                result['primary_subflow_seen_after_link_up'] = any(
                    row['time_ns']>=up and any(pair.get('local')=='10.203.0.1'
                        for pair in row.get('subflows',[])) for row in rows)
                lab.command(1,['sysctl','-w','net.mptcp.enabled=0'])
                disabled = json.loads(lab.command(1,[sys.executable,'-B',__file__,'capabilities']))
                save(path/'disabled-capability.json',disabled)
                result['namespace_disable_prevented_mptcp_socket'] = not disabled['socket_supported']
                save(path/'summary.json',result)
                return result
            finally:
                stop(client)
                stop(server)
                lab.close()


if __name__=='__main__':
    if sys.argv[1]=='capabilities':
        print(json.dumps(capabilities()))
    else:
        socket_worker(sys.argv[1],sys.argv[2],float(sys.argv[3]))
