"""Opt-in HTTP/3 gateway and open-loop client for the existing application backend."""
import argparse
import asyncio
from functools import partial
import json
import os
from pathlib import Path
import random
import re
import sys
import time

from aioquic.asyncio import connect, serve
from aioquic.h3.connection import H3_ALPN, H3Connection
from aioquic.h3.events import DataReceived, HeadersReceived
from aioquic.quic.configuration import QuicConfiguration
from aioquic.quic.events import (
    ConnectionTerminated, HandshakeCompleted, ProtocolNegotiated, StopSendingReceived, StreamReset,
)
from aioquic.quic.logger import QuicLogger

from flowcore_adapter import FlowcoreAdapter
from http_benchmark import summarize
from otel_client import OtlpMetrics
from quic_adapter import CooperativeQuic
from service_stack import MAX_RESPONSE, backend_pool, backend_request


PATH = re.compile(rb'/records/([1-9][0-9]{0,3})/([0-9]{1,3})/([0-9]{1,5})')


class Protocol(CooperativeQuic):
    def __init__(self, *args, backend=None, active=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.backend = backend
        self.active = active
        self.h3 = None
        self.pending = {}
        self.responses = {}
        self.closed = False

    def quic_event_received(self, event):
        if isinstance(event, ProtocolNegotiated):
            self.h3 = H3Connection(self.connection)
        if isinstance(event, ConnectionTerminated):
            self.closed = True
            for state in self.pending.values():
                if not state['future'].done():
                    state['future'].set_exception(ConnectionError('HTTP/3 connection closed'))
        if isinstance(event, StreamReset) and event.stream_id in self.pending:
            future = self.pending[event.stream_id]['future']
            if not future.done():
                future.set_exception(ConnectionError('HTTP/3 stream reset'))
        if isinstance(event, (StopSendingReceived, StreamReset)) and event.stream_id in self.responses:
            self.responses[event.stream_id] = False
        if self.h3 is not None:
            for message in self.h3.handle_event(event):
                if self.backend is not None:
                    if isinstance(message, HeadersReceived):
                        self.dispatch(message)
                elif isinstance(message, (HeadersReceived, DataReceived)):
                    state = self.pending.get(message.stream_id)
                    if state is None or state['future'].done():
                        continue
                    if isinstance(message, HeadersReceived):
                        state['status'] = dict(message.headers).get(b':status')
                    else:
                        state['length'] += len(message.data)
                    if state['length'] > MAX_RESPONSE:
                        state['future'].set_exception(ValueError('HTTP/3 response exceeds bound'))
                    elif message.stream_ended:
                        state['future'].set_result((state['status'],state['length']))
        # The base adapter normally creates asyncio stream readers for raw QUIC.
        # HTTP/3 owns the stream data on this connection.
        if isinstance(event, HandshakeCompleted):
            self.handshake_confirmed = True

    def reply(self, stream_id, status, payload=b''):
        if self.closed:
            return
        self.h3.send_headers(stream_id, [(b':status',str(status).encode()),
                                         (b'content-type',b'application/json')])
        self.h3.send_data(stream_id,payload,end_stream=True)
        self.transmit()

    def dispatch(self, event):
        headers = dict(event.headers)
        match = PATH.fullmatch(headers.get(b':path',b''))
        if headers.get(b':method') != b'GET' or not event.stream_ended or not match:
            self.reply(event.stream_id,400)
            return
        key, work_ms, size = map(int,match.groups())
        try:
            budget = int(headers.get(b'x-deadline-ms',b'100'))
        except ValueError:
            budget = 0
        if not 1 <= key <= 4096 or not 0 <= size <= MAX_RESPONSE or not 1 <= budget <= 2000:
            self.reply(event.stream_id,400)
            return
        if len(self.active) >= 32:
            self.reply(event.stream_id,503)
            return
        self.responses[event.stream_id] = True
        task=asyncio.create_task(self.respond(event.stream_id,key,work_ms,size,budget))
        self.active.add(task)
        task.add_done_callback(self.active.discard)

    async def respond(self, stream_id, key, work_ms, size, budget):
        try:
            try:
                status,payload,_ = await asyncio.to_thread(backend_request,self.backend,key,work_ms,size,budget)
            except TimeoutError:
                status,payload=504,b''
            except Exception:
                status,payload=503,b''
            if self.responses.get(stream_id):
                self.reply(stream_id,status,payload)
        finally:
            self.responses.pop(stream_id,None)

    def cancel_response(self, stream_id):
        if self.closed:
            return False
        try:
            self.connection.stop_stream(stream_id,0x10c)
        except ValueError:
            # A completed stream can disappear as the application timer fires.
            return False
        self.transmit()
        return True

    def start_request(self, key, work_ms, size, deadline, authority):
        stream = self.connection.get_next_available_stream_id()
        future = asyncio.get_running_loop().create_future()
        self.pending[stream] = dict(future=future,status=None,length=0)
        self.h3.send_headers(stream, [(b':method',b'GET'),(b':scheme',b'https'),
            (b':authority',authority.encode()),(b':path',f'/records/{key}/{work_ms}/{size}'.encode()),
            (b'x-deadline-ms',str(deadline).encode())],end_stream=True)
        self.transmit()
        return stream,future


async def server(args):
    pool = backend_pool(args.upstream_port,args.pool_size)
    config = QuicConfiguration(is_client=False, alpn_protocols=H3_ALPN)
    config.load_cert_chain(args.certificate,args.key)
    listener = await serve(args.host,args.port,configuration=config,
                           create_protocol=partial(Protocol,backend=pool,active=set()))
    print(json.dumps({'ready':True,'protocol':'HTTP/3'}),flush=True)
    try:
        await asyncio.Future()
    finally:
        listener.close()


async def client(args):
    logger = QuicLogger()
    config = QuicConfiguration(is_client=True, alpn_protocols=H3_ALPN,
                               server_name='localhost',quic_logger=logger)
    config.load_verify_locations(args.certificate)
    adapter = FlowcoreAdapter('http3-client',priority='standard',deadline_ms=args.deadline_ms,
                              enabled=not args.no_flowcore)
    rows=[]
    started=time.monotonic_ns()
    try:
        async with asyncio.timeout(args.duration+8):
            async with connect(args.host,args.port,configuration=config,create_protocol=Protocol) as protocol:
                if not protocol.handshake_confirmed or protocol.h3 is None:
                    raise RuntimeError('TLS/HTTP3 negotiation not confirmed')
                setup_ms=(time.monotonic_ns()-started)/1e6
                try:
                    cookie=protocol.declare(adapter.sdk,'api-http3') if adapter.sdk else 0
                except (OSError, RuntimeError):
                    cookie=0
                start=args.start_ns or time.monotonic_ns()+300_000_000

                async def worker(index):
                    rng=random.Random(args.seed+index)
                    for ordinal in range(index,args.rate*args.duration,args.concurrency):
                        scheduled=start+ordinal*1_000_000_000//args.rate
                        await asyncio.sleep(max(0,(scheduled-time.monotonic_ns())/1e9))
                        dispatch=time.monotonic_ns()
                        key,size=rng.randrange(1,4097),rng.choice((128,512,2048,4096))
                        outcome,status,length='failure',None,0
                        stream=None
                        cancel_signalled=False
                        if not protocol.closed:
                            stream,future=protocol.start_request(key,2,size,args.deadline_ms,args.host)
                            try:
                                with adapter.request(cookie=cookie,deadline_ms=args.deadline_ms,
                                                     stream_id=stream) as scope:
                                    try:
                                        status,length=await asyncio.wait_for(future,args.deadline_ms/1000)
                                        outcome='ok' if status==b'200' and length==size else 'timeout' if status==b'504' else 'failure'
                                    except TimeoutError:
                                        outcome='timeout'
                                        cancel_signalled=protocol.cancel_response(stream)
                                    except (ConnectionError, ValueError):
                                        outcome='failure'
                                    if scope and outcome!='ok':
                                        scope.complete(1 if outcome=='timeout' else 2)
                            finally:
                                protocol.pending.pop(stream,None)
                        ended=time.monotonic_ns()
                        rows.append(dict(request=ordinal, stream_id=stream, scheduled_ns=scheduled,
                            start_ns=dispatch,end_ns=ended,latency_ms=(ended-dispatch)/1e6,
                            response_bytes=length, status=int(status) if status else None,
                            outcome=outcome, cancel_signalled=cancel_signalled, deadline_miss=outcome!='ok' or ended-scheduled>args.deadline_ms*1e6))

                await asyncio.gather(*(worker(i) for i in range(args.concurrency)))
        events=logger.to_dict()['traces'][0]['events']
        loss_events=sum(event['name']=='recovery:packet_lost' for event in events)
        report=summarize(rows,args.rate,args.duration)
        report['response_goodput_mbps']=sum(row['response_bytes'] for row in rows if row['outcome']=='ok')*8/args.duration/1e6
        metrics=OtlpMetrics('http3-client')
        for row in rows:
            attrs={'flowcore.traffic_class':'standard'}
            metrics.observe('flowcore.http3.request.duration',row['latency_ms']/1000,attrs)
            metrics.add_counter('flowcore.http3.requests',1,attrs)
            if row['outcome']!='ok':
                metrics.add_counter('flowcore.http3.errors',1,attrs)
            if row['deadline_miss']:
                metrics.add_counter('flowcore.http3.deadline_misses',1,attrs)
        adapter.flush()
        result=dict(schema_version=1,transport='HTTP/3 over QUIC',setup_ms=setup_ms,
                    summary=report,client_quic_loss_events=loss_events,qlog_event_count=len(events),
                    sdk=adapter.status(),otel_export=metrics.export(args.metrics_endpoint,
                        os.environ.get('OTEL_EXPORTER_OTLP_HEADERS_TOKEN','')),
                    requests=sorted(rows,key=lambda row:row['request']))
        Path(args.output).write_text(json.dumps(result,indent=2,allow_nan=False)+'\n')
    finally:
        adapter.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode',choices=('server','client'))
    parser.add_argument('--host',default='127.0.0.1')
    parser.add_argument('--port',type=int,default=9043)
    parser.add_argument('--certificate',required=True)
    parser.add_argument('--key')
    parser.add_argument('--upstream-port',type=int,default=9010)
    parser.add_argument('--pool-size',type=int,default=8)
    parser.add_argument('--rate',type=int,default=20)
    parser.add_argument('--duration',type=int,default=10)
    parser.add_argument('--concurrency',type=int,default=4)
    parser.add_argument('--deadline-ms',type=int,default=100)
    parser.add_argument('--start-ns',type=int,default=0)
    parser.add_argument('--seed',type=int,default=1)
    parser.add_argument('--no-flowcore',action='store_true')
    parser.add_argument('--metrics-endpoint',default='')
    parser.add_argument('--output')
    args=parser.parse_args()
    if not (1<=args.port<=65535 and 1<=args.upstream_port<=65535 and 1<=args.pool_size<=32
            and 1<=args.rate<=100 and 1<=args.duration<=60 and 1<=args.concurrency<=16
            and 1<=args.deadline_ms<=2000 and args.start_ns>=0):
        parser.error('unsupported HTTP/3 workload bounds')
    if args.mode=='server' and not args.key or args.mode=='client' and not args.output:
        parser.error('server requires a key and client requires output')
    asyncio.run(server(args) if args.mode=='server' else client(args))


if __name__=='__main__':
    main()
