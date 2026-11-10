# Adaptive decision trace

~~~sh
build/flowcore controller trace
build/flowcore policy history
build/flowcore bench actions results/EXPERIMENT/r0-optimizer/decisions.json
~~~

The trace uses the existing authenticated read-only Unix socket and
bounded decision history. Each traced row carries schema version,
event sequence, controller session, reason code, detail code,
monotonic timestamp, observer generation, current policy generation,
intent revision, mode and strategy. Proposal and verdict rows share
a proposal identifier. Kernel readback timestamps and transaction
diffs distinguish a planned action from an applied or restored one.
The retained journal holds up to 128 records with a 256 KiB budget.

| Reason | Meaning |
| --- | --- |
| NO_CONGESTION | Available root signals did not cross the local pressure threshold. |
| NO_CONTROLLABLE_BOTTLENECK | Directional attribution failed or the target class did not show queue pressure at its reservation. |
| NO_FEASIBLE_ACTION | No eligible reservation transfer survived the policy limits. |
| INSUFFICIENT_SAMPLES | Registered services lacked a sufficient successful application cohort. |
| LOW_CONFIDENCE | Telemetry was missing, stale or incomplete, observations failed quality checks, or hysteresis was incomplete. |
| COOLDOWN_ACTIVE | A candidate existed and a previous action still held the cooldown. |
| POLICY_CONFLICT | Authorized constraints were infeasible, a scoped generation was stale, or another owner held actuation authority. |
| CHANGE_NOT_WORTH_COST | The service met the policy deadband or the solver retained the current allocation. |
| ACTION_BLOCKED | Observe or shadow mode, an exhausted budget, a fault, or a rejected transaction prevented a verified change. |
| ACTION_PROPOSED | A bounded change is awaiting kernel verification. |
| ACTION_APPLIED | Kernel readback verified the change. A separate outcome may follow. |
| ACTION_REVERTED | Verified rollback or recovery restored a prior allocation. |

A proposal carries the changed classes, bounds and expected effect.
A post-action verdict includes the preceding and subsequent application
cohorts, traffic evidence, regression checks, evaluation timing and any
verified rollback. Transaction rejection and failed compensation are
explicit. The legacy actions counter remains the strategy's action
budget count. Action validation counters distinguish changed kernel
transactions, reverts and outcome classifications. They reset on
activation. Static experiment arms can have no decision records.

A zero-action trial is evaluated from its reason distribution and
its observed workload. The offline summary labels an applied probe
without a verdict as inconclusive, with the reason right_censored.
A shortage of samples or controller fault
carries a different interpretation from a well-observed idle path.
A verified kernel mutation by itself does not establish performance
benefit. The trace contains no estimate of per-request packet
scheduling, and temporal change alone does not establish causation.

[Engine and safety](ADAPTIVE_ACTION_ENGINE.md) · [Validation](ACTION_VALIDATION_RESULTS.md)
