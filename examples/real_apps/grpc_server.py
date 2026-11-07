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


class Application(grpc_app_pb2_grpc.ApplicationServicer):
    def __init__(self, max_work_ms):
        self.max_work_ms = max_work_ms

    def Critical(self, request, context):
        remaining = context.time_remaining()
        work_ms = min(self.max_work_ms, int(request.work_ms))
        if remaining is not None and work_ms / 1000 >= remaining:
            context.abort(grpc.StatusCode.DEADLINE_EXCEEDED, 'deadline cannot cover requested work')
        time.sleep(work_ms / 1000)
        if not context.is_active():
            context.abort(grpc.StatusCode.CANCELLED, 'caller cancelled request')
        return grpc_app_pb2.CriticalReply(
            request_id=request.request_id, payload=b'G' * min(request.response_bytes, 65536))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bind', default='0.0.0.0:9002')
    parser.add_argument('--workers', type=int, default=32)
    parser.add_argument('--max-work-ms', type=int, default=1000)
    args = parser.parse_args()
    if not 1 <= args.workers <= 256 or not 0 <= args.max_work_ms <= 1000:
        parser.error('server limits outside supported bounds')
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=args.workers),
                         options=[('grpc.max_concurrent_streams', 128),
                                  ('grpc.max_receive_message_length', 65536),
                                  ('grpc.max_send_message_length', 65536)])
    grpc_app_pb2_grpc.add_ApplicationServicer_to_server(Application(args.max_work_ms), server)
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
