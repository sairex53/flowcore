# Bandwidth-Preserving Hybrid Controller 3.0

Hybrid V3 is an opt-in Research Lab arm. It coordinates the existing HTB class
backend and the existing cooperative TCP pacing SDK. The standalone daemon and
earlier controller strategies keep their own entry points. V3 uses a
deterministic C++23 planner with a Python coordinator in the experiment runner.

## Accounting and scope

| Input | Units and provenance |
| --- | --- |
| Capacity | Configured and verified HTB parent rate and ceiling, up to 12,000 kbit/s. The planner uses decimal bytes/s. |
| Class configuration | Desired generation with HTB rate, ceiling and priority verified against the owned tree. The rate is a reservation. The ceiling bounds borrowing. |
| Pacing | Actual per-socket cap, sum, minimum and active cohort from cooperative bulk sockets. These are cap values rather than measured sending rates. |
| Wire and class traffic | Deltas of sampled sender qdisc and HTB class bytes, with the critical leaf backlog in bytes. |
| Demand and background progress | TCP unsent bytes as a demand indication and receiver payload bytes for observed goodput. |
| Application | Critical service request histograms and deadline reports. Control P99 is the largest eligible per-service estimate. Benchmark P99 is computed independently from the request ledger. |
| Other guardrails | Background Jain index within the same active cohort, sender utilization, failures and daemon plus collector CPU seconds. |

The upper bound on background traffic is the minimum of link capacity,
background class ceiling and summed socket caps. Actual traffic depends on
contention, scheduler behavior, TCP and the application. Estimated spare capacity
is the verified configured rate minus observed qdisc throughput, clamped at zero.
Neither number estimates upstream path capacity.

The scope is the sender egress of the owned veth pair. TCP destination ports
select four HTB classes with fq_codel leaves. Cooperative workload hints associate
request samples with the critical class. Several critical services can share that
class and each can contribute to its SLO signal. V3 makes class-level decisions
and leaves per-service allocation within that class to the existing scheduler.
The CAKE comparison uses opt-in workload DSCP hints instead of HTB port filters.

## Decisions

- **Hold** when measurements are missing, demand is unclear, a class ceiling
  binds, a socket cap already exceeds available capacity, or safety checks fail.
  A limiting-actuator estimate can still be useful when the action is hold.
- **Release the socket cap** when the aggregate cap is below both the class
  ceiling and configured link rate, unused bandwidth is at least 5%, background
  receiver goodput reaches at least 85% of the aggregate cap, unsent bytes
  corroborate demand and the application tail is within target. The worker
  sets its sockets to the laboratory upper bound. On the verified link this
  bound allows HTB and TCP to decide how much can be sent.
- **Probe HTB** on a saturated link with critical backlog, critical class
  throughput near or above its reservation and an application P99 at least 10%
  over target. All tracked socket caps must individually allow the whole link
  rate. The probe transfers five percentage points from background to critical
  while retaining class ceilings, priority and the operator background floor.
  The original allocation is restored at the end of the probe.

These choices use precedence and guardrails rather than a fitted weighted score.
A socket release requires evidence of unused capacity. A class probe requires
evidence of local congestion and SLO pressure. V3 keeps borrowing available
through the configured ceiling and tests actual throughput. An operator ceiling
remains an upper bound even if traffic could use more capacity. Increasing an
HTB guarantee may have little effect when critical traffic already borrows.

## Stability and outcome checks

Two qualifying observations are required. A configurable cooldown and a run
budget of one to three actions bound repeated changes. The controller reserves at
least six seconds before scheduled changes or measurement end. Longer application
windows require twice the configured width plus two seconds.
The outcome uses two complete non-overlapping application windows that start
after kernel verification, with receiver deltas for the same socket cohort.
It allows up to 10% higher critical P99, two percentage points more deadline
misses, no failures, the configured goodput regression within the background
floor, and at most 0.05 less Jain fairness or utilization. The measured daemon
and collector CPU delta may rise by at most 0.05 seconds per sample interval.
The targeted metric must improve by at least 5% in both windows before the
coordinator records an observed benefit. A missing
or adverse outcome freezes further actions. HTB probes always restore static
QoS. A release has already returned socket behavior to the static policy and
keeps that fallback. The same run never oscillates back to the old cap.

See [actuator coordination](ACTUATOR_COORDINATION.md) for transaction boundaries
and [results](HYBRID_V3_RESULTS.md) for the measured workloads and limits.
