# FLOWCORE runtime performance engineering

> Historical scoped evidence. The independent maintainer review is in [BENCHMARK_REPORT.md](BENCHMARK_REPORT.md) and [FEATURE_MATRIX.md](FEATURE_MATRIX.md); older results below are not new independent validation or general performance guarantees.

## Measurements and changes

Fedora 45 AArch64, kernel 7.2.11-300.fc45.aarch64. The system has neither
perf nor strace installed. The recorded measurements use Python cProfile,
process CPU clocks, /proc task CPU/RSS/context-switch counters, existing
collector/actuator instrumentation and real namespace TCP experiments.
System-call counts and hardware performance counters were not measured.

The fixed aggregation fixture represents 60 one-second buckets, eight
services and five occupied histogram bins per service. It computes 900
window summaries. Before optimization, cProfile attributed 1.224 seconds
of a 1.377-second profile to 170,400 Histogram.merge calls. Each call
scanned all 257 bins regardless of occupancy.

Histogram.merge now traverses occupied indices. Storage stays bounded by
257 bins, and add, merge, overflow counts and quantile boundaries retain
their meanings. A regression test compares merged bin arrays and quantiles
against a dense reference, including overflow. The extra occupied-index
list trades bounded memory for lower merge cost.

The daemon event loop now waits up to one second in host-only idle mode.
Active labs retain the 250 ms loop. Socket and netlink events wake select
immediately. Collector refresh and watchdog processing continue. This is
adaptive event-loop waiting, not adaptive INET_DIAG decimation.

| Single profiled fixture | Before | After |
|---|---:|---:|
| Aggregation CPU | 1.369 s | 0.196 s |
| Idle voluntary switches over 3 s | 11 | 2 |
| Idle RSS | 26.67 MB | 27.01 MB |
| Observe IPC median, 100 requests | 0.909 ms | 0.727 ms |
| Observe IPC P99 | 3.889 ms | 2.158 ms |

The aggregation fixture improved about sevenfold in that instrumented
comparison. The idle and IPC observations are short and noisy. RSS did
not decrease. Five additional alternating before/after aggregation pairs
are saved separately, using the original module loaded directly from Git.
These are synthetic aggregation measurements rather than application
latency results.

## Profiling and regression commands

```sh
python3 -B scripts/runtime_profile.py results/performance.json
python3 -B scripts/runtime_profile.py results/performance-check.json \
    --baseline results/performance.json --max-regression .25
python3 -m pstats results/performance.json.prof
ctest --test-dir build --output-on-failure
sudo python3 -B scripts/observer_perf.py --duration 4 --rate 80 \
    --repetitions 1 --modes off basic diag bpf shadow active --verify-live
```

The optional gate records ratios and PASS/REGRESSION per CPU, RSS and IPC
P99 metric. It is a local budget check, not a statistical performance
claim. Use repeated, isolated runs and suitable host-specific budgets.
Event-loss and native controller tests remain in CTest and observer_perf's
explicit live/loss validation. The profile records CPU clock granularity.

The traffic matrix uses the same controlled sender shaper and receiver
payload accounting across modes. Off disables observation but retains the
daemon and its lab setup. Basic bounds the socket snapshot to 64 entries,
diag uses the standard deep snapshot, bpf additionally attaches TCP hooks,
and shadow/active start the hybrid strategy with application reporting.
Idle is independently measured by runtime_profile. Completely absent
daemon traffic overhead is not measured by the off arm.

## eBPF review

The namespace filter runs before event reservation. The collector has a
256 KiB ring, a bounded 4,096-entry LRU lifecycle map and fixed scope/loss/
sequence maps. Loss and sequence updates use atomic global counters.
A per-CPU redesign might reduce contention but would change accounting
and identity semantics. No contention profile justified that change here.

The native helper batches output into a bounded 4 KiB buffer and uses
nonblocking pipe transport. Ring reserve, transport, decoding and parser
loss remain separate observable counters. Python reads at most 64 KiB
per drain, and lifecycle/event retention is bounded. The real off/diag/BPF
matrix and live report verification are preserved before and after.
The integration uses admitted namespaces rather than global TCP tracing.

## Remaining limits

The lab still spawns multiple tc processes per collection. Existing
instrumentation records kernel counter collection, INET_DIAG processing,
aggregation, policy computation, verified HTB writes and IPC round trips.
Replacing tc reads requires a separately verified native decoder for each
supported qdisc. Actuation verification was retained.

TCP polling intervals remain configured and short connections can fall
between snapshots. Event buffering is bounded with explicit loss, rather
than a guarantee of complete coverage. IPC remains authenticated bounded
one-request stream JSON. Binary framing and persistent sessions would
require their own security and compatibility measurements.

The changes target a verified CPU hot path and idle wakeups. They do not
establish lower application P99 or zero overhead across deployments.
Long-duration leak testing, syscall profiles, an absent-daemon traffic
baseline and statistically powered controller-impact measurements remain
open.

## Real traffic validation

Five alternating aggregation pairs gave median CPU 1.300 s before and
0.119 s after, with ranges 1.286–1.329 and 0.119–0.122 s. This unprofiled
fixture improved about 10.9 times. The instrumented and uninstrumented
timings are separate measurements.

In the live four-second workload the daemon CPU deltas were:

| Mode | Before CPU seconds | After CPU seconds |
|---|---:|---:|
| Observation off, daemon present | 0.0568 | 0.0583 |
| INET_DIAG | 0.1050 | 0.0979 |
| BPF plus INET_DIAG | 0.4410 | 0.4490 |
| Shadow hybrid | unavailable | 0.2429 |
| Active hybrid | unavailable | 0.3002 |
| Basic bounded INET_DIAG | unavailable | 0.1100 |

These deltas cover about seven wall seconds including workload warmup.
Subprocess CPU is separately recorded. It was roughly 0.12–0.21 seconds
in the after runs. The before/after experiments have one repetition,
and the application P99 and memory changes are inconclusive. All six
after modes and the additional live-report check had zero failed requests.
BPF mode did not show a daemon CPU reduction. No such reduction is claimed.

Raw evidence is in results/performance-before.json,
results/performance-after.json, results/performance-paired-aggregation.json,
results/observer-perf-1793876301257854509/ and
results/observer-perf-1793876399638483193/. The .prof files preserve the
aggregation profiles. The IPC measurements used the existing authenticated
Unix daemon socket with unchanged JSON framing.
