# Multi-Objective SLO Optimization Engine

The engine has two layers. The existing native allocation solver proposes
static, weighted max-min, proportional and bounded adaptive HTB reservations.
The new native Pareto solver compares objective vectors from eligible policies.
Python connects the solvers to the Research Lab and its paired request ledger.
Live shadow uses the existing ownership checks, admitted application intents,
queue pressure and deadline estimator. The report and shadow commands are
read-only.

## Resource feasibility

- HTB guarantees and ceilings are integer decimal kbit/s. The parent rate
  must agree with the parent ceiling. The sum of child guarantees must fit
  inside the parent. Each rate must lie between its protected floor and
  authorized ceiling. Class floors are recomputed against each observed
  capacity. The existing optimizer additionally verifies that admitted
  service minima fit inside authorized class reservations.
- The experiment report checks the owned HTB topology and sampled class
  allocations in each trial. The shadow command checks live ownership using
  the existing daemon and verifies the actual allocation before proposing
  alternatives. A conflict, a foreign scheduler or missing kernel evidence
  blocks eligibility.
- Observed CPU, RSS and policy-update budgets are separate from scheduler
  constraints. Other measured limits can cover deadline misses, background
  progress or fairness. An observed budget violation in any trial makes the
  arm ineligible. Measurement on the VM cannot enforce those budgets on
  another deployment. The live controllers retain their existing bounded
  action budgets, cooldowns and verification rules.
- The deadline engine can identify an explicitly supplied processing budget
  that already exceeds the request budget. Queue estimates need fresh
  scoped telemetry. Failure to meet a measured latency target identifies
  an observed violation. General impossibility needs additional evidence.

HTB's configured rate is a scheduling reservation and the ceiling limits
borrowing. Receiver goodput and request completion depend on demand, TCP,
queueing and the application. Objective weights express operator preferences.
[Upstream HTB semantics](https://raw.githubusercontent.com/iproute2/iproute2/main/man/man8/tc-htb.8)

## Measurement model

| Metric | Unit and source | Preference |
| --- | --- | --- |
| Critical P99 | ms, critical class request ledger | Lower |
| Deadline miss ratio | Fraction of all offered requests that fail or miss their deadline | Lower |
| Success ratio | Fraction of offered requests completed successfully | Higher |
| Background goodput | Decimal Mbit/s of payload received | Higher |
| Service goodput | Minimum received payload Mbit/s across declared critical services | Higher |
| Fairness | Jain index of concurrent bulk-flow receiver goodput | Higher |
| Utilization | Duration-weighted sender wire rate divided by each verified phase capacity, capped at one for scoring | Higher |
| CPU | Percent of one CPU for the daemon and its collectors over the trial | Lower |
| Churn | Verified policy groups per minute, including compensated policy changes, excluding scheduled fault changes | Lower |
| Recovery | Seconds to two complete healthy critical request bins after the final confirmed restoration to original capacity | Lower |

Recovery uses one-second scheduled-request bins with at least five critical
requests and 95% deadline attainment in each bin. Runs without a capacity
restoration have no recovery observation. Failure to observe recovery before
the end of the experiment is right-censoring. Neither case is treated as zero
seconds. The application tail is withheld from selection when critical
requests failed or the sample floor is unmet. Per-service goodput and
failure counts remain in the raw summary.

## Integration

```sh
./build/flowcore slo shadow --profile config/slo-optimization.json \
  --budget-ms 20 --payload-bytes 24576 --processing-us 2000
./build/flowcore slo report results/EXPERIMENT/summary.json \
  --profile config/slo-optimization.json
```

Shadow queries the running daemon. When running the CLI as root, use a
root-owned profile or the profile from the installed data directory. It returns verified current HTB parameters,
the existing solver's alternatives and conflicts, and optional deadline advice.
`--hybrid-frame` includes a separate dry-run appraisal of the supplied
socket caps and queue frame. Shadow labels the allocation scores as surrogates.
The measured report recommends only among experimental arms of the same
paired matrix. The previous validated policy remains the fallback.

Hybrid V3 accepts an optional `objective_guard` in its research profile.
The guard uses the two post-action windows the coordinator already verifies
on the same socket cohort. It scores measured critical latency, deadline
attainment, background progress and any other selected live dimensions.
Both windows must clear the weighted gain. The guard can veto continuation,
and inherits the existing HTB rollback, pacing fallback and action limits.
The same-trial windows provide feedback and have limited statistical power.

The evaluator records verified HTB and socket update costs when timestamps
are present. Churn, CPU, goodput and application outcomes include their
observed run-level effects. Sub-window queue disturbances and unobserved
kernel failures remain unknown. More on selection and measurement uncertainty
is in [Pareto analysis](PARETO_ANALYSIS.md).
