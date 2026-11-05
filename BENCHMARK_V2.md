# FLOWCORE benchmark v2

## What is compared

Run the v2 manifests through the existing Research Lab. Each run starts
fresh sender and receiver network namespaces linked by a veth. The client
at `10.203.0.1` sends application requests and bulk payload through
`fc0`. The server at `10.203.0.2` receives on `fc1`. The configured
bottleneck is the sender's egress. Netem runs on receiver **egress** and
impairs ACKs and application replies. The historical field name
`base_rtt_ms` configures this one-way netem delay. It is not a measured
RTT or symmetric path impairment. [iproute2 netem documentation](https://github.com/iproute2/iproute2/blob/main/man/man8/tc-netem.8)

| Mode | Sender configuration |
|---|---|
| `linux-default` | Native veth noqueue on both ends, with neither rate shaping nor netem. A separate descriptive reference because the delivered capacity differs. |
| `fifo` | Single HTB bottleneck class with explicit pfifo, 1,000 packets. |
| `fq_codel` | Same single HTB bottleneck class with fq_codel, 1,000 packets. |
| `cake` | Rate-limiting CAKE root, besteffort and per-flow isolation. |
| `htb-fq_codel` | Fixed product-default HTB reservations and four fq_codel leaves, with port classification. |
| `static` | Same classified HTB hierarchy with a frozen critical reservation selected on independent pilot data. |
| `threshold`, `slo` | Same classified starting hierarchy with the existing FLOWCORE threshold or SLO controller. |

The legacy v1 mode named `linux` is an HTB-shaped baseline with an implicit
leaf. It remains available for replay of v1 evidence. The v2 default veth
reference uses the same experiment offload controls and is not a physical
NIC baseline. Classified and single-class policies differ in leaf count and
queue limits. They are whole-policy comparisons. The controlled comparison
for attribution to a controller uses the same classified starting hierarchy
as its frozen static peer. The supplied evaluation uses the winning pilot
reservation as its static reference.

`topology.json` records separate configuration, verified kernel readback
and observed link/class/qdisc counters. The runner checks veth addresses,
MTU, TSO/GSO/GRO, root qdisc, shaper rate/ceil, leaf hierarchy, and the TCP
flower destination-port mapping for IPv4 and IPv6. Benchmark traffic is
IPv4. For each sampled constant-condition span, the result records bytes
and packets in each direction and compares sender TX with receiver RX and
sender qdisc counters. It estimates actual egress rate using kernel link
byte deltas and elapsed monotonic time. Overlimits together with observed
egress within 15% of readback support saturation in that span. Otherwise
the cap is verified as configured and saturation remains unestablished.
Kernel counter domains and sample boundaries can differ. Gaps above 5%
are investigated as warnings, with raw counters retained.

## Workload and measurements

- The manifest fixes payload size, duration, concurrency cap, per-phase
  arrival rates, bulk stream counts, optional application pacing, warmup,
  class proportions and deadlines. Each request opens a new TCP connection
  and validates a 64-byte reply. Bulk streams reuse connections where the
  peer permits. Zero pacing means greedy bulk traffic. Pacing is a
  userspace target subject to timers and TCP backpressure.
- Warmup uses the first phase's bulk streams before the request measurement
  window. In v2, finite bulk budgets require zero warmup. Impairment offsets
  are relative to the measurement start. Applied timestamps and readback
  are preserved so an operator can see transition timing error.
- A fresh topology is constructed for each mode and repetition. Distinct
  deterministic block seeds drive class selection and netem. Every mode in
  one block shares that seed and workload. Order is randomized within blocks.
  Loss seeds do not force identical packets to be dropped when policies
  alter packet ordering. Logical critical services have separate ledger
  identities and share the critical port and controller feedback channel.
- Latency runs from scheduled issue to validated completion, including
  dispatch delay, connection establishment, request and reply. P99 is
  reported for successful requests alongside offered, completed, failure
  and timeout counts. Deadline attainment divides on-time successes by
  all offered requests. Late successes, timeouts, errors and inflight-cap
  rejections all miss the deadline. The request ledger includes the drain
  after load generation. Goodput counts only requests completed within the
  measurement window and bulk payload received in that same window.
- Host evidence includes kernel, VM identity where exposed, CPU topology,
  affinity and governor, load average, aggregate CPU ticks and PSI counters.
  Dispatch lag, high steal time, pressure and counter gaps are warning
  signals. Reports also flag observations outside Tukey fences when a mode
  has at least six usable runs. No observations are automatically removed. Application dispatch
  lag includes event-loop overhead and is not a direct scheduler trace.
  A guest cannot observe every source of hypervisor noise.

## Gates and uncertainty

Use independent pilot seeds to select 20%, 30% or 40% critical reservation.
The tuning command requires at least two valid, failure-free pilot runs
per eligible mode and median background goodput of at least half the
configured cap. It ranks eligible modes by median deadline attainment,
then successful P99, then smaller reservation. It writes a separate
evaluation manifest and records the pilot digest.

Declare the baseline, margins and minimum number of pairs in the manifest
before evaluation. The default serious comparison has 10 blocks. A block,
rather than a request, is the statistical unit. Analysis uses distribution-free
order-statistic intervals for median paired effects with Bonferroni coverage
across the planned baseline comparisons and metrics. It assumes independent
blocks with a stable effect distribution. Shared VM activity can violate
these assumptions. Run order, effects, variance and warnings remain
available for inspection. Repeats on one VM do not generalize to other hosts.

A gate is PASS when its upper interval bound stays within the declared
harm margin, REGRESSION when the lower bound exceeds it, and INCONCLUSIVE
when the interval overlaps the margin or there are too few eligible pairs.
An infrastructure or readback failure produces INVALID EXPERIMENT. A report
from an incomplete block schedule cannot pass a gate. A
successful-only P99 pair is excluded from that gate if either run has a
failure. The same pair remains in failure and deadline comparisons.
A PASS means the harm margins were met. A lower-P99 claim additionally
requires a strictly negative upper bound for the P99 effect.

## Reproduce

```sh
sudo ./build/flowcore bench run scenarios/v2-static-pilot.json
./build/flowcore bench tune-static <pilot-directory>/summary.json \
    scenarios/v2-heavy-paired.json results/v2-heavy-evaluation.json
sudo ./build/flowcore bench run results/v2-heavy-evaluation.json
sudo ./build/flowcore bench run scenarios/v2-linux-default.json
sudo ./build/flowcore bench run scenarios/v2-low-load.json
sudo ./build/flowcore bench run scenarios/v2-transitions.json
sudo ./build/flowcore bench run scenarios/v2-outage.json
sudo ./build/flowcore bench run scenarios/v2-blackhole.json
```

The low-load and transitions manifests use one repetition for functional
coverage. Increase repetitions in a copied manifest before a serious
comparison. Transitions cover low load, sudden background bursts, bandwidth
reduction/restoration, loss, high configured reverse delay and mixed classes.
The privileged benchmark runs in a trusted checkout. It still uses the
historical research runner and does not exercise the split network helper.

Per experiment, keep `summary.json`, `experiment.json`, `reproduce.sh`,
`report.html`, `report.json`, `report.csv` and `aggregate.csv`. Each run
keeps the request and receiver ledgers, kernel and fault timeline, topology,
host-noise samples, controller history, configuration and logs. The
recorded Git commit, build digest and source hashes identify the run
implementation. Raw artifacts remain in the local ignored results tree.

## Fedora 45 ARM64 validation

The two-block pilot selected a 30% critical reservation. The 30% mode
had slightly higher median deadline attainment than the 40% mode, which
had the lower successful-request P99. The selection was frozen before
starting evaluation. In this evaluation, both static and SLO therefore
started from the same 30% critical reservation.

The heavy-load evaluation completed 10 paired blocks, with 20 valid runs
and 5,400 offered requests per mode. Each measurement lasted six seconds.

| Metric | Tuned static | FLOWCORE SLO |
|---|---:|---:|
| Median run successful-request P99 | 21.00 ms | 21.08 ms |
| Median run background goodput | 6.419 Mbit/s | 6.401 Mbit/s |
| Median run combined goodput | 9.365 Mbit/s | 9.347 Mbit/s |
| Median run 25 ms deadline attainment | 99.63% | 99.63% |
| Failed requests | 0 / 5,400 | 0 / 5,400 |

Sender egress ranged from 9.848 to 10.003 Mbit/s across the 20 runs
against the 10 Mbit/s readback. All 20 windows also recorded shaper
overlimits and positive byte increments in every configured class. The
largest sender TX to receiver RX gaps were below 0.08% for both bytes
and packets. The median paired P99 change for SLO versus static was
**+2.45%**, with an exact conservative simultaneous interval from
**−25.97% to +52.26%**. The 10% harm margin falls inside the interval.
The P99 gate and overall result are **INCONCLUSIVE**. The combined
goodput, failure-rate and deadline-attainment gates passed for this
workload. One SLO adjustment was recorded across the ten short runs.
This experiment provides limited evidence about behavior after a
controller adjustment. The host ran local unit checks during an early
block. That block was retained and possible scheduling interference
remains a limitation.

Raw pilot `results/research-1793844111837735576/`, frozen manifest
`results/v2-heavy-evaluation.json`, and evaluation
`results/research-1793844193353514534/`. The evaluation began before
its source was committed. All 53 recorded source digests match commit
`c9cae43`. The build digest and dirty Git status at launch remain in
the original summary.

The transition matrix completed seven valid functional runs. It observed
egress near 7 Mbit/s during the capacity reduction and near 10 Mbit/s after
restoration when the shaper was saturated. The low-load runs observed
about 1.84 Mbit/s egress against a 10 Mbit/s configured cap. The link-down
scenario produced 501 socket errors out of 580 offers in each mode.
Its conditional P99 was rendered, with zero eligible P99 pairs and the
failure and deadline data retained for the gates. These one-block
experiments are functional checks.

Other evidence directories in `results/` are
`research-1793844369151764735` (transitions),
`research-1793844442248983740` (low load), and
`research-1793844467587775171` (link outage). The initial Linux default
attempt was retained as invalid after discovering that the daemon allows
one registration per traffic class. The implementation now records
logical service identities in the workload ledger and registers each
traffic class once. The later Linux default run exposed the absence of
qdisc accounting for noqueue. The report now marks that counter domain
unavailable and uses the independent link counters.
