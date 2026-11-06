"""Eight overlapping application requests on one TCP socket; no packet-priority claim."""
import contextlib
import json
import math
import socket
import struct
import sys
import time
from flowcore_intent import Service


def read(sock,size):
    data=bytearray()
    while len(data)<size:
        part=sock.recv(size-len(data))
        if not part:
            raise EOFError('multiplex connection closed')
        data.extend(part)
    return data


def run(library,path,port):
    records=[]
    completed=[]
    with socket.create_connection(('127.0.0.1',port),timeout=2) as network, \
         Service(library,path,'mux_critical',priority=2) as critical, \
         Service(library,path,'mux_standard') as standard, contextlib.ExitStack() as contexts:
        network.setsockopt(socket.IPPROTO_TCP,socket.TCP_NODELAY,1)
        if not critical.ready() or not standard.ready():
            raise RuntimeError('explicit test service priorities must be authorized')
        critical_cookie=critical.associate_socket(network.fileno())
        standard_cookie=standard.associate_socket(network.fileno())
        if critical_cookie!=standard_cookie:
            raise RuntimeError('socket attestation mismatch')
        for identifier in range(8):
            urgent=identifier%2==0
            service=critical if urgent else standard
            deadline=7000 if urgent else 25000
            start=time.monotonic_ns()
            request=contexts.enter_context(service.request(deadline,critical_cookie))
            records.append(dict(wire_id=identifier,request=request,start_ns=start,deadline_us=deadline,
                                service='mux_critical' if urgent else 'mux_standard'))
        try:
            for record in records:
                payload=b'payment' if record['service']=='mux_critical' else b'database'
                network.sendall(struct.pack('!II',record['wire_id'],len(payload))+payload)
            seen=set()
            for _ in records:
                identifier=struct.unpack('!I',read(network,4))[0]
                if identifier in seen or identifier>=len(records):
                    raise ValueError('invalid multiplex response identity')
                seen.add(identifier)
                record=records[identifier]
                latency=(time.monotonic_ns()-record['start_ns'])/1e6
                record['request'].complete(0)
                completed.append(dict(wire_id=identifier,request_id=record['request'].id,
                    service=record['service'],latency_ms=latency,
                    deadline_missed=latency*1000>record['deadline_us']))
        finally:
            # Every offered request stays in the denominator, including failed I/O.
            durations=sorted(row['latency_ms'] for row in completed)
            critical.flush();standard.flush()
            report=dict(offered=len(records),completed=len(completed),failed=len(records)-len(completed),
                max_outstanding=8,tcp_connections=1,shared_cookie=str(critical_cookie),
                p99_success_ms=durations[math.ceil(.99*len(durations))-1] if durations else None,
                deadline_misses=sum(row['deadline_missed'] for row in completed)+len(records)-len(completed),
                results=completed,classification='none; mixed request priorities share one socket',
                sdk={name:dict(events=client.statistics(),transport=client.transport_statistics())
                     for name,client in (('critical',critical),('standard',standard))})
            print(json.dumps(report),flush=True)


if __name__=='__main__':
    if len(sys.argv)!=4:
        raise SystemExit('usage: multiplex_demo.py LIBRARY CONTROL_SOCKET PORT')
    run(sys.argv[1],sys.argv[2],int(sys.argv[3]))
