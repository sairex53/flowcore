# eBPF flow-engine measurements

Outcome: **INCONCLUSIVE** for a latency or throughput advantage over existing
flower classification. No general eBPF speedup or zero-overhead claim is supported.

## Actual experiment

Fedora 45 ARM64 VM; kernel 7.2.11-300.fc45.aarch64; libbpf 1.7.0.
The native loader/BPF program were built and loaded on this machine, not simulated.
Two fresh network namespaces and one veth pair per independent run. Sender fc0
egress: HTB 20,000 kbit/s, four existing fq_codel leaves. Receiver fc1: reverse-path
netem delay 5 ms, jitter 1 ms, configured loss 0%. No host network policy is changed.

Thirty runs: 10 paired blocks, one of each mode in seeded randomized order (42631):
- none: identical HTB tree, no FLOWCORE flow classification; default standard class.
- flower: existing userspace-configured kernel flower rules for all compared TCP/UDP
  ports and both families. This is not a fictional userspace per-packet classifier.
- bpf: same HTB tree, no flower filters, full TCX engine with identical service/class
  mappings, counters and flow tracking. This measures the complete engine, not a
  classification-only microbenchmark.

Two-second workloads run six persistent TCP RPC streams, two connected UDP echo
streams and two bulk TCP streams across IPv4/IPv6. RPC payload 256 bytes, TCP reply
64 bytes; UDP replies echo payload; bulk request 65,536 bytes and reply 64 bytes.
No simulated application processing delay. Non-bulk streams sleep 10 ms after each
request; this is explicitly closed-loop, not identical open-loop offered load.
Socket timeout one second. No discarded warmup or outliers. Actual run durations,
all request latency arrays and failures are retained. P99 is the nearest-rank
successful latency; errors and deadline attainment (25 ms) are reported separately.

## Recorded results

Medians of run-level metrics; every mode has ten independent runs:

| Mode | Successful P99 ms | Total payload Mbit/s | Background payload Mbit/s | VM busy CPU seconds/run | Offered RPC/UDP requests | Failed |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| HTB default | 10.667 | 18.211 | 17.282 | 0.500 | 9345 | 0 |
| flower | 9.069 | 18.109 | 17.086 | 0.545 | 10039 | 0 |
| TCX engine | 7.699 | 18.005 | 16.979 | 0.545 | 10178 | 0 |

The ratio of these medians is not the paired effect estimate. Paired run-level
TCX-versus-flower changes and distribution-free median intervals:

| Metric | Median paired change | Median interval |
| --- | ---: | --- |
| Successful P99 | -9.53% | [-51.59%, +24.33%] |
| Total payload throughput | -0.61% | [-2.95%, +6.22%] |
| VM busy CPU | +0.86% | [-5.56%, +5.66%] |

Intervals use order statistics, nominal at least 95%; with n=10 the selected
interval's binomial coverage is 97.8515625%. Units of independence are runs, not
individual requests. Differences overlap zero: this is not proof of acceleration.
TCX-versus-default CPU change was +8.00%, interval [+3.64%, +13.46%], in this short
VM experiment. Classification/observability can add measurable work compared with
doing neither; no cost-free claim is appropriate.

All 100 retained BPF flow records (ten per BPF run) matched expected service/class
metadata, and integration checked actual bytes in all four HTB classes.
This is retained-flow accuracy for these known workloads, not proof that all
possible packets, NAT flows, unsupported headers or high-churn flows are classified.
Each BPF run reported zero map-full packets and zero output_lost. The separate
16-entry exhaustion test intentionally does lose flow retention while preserving
classification; it must not be merged into a false universal zero-loss claim.

Engine VmHWM ranged 5,124,096–6,656,000 bytes. Kernel fdinfo for the 4096-entry
configuration reported map memlock accounting of 792,632 bytes: policies 3904,
flows 788064, per-CPU totals 664. This is separate from process RSS and excludes
program/JIT/BTF and other kernel allocations. It is not a complete system-memory
charge measurement. Flow key/value sizes were 56/72 bytes; policy value 536 bytes.

Engine userspace CPU deltas rounded to zero ticks in these short windows.
That means below this measurement's scheduling-clock resolution, not zero CPU.
BPF execution is charged elsewhere in the network path. VM busy CPU includes
application processes, softirq, background activity and orchestration; it is not
a dedicated per-program cycle measurement. Host metadata, pressure and load
samples are preserved so noise can be investigated.

## Evidence and reproduction

```sh
cmake -S . -B build -G Ninja -DFLOWCORE_BPF=ON
cmake --build build
ctest --test-dir build --output-on-failure
sudo python3 -B tests/flow_engine_integration.py
sudo python3 -B scripts/flow_engine_benchmark.py --repetitions 10 --duration 2 --seed 42631
```

Primary raw artifact directory:
results/flow-benchmark-1793948437029578802/
- manifest.json: kernel/VM metadata, boot identity, Git base and source SHA256s.
  Measurements were made in the explicitly dirty implementation tree before commit.
- runs.json and runs.csv: all 30 per-run summaries.
- summary.json: means, medians, SD, run-level intervals and paired changes.
- rN-MODE/result.json: raw application samples/failures, host before/after samples,
  complete retained flow dumps and actual tc counters.
- per-run commands, offload settings, namespace identities, receiver/client logs.

Map-memory evidence: results/flow-engine-1793948577898849086/map-memory.json.
Additional exploratory datasets are preserved under results/flow-benchmark-*;
none were deleted to select a favorable result. Raw local artifacts are ignored by
Git under the repository workflow; methodology and these measured summaries are
committed. Root test logs: results/flow-engine-integration.log and
results/flow-engine-service-regression.log.

## Limits and next measurements

This is a low-rate, short veth/VM experiment. Closed-loop offered counts differ;
pooled P99 combines three TCP classes and UDP, so changing completion composition
can change the percentile. Inspect per-stream raw samples before service-specific
claims. No claim covers physical NICs, very high PPS, high-cardinality production
load, NUMA, prolonged memory pressure, loss recovery, NAT or arbitrary protocols.

No new per-packet event stream exists; ring-buffer event-loss percentage is not
applicable. Flow retention failures, exporter loss, cache overwrites and unsupported
packets are distinct coverage signals. Existing TCP tracepoint event loss has its
own accounting and was not silently substituted with zero here.

Next: fixed open-loop per-service offered schedules, longer steady-state windows,
high-PPS small UDP, concurrent same-socket contention, independent VM/host-noise
control, verifier-negative fixtures and long-duration reload/GC soak. Add program
cycle attribution only with a measured need and explicit profiling overhead.
Until then the defensible conclusion is working scoped intelligence with bounded
resources and inconclusive comparative latency/throughput performance.
