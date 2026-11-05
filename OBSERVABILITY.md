# FLOWCORE Deep Observability Engine

## Run it on the Fedora laboratory

Build in the canonical project directory. CMake builds the optional CO-RE object
and C++23 collector when libbpf, BTF, bpftool and a suitable clang are available.
Set `-DFLOWCORE_BPF=OFF` at configure time to build without that collector.
At runtime, select `observability.bpf: "off"` for INET_DIAG only or
`observability.enabled: false` to disable the observer. Startup does not depend
on BPF attachment success. The health view reports BTF, attached hooks and any
fallback reason.

```sh
cmake --build build
ctest --test-dir build --output-on-failure
sudo ./build/flowcored --config config/service.json
```

Run these commands from another terminal:

```sh
sudo ./build/flowcore lab up
sudo ./build/flowcore observe --interface fc0 --watch 1
sudo ./build/flowcore connections --limit 64 --csv
sudo ./build/flowcore congestion --json
sudo ./build/flowcore events --jsonl --watch 0.5
sudo ./build/flowcore health --json
sudo ./build/flowcore metrics --json
sudo ./build/flowcore metrics --prometheus
```

The Prometheus command writes one text exposition. A scheduled scrape adapter or
a node_exporter textfile collector can consume it. Exported dimensions are
limited to configured services, traffic classes, fixed queue names, outcomes,
percentiles and windows. The CSV and connection JSON are paginated socket
snapshots. An event follow stream emits `stream_gap` when its cursor falls behind
the 1,024-record retention buffer. Ring reservation failures and pipe drops are
independent counters exposed by health. Zero means the collector reported no loss
at the last sample. A null loss counter means no report is available.

## Metric provenance

| Metric | Source and unit | Interpretation and limits |
|---|---|---|
| Smoothed RTT, RTT variation, RTO | TCP_INFO via INET_DIAG or getsockopt, µs | Kernel TCP estimates. The RTT histogram samples established sender sockets with positive SRTT. Sockets can appear in multiple snapshots. |
| Congestion window | TCP_INFO, segments | Sender cwnd at the sample. |
| Retransmission and RTO totals | TCP_INFO, segments and timeout counts | Lifetime counters for the sampled socket. RTO count and time (ms) are emitted only when the kernel returns those fields. Timer/backoff fields are diagnostic state, not lifetime totals. |
| Retransmit ratio | TCP_INFO counter deltas on matching sender sockets | Delta of total retransmitted segments divided by delta of data segments out over matched snapshots. Control retransmits can contribute to the numerator. Disappearing sockets, resets/wraps and truncated samples can omit intervals. |
| Delivery rate and bytes | TCP_INFO, bytes/s and bytes | Last kernel delivery-rate estimate and cumulative sent, acknowledged, received and retransmitted octets. Delivery rate is not sustained link throughput. |
| Receive windows | TCP_INFO, bytes | Peer and local advertised windows when returned. Receive space is a separate kernel receive-space estimate. |
| Buffer and socket queues | INET_DIAG SKMEMINFO and socket diagnostic queues | Allocations and limits where returned. TCP listen queue fields count connections rather than bytes. Instrumented client also saves one own-socket TCP_INFO/SO_SNDBUF/SO_RCVBUF snapshot. |
| Lifecycle | Scoped raw TCP tracepoints and INET_DIAG | Trace state transitions and received RSTs cover short connections. First/last observed by polling are sampling events. A vanished socket has unknown disposition. |
| Lifetime | eBPF CLOCK_MONOTONIC, ns; observed polling age, ms | Trace duration runs from an observed SYN transition or first ESTABLISHED to TIME_WAIT/CLOSE. `origin_state` distinguishes these cases. Missing map state gives no duration. Polling age starts at the first seen snapshot. |
| Connection establishment failures | Application-reported outcomes and SYN-to-CLOSE trace candidates | A SYN-to-CLOSE transition can also represent cancellation. It is a suspected handshake failure, not a definitive connect errno. |
| Process and network association | INET_DIAG UID, inode, cookie, cgroup ID, bound ifindex when supplied | Owner PID is not inferred from execution context. Shared FDs and PID reuse prevent that inference. Bound ifindex is not a guessed route. The daemon labels the two lab namespaces. |
| Qdisc backlog, drops, queue length and overlimits | `tc -s -j`, bytes/packets and scheduler counts | Scheduler-specific counters. Root and leaf counters may overlap. The engine does not sum parent and child drops. |
| Class throughput and utilization | HTB counter deltas, bytes/s and fraction of configured link capacity | Uses actual elapsed poll time. Utilization can briefly exceed 1 due to burst scheduling. The saturation indicator requires sampled backlog and at least 90% utilization in the longest configured window. |
| Interface bytes, packets, drops and errors | Native RTM_GETLINK IFLA_STATS64 | Interface counters are separate from qdisc drops. |
| Application latency and failures | Explicit Unix socket reports, µs | P50/P95/P99 cover successful samples. Failure and timeout counts and error ratio are separate. No latency percentiles are inferred from RTT. |
| Deadline misses | Explicit per-request deadline, ms | Count reports exceeding the supplied deadline or timing out. A deadline is application-provided. |

Socket cookies are scoped by namespace and topology generation, with tuple
checks before carrying ages or counter deltas forward. In the BPF path
`flow_id` is a collector-local sequence allocated at the first tracked state.
Private kernel socket pointers serve only as short-lived keys in a 4,096-entry
LRU map. A zero cookie or flow ID is unknown. Generation changes invalidate
cross-lab correlation. Neither process IDs nor kernel addresses are exported
from tracepoint context.

The ring is 256 KiB. Its collector drains at a 20 ms cadence and writes bounded
nonblocking batches. Filtering is by the two lab namespace inodes before
recording. Raw hooks still run system-wide and execute the filter. A kernel
ring reservation failure or pipe write failure increments its own counter.
On shutdown links and maps are closed. A parent-death signal also handles
daemon loss. The loader attempts each hook independently after loading its
CO-RE object. A load failure falls back to snapshots.

## Windows and detector

The default windows are 1, 10 and 60 seconds. Configure up to eight windows of
1–300 seconds and up to 16 registered service labels. The observer keeps
per-second buckets and 257-bin logarithmic histograms. Quantiles use the
nearest-rank bucket upper bound, about 9.1% relative error plus rounding for
positive values in range. An overflow quantile is null. The current partial
second is included. TCP and qdisc deltas are assigned to the bucket at the
end of their sampling interval, with actual covered time shown for queue rates.
A one-second window can be sparse when polling is slower than one second.
Late events within retention are accepted, and older events are counted as
late drops. Event sequence follows arrival order. Timestamps retain the original
monotonic source time.

Detection evaluates at configurable observation intervals. RTT and backlog use
P95 threshold crossings. Retransmit ratio, drop rate, handshake candidates,
application failures and SLO P99 have separate signals. A throughput floor
is opt-in because idle traffic alone is not degradation. Entry and exit require
configurable consecutive eligible windows. Missing samples mark the finding
unavailable and do not add to estimated degraded time. Simultaneous active
findings produce a correlation event with supporting values. These events are
observations and do not establish a cause.

## Application IPC

Use the existing daemon Unix socket. Messages are one JSON object terminated
by a newline, with one response per connection. The daemon authenticates the
peer using SO_PEERCRED and requires its own effective UID. With a root daemon
that means reports come from trusted root workloads or a trusted collector.
Separate unprivileged ingestion and service-specific identity are future
privilege-separation work.

For authoritative application timing use the application's monotonic clock,
then send `request_sample` with `service`, `duration_us`, `outcome` and optional
`class` and `deadline_ms`. Service names and classes must match registration.
Failed samples may include a fixed failure_reason such as connect_refused.
The daemon assigns the sample to its receipt time. For example:

```sh
sudo ./build/flowcore report sample demo --duration-us 12345 --outcome ok --deadline-ms 20
sudo ./build/flowcore report start demo --deadline-ms 20
sudo ./build/flowcore report complete demo --token TOKEN --outcome ok
```

Start and completion are paired by a daemon-issued, one-use token scoped to
service and peer UID. The resulting duration includes the two IPC boundaries
and is reported separately as `ipc_boundary`. Up to 1,024 starts can be in flight,
with expiry at 600 seconds. `scripts/workload.py --report-service demo` reports
actual completion durations through a bounded asynchronous queue and records
accepted and dropped reports.

## Overhead and regression runs

Stop the management daemon before the test. Use the same duration, offered
rate, order seed and class configuration in repeated comparisons.

```sh
sudo python3 -B scripts/observer_perf.py --duration 6 --repetitions 2 --verify-live
sudo python3 -B tests/service_integration.py
sudo python3 -B scripts/integration_test.py
```

The overhead script alternates disabled, INET_DIAG and BPF+INET_DIAG modes.
It records per-run application P99, failures, background receiver goodput,
daemon/child CPU seconds, BPF helper CPU seconds, RSS and event loss/processing
rates. Host CPU measurements include the rest of the VM. They do not isolate
the cost of the tracepoint dispatch path. The live verification also reports
each real request and verifies a refused TCP connection. See
OBSERVABILITY_RESULTS.md for this machine's measurements.

References: [Linux BPF ring buffer](https://docs.kernel.org/bpf/ringbuf.html)
and [libbpf and CO-RE](https://docs.kernel.org/bpf/libbpf/libbpf_overview.html).
