# Adaptive Intelligence Engine 2.0

The engine integrates a native C++23 constraint allocator with the existing
daemon's verified HTB actuator and bounded outcome/rollback loop.

## Components and policy

| Component | Implementation |
|---|---|
| State estimator and pressure attribution | intelligence.py estimates target leaf pressure and compares throughput to the guarantee. It exposes drops, borrowing, deadline misses and sampled TCP RTT context. |
| Service SLO evaluator | adaptive.objective uses application success histograms, explicit deadline misses, failures and reported demand. |
| Allocator and constraints | intelligence.cpp implements a bounded four-class C ABI. It rejects invalid ceilings, overcommitted reservations and violated background floors. |
| Action planner | The Python strategy scores eligible services by normalized latency deficit with weights 4, 2, 1 for critical, interactive and standard. Existing boosts reduce marginal utility. |
| Actuator | adaptive_tc performs incremental HTB class changes, precondition checks, readback and rollback. Operator class priorities are preserved. |
| Outcome and rollback | Fixed complete post-change windows, workload-count comparability, failure/deadline checks and peer-service regression checks determine retention. |
| Decision history | Existing bounded journal records observations, constraints, prediction, changes, actuator timing, outcome and restoration. |

The C++ allocator does no kernel I/O and allocates no heap memory. The daemon
loads the shared library from its installed binary directory. Its supported
variables are class guarantees and admitted class ceilings. Operator priorities
are configured in the existing policy and verified on every incremental change.
Priority and ceiling changes are not speculative optimization variables in this
revision. A guarantee is a reservation and traffic can borrow up to its ceiling.

New strategies are queue-aware, slo-aware and hybrid. Each requires target
network-pressure evidence and sufficient successful samples. Queue-aware
responds to eligible pressure, slo-aware weights SLO deficit, and hybrid adds
deadline misses and bounded drop pressure. RTT is diagnostic context because
a short TCP snapshot alone cannot attribute application delay to a class.

The optimization operates on the three service classes and a protected
background donor. It selects one bounded transfer per evaluation cycle.
Multiple named services can share a class after explicitly starting a new
strategy. Shared-class ceilings are intersected and aggregate admitted minima
must fit the baseline reservation. Shared services have a common kernel
allocation and cannot receive independent bandwidth enforcement within that
class. Legacy strategies retain their exclusive-class admission rule.

Aggregate desired throughput above capacity is exposed as infeasible. SLOs
that remain violated without attributable network pressure are reported as
unattributed; the engine cannot prove arbitrary latency targets feasible.
The requested background floor is a protected reservation. Actual throughput
also depends on demand and TCP behavior.

## Modes and lifecycle

Use controller start with --strategy hybrid for active experiments, controller
shadow for hypothetical decisions, controller observe for observations without
actions, controller stop to disable, and controller fallback for an explicit
baseline restoration and emergency state. All mutation modes require the
existing owned lab. SDK registration does not enable host actuation.

The explicit state vocabulary is DISABLED, OBSERVING, HEALTHY, CONGESTED,
ADJUSTING, EVALUATING, RECOVERING, DEGRADED and SAFE_FALLBACK. intelligence.State
checks legal transitions. Startup requires explicit activation and observes
before acting. Eligible pressure precedes adjustment, a confirmed action
enters evaluation, and its verdict enters recovery. Faults restore the safe
baseline where ownership permits and expose fallback. DEGRADED exposes infeasible operator objectives. Current telemetry loss
takes the existing conservative fallback path.

The engine retains the root-cause fixes for complete windows, minimum 100
successful samples, class-specific backlog evidence, bounded actions,
hysteresis, deadbands and actuation-independent verdict deadlines.
Rejected probes increase cooldown with the reversal count. The maximum
action and reversal budgets remain enforced. Ambiguous evidence restores the
previous allocation and is recorded as inconclusive. Confidence labels describe
heuristic sample adequacy, not a statistical confidence interval.

## Use and reproduce

Build with CMake, run CTest, and start a disposable lab using the existing
privileged test runner. The normal monitor and clients remain unprivileged.
Run the matrix with:

```sh
sudo ./build/flowcore bench run scenarios/adaptive-v2.json
sudo python3 -B tests/adaptive_integration.py
```

Use the independently tuned static workflow in BENCHMARK_V2.md for serious
comparisons. The supplied one-block matrix is functional evidence. Increasing
repetitions to at least ten and tuning static on separate seeds is required
before an improvement claim.

The separate constrained helper currently supports a narrower set of network
operations. The adaptive lab backend continues to be an explicitly privileged
isolated runner. Production host actuation, automatic priority optimization,
physical-link validation and statistical superiority remain open.
