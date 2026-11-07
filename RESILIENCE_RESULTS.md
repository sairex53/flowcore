# Network Research Lab 2.0 — validation results

> Historical scoped evidence. The independent maintainer review is in [BENCHMARK_REPORT.md](BENCHMARK_REPORT.md) and [FEATURE_MATRIX.md](FEATURE_MATRIX.md); older results below are not new independent validation or general performance guarantees.

## Scope

The runs below validate the version 3 scenario parser, timed-event actuator, IPv6 endpoint setup/recovery, paced UDP generator/accounting, artifact generation, replay schedule, minimization, namespace cleanup, and host-network boundary. They do not establish that a controller strategy is better, nor that a particular packet impairment is deterministic.

## Real isolated scenario run

One `fq_codel` run used `scenarios/chaos-overlap-v3.json` on Fedora 45 ARM64 in a 4-vCPU QEMU VM. Kernel: `7.2.11-300.fc45.aarch64`; FLOWCORE reported a dirty source tree based on commit `f6ffa86` because this implementation was under test. The workload seed was `552830878`.

- 1,275 TCP requests offered; 1,228 completed and 47 failed (3.69% failure rate; 96.31% completion).
- Successful-request P99 was 2,615.99 ms; deadline attainment was 56.39% for the configured 25 ms deadline. This P99 excludes failures and is reported with them.
- Receiver-measured TCP background goodput was 4.99 Mbit/s; UDP receiver goodput was 0.134 Mbit/s. TCP background-flow Jain index was 0.992.
- All eight non-initial event batches were applied and kernel-confirmed. Schedule lag ranged from 13.6 to 68.5 ms; operation and readback time ranged from 9.6 to 20.0 ms. The interface flap readback confirmed restoration of the sender IPv6 address. The runner's post-fault ledger contained successful IPv6 requests.
- The run was valid as a fault-injection experiment but its comparison outcome was `INCONCLUSIVE`: one repetition cannot support a strategy comparison.

Raw JSON, request ledger, event transitions, receiver samples, qdisc readbacks, and HTML/CSV report: `results/chaos-1793974840940382316/`. Results are preserved locally and intentionally excluded from source control.

## Privileged integration

The privileged test ran a legacy chaos/replay and cancellation path, followed by two v3 runs (original and replay). Before the stronger host-state assertions were added, both v3 executions were valid and generated receiver-measured UDP traffic:

| Replay | Offered | Completed | Failed | Success P99 | Failure rate | TCP background | UDP background |
|---|---:|---:|---:|---:|---:|---:|---:|
| Original | 1,275 | 1,229 | 46 | 2,578.93 ms | 3.61% | 4.87 Mbit/s | 0.134 Mbit/s |
| Replay | 1,275 | 1,227 | 48 | 2,704.79 ms | 3.76% | 4.93 Mbit/s | 0.134 Mbit/s |

These are two repetitions of one configuration, not independent randomized strategy comparisons. The event offsets and seed are replayed; packet loss outcomes, application completion counts, and measured latency can vary. The final integration run verifies request classes appear after recovery, receiver-side UDP accounting is positive, event offsets match, host interface/address/qdisc state is identical before and after, and namespace holders are gone. It passed at `results/research-integration-1793976080240906277/`; the same result is recorded in `build/research-integration-legacy-compat.log`. The legacy rerun also confirmed old-format qdiscs did not acquire a reverse-path rate limit.

## Checks executed

- CMake build and CTest: 18/18 passed.
- Research contract tests: 11 passed.
- Deterministic generated properties: 1,000 scenario/minimizer cases plus 100 service-policy boundary pairs, 100 namespace cleanup/ownership cases, and controller-state edges passed.
- Ruff critical-error checks and repository style check passed.
- A live isolated v3 run generated both TCP and UDP traffic and emitted JSON, CSV, and HTML artifacts.
- The strengthened privileged research integration passed chaos, measured replay, report generation, minimization, scoped cancellation, UDP accounting, request-class recovery checks, host network state comparison, and namespace cleanup.

## Limits and next work

This scenario currently tests IPv6 TCP request/response, TCP bulk streams, and paced one-way UDP background datagrams. UDP is not classified by service, and the requested interface flap is an owned-veth link down/up rather than physical host NIC flapping. Netem delay/jitter/loss/duplicate/reorder and impairment are applied on the receiver egress. Linux general-purpose scheduling prevents a guarantee of exact actuator times or deterministic packet sequences. The optional soak harness is bounded; no multi-hour soak or thousands-of-policy-changes result is claimed. Further acceptance should add longer multi-cycle soak evidence and explicitly exercise controller active/shadow modes under the same v3 event schedule.
