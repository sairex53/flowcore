# Adaptive Intelligence Engine 2.0 results

Fedora 45 AArch64. Initial functional matrix used an 18-second workload,
two phases at 90 and 170 requests/s, four bulk streams, a 10 Mbit/s cap,
12 ms controller objective and 25 ms deadline. Each arm offered 2,420
requests. All five arms completed without request failures.

| Strategy | Successful-request P99 |
|---|---:|
| Static 30% critical reservation | 59.69 ms |
| Threshold | 65.96 ms |
| Queue-aware | 54.47 ms |
| SLO-aware | 54.70 ms |
| Hybrid | 56.96 ms |

This exploratory run was performed while integration refinements were being
completed. It is not the final revision's performance evidence. Its raw
source hashes and artifacts are preserved in
results/research-1793875793530332991/. The result is INCONCLUSIVE.

The static30 reference follows the historical pilot from BENCHMARK_V2.md.
That pilot used a shorter, different load profile. It cannot be called the
best static policy for every workload here. ADAPTIVE_ROOT_CAUSE.md also
records workloads where static35 was faster than the adaptive controller.
No superiority over the best freshly tuned static QoS is established.

The existing privileged integration passed SDK reports, readback, dry-run,
shadow, mixed loads, expiration, policy conflict, capacity drift and cleanup.
The new tests cover native feasibility, reservation sums, donor floor,
deterministic selection, explicit shared-class admission and every legal
and illegal state-machine edge. They complement the existing telemetry-gap,
oscillation, failure and rollback tests.

Native controls preserve kernel class ceilings and operator priorities.
Autonomous priority changes and speculative ceiling optimization are not
validated features. Host interfaces remain outside the adaptive lab backend.
The system remains experimental until these boundaries and repeated
performance measurements support a deployment claim.

## Final revision functional repeat

The stable solver/daemon revision completed another five-arm matrix.

| Strategy | Successful P99 | 25 ms deadline attainment | Failed requests | Changes |
|---|---:|---:|---:|---:|
| Static30 | 79.06 ms | 78.18% | 0 | 0 |
| Threshold | 52.07 ms | 79.92% | 0 | 0 |
| Queue-aware | 53.89 ms | 81.94% | 0 | 1 retained |
| SLO-aware | 55.11 ms | 82.44% | 0 | 1 retained |
| Hybrid | 55.41 ms | 80.50% | 0 | 0 |

Each mode completed 2,420 requests. No controller error was reported.
The new native allocation path was exercised by both retained changes.
The outcome remains INCONCLUSIVE because this was one block. Local tests
also overlapped parts of functional validation and could influence VM
scheduling. These numbers demonstrate executable behavior, not a causal
or statistically supported advantage.

Final matrix evidence is in results/research-1793875983123077755/.
The first matrix's source was still changing and is preserved separately.
Kernel readbacks, allocation history, workload ledgers, host noise and
intervals remain in the normal benchmark report artifacts.

All ten CTest targets passed, including four new native/controller tests.
The installed-mode test loads the staged native library and verifies the
unprivileged monitor, SDK, watchdog, metrics and staged removal.
A short local binding microbenchmark measured about 4.7 microseconds
CPU per solver call over 10,000 calls on this guest. It excludes telemetry
and kernel actuation and is not a service throughput measurement.

Production host deployment and proof against freshly tuned static policies
remain open. The bounded implementation deliberately preserves operator
ceilings and priorities. It does not optimize those parameters automatically.

The capacity-change/loss/recovery matrix also completed three valid arms.
Successful P99 was 1,033.61 ms queue-aware, 1,051.91 ms static30 and
1,056.49 ms hybrid. Each offered 580 requests with zero request failures.
Hybrid lost to the static reference on that successful-tail metric.
The single block is INCONCLUSIVE. Evidence is in
results/research-1793876084824972462/.
