# TCP pacing engine

FLOWCORE's pacing engine caps sending on cooperating applications' TCP sockets. The C++23 SDK owns the policy transaction and the local controller. The existing Research Lab supplies verified bottleneck configuration, samples the sender's qdisc, runs the same application workload across modes, and preserves the result ledger.

## Control surface

| Strategy | Effect |
|---|---|
| Static | The application sets a fixed maximum pacing rate on each selected socket. |
| Queue | Backlogged sender qdisc and estimated bulk demand can trigger a lower bulk socket cap. |
| SLO | Requires the same queue and demand evidence plus a populated critical-request P99 window above its target. |
| Hybrid | Uses a preinstalled, verified static HTB class hierarchy and local bulk socket pacing. RTT or application SLO pressure can corroborate queue pressure. |
| Class only | The existing daemon retains HTB control. The pacing controller suppresses socket proposals during active class control or a class transition. |

The research modes choose one dynamic actuator. The combined mode layers fixed HTB and adaptive socket caps without changing both in one tick. FLOWCORE's existing HTB controller and verification remain in use for the class-only comparison. The SDK does not remotely set options on other processes' sockets.

The controller receives available **configured** rate in bytes/s, observed background ACK throughput, an offered-demand proxy, queue bytes, maximum sampled bulk RTT and a critical application request window. The laboratory demand proxy uses unsent socket bytes, current pacing caps and measured ACK throughput. Missing snapshots and unmatched socket lifetimes cannot furnish trustworthy ACK deltas. An RTT sample is transport evidence. It cannot replace application request duration or identify a request deadline.

Two pressure observations and at least 500 ms between observations are required before a reduction. Pressure uses a queue threshold equivalent to 8 ms at the configured capacity. Hybrid also accepts RTT at or above 12 ms as supporting evidence. The SLO branch requires at least 12 requests in the observation and checks the configured application threshold. Three quiet windows can restore capacity toward the static fallback when demand remains. The implementation has a 12.5% deadband, at most 25% step size for normal changes, a one-second cooldown and a 16-action budget before fallback. Per-socket operator bounds apply to every configured cap.

The next comparable observation tests for a severe throughput drop without an SLO improvement and can roll back. Flow-count and capacity changes invalidate the pending assessment. Stale telemetry falls back to the configured static cap when socket actuation is available. This check is deliberately conservative. ACK windows and workload changes can confound attribution. A successful native update requires readback. Group updates are sequential with verified compensation for already-changed members on failure. The Research Lab shares an exclusive lock between dynamic socket updates and scheduled HTB transitions. A socket tick skips contention, and a class transition refuses to wait longer than one second. The lock covers the observed class state and socket readback. Socket and class operations remain separate transactions.

The fallback, lower cap bound and HTB reservation protect different resources. A cap bounds configured sending behavior, and the HTB floor reserves service for backlogged traffic. A cap can leave some of that reservation unused. Neither provides an end-to-end throughput floor or a latency guarantee.

## Capability and deployment boundary

The local application can use SO_MAX_PACING_RATE, ordinary SO_PRIORITY values 0–6, TCP_NOTSENT_LOWAT, TCP_INFO and SO_COOKIE with the TCP socket it owns. Linux can restrict TCP_CONGESTION selection to the configured unprivileged allowlist. The reviewed Linux socket path allows SO_MARK writes and elevated priorities with CAP_NET_ADMIN or CAP_NET_RAW in the governing user namespace. Changing qdiscs requires CAP_NET_ADMIN in the target network namespace. The SDK probes mark access on a scratch socket and exposes no mark-setting policy. The lab uses its existing privileged namespace owner to set qdiscs. BPF program type support and hook attachment authorization are separate checks. The engine uses no new BPF attachment. [Socket option source](https://github.com/torvalds/linux/blob/master/net/core/sock.c) · [IP sysctls](https://docs.kernel.org/networking/ip-sysctl.html) · [BPF attach types](https://docs.kernel.org/bpf/libbpf/program_types.html)

FQ reads TCP departure-time state and can hold packets until eligible. Other qdiscs have different behavior, and TCP also has an internal pacing path. A socket cap is independent of HTB guarantees and may interact with qdisc scheduling, the congestion window and TCP's rate estimator. The benchmark tests configured caps on the actual path and measures receiver goodput. [FQ implementation](https://github.com/torvalds/linux/blob/master/net/sched/sch_fq.c) · [TCP output implementation](https://github.com/torvalds/linux/blob/master/net/ipv4/tcp_output.c)

Run the capability report as the workload UID and in the relevant network namespace. An explicit optional probe uses bpftool to check the kernel's program types and selected sock_ops helpers. It does not attach a program to any workload or interface.

```sh
python3 -B scripts/pacing_capabilities.py
sudo python3 -B scripts/pacing_capabilities.py --probe-bpf
```

[Socket SDK](SOCKET_INTELLIGENCE.md) · [Results and benchmark commands](TCP_PACING_RESULTS.md)


The receiver-guarded research arm and follow-up evaluation are documented in
[Pacing remediation](PACING_REMEDIATION.md) and [Pacing regression results](PACING_REGRESSION_RESULTS.md).
