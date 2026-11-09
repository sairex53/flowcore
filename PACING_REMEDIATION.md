# Pacing remediation

`pacing-guarded` is an experimental v2 Research Lab arm. The ordinary daemon and application SDK require explicit socket policy. Legacy modes are retained to reproduce earlier findings.

1. **Scope.** Admit only the owned HTB and flower baseline, four fq_codel leaves, a fresh kernel sample, a live cohort, at least twelve successful critical reports and no critical failures. Initial cap is 1.5 MB/s per owned bulk socket, nonbinding under the experiment's 12 Mbit/s maximum. Reject stale, class-active or changed scopes.
2. **Evidence.** Require two receiver windows of 750–1,500 ms, root backlog corresponding to at least 8 ms at the configured rate, observable critical-leaf backlog, application P99 above target and bulk demand corroborated by unsent bytes. Delivery must meet the protected HTB reservation. Use the larger of the two observed rates as reference.
3. **Probe.** Set a bounded per-socket cap with aggregate budget at least 97% of observed receiver goodput and at least the protected class reservation. Decline the probe when its aggregate target reaches the verified background ceiling. A socket cap remains a limit on the sender. It cannot guarantee receiver progress.
4. **Outcome.** Assess two distinct post-write receiver windows and full application intervals. Flag sustained goodput below the larger of the HTB floor and 90% of reference, or P99 over 110% of its reference. Restore the nonbinding cap after evaluation or expiry. The four-second deadline is enforced on the next usable planner tick. Temporary favorable samples do not authorize a persistent cap.
5. **Compensation.** Scope loss, capacity generation changes or membership changes invalidate accumulated evidence. Group writes remain fd/cookie-pinned and read back. Partial failures compensate accepted writes. A failed restoration faults the experiment. The cooldown and action budget bound churn.

## Concurrency and timing

The workload acquires the same flock for adaptive ticks and membership callbacks. A class transition invalidates the feed while holding the lock. Waiting membership yields to asyncio, bounds the wait to one second and prevents another planner tick. The lock coordinates the two cooperating processes. Linux socket option reads and writes cannot stop an unrelated privileged actor from changing a socket.

The native set-rate path avoids a redundant pre-read and retains a read on no-op requests. An additive bridge records fd pinning, ownership precheck, setsockopt, readback and per-socket errors. The original bridge entry points remain. Monotonic timestamps join:

| Artifact | Timing boundary |
|---|---|
| `socket-pacing.jsonl` | TCP collection, feedback file read, kernel sample age, native planning, decision reason, receiver interval and verification |
| `socket-actuation.jsonl` | Group apply and compensation, lock acquisition and individual kernel operation durations |
| `socket-lock.jsonl` | Busy adaptive ticks |
| `transitions.jsonl` | Scheduled fault, HTB write and readback, generation, class lock acquire/release |
| `workload-phases.jsonl` | Scheduled and actual phase change with configured rate and bulk cohort |
| `receiver.jsonl` and `requests.csv` | Timestamped receiver byte counters and individual request completions |

The receiver counter samples are interval evidence. They do not timestamp every byte or identify individual TCP stalls. P99 and goodput thresholds guide operational rollback. Statistical conclusions use independently randomized run blocks.
