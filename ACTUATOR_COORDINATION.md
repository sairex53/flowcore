# Hybrid V3 actuator coordination

## Ownership and sequencing

The namespace benchmark requires root for its disposable veth and qdisc setup.
The research daemon authenticates the owning runner with Unix peer credentials
and checks its parent identity before accepting the typed Hybrid HTB request.
This path requires the explicit research mode. The established HTB backend
checks namespace and class ownership, plans the incremental rate changes, applies
reductions before increases, checks the result and compensates when possible.
Kernel transactions spanning several tc writes can fail partway.

The runner and the background socket process use the same flock in a private
runtime directory. The control plan, readback files and receiver counter input
live there during the trial. The receiver log and latest socket snapshot are
exported as experiment evidence. The background process can change only its
own SDK socket handles. The runner compares the socket snapshot under the lock
before committing an HTB probe. A stale revision, configured capacity or
observer stamp blocks a shift. The worker checks command age, revision, cohort
and exact socket caps before changing anything, then returns a verified receipt.
The class and socket paths share one action budget and at most one evaluation
is pending. This design keeps independent writes separate until evidence
justifies another action.

A scheduled capacity change publishes a transition, restores any outstanding
HTB probe, rebases configured HTB percentages and invalidates older decisions.
A cohort change invalidates socket commands and outcome comparisons for the old
cohort. An expired command has no authorization to write. Failed verification or
uncertain compensation aborts the trial and removes the owned lab. Runner
interruption follows the existing child cleanup path. The parent-death signal
and namespace lifetime handle abrupt runner loss. Public host-interface and
arbitrary socket activation are outside this mode.

Class ceilings, priorities, flower filters and qdisc types remain the operator
baseline. A combined HTB and socket transaction would require separate causal
evidence and a way to verify compensation across the two owners. The current
planner uses one actuator at a time.

## Inspect without changing kernel state

```sh
./build/flowcore hybrid inspect results/EXPERIMENT/r0-hybrid-v3/hybrid-v3.jsonl
jq -sc '[.[] | select(.event == "plan") | .proposal.current] | last' \
  results/EXPERIMENT/r0-hybrid-v3/hybrid-v3.jsonl > results/hybrid-frame.json
./build/flowcore hybrid plan results/hybrid-frame.json
```

The input to `plan` is the strict numeric frame emitted under
`proposal.current`. Rate fields are decimal bytes/s, `critical_backlog` is
bytes, and `p99_us` and `target_us` are microseconds. `sockets` is a count.
`fresh` and `demand` are booleans. The JSON response includes current and
proposed settings, the arithmetic throughput bound, inferred limiting actuator,
spare capacity estimate, intended effect and regression dimensions. A plan is
advisory. The live runner separately checks authorization, freshness, repeated
evidence, cooldown, action budget and time left for evaluation.

`hybrid_v3` in the benchmark manifest explicitly controls the background floor,
goodput regression limit, cooldown, maximum actions and startup cap. The nonzero
startup cap is a laboratory fixture applied to the cooperative cohort. Set it
to zero to begin at the nonbinding laboratory upper bound. When physical
capacity falls, the protected configured share is relative to the new
bottleneck. Goodput can fall even if the scheduler honors the reservation.
