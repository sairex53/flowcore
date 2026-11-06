# Application integration

Native core: C++23, no additional runtime dependency beyond threads/libstdc++.
Build with `cmake -S . -B build -G Ninja -DFLOWCORE_BPF=OFF && cmake --build build`.
Installed CMake consumers use `find_package(flowcore CONFIG REQUIRED)` and link
`flowcore::sdk`. Headers: flowcore/intent_v2.hpp and flowcore/intent_c.h.
V1 flowcore/client.hpp remains available.

## C++ scopes

```cpp
#include <flowcore/intent_v2.hpp>
using namespace flowcore::v2;
Intent intent;
intent.service = "payments";
intent.priority = Priority::Standard; // permission granted by default
intent.deadline_us = 25000;
Service service("/private/runtime/control.sock", intent);
// Optional blocking setup outside request processing:
if (service.wait_ready(std::chrono::seconds(2))) {
    // Optionally associate_socket(connected_tcp_fd) once, then pass cookie below.
}
auto request = service.begin_request(25000);
// Perform actual application I/O and processing.
bool queued = request.complete(Outcome::Ok);
// Also support Timeout / Failure; destruction without complete submits Cancelled.
// queued reports enqueue, not daemon acknowledgement.
(void)queued;
```
Keep Service for the application's lifetime; share it across threads.
Applications reuse their own TCP/UDP sockets; the worker now also reuses its IPC connection. Setup/FD association, wait_ready,
flush and destruction may block and belong outside latency-sensitive scopes.
Set timeouts/cancellation in the application itself: a request deadline reports
an objective and does not interrupt a blocking system call.
Always report unsuccessful requests as well as successful ones. Inspect SDK
statistics and notifications; zero observed errors does not prove zero telemetry loss.

The SDK uses monotonic nanoseconds; system wall-clock changes cannot change request
duration. Requests completing during daemon loss may be dropped. Ordinary application
network I/O continues without FLOWCORE, and re-registration is automatic after restart.
Compare registration_generation() (Python generation(), C fc_v2_generation) during
explicit recovery. Re-associate live descriptors after a generation change.
Ordinary transport reconnect and lease renewal keep the same generation/cookies.
After request flush, call dissociate_socket(cookie) to free slots as connections
are retired. A nonzero return counts abandoned telemetry records, not application
request cancellations.

## C ABI and language bridges

`libflowcore_intent.so` exports fc_v2_*; set options version=2 and size=sizeof(options).
See `examples/intent_c_demo.c` for initialization, wait, begin, finish, flush and close.
fc_v2_finish consumes its request exactly once. Outcome values are 0 OK, 1 timeout,
2 failure, 3 cancellation. Null handles/error return values must be checked.
fc_v2_error is thread-local text. The C ABI is versioned by symbol prefix and exact
options structure size; incompatible extensions will use a new version.

`examples/python/flowcore_intent.py` is a small ctypes bridge to that same ABI:
```python
from flowcore_intent import Service
with Service("build/libflowcore_intent.so", "/private/runtime/control.sock", "database") as client:
    if client.ready():
        with client.request(deadline_us=50000):
            perform_database_request()
        client.flush()
        print(client.statistics(), client.notifications())
```
The Python context yields a Request with id and complete(outcome). Values are
0 OK, 1 timeout, 2 failure, 3 cancellation; an explicit completion is not duplicated
on context exit. TimeoutError reports timeout; other exceptional exits report
cancellation. The C ABI also exposes request IDs. IDs are scoped to a lease/service,
not globally unique across services sharing a connection.
Python context allocation/GIL overhead is not the native C++ hot path.
`examples/intent_go_demo.go` provides an opt-in cgo example through the C ABI:
`go run examples/intent_go_demo.go /private/runtime/control.sock`.
A C compiler, cgo and the built shared library are required. This example was
compiled and executed against the live daemon on Fedora ARM64 with Go 1.27.3.
That toolchain was downloaded from go.dev into ignored build/toolchains and its
archive SHA256 checked against official release metadata; no host Go installation
was changed. This remains a small bridge example, not an independent full Go SDK.
It pins its demonstration goroutine while reading thread-local C error text;
larger cgo wrappers can instead copy error text within the same C call.

## Reproducible demonstrations and tests

Run `ctest --test-dir build --output-on-failure`.
`python3 -B tests/intent_v2_test.py --live` starts a private ordinary-user daemon
and a real TCP server on a dynamically allocated loopback port. It explicitly
allows test critical/interactive registration and installs no network policy.
Three simultaneous C++ processes perform 60 sequential requests each on a reused
TCP connection: payment 4096-byte payload, database 16384, background 65536; all
receive 64 bytes after a simulated 1 ms server-processing delay. TCP_NODELAY and
two-second socket timeouts avoid incidental Nagle/ACK and infinite blocking.
Each process attests its actual socket FD, records monotonic request durations,
and emits successful P99 plus accepted/rejected/dropped telemetry counters.
A failed I/O terminates this smoke demo with nonzero exit rather than producing a
misleading successful-only comparison. For failure-aware performance studies use
the existing BENCHMARK_V2 methodology, not this 60-request smoke test.

Tests exercise concurrent native callers, offline fallback, C and Python bridges,
cancelled/timeout lifecycle, forged UID/PID, delegated connected descriptors,
priority denial, invalid versions/fields, TTL/birth mismatch, sticky revocation,
duplicate completion, cookie attestation, inflight quotas, 1000 deterministic
malformed event fixtures, same-client daemon restart, ambiguous IPC timeout without duplicate leases, invalid
descriptor cleanup and actual SIGKILL cleanup. The native test also submits a
20,000-request burst and checks bounded-queue degradation.
Existing V1/security/controller tests remain in CTest. Installed-mode validation:
`python3 -B tests/install_test.py` (ordinary user).

## Evidence and remaining work

Local raw logs are retained under results/intent-v2-*.log and results/intent-session-*.log (ignored by Git).
This implementation was validated on Fedora 45 ARM64. Numerical smoke observations
are observations of loopback application I/O, not evidence of adaptive-controller
improvement, fairness under egress congestion, or scheduling guarantees.
This SDK does not load BPF, create kernel modules, replace TCP or require network capabilities.

The optional privileged regression runner also passed:

`sudo python3 -B tests/service_integration.py` (disposable namespaces only).
This checks the existing Unix API, IPv4/IPv6, four classes, CAKE, netem, link events,
recovery and rollback; evidence: results/intent-v2-privileged-service.log.

Remaining: richer per-operation error details; a lossless notification protocol;
generation callbacks (polling is available); a long-duration soak; and multi-tenant
application ACLs beyond the existing daemon UID boundary.
These limitations preclude claiming a fully production-hardened multi-tenant API.
See INTENT_API.md for exact enforcement and correlation boundaries.


An earlier V2 one-shot-transport smoke run emitted (historical, not a current speed comparison):

| Workload | Requests | Successful P99 ms | Events accepted / rejected / dropped |
| --- | ---: | ---: | --- |
| payment | 60 | 1.55546 | 120 / 0 / 0 |
| database | 60 | 1.62267 | 120 / 0 / 0 |
| background | 60 | 2.22288 | 120 / 0 / 0 |

Environment: Fedora 45 ARM64, kernel 7.2.11-300.fc45.aarch64. These are one-run
smoke measurements, not independent repeated comparisons or controller effect
sizes. Each process exited successfully; no failed requests occurred in this run.
The separate native concurrency probe reported 255 acknowledged events and one
local drop; event submission is deliberately best effort. Raw reproduction:
python3 -B tests/intent_v2_test.py --live; evidence results/intent-v2-live.log.


## Persistent-channel and multiplex verification

The updated live test starts eight independent C++ Service clients concurrently.
Each reuses one peer-authenticated session. A one-second lease remains generation 1
through several renewals while retaining a UDP-cookie binding and an active request.
The recorded probe used one session for 23 frames, acknowledged 18 telemetry events,
and observed no local drops in that probe. FD setup uses the separate legacy control
path. This proves connection reuse, not lower application latency.

The test also temporarily removes the sidecar pathname to exercise V2 stream
compatibility, resumes the same SDK after SIGSTOP/IPC timeout without duplicate
leases, restarts the daemon and checks generation advancement/re-attestation.
Raw framing tests cover delegated socket credentials, 70,000-byte packets, zero-data
SCM_RIGHTS, truncated FD arrays, 16-client bounds, idle expiry and round-robin service.
Socket churn tests reuse more than 64 bindings through explicit dissociation.
A real framed TCP request also completes while its SDK points at an absent daemon;
telemetry drops are counted instead of blocking the application operation.
Clock-namespace mismatch, non-UTF-8 process names, renewal/cookie retention,
revocation audit, all terminal outcomes and acknowledgement accounting are included.
Native integer parsing has boundary and 1000 malformed numeric fixtures;
the transport checks response version 2, in addition to the original event-parser fuzz fixtures.

examples/python/multiplex_demo.py is a real framed TCP demonstration: eight
overlapping application requests, four critical and four standard, share one
socket. The test server replies in reverse order with 2 ms between responses.
Each response completes its own SDK scope; native request ID plus service identifies
the declaration. Example execution requires that test server and explicit critical
priority authorization:

```sh
python3 -B examples/python/multiplex_demo.py build/libflowcore_intent.so CONTROL_SOCKET TEST_PORT
```

A recorded run completed 8/8 requests, with 3 deadline misses and successful P99
21.555 ms. Deadlines were observation budgets, not automatic cancellations.
The returned JSON retains each latency, completion/failure counts, deadline misses
and SDK delivery counters. This is a short integration measurement, not an HTTP/2
or QUIC implementation or a controlled QoS benchmark. The same cookie under two
service priorities demonstrates why packet-level request scheduling is not implied.
Use BENCHMARK_V2 for performance comparisons and always retain failures.

Current evidence snapshot: results/intent-session-evidence-1793955514206584729.json.


Validation for this update: 16/16 CTest targets with BPF enabled and disabled,
installed-mode consumers in both configurations, rebuilt Go/cgo example, and the
existing privileged namespace regression suite. The installed SDK consumer asserts
that requests reuse one session after daemon readiness. Logs:
results/intent-session-all-tests.log, intent-session-no-bpf.log,
intent-session-installed.log, intent-session-no-bpf-installed.log and
intent-session-privileged-regression.log (all under results/).
