# gRPC integration

The example uses Python gRPC's public server and client APIs and the existing FLOWCORE C ABI adapter. It does not replace or wrap grpcio's transport.

## Build and run

Install optional dependencies:

```sh
python3 -m venv .venv
. .venv/bin/activate
python3 -m venv build/grpc-venv
. build/grpc-venv/bin/activate
python -m pip install -r examples/real_apps/requirements.txt
```

Generate protobuf stubs outside the source tree and start the server:

```sh
export FLOWCORE_GRPC_PROTO_DIR="$(mktemp -d)"
python3 -m grpc_tools.protoc -I examples/real_apps \
  --python_out="$FLOWCORE_GRPC_PROTO_DIR" \
  --grpc_python_out="$FLOWCORE_GRPC_PROTO_DIR" \
  examples/real_apps/grpc_app.proto
export PYTHONPATH="$FLOWCORE_GRPC_PROTO_DIR"
python3 examples/real_apps/grpc_server.py --bind 127.0.0.1:9002 --workers 32
```

In another terminal, run the unary client:

```sh
python3 examples/real_apps/grpc_benchmark.py --target 127.0.0.1:9002 \
  --rate 20 --duration 10 --concurrency 8 --deadline-ms 50 \
  --output grpc-result.json
```

The client keeps one gRPC channel open, sends concurrent unary RPCs, passes an explicit deadline to each call, and records successful latency, errors, deadline misses, completion ratio, and P50/P95/P99. Its telemetry uses logical request IDs. The server checks the propagated deadline and cancellation context.

## FLOWCORE semantics

Set `FLOWCORE_INTENT_LIBRARY` and `FLOWCORE_SOCKET` to enable registration and request reports. FLOWCORE receives a multiplexed logical-session declaration and each request's stream ID. The grpcio public API does not expose the channel's socket descriptor, so the example cannot associate an exact socket cookie.

HTTP/2 multiplexes calls on one connection. The kernel can classify or shape that connection's packets using socket or cgroup metadata, but this integration does not provide arbitrary per-RPC packet scheduling. All calls share the connection's network policy. Per-RPC SDK scopes improve application telemetry only.

When FLOWCORE is unavailable, gRPC requests still proceed; telemetry is omitted. Install the optional packages from `requirements.txt` to run this example.

## Combined application lab

The same namespace runner can include gRPC on the interactive class alongside the API, PostgreSQL, backup transfer, FLOWCORE, and telemetry:

    sudo python3 scripts/real_app_lab.py --postgres --grpc --modes fq_codel,cake,static,hybrid,predictive --repetitions 10 --capacity-kbit 1500 --rate 60 --grpc-rate 20 --duration 15

The run artifact records gRPC application latency, errors, deadlines, OTel export, and gRPC server CPU. Requests share a reused HTTP/2 connection and are classified at connection level by destination port 9002.
