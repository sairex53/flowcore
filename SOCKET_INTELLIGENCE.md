# Socket intelligence SDK

Use flowcore::sdk with <flowcore/socket_pacing.hpp>. The socket API is part of the C++23 v2 SDK. Policy ownership is explicit and local to a cooperating application. The service label and throughput, latency and deadline preferences are advisory. The SDK writes only the selected socket options within the provided cap bounds.

```cpp
namespace pacing = flowcore::v2::pacing;
pacing::Policy policy;
policy.service = "backup";
policy.minimum_cap = 50'000;
policy.maximum_cap = 1'500'000;
policy.initial.maximum_rate = 300'000;
policy.initial.notsent_lowat = 16'384;
auto managed = pacing::SocketPolicy::attach(connected_tcp_fd, policy);
if (!managed) throw std::system_error(managed.error());

if (auto failure = managed->set_rate(200'000)) throw std::system_error(failure);
auto snapshot = managed->snapshot();
```

The caller holds and serializes access to its descriptor. At attach and for each operation, the SDK temporarily duplicates the descriptor and checks the socket protocol, address family and cookie. The temporary duplicate pins that operation against descriptor reuse. The handle itself does not pin a connection open. Keep the original descriptor stable during setup and call remove before closing it if other duplicates will stay open. On replacement or reconnect, attach a new policy to the new connection. Numeric descriptor identity is never sent to the daemon.

The SDK prevents concurrent policy leases on the same cookie in one SDK instance and bounds the registry to 256 policies. It refuses inherited handles after fork. Use one cooperative writer per socket. Linux socket option operations do not provide cross-process compare-and-swap against another writer. Readback detects divergence, and removal preserves settings that no longer match what FLOWCORE applied. Other independently owned options can still be restored. A reported compensation failure means the resulting state needs inspection.

RAII destruction attempts cleanup. For explicit error reporting use remove before closing the socket. Call tick from the application's owner timer to enforce expiration even when there is no traffic. An expired handle cannot update the rate. There is no per-socket timer thread. On process exit the kernel closes its FDs, while intentional descriptor sharing may keep the underlying socket alive.

The daemon does not retain a descriptor for actuation. SocketPolicy::attest reuses the existing service association protocol to attach a cookie to an authenticated FLOWCORE intent lease. Use the attested cookie when opening a request scope and send the actual outcome via complete. Explicitly dissociate the cookie through Service during teardown when the lease is reachable. On daemon restart, the application-local policy still applies, but telemetry needs to wait for the new service lease and reattest the same live socket. Application-owned policy state is not reconstructed from the old numeric FD.

```cpp
const auto cookie = managed->attest(service);
auto request = service.begin_request(40'000, cookie);
// Perform the application's request, then report its actual outcome.
request.complete(flowcore::v2::Outcome::Ok);
```

## Metric provenance

| Field | Source and meaning |
|---|---|
| Maximum pacing rate | SO_MAX_PACING_RATE readback, bytes/s |
| Current pacing and delivery rates | TCP_INFO estimates, bytes/s, subject to sample age and application-limited state |
| ACKed bytes | TCP_INFO cumulative counter, differenced only across the same live socket cookie |
| Unsent bytes | TCP_INFO send sequence difference sampled at read time |
| CWND, RTT and retransmissions | TCP_INFO, in segments, microseconds and cumulative retransmits |
| Output queue | SIOCOUTQ, queued sequence space including unacknowledged bytes |
| Send buffer | SO_SNDBUF kernel accounting limit. It does not share the same accounting domain as SIOCOUTQ |
| Send readiness threshold | TCP_NOTSENT_LOWAT readback. Writes can cross the threshold and transport/application buffers are separate |
| Request P99 and deadline misses | Application timestamps and outcomes, including failures in deadline accounting |

The SDK reads only TCP_INFO fields covered by the length returned from the kernel. A missing field remains optional. The native loopback fixture tests POLLOUT directly with a stalled receiver. The workload separately measures asyncio drain wait, which also reflects its own transport buffer.

Select congestion control explicitly with select_congestion on the application's socket before connecting. Record readback and use the kernel's currently registered algorithms. This experiment leaves the global default and allowlist unchanged. TCP sockets are supported by the pacing API. MPTCP and UDP option semantics are outside this API's verified scope. [TCP UAPI](https://github.com/torvalds/linux/blob/master/include/uapi/linux/tcp.h)

[Engine](TCP_PACING_ENGINE.md) · [Experiments](TCP_PACING_RESULTS.md)
