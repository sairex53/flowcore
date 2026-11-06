"""Real TCP/MPTCP/QUIC echo workloads; QUIC via aioquic, not HTTP/3."""
import argparse
import asyncio
import json
import socket
import time
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'examples/python'))
from paths import BINDIR, INSTALLED
from flowcore_intent import Service
from quic_adapter import CooperativeQuic
from aioquic.asyncio import serve, connect
from aioquic.quic.configuration import QuicConfiguration

INTENT_LIBRARY=(ROOT/(ROOT/'intent-library').read_text().strip()).resolve() if INSTALLED else BINDIR/'libflowcore_intent.so'
SIZE=4096
ALPN=['flowcore-echo-v1']


async def echo(reader,writer):
    try:
        data=await asyncio.wait_for(reader.readexactly(SIZE),5)
        writer.write(data)
        await writer.drain()
        writer.write_eof()
    except (OSError,asyncio.TimeoutError,asyncio.IncompleteReadError):
        writer.close()


async def server(args):
    cfg=QuicConfiguration(is_client=False,alpn_protocols=ALPN)
    cfg.load_cert_chain(args.cert,args.key)
    tasks=set()
    def stream(reader,writer):
        if len(tasks)>=64:
            writer.close()
            return
        task=asyncio.create_task(echo(reader,writer));tasks.add(task)
        task.add_done_callback(tasks.discard)
    quic=await serve('0.0.0.0',19017,configuration=cfg,stream_handler=stream)
    sock=socket.socket(socket.AF_INET,socket.SOCK_STREAM,262)
    sock.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
    sock.bind(('0.0.0.0',19017));sock.listen(64);sock.setblocking(False)
    async def tcp_echo(reader,writer):
        try:
            while True:
                data=await reader.readexactly(SIZE)
                writer.write(data);await writer.drain()
        except (asyncio.IncompleteReadError,OSError):
            writer.close()
    tcp=await asyncio.start_server(tcp_echo,sock=sock)
    Path(args.ready).touch()
    try:
        await asyncio.Event().wait()
    finally:
        quic.close();tcp.close();await tcp.wait_closed()
        for task in tasks:task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)


async def client(args):
    services={}
    rows=[]
    confirmations={}
    setup={}
    cookies={}
    payload=b'F'*SIZE
    for kind in ('tcp','mptcp','quic'):
        service=Service(INTENT_LIBRARY,args.socket,'tr_'+kind)
        if not await asyncio.to_thread(service.ready):raise RuntimeError('SDK daemon unavailable')
        services[kind]=service

    async def tcp(kind):
        sock=socket.socket(socket.AF_INET,socket.SOCK_STREAM,262 if kind=='mptcp' else 6)
        sock.setblocking(False);sock.setsockopt(socket.IPPROTO_TCP,socket.TCP_NODELAY,1)
        start=time.monotonic_ns()
        await asyncio.wait_for(asyncio.get_running_loop().sock_connect(sock,(args.host,19017)),5)
        setup[kind]=(time.monotonic_ns()-start)/1e6
        negotiated=bool(int.from_bytes(sock.getsockopt(socket.IPPROTO_TCP,43,4),sys.byteorder))
        confirmations[kind]=dict(is_mptcp=negotiated,multipath_proven=False)
        if kind=='mptcp' and not negotiated:raise RuntimeError('MPTCP fell back to TCP')
        service=services[kind]
        cookie=await asyncio.to_thread(service.lib.fc_v2_associate,service.handle,sock.fileno())
        if not cookie:raise RuntimeError('socket association failed')
        cookies[kind]=int(cookie)
        await asyncio.to_thread(service.declare_transport,kind,kind,False,False,int(cookie))
        reader,writer=await asyncio.open_connection(sock=sock)
        return reader,writer

    connections={kind:await tcp(kind) for kind in ('tcp','mptcp')}
    config=QuicConfiguration(is_client=True,alpn_protocols=ALPN,server_name='localhost')
    config.load_verify_locations(cafile=args.cert)
    start=time.monotonic_ns()
    async with connect(args.host,19017,configuration=config,create_protocol=CooperativeQuic) as quic:
        setup['quic']=(time.monotonic_ns()-start)/1e6
        confirmations['quic']=dict(handshake_confirmed=quic.handshake_confirmed,library='aioquic')
        if not quic.handshake_confirmed:raise RuntimeError('QUIC handshake not confirmed')
        cookies['quic']=await asyncio.to_thread(quic.declare,services['quic'],'quic')
        async def request(kind,phase):
            start=time.monotonic_ns();outcome='ok'
            try:
                if kind=='quic':
                    reader,writer=await quic.create_stream()
                    scope=services[kind].stream_request(writer.get_extra_info('stream_id'),priority=4,
                                                       deadline_us=50000,cookie=cookies[kind])
                else:
                    scope=services[kind].request(deadline_us=50000,cookie=cookies[kind])
                with scope:
                    if kind=='quic':
                        writer.write(payload);writer.write_eof()
                    else:
                        reader,writer=connections[kind];writer.write(payload);await writer.drain()
                    data=await asyncio.wait_for(reader.readexactly(SIZE),3)
                    if data!=payload:raise RuntimeError('echo payload mismatch')
            except (OSError,TimeoutError,asyncio.IncompleteReadError,RuntimeError):
                outcome='failure'
            end=time.monotonic_ns()
            rows.append(dict(kind=kind,phase=phase,start_ns=start,end_ns=end,
                             latency_ms=(end-start)/1e6,outcome=outcome,bytes=SIZE if outcome=='ok' else 0))
        for kind in ('tcp','mptcp','quic'):
            for _ in range(args.requests):await request(kind,'individual')
        if args.active:
            Path(args.active).touch()
            deadline=time.monotonic()+10
            while not Path(args.go).exists():
                if time.monotonic()>deadline:raise RuntimeError('fault gate timeout')
                await asyncio.sleep(.01)
        for _ in range(args.requests):
            await asyncio.gather(*(request(kind,'mixed') for kind in ('tcp','mptcp','quic')))
        async def cancel():
            with services['quic'].request(deadline_us=50000,cookie=cookies['quic']):
                reader,writer=await quic.create_stream()
                writer.write(b'C')
                try:
                    await asyncio.sleep(10)
                finally:
                    writer.write_eof()
                    quic.cancel_stream(writer.get_extra_info('stream_id'))
        task=asyncio.create_task(cancel());await asyncio.sleep(.01);task.cancel()
        await asyncio.gather(task,return_exceptions=True)
        await asyncio.to_thread(services['quic'].report_transport_state,'quic',True,'cancelled')
    for reader,writer in connections.values():writer.close();await writer.wait_closed()
    statistics={}
    reporting=None
    for kind,service in services.items():
        if not await asyncio.to_thread(service.flush):raise RuntimeError('telemetry flush failed')
        statistics[kind]=service.statistics()
    from cli import request as daemon_request
    reporting=daemon_request('observe')
    for service in services.values():service.close()
    metrics={}
    for kind in services:
        samples=[r for r in rows if r['kind']==kind]
        successful=sorted(r['latency_ms'] for r in samples if r['outcome']=='ok')
        elapsed=(max(r['end_ns'] for r in samples)-min(r['start_ns'] for r in samples))/1e9
        metrics[kind]=dict(offered=len(samples),completed=len(successful),
            failure_ratio=(len(samples)-len(successful))/len(samples),
            successful_p99_ms=successful[max(0,__import__('math').ceil(len(successful)*.99)-1)] if successful else None,
            deadline_attainment=sum(r['outcome']=='ok' and r['latency_ms']<=50 for r in samples)/len(samples),
            goodput_mbps=sum(r['bytes'] for r in samples)*8/elapsed/1e6)
    for kind in services:
        metrics[kind]['by_phase']={}
        for phase in ('individual','mixed'):
            samples=[r for r in rows if r['kind']==kind and r['phase']==phase]
            successes=sorted(r['latency_ms'] for r in samples if r['outcome']=='ok')
            elapsed=(max(r['end_ns'] for r in samples)-min(r['start_ns'] for r in samples))/1e9
            metrics[kind]['by_phase'][phase]=dict(
                offered=len(samples),completed=len(successes),
                failure_ratio=1-len(successes)/len(samples),
                successful_p99_ms=successes[max(0,__import__('math').ceil(len(successes)*.99)-1)] if successes else None,
                goodput_mbps=sum(r['bytes'] for r in samples)*8/elapsed/1e6,
                deadline_attainment=sum(r['outcome']=='ok' and r['latency_ms']<=50 for r in samples)/len(samples))
    result=dict(application_reporting=reporting,rows=rows,metrics=metrics,establishment_ms=setup,
                confirmations=confirmations,sdk_statistics=statistics,
                cancellation_test='partial QUIC stream reset requested through aioquic; coroutine cancellation reported through SDK RAII',
                deadline_ms=50,scope='closed-loop echo, no HTTP or HTTP/3')
    Path(args.output).write_text(json.dumps(result,indent=2))


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('mode',choices=('server','client'))
    parser.add_argument('--cert',required=True)
    parser.add_argument('--key')
    parser.add_argument('--ready')
    parser.add_argument('--host',default='127.0.0.1')
    parser.add_argument('--socket')
    parser.add_argument('--active')
    parser.add_argument('--go')
    parser.add_argument('--output')
    parser.add_argument('--requests',type=int,default=20)
    args=parser.parse_args()
    if not 1<=args.requests<=100:parser.error('requests must be 1..100')
    asyncio.run(server(args) if args.mode=='server' else client(args))

if __name__=='__main__':main()
