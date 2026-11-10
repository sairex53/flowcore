# Overload remediation experiment

The primary experiment was frozen at commit 3e84747 before the first
paired trial. It uses ten independent seed blocks with counterbalanced
ordering and 667 scheduled requests per arm. The same workload
contains four rate phases, up to six TCP bulk streams, UDP background
traffic, two critical services and a fresh TCP connection per admitted
request. The configured forward bottleneck starts at 12 Mbit/s,
steps to 7 and then 6 Mbit/s, and recovers to 12 Mbit/s. A separate
reverse limit changes during the collapse. The gradual companion
manifest tests four successive reductions and restoration as a smoke
fixture.

The matched intervention is optimizer versus optimizer-overload.
TCP port classification and HTB reservations are identical for those
arms. The intervention moves UDP bulk traffic to the background
class. Shaped fq_codel, CAKE best effort and tuned static HTB are
end-to-end comparators at the same bottleneck. Their packet schedulers
and queue limits have different semantics.

The predeclared infrastructure checks cover scheduling lag, CPU PSI,
steal, counters and ledger integrity. Failed workload trials retain
their full offered counts and remain distinct from infrastructure
failures. The primary gate treats more than 5% failed attempts as
FAILED_WORKLOAD and censors success-only P99 comparisons when either
arm has failed requests. Guardrails additionally cover deadline
attainment, payload goodput, fairness and CPU. A read-only overload
guard can flag a confirmed regression on a metric with complete
paired evidence, including offered-denominator metrics in failed
workload blocks.

~~~sh
sudo build/flowcore bench run scenarios/overload-collapse-paired.json
sudo build/flowcore bench run scenarios/overload-confirmation-paired.json
sudo build/flowcore bench run scenarios/overload-gradual-smoke.json
sudo python3 -B tests/overload_integration.py
build/flowcore bench verify results/EXPERIMENT/summary.json
build/flowcore bench overload-guard results/EXPERIMENT/summary.json \
  --baseline-mode fq_codel --candidate-mode optimizer-overload
~~~

[Engine and safety boundary](OVERLOAD_ENGINE.md) ·
[Request accounting](REQUEST_FAILURE_DIAGNOSTICS.md)

## Five-arm Fedora ARM64 matrix

The Fedora 45 ARM64 VM ran kernel 7.2.11-300.fc45.aarch64. The 50
trials are retained at results/research-1794300550608516030. Forty-nine
had valid infrastructure. Trial r4-optimizer crossed the prespecified
release P99 limit at 98.65 ms versus 25 ms and the loop dispatch limit
at 143.05 ms versus 110 ms. That record includes its 61 observed
failures. It invalidates the prespecified primary comparison.
The table is descriptive for the infrastructure-valid records.

| Arm | Runs | Failed / offered | Conditional P99 (ms) | Deadline attainment | TCP / UDP background (Mbit/s) | Total (Mbit/s) | Jain index | CPU (s) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Shaped fq_codel | 10 | 0 / 6670 | 977.86 | 51.50% | 4.81 / 0.54 | 9.18 | 0.9977 | 1.087 |
| CAKE best effort | 10 | 0 / 6670 | 665.51 | 49.48% | 4.71 / 0.48 | 9.01 | 0.9989 | 1.068 |
| Tuned static HTB | 10 | 591 / 6670 | 4196.80 | 47.30% | 4.90 / 0.68 | 9.05 | 0.9976 | 1.108 |
| Optimizer | 9 | 528 / 6003 | 4259.59 | 46.93% | 4.86 / 0.67 | 9.01 | 0.9964 | 1.157 |
| Corrected optimizer | 10 | 0 / 6670 | 2648.14 | 54.42% | 4.72 / 0.64 | 9.18 | 0.9964 | 1.185 |

Rates and percentiles are medians of per-run metrics. Jain is the
background TCP stream index. CPU covers daemon and child processes.
The optimizer arms issued zero adaptive actions. Planned capacity
updates are separate from adaptive updates. The lower success-only
P99 of CAKE and shaped fq_codel coexisted with lower deadline
attainment than the corrected optimizer in this matrix. That is a
distribution tradeoff. Background TCP and UDP goodput both remain
visible because the correction makes them share a class.

## Independent two-arm confirmation

The separate confirmation at results/research-1794301242685434911
contains ten fresh paired seeds, 20 infrastructure-valid trials and
no altered thresholds. Git recorded 7cef71b with working-tree edits
to diagnostics and reporting. The live classification and workload
paths matched the preregistered implementation. The campaign metadata
records the source fingerprints. Per-run manifests seal raw hashes. The matrix outcome is
FAILED_WORKLOAD because the original optimizer exceeded the failure
limit. The failure effect uses the complete offered denominator.

| Metric | Optimizer | Corrected optimizer |
| --- | ---: | ---: |
| Failures / offered | 587 / 6670 | 0 / 6670 |
| In-flight limit rejections | 537 | 0 |
| Response timeouts | 50 | 0 |
| P50 / P95 / conditional P99 (ms) | 62.90 / 3652.91 / 4270.58 | 51.57 / 1649.64 / 2565.09 |
| Deadline attainment | 47.00% | 55.25% |
| TCP / UDP background goodput (Mbit/s) | 4.90 / 0.68 | 4.73 / 0.64 |
| Total goodput (Mbit/s) | 9.05 | 9.19 |
| Background TCP Jain index | 0.9972 | 0.9953 |
| Daemon and child CPU (s) | 1.193 | 1.178 |
| Peak daemon RSS (MiB) | 37.09 | 36.91 |
| Median peak sender backlog (bytes) | 573312 | 300295 |
| Adaptive updates | 0 | 0 |

The corrected arm reduced the median paired failure rate by 8.70
percentage points with a family-adjusted bootstrap interval of 7.50
to 10.04 points. Median deadline attainment increased by 8.02 points
with an interval of 6.60 to 10.04 points. Total payload goodput
increased 1.42% [1.04%, 2.01%]. TCP background goodput fell 3.34%
[2.44%, 4.37%]. These effects apply to the tested workload and
configuration. Conditional P99 is descriptive because the original
arm lost requests. The prespecified P99 gate retains zero eligible
pairs against that arm. Socket pacing was absent in both arms.

Against shaped fq_codel and CAKE best effort the corrected arm had
ten complete failure-free pairs per comparator in the five-arm
matrix. Paired conditional P99 increased 168.4% [146.8%, 283.7%]
and 290.0% [243.2%, 360.1%], respectively. Both exceeded the 10%
limit. The guard returned KEEP_BASELINE for both comparisons.
The deadline-attainment increases were 3.07 and 5.02 percentage
points respectively. The default remains unchanged and the corrected
configuration remains an explicit research arm.

## Mechanism and fault validation

- The direct Fedora veth fixture delivered eight IPv4 and eight IPv6
  UDP packets. The original policy recorded 18 standard packets,
  including other traffic, and zero background packets. The corrected
  policy recorded zero standard packets and 16 background packets.
  The fixture then removed the owned IPv4 rule and verified rejection
  of the unexpected readback. The initial test harness lacked a lab
  reference. The corrected fixture passed.
- The gradual run, results/research-1794301522854762111, completed
  one pair with zero failures. Successful P99 was 3095.83 ms for
  the original optimizer and 821.65 ms for the corrected arm. This
  is a capacity-transition smoke check with insufficient repetitions
  for a comparative claim.
- The integrated smoke, results/research-1794301579635212087,
  recorded all 667 scheduled requests as released, admitted,
  submitted and completed. It recorded 302 deadline misses and
  verified HTB generations zero through three during planned
  capacity changes. The repeated trials predate this snapshot
  generation field and retain the separate capacity and action logs.
- The native build and CTest passed on Fedora ARM64 with 54 passes
  and one privileged UDP fixture skipped in CTest. That fixture
passed separately in a disposable network namespace. The runtime
  safety fixture passed, including stale proposals, wrong filters,
  partial writes, lost acknowledgement, verified compensation and
  daemon crashes. Staged installation and the installed SDK consumer
  passed. The advisory tests exercise stale observations, duplicate
  sampling and lost IPC. A live daemon query confirmed the idle
  owned-egress response and preserved the caller's concurrency. Application-concurrency benefits and
  automatic overload rollback remain unmeasured in this work.

The primary, confirmation and gradual evidence digests verified.
Raw evidence and guard JSON are retained under the experiment
directories and results/overload-validation. This study is specific
to a Fedora ARM64 VM and controlled local egress.
