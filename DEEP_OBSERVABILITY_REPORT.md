# FLOWCORE Deep Observability Engine completion

> Historical scoped evidence. The independent maintainer review is in [BENCHMARK_REPORT.md](BENCHMARK_REPORT.md) and [FEATURE_MATRIX.md](FEATURE_MATRIX.md); older results below are not new independent validation or general performance guarantees.

This increment extends the existing daemon, bounded aggregation windows,
Unix IPC, SDK reporting, native inventory and optional CO-RE collector.
It does not create a second control service.

| Area | Working path and validation boundary |
|---|---|
| TCP metrics | INET_DIAG/TCP_INFO and a direct owned-socket API. State, SRTT, RTT variation, RTO estimate and returned totals, cwnd, window estimates, octets, delivery rate, ECN delivery counters and buffers are length-gated. Native C++ layout assertions verify supported UAPI members on the build architecture. |
| Host observations | The ordinary-user daemon now populates observe/connections from the same native inventory collection. Kernel counters, root queue data, socket samples and availability are real. Host interface selection does not imply a route-based association for unbound sockets. |
| CO-RE backend | Three scoped raw tracepoints for TCP state, retransmission and received reset. BTF/build prerequisites and actual attachment outcomes are reported. The collector uses a 256 KiB ring and a 4,096-entry LRU start map. No new privileged host tracing service is enabled. |
| Lifecycle | Bounded userspace records retain addresses, ports, state transitions, observed lifetime and event counts across short connections. Namespace, generation and collector flow ID scope identity. Known cookie conflicts and time regressions are rejected. Snapshot correlation requires a matching kernel cookie in the same namespace. |
| Identity | INET_DIAG supplies UID, inode, cookie, cgroup ID and bound interface where supported. The direct API supplies caller PID and start ticks. External socket-owner PID is unknown. Kernel trace execution context is not treated as an owner. |
| Windows | Existing 1/10/60-second and configurable bounded windows retain application and TCP distributions separately, plus throughput and matched-socket counter deltas. Missing kernel fields remain absent or null. |
| Detection and correlation | Existing threshold, minimum-sample and hysteresis detection now carries explicit timestamped window evidence. Missing data makes a finding unavailable. Handshake candidates use an actual event count for minimum samples. Correlation events explicitly avoid causal claims. |
| Export and CLI | JSON snapshots include a metric catalog. JSONL event streams and paginated TCP/lifecycle CSV are available. Prometheus exports use bounded configured labels and never socket cookies, remote addresses or PID labels. |
| Robustness | The native collector enforces atomic pipe-sized batches. Strict transport parsing rejects malformed versions, ranges and addresses. Kernel reserve loss, pipe loss, parse drops, timestamp drops and userspace retention eviction have separate counters. |

## Reproduction

```sh
cmake -S . -B build -G Ninja -DFLOWCORE_BPF=ON
cmake --build build
ctest --test-dir build --output-on-failure
python3 -B tests/deep_integration.py
sudo python3 -B scripts/observer_perf.py --duration 4 --repetitions 2 --verify-live --verify-loss
```

Use a separate build tree with `-DFLOWCORE_BPF=OFF` for the fallback build.
The host test runs without capabilities and verifies a real loopback TCP
connection against the caller's TCP_INFO, application start/completion,
failure/deadline counting, native host queues and bounded exports.
The privileged test uses disposable namespaces and also exercises
a refused connection, traced states/resets/duration, IPv4 and IPv6
address decoding, forced ring pressure and fallback after collector exit.

## Interpretation limits

Polling can miss short-lived sockets. A socket disappearing from INET_DIAG
does not establish a reset, close cause or application failure. Traced
SYN-to-CLOSE transitions are handshake-close candidates because cancellation
can produce the same observation. Received-reset traces cover their attached
hook and do not count every possible sent reset. Sampled retransmission ratios
omit unmatched, truncated and reset/wrapped intervals.

BPF flow lifetime starts at an observed SYN transition or first ESTABLISHED,
and ends at TIME_WAIT/CLOSE. Missing kernel start-map entries, LRU eviction
and event loss limit completeness. Kernel start-map LRU eviction has no direct
eviction counter. The userspace retention table does. Durations and states
represent observed events, with origin and loss metadata available.

TCP_INFO delivery rate is a last estimate. Interface throughput uses actual
counter deltas. Application P50/P95/P99 use explicitly reported successful
requests; error and deadline counts remain separate. Host SRTT is sampled
across observed local sockets, including both endpoints of local connections.
Histograms have the existing logarithmic upper-bound error and overflow rules.

The packaged read-only monitor stays capability-free. Owned lab tracing still
uses the trusted privileged test runner and the existing separate BPF process.
CAP_BPF/CAP_PERFMON and kernel policy govern tracing access. This work does not
establish complete sandbox isolation or a production multi-tenant tracing API.

## Measurements on Fedora 45 AArch64

The final overhead experiment used the existing seeded order with two
repetitions of disabled, INET_DIAG and BPF+INET_DIAG modes. Each arm
offered 400 real TCP requests during four measurement seconds, with
four background flows and the same 20 Mbit/s bottleneck. CPU accounting
covered roughly seven seconds including workload warmup.

| Mode | Request P99 per run | Daemon CPU per accounting interval | Daemon RSS | Helper CPU and RSS |
|---|---|---|---|---|
| Disabled | 16.41, 16.89 ms | 0.051–0.063 s | 24.1–25.0 MiB | Not running |
| INET_DIAG | 16.81, 15.90 ms | 0.117–0.124 s | 24.9–25.8 MiB | Not running |
| BPF + INET_DIAG | 17.20, 16.52 ms | 0.527–0.554 s | 27.2–28.5 MiB | 0.020 s and 4.9–5.3 MiB |

Every measured arm completed without a failed request. Background goodput
was 15.47–15.56 Mbit/s across those six runs. The BPF collector processed
4,389 and 4,396 events, averaging about 593–595 events/s over its observed
lifetime, and reported zero ring, transport or parser losses during
these normal runs. Summing daemon, tc child and BPF helper accounting
gives about 0.70–0.72 CPU seconds for the BPF arms, compared with
0.19–0.21 disabled. The extra processing cost is measurable. Two repeats
are an exploratory overhead check and do not establish a P99 regression
bound.

The separate forced-pressure phase stopped consumption while 900 real TCP
exchanges ran. The resumed collector reported 6,019 ring-reservation drops
and 2,723 transport drops, with zero malformed parsed records. These are
different loss stages. Their sum does not identify which connection states
are missing. IPv6 traces independently recorded both
fd42:203::1→fd42:203::2 and the reverse endpoint with closed states.
After explicit helper termination, the daemon continued sampling
INET_DIAG.

Normal measurements, raw requests, kernel snapshots and the live tests
are in `results/observer-perf-1793874967706915208/`. The ordinary-user
host proof is in `results/deep-host-1793874668876114221/`. The earlier
address-copy failure and its diagnostic artifacts remain in the original
overhead directory. That failure led to the explicit-width CO-RE read
and the live address assertions.

The compiled TCP_INFO test checks offsets against this machine's UAPI.
Fields absent from older build headers are skipped by that layout test,
while Python still length-gates every runtime read. RTO totals, new receive
window fields and cgroup IDs therefore remain kernel-dependent. The
mechanism reports values actually returned by the kernel.

## Metric catalog

| TCP field | Unit | Meaning |
|---|---|---|
| `state_code` | count_or_code | Linux TCP state enumeration at this snapshot. |
| `congestion_state_code` | count_or_code | TCP_CA state from Linux UAPI. |
| `current_retransmits` | count_or_code | Current retry/backoff state, not lifetime retransmissions. |
| `rto_us` | microseconds | Current retransmission timeout estimate, not a timeout count. |
| `send_mss_bytes` | bytes | Current send maximum segment payload size. |
| `unacked_segments` | segments | Outstanding unacknowledged segments. |
| `lost_segments` | segments | Segments currently considered lost by TCP. |
| `current_retrans_segments` | segments | Currently retransmitted unacknowledged segments. |
| `rtt_us` | microseconds | Kernel smoothed RTT, sampled per socket. Not request latency. |
| `rttvar_us` | microseconds | Kernel RTT mean deviation estimate. |
| `cwnd_segments` | segments | Current sender congestion window in segments. |
| `rcv_space_bytes` | bytes | Kernel receive-space estimate, distinct from advertised receive window. |
| `total_retrans_segments` | segments | Lifetime retransmitted segments, including handshake retransmissions. |
| `bytes_acked` | bytes | Lifetime data octets acknowledged, kernel TCP accounting. |
| `bytes_received` | bytes | Lifetime data octets received. |
| `segments_out` | segments | Lifetime sent TCP segments including control segments. |
| `segments_in` | segments | Lifetime received TCP segments including control segments. |
| `notsent_bytes` | bytes | Data buffered but not yet sent. |
| `min_rtt_us` | microseconds | Minimum RTT observed by TCP. |
| `data_segments_in` | segments | Lifetime received TCP data segments. |
| `data_segments_out` | segments | Lifetime transmitted data segments, including retransmissions. |
| `delivery_rate_bytes_per_second` | bytes_per_second | Last delivery-rate estimate. Not sustained interface goodput. |
| `busy_time_us` | microseconds | Cumulative time busy sending. |
| `receive_window_limited_us` | microseconds | Cumulative sending time limited by peer receive window. |
| `send_buffer_limited_us` | microseconds | Cumulative sending time limited by sender buffer. |
| `delivered_segments` | segments | Segments delivered to receiver according to TCP delivery accounting. |
| `delivered_ce_segments` | segments | Delivered segments reported with ECN congestion experienced marks. |
| `bytes_sent` | bytes | Lifetime transmitted data octets including retransmitted data. |
| `bytes_retrans` | bytes | Lifetime retransmitted data octets. |
| `peer_receive_window_bytes` | bytes | Scaled receive window advertised by the peer. |
| `local_receive_window_bytes` | bytes | Scaled local advertised receive window. |
| `total_rto` | count_or_code | Lifetime RTO timeout count including handshake and recurring timeouts. |
| `total_rto_recoveries` | count_or_code | Lifetime RTO recovery periods including unfinished recovery. |
| `total_rto_time_ms` | milliseconds | Lifetime time in RTO recovery, including unfinished recovery. |

Every field uses TCP_INFO from a held descriptor or INET_DIAG. The JSON catalog also gives the minimum returned structure length for each field.

References for field semantics and transport are the [Linux TCP UAPI](https://github.com/torvalds/linux/blob/master/include/uapi/linux/tcp.h) and [BPF ring-buffer documentation](https://docs.kernel.org/bpf/ringbuf.html).

## Verification status

Both BPF-enabled and BPF-disabled builds passed 9/9 CTest targets on the
Fedora AArch64 machine. Staged installation and SDK consumption passed for
both builds. Deterministic tests cover 100,000 histogram samples, 10,000
lifecycle insertions with a 32-record cap, 2,000 seeded malformed transport
mutations, cookie conflicts, timestamp order, counter resets, collector
failure, split transport records and reported loss. These are test fixtures.
The request and collector measurements above came from real socket workloads.

The native transport was revised after a live test exposed an incorrect
one-byte address copy. The corrected collector was rebuilt and its IPv4
and IPv6 addresses were verified against the dedicated namespace endpoints.
The first failed test remains available under
`results/observer-perf-1793874742108328156/`.

Native event-size/address decoding failures also have a separate kernel_decode_drops counter. The collector does not silently count them as transport loss. The workload now uses the direct owned-socket API for its recorded TCP_INFO snapshot.
