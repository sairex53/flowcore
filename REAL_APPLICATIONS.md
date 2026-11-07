# Real application integrations

FLOWCORE now has an opt-in application adapter layer and runnable HTTP, gRPC, PostgreSQL, and background-download workloads. The adapters use the existing C++23 C ABI and SDK; application libraries remain usable when FLOWCORE is unavailable.

## Components

- `examples/python/flowcore_intent.py`: ctypes binding to the supported version 2 C ABI.
- `examples/real_apps/flowcore_adapter.py`: one optional, bounded SDK channel per process. Service registration happens during setup; request scopes submit through the SDK's bounded asynchronous queue. If the library or daemon is unavailable, the application keeps running without intent reporting.
- `http_service.py` / `http_benchmark.py`: persistent HTTP/1.1 critical requests, bounded concurrent workers, deadline/error/completion accounting, streamed artifact downloads, and bounded backup uploads.
- `grpc_app.proto`, `grpc_server.py`, `grpc_benchmark.py`: unary gRPC over a reused HTTP/2 channel. Request scopes carry logical stream IDs; packet policy remains connection-level.
- `postgres_benchmark.py` and `postgres_schema.sql`: randomized indexed PostgreSQL reads over TCP.
- `metrics_bridge.py`: bounded OTLP/HTTP JSON receiver at `/v1/metrics` and Prometheus scrape endpoint at `/metrics`.
- `scripts/real_app_lab.py`: randomized, repeatable experiments confined to FLOWCORE-created network namespaces and one disposable Podman PostgreSQL container per run.

The existing native and C ABI examples remain the supported low-level integration surfaces: `examples/sdk_request.cpp`, `examples/intent_v2_demo.cpp`, and `examples/intent_c_demo.c`. No application networking library is replaced.

## Run an HTTP-only service

Build the SDK and daemon:

```sh
cmake -S . -B build -DFLOWCORE_BPF=OFF
cmake --build build -j2
```

Start the service and metrics bridge in separate terminals:

```sh
mkdir -p artifacts
python3 examples/real_apps/http_service.py --bind 127.0.0.1 --artifact-dir ./artifacts
python3 examples/real_apps/metrics_bridge.py --bind 127.0.0.1 --port 9464
```

Create a fixture file named `backup.bin` in `./artifacts`, then run:

```sh
python3 examples/real_apps/http_benchmark.py --host 127.0.0.1 --port 9000 \
  --background-port 9001 --background-path /artifact/backup.bin \
  --rate 30 --duration 10 --concurrency 8 --output http-result.json
```

The SDK is optional in this standalone invocation. Set `FLOWCORE_INTENT_LIBRARY` and `FLOWCORE_SOCKET` to use the application intent path. To apply real network controls, run the isolated lab command below; a standalone server does not alter host networking.

## Full multi-service lab

Run the HTTP API, a disposable PostgreSQL instance, unary gRPC over HTTP/2, background upload, FLOWCORE, and the OTLP bridge in one isolated namespace experiment:

    sudo python3 scripts/real_app_lab.py --postgres --grpc --modes fq_codel,cake,static,hybrid,predictive --repetitions 10 --capacity-kbit 1500 --rate 60 --grpc-rate 20 --duration 15

The runner records separate request metrics and service CPU deltas. gRPC requests share one HTTP/2 connection and receive connection-level class treatment; individual RPC packet scheduling is not claimed.

## OpenTelemetry bridge

The receiver accepts the OTLP/HTTP JSON metrics request shape at `POST /v1/metrics`. It exposes bounded series as Prometheus text at `GET /metrics`. The bridge is intentionally a small integration adapter, not a general OpenTelemetry Collector: it accepts only the documented FLOWCORE example metric names and a small attribute allowlist.

The receiver binds to loopback by default. A non-loopback bind requires a bearer token; the same authorization protects metric scrapes. Trace IDs from OTLP histogram exemplars are retained only with both `--allow-trace-ids` and a bearer token, in a 128-entry bounded ring, and are not Prometheus labels. The example exporters do not fabricate trace IDs.

An application can report a self-declared service name and measurements; neither the service name nor app timings are proof of network traffic or a trusted identity. The FLOWCORE daemon independently authenticates SDK clients with Unix peer credentials and applies its own priority permissions and quotas. Keep the metrics bridge private or configure an explicit service allowlist.

## Intent and policy behavior

HTTP API requests declare the critical priority, a target P99, and a deadline; the client bounds socket operations by that deadline and closes timed-out connections. PostgreSQL applies the declared deadline as server-side statement_timeout and records query cancellations. The lab backup workload streams a real seeded file from sender to receiver through the same local egress bottleneck as API requests. The PostgreSQL client declares `standard` because the lab's socket filter assigns port 5432 to the standard class. The gRPC client declares `interactive` and a logical request identity. These are application intentions, not guaranteed service levels.

In the research lab, packet classes are selected by fixed destination-port rules on the owned veth: HTTP critical 9000, artifact/background 9001, gRPC interactive 9002, and PostgreSQL 5432. Socket-cookie association is exact when the SDK can pass the live socket descriptor. The gRPC Python API does not expose its private HTTP/2 socket, so the demo reports logical requests and cannot claim per-request packet priority within the shared connection.

The download artifact route only opens a single filename relative to an already-open directory, rejects path traversal and symlinks, and caps files at 64 MiB. The background upload endpoint reads a bounded body and retains no uploaded data. Neither route executes arbitrary backup commands.

## Limits

- Client request latency includes scheduling, transport, server work, and response transfer.
- Server-side duration does not include client scheduling or the full network round trip.
- PostgreSQL query latency includes database execution and client scheduling as well as TCP. The sample query is a bounded indexed lookup; the benchmark cannot attribute all query time to networking.
- HTTP/2 multiplexing and HTTP/1.1 keep-alive are handled as shared connections. Request telemetry does not imply packet-level prioritization.
- C ABI submission is best effort. Dropped SDK events are reported by SDK counters and do not block application requests.
- The provided configuration is for an isolated lab. No host interface is modified by these examples.
