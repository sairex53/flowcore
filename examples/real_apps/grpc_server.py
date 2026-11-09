"""Unary gRPC service. Install requirements and generate stubs with run_grpc.sh."""
import argparse
from concurrent import futures
import json
import time
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
if os.environ.get('FLOWCORE_GRPC_PROTO_DIR'):
    sys.path.insert(0, os.environ['FLOWCORE_GRPC_PROTO_DIR'])
import grpc
import grpc_app_pb2
import grpc_app_pb2_grpc
from service_stack import backend_pool, backend_request


class Application(grpc_app_pb2_grpc.ApplicationServicer):
    def __init__(self, max_work_ms, upstream=None):
        self.max_work_ms = max_work_ms
        self.upstream = upstream

    def Critical(self, request, context):
        remaining = context.time_remaining()
        work_ms = min(self.max_work_ms, int(request.work_ms))
        if remaining is not None and work_ms / 1000 >= remaining:
            context.abort(grpc.StatusCode.DEADLINE_EXCEEDED, 'deadline cannot cover requested work')
        if self.upstream:
            try:
                status, payload, _ = backend_request(
                    self.upstream, request.request_id%4096+1, work_ms,
                    min(16384, request.response_bytes), remaining*1000 if remaining is not None else 0)
            except TimeoutError:
                context.abort(grpc.StatusCode.DEADLINE_EXCEEDED, 'backend deadline')
            except Exception:
                context.abort(grpc.StatusCode.UNAVAILABLE, 'backend disconnected')
            if status != 200:
                context.abort(grpc.StatusCode.DEADLINE_EXCEEDED if status==504 else grpc.StatusCode.UNAVAILABLE,
                              'backend request failed')
        else:
            until = time.monotonic()+work_ms/1000
            while context.is_active() and (left := until-time.monotonic()) > 0:
                time.sleep(min(left,.005))
            payload = b'G' * min(request.response_bytes, 65536)
        if not context.is_active():
            context.abort(grpc.StatusCode.CANCELLED, 'caller cancelled request')
        return grpc_app_pb2.CriticalReply(
            request_id=request.request_id, payload=payload)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bind', default='0.0.0.0:9002')
    parser.add_argument('--upstream-port', type=int)
    parser.add_argument('--pool-size', type=int, default=8)
    parser.add_argument('--workers', type=int, default=32)
    parser.add_argument('--max-work-ms', type=int, default=1000)
    args = parser.parse_args()
    if not 1 <= args.workers <= 256 or not 0 <= args.max_work_ms <= 1000:
        parser.error('server limits outside supported bounds')
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=args.workers),
                         options=[('grpc.max_concurrent_streams', 128),
                                  ('grpc.max_receive_message_length', 65536),
                                  ('grpc.max_send_message_length', 65536)])
    if args.upstream_port is not None and not 1 <= args.upstream_port <= 65535:
        parser.error('invalid upstream port')
    upstream = backend_pool(args.upstream_port,args.pool_size) if args.upstream_port else None
    grpc_app_pb2_grpc.add_ApplicationServicer_to_server(Application(args.max_work_ms,upstream), server)
    port = server.add_insecure_port(args.bind)
    if not port:
        raise RuntimeError('gRPC could not bind requested endpoint')
    server.start()
    print(json.dumps({'ready': True, 'bind': args.bind}), flush=True)
    try:
        server.wait_for_termination()
    except KeyboardInterrupt:
        server.stop(grace=2).wait()


if __name__ == '__main__':
    main()
