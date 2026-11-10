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
