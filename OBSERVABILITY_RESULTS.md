# Deep Observability Engine validation

Target: Fedora 45 ARM64, Linux 7.2.11-300.fc45.aarch64. The local kernel BTF
was available. The CO-RE object loaded and the raw tracepoints
inet_sock_set_state, tcp_retransmit_skb and tcp_receive_reset all attached.

## Live end-to-end evidence

Raw evidence is in `results/observer-perf-1793810652893218014/live/`.
The veth workload completed 600 real TCP requests. The application reporter
submitted all 600 timings and dropped none. The raw client P99 was 22.02 ms.
The streaming histogram returned a 23.171 ms bucket upper bound for the same
successful request sample count. Separately, 39 sender socket observations
produced a TCP SRTT P99 bucket upper bound of 17.867 ms.

The helper processed 6,512 events at about 674 events/s before the deliberate
failed connect. Kernel ring reservation, pipe transport and parse loss counters
were all zero at the last report. The 10-second post-failure window recorded
6,029 state transitions, 446 retransmit trace events, one received RST and
three SYN-to-CLOSE candidates across both namespaces. A real ECONNREFUSED
from the client's closed-port probe was also reported independently as one
`connect_refused` application error. The trace candidates are not a count
of definitive application connection errors.

A forced SIGTERM of the owned libbpf process left the daemon responsive.
`after-collector-exit.json` records inactive BPF, a fallback reason and a
fresh INET_DIAG sample. The workload also saved TCP_INFO directly from its
own socket. Prometheus text, connection CSV, JSON snapshots and JSONL events
are in the same run directory.

## Observer cost

`results/observer-perf-1793810464879702722/summary.json` contains the complete
six-run comparison. Each run used 600 offered requests at 100/s, four
background streams, a 20 Mbit/s shaper and the same lab profile. Run order
was shuffled with seed 20261105. Application reporting was disabled for these
six runs. CPU is daemon + reaped subprocesses + live BPF helper CPU divided
by measured wall time, expressed as a percentage of one logical CPU.

| Mode | Request P99 per run (ms) | CPU per run (% of one core) | Daemon RSS range (MiB) | BPF RSS (MiB) | Background range (Mbit/s) |
|---|---:|---:|---:|---:|---:|
| Observer disabled | 52.51, 32.12 | 3.59, 3.62 | 22.63–24.08 | — | 14.64–14.81 |
| INET_DIAG only | 22.86, 26.03 | 5.04, 3.82 | 22.45–23.99 | — | 15.09–15.10 |
| INET_DIAG + eBPF | 25.20, 83.06 | 7.95, 7.76 | 23.48–24.24 | 5.26 | 14.85–15.00 |

All six completed with zero request failures. The BPF runs processed 6,494 and
6,503 trace events with zero reported ring or transport losses. This is
exploratory evidence from two short repetitions per mode. It shows extra
userspace CPU cost and a variable application tail, including an 83.06 ms
BPF run. It does not establish a causal latency impact. Kernel dispatch
CPU and map memory are not attributed by these userspace measurements.

## Verification and remaining boundaries

Both the CO-RE and FLOWCORE_BPF=OFF builds passed all three CTest targets.
Tests cover actual loopback INET_DIAG versus getsockopt TCP_INFO, short
kernel structs, quantile accuracy at 100,000 samples, retention, timestamp
ordering, cookie reuse, loss parsing, fallback, request bounds and detector
hysteresis. The privileged service and original adaptive controller regression
suites passed. Their logs are under results/observer-service-regression.log
and results/observer-adaptive-regression.log.

The observer is lab-scoped. Sockets may be missed by bounded polling.
The daemon does not assert owner PID, route selection for unbound sockets,
exact lifetime for pre-existing sockets or causality from coincident signals.
The socket remains same-UID, and the Prometheus exporter is text output.
The observer publishes evidence for a future controller integration.
