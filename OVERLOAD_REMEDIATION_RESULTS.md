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
sudo build/flowcore bench run scenarios/overload-gradual-smoke.json
sudo python3 -B tests/overload_integration.py
build/flowcore bench verify results/EXPERIMENT/summary.json
build/flowcore bench overload-guard results/EXPERIMENT/summary.json \
  --baseline-mode fq_codel --candidate-mode optimizer-overload
~~~

[Engine and safety boundary](OVERLOAD_ENGINE.md) ·
[Request accounting](REQUEST_FAILURE_DIAGNOSTICS.md)
