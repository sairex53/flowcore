# Real Application Performance Engine 2.0

This extends the existing real application runner. HTTP/1.1 reuses the
threaded service, persistent client connections and digest checked bulk upload.
The gRPC example still uses one reusable HTTP/2 channel and the same protobuf
unary method. Both gateways can now forward to the same backend. The PostgreSQL
example continues to issue parameterized indexed queries on persistent worker
connections. The application backend adds a shared, bounded pool so pooled
and independent query traffic can run together.

The existing C++23 Intent V2 SDK remains the source of request scopes and
optional socket association through the C ABI. The Python adapter supplies
request outcome, declared deadline and service identity. HTTP and libpq
socket cookies are used when available. gRPC supplies logical stream IDs
because its public API does not expose the TCP descriptor. HTTP/3 confirms
TLS and ALPN with aioquic, then associates its UDP socket through the same
cooperative adapter used by the QUIC echo experiment. The new endpoint
carries HTTP/3 requests to the application backend.

The existing OTLP JSON metrics bridge accepts per-protocol requests, errors
and latency distributions. Authentication uses a fresh token, passed to the
collector through an inherited descriptor. Raw request rows retain release,
start, finish, outcome and deadline accounting. Policy history, class and
link readback come from the daemon. SDK success counters and OTLP receipts
are reported independently from application outcomes.

## Workload profiles

| Profile | Workload | Network conditions |
| --- | --- | --- |
| Stable | Variable response sizes, pooled database reads, concurrent gRPC and HTTP/3, optional verified upload | Fixed shaped egress and fixed reverse delay |
| Dynamic | Stable stack with groups of four HTTP releases and overlapping bulk traffic | Half capacity plus reverse delay and loss, link down, then restore |
| Abundant | Same database and API stack with the background transfer disabled | Use a high configured capacity, or Linux default |
| Application | Same stack, with 40 ms of configured HTTP backend work and no bulk transfer | Use high capacity to expose processing delay |

The runner randomizes modes within seed blocks. Repeated modes share the
same offered schedule, including the gRPC cancellation schedule where enabled.
Actual dispatch can lag scheduled release at finite concurrency. End-to-end
latency starts at actual client dispatch. Deadline attainment includes
release lag and counts failed, timed out and cancelled requests as misses.
Local socket and RPC timeouts operate from actual dispatch. A successful
request may therefore still miss the offered deadline.

## Policy applicability

- `linux-default` verifies noqueue on both veths. The stack disables SDK
  submission for that mode. It is an unshaped reference with no netem delay.
- `fq_codel` is one shaped HTB leaf with fq_codel. `cake` shapes best effort.
- `static` uses the tuned HTB/fq_codel class layout. `slo-aware` and
  `hybrid` use the existing adaptive controller strategies. The hybrid
  in this lab is queue plus SLO control.
- TCP destination-port classification can distinguish the HTTP, gRPC and
  bulk connections on sender egress. Priority labels on multiplexed RPCs
  are advisory. QUIC uses the HTB default leaf. The scheduler receives no
  HTTP/2 or HTTP/3 stream identifiers.
- The UDP per-message SDK requires application-selected datagrams and an
  authorized socket. These HTTP/3 requests use encrypted QUIC stream data.
  The per-message SDK and socket-pacing Hybrid V3 are ineligible as modes
  in this stack.

Use the shaped baselines for equal-capacity comparisons. The unshaped
reference answers a different question. See the [stack setup](SERVICE_STACK.md)
and [measurements](APPLICATION_PERFORMANCE_RESULTS.md).
