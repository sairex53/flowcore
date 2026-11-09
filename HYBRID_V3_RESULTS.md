# Hybrid V3 results

## Method

Measured on a Fedora 45 ARM64 QEMU guest with four vCPUs, Linux
`7.2.11-300.fc45.aarch64`, iproute2 7.0.0 and the Debug build with BPF disabled.
Each arm had a fresh namespace pair. The sender veth applied the verified
bottleneck. Receiver egress applied the reverse-path netem delay. Offload and
qdisc readbacks, request ledgers, receiver goodput, trial order, host pressure,
seeds and source fingerprints are in the raw artifacts.

The short matrix used ten independent randomized blocks. Each block ran the
same request schedule and phase transitions on all three arms. There were two
critical services, 4 KiB requests on fresh TCP connections, one then six
background sockets, a 65 ms request deadline and a 12 → 7 → 12 Mbit/s
configured transition. The existing static benchmark profile supplies the
30/20/20/30% HTB guarantees, link-rate ceilings, priorities and fq_codel leaves.
Static HTB and HTB + fq_codel are therefore one arm in these profiles.
The previous hybrid uses the existing cooperative pacing controller over the
same class topology. V3 started with an explicitly configured 400,000 bytes/s
cap on each bulk socket. This models an inherited restriction and changes the
initial policy relative to static HTB. It is part of the treatment.

## Short paired matrix

| Arm | Valid | P50 / P95 / P99 ms | Background / total Mbit/s | Deadline misses / offered | Failures | Jain | Daemon + collector CPU s |
| --- | ---: | --- | --- | --- | ---: | ---: | ---: |
| Static HTB + fq_codel | 10 | 15.02 / 20.98 / 25.90 | 9.41 / 10.48 | 1 / 4,580 | 0 | 0.9997 | 1.098 |
| Previous hybrid | 10 | 16.34 / 23.02 / 26.50 | 4.84 / 5.92 | 6 / 4,580 | 0 | 0.9999 | 1.102 |
| Hybrid V3 | 10 | 16.41 / 22.96 / 30.12 | 4.85 / 5.92 | 0 / 4,580 | 0 | 0.9999 | 1.103 |

Table values are medians of run-level metrics except for the outcome counts.
Jain describes receiver goodput of concurrent background flows. CPU excludes
the workload and experiment runner. One static and one previous-hybrid run
had request dispatch P99 lag over 10 ms. All were retained. One previous-hybrid
run had a request P99 of 96.65 ms.

The outcome was **REGRESSION** against static HTB. V3 lost 43.54% in total
goodput at the paired median, with a simultaneous interval for the loss of
[43.26%, 43.78%]. The previous hybrid lost 43.57% [43.28%, 43.96%].
V3's paired P99 change was +9.69% [−38.86%, +63.93%], inconclusive.
Failure-rate and deadline-attainment regression gates passed. The intervals
are order-statistic intervals at the coverage required for the planned
Bonferroni family. Each independent block is one observation.

V3 verified zero actions in these ten runs. The first two eligible sampled
windows arrived too close to the scheduled capacity transition to reserve a
full evaluation lease. The startup cap therefore remained in place. This
matrix measures the cost of withholding intervention under these conditions.
It provides no estimate of the benefit of a completed release or HTB probe.

## Single-release functional run

The 16-second smoke workload had one bulk socket initially and six later.
V3 verified a single release from 400,000 to 1,500,000 bytes/s during the
one-socket phase. In the coordinator's last pre-action and two non-overlapping
post-action windows, the receiver measured 399,477 → 1,270,065 and 1,286,722
bytes/s. Sampled wire utilization went from 38.2% to 98.3% and 98.5%.
The critical histogram tail estimates were 30.05, 27.56 and 25.27 ms.
All 524 requests completed and met their deadlines over the run. This is an
observed before/after comparison on one cohort. It does not establish a
general advantage over static QoS.

## Expanded baseline check

The two-block matrix lengthened the first phase to 12 seconds and moved the
capacity transition to second 13. All five arms used the same workload DSCP
markings. CAKE was configured with a 100 ms RTT setting, flow isolation and
`diffserv4`. The kernel counters confirmed packets in all four tins. Bulk
and Voice transmitted 15,137 and 2,232 packets in the first trial and 15,116
and 2,288 in the second. DSCP follows the upstream CAKE mapping for the
configured tins. [iproute2 CAKE documentation](https://raw.githubusercontent.com/iproute2/iproute2/main/man/man8/tc-cake.8)

Pacing-only used the existing 180,000 bytes/s cap per background socket with
an fq leaf below the common-rate, single-class HTB shaper. That arm models
socket control without differentiated class QoS.

| Arm | P50 / P95 / P99 ms | Background / total Mbit/s | Deadline misses / offered | Failures | Jain | Daemon + collector CPU s |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Static HTB + fq_codel | 14.71 / 20.08 / 22.99 | 9.58 / 10.65 | 0 / 1,108 | 0 | 0.9968 | 1.237 |
| CAKE DiffServ | 18.10 / 22.43 / 27.02 | 9.60 / 10.67 | 0 / 1,108 | 0 | 1.0000 | 1.378 |
| Pacing-only | 18.56 / 80.11 / 227.08 | 3.12 / 4.19 | 61 / 1,108 | 0 | 1.0000 | 1.253 |
| Previous hybrid | 16.53 / 22.87 / 25.71 | 4.57 / 5.63 | 0 / 1,108 | 0 | 0.9999 | 1.238 |
| Hybrid V3 | 14.96 / 21.60 / 25.99 | 7.89 / 8.96 | 0 / 1,108 | 0 | 0.9995 | 1.194 |

All ten trials were valid. Two repetitions per arm leave every comparison
**INCONCLUSIVE**. Both V3 trials released the cap, moving from roughly
0.40 MB/s to 1.27–1.30 MB/s in receiver samples. One passed the outcome
guards. The other showed a critical histogram P99 of 35.73 ms in its second
post-release window, above the relative guard of 33.05 ms. It froze further
actuation and stayed on static HTB without reintroducing the startup cap.

## Live HTB probe and final smoke

The pressure pair used 24 KiB requests at 28 requests/s, five bulk flows,
a 22 ms critical target, 160 ms request deadlines and no binding startup cap.
An initial pressure run exposed a collector bug. The TC class byte counter
was nested in `stats` and V3 treated it as zero. V3 now accepts the observed
layout and withholds actuation when a required counter is absent.

After the fix, the live planner moved the critical/background rates from
3,600/3,600 to 4,200/3,000 kbit/s. Both ceilings stayed at 12,000 kbit/s.
The typed actuator confirmed the kernel values in 32.6 ms including its
pre-read, mutation and verification. In the two post-action windows
critical P99 was 46.34 and 50.54 ms from 50.54 ms before the probe.
Background goodput was 739,196 before and 736,968 and 738,114 bytes/s after.
The probe passed the safety guards and failed the sustained improvement
test. V3 verified restoration of the original rates and froze further
actions. The full-run P99 was 49.58 ms for V3 and 51.11 ms for static HTB,
with 5.86 Mbit/s background goodput on both and zero misses or failures
out of 504 requests per arm. One pair is **INCONCLUSIVE**.

The final smoke verified a cap release and measured 400,126 → 1,286,536
and 1,270,396 bytes/s. The second post-release P99 of 32.77 ms exceeded
the relative guard of 27.79 ms. V3 froze future actions while keeping the
static fallback. All 524 requests met their deadlines. The measured median
planner call cost, including the Python/native boundary and explanation
formatting, was 244 µs in the pressure run and 263 µs in the smoke run.
These samples are from the Debug VM build.

The ten-block regression remains applicable to the release behavior. In
that matrix the tracked socket caps remained below link capacity, so the
HTB probe was ineligible independently of the class counter layout.

## Verification

All 49 CTest entries passed on the Debug build. The native planner bridge
and CLI also built from a clean Release directory and passed the V3 unit
suite. The staged install ran the same dry-run command. The privileged
fixture rejected a non-owning peer, an old generation and stale telemetry.
It checked the shifted rates and the restore-before-rebase path with direct
kernel snapshots. The existing safety integration suite also passed the
concurrent revision, SIGKILL, restart, interface deletion and foreign
ownership cases.

## Reproduction and evidence

```sh
cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Debug -DFLOWCORE_BPF=OFF
cmake --build build
ctest --test-dir build --output-on-failure
sudo python3 -B tests/hybrid_lab.py
sudo ./build/flowcore bench replay scenarios/hybrid-v3-smoke.json
sudo ./build/flowcore bench replay scenarios/hybrid-v3-paired.json
sudo ./build/flowcore bench replay scenarios/hybrid-v3-baselines.json
sudo ./build/flowcore bench replay scenarios/hybrid-v3-pressure.json
./build/flowcore nextgen report results/EXPERIMENT/summary.json
```

| Experiment | Local artifact directory |
| --- | --- |
| Single-release smoke | `results/research-1794209037014473386/` |
| Short paired matrix | `results/research-1794209185669786898/` |
| Expanded baseline check | `results/research-1794209682857745007/` |
| Pressure run exposing counter layout | `results/research-1794209881773730946/` |
| Pressure pair after counter fix | `results/research-1794209981406003215/` |
| Final socket smoke | `results/research-1794210022474723060/` |

The repository tracks the manifests and reproduction instructions. Raw runs
remain on the test machine under the paths above. The first two experiments
used the implementation before moving the live feed and lock to a private
runtime directory and adding planner blocker diagnostics. Their scheduling
thresholds and startup fixture are the same as in the checked-in manifests.
The expanded matrix exercised the private runtime. The subsequent validation
uses closed one-second histogram buckets and their exact interval bounds
for post-action comparisons.
