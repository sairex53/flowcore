# Adaptive Action Validation Engine

The owned HTB controller remains a single writer. The daemon aggregates
application completions and kernel queue counters into complete monotonic
windows. The directional bottleneck gate requires fresh local egress
evidence. Each strategy evaluates application objectives and class queue
pressure, then the allocator or native optimizer proposes a bounded
transfer. Existing HTB preflight checks ownership, rate and ceil, hard
reservation floors and the expected kernel generation. The transaction
records its exact diff, write and readback durations. Scoped compensation
handles partial updates. The read-only monitor remains the deployment
default.

An optimizer transfer requires a backlogged service consuming at least
90% of its class reservation, an application objective outside its
deadband, complete local queue and background observations, and a
minimum cohort of successful requests. The presence of class pressure
alone previously admitted an optimizer candidate even when the service
met its objective. Explicit authorized ceiling enforcement remains
independent of adaptive SLO eligibility.

A candidate first crosses the existing multi-window hysteresis,
cooldown, direction, and adjustment budget. The post-action window
starts on a whole monotonic second after wash-in. A request spanning
the kernel change makes its cohort incomparable. Reports per second
must be within 20% between the pre and post windows. Retention requires
a relevant class throughput increase of at least 5% or a class backlog
decrease of at least 10%, plus improved application latency or desired
throughput. Capacity changes and lost telemetry interrupt the
comparison. Scope changes fail closed for actuation.

The verdict can be useful, harmful or inconclusive. Unhelpful probes
return to the previous verified allocation through the existing
transaction. Regression checks cover application errors and deadline
misses, conditional P99, background class throughput under comparable
demand, the protected background floor and Jain fairness among the same
set of materially active classes, each carrying at least 1% of the
observed class byte rate. Incidental control traffic does not
change that cohort. Deadline regression uses attempts with a
reported deadline. Class throughput is a kernel byte-counter rate.
The lab separately measures payload goodput and per-flow fairness.

## Configuration and actuator scope

The existing adaptive window, enter-window count, cooldown, recovery
windows, floor, step and action limits remain in effect. Optional
evaluation_seconds inherits window_seconds when null.
minimum_probe_successes defaults to 100 and can be increased.
max_p99_loss_pct, max_background_loss_pct and
max_class_fairness_loss_pct default to 10, 15 and 5, respectively.
The last limit is a percentage-point difference in the class Jain
index. Policy configuration bounds are hard. Post-action measurements
are conditional checks under the observed load.

require_full_transport defaults to true. The optimizer-local lab arm
opts out only for an owned HTB scope with an admitting directional
gate. In that arm socket sampling truncation stays visible. A class
reservation transfer uses observed qdisc counters and application
reports, independent of incomplete socket statistics. Missing or stale
collector observations still block actuation. The default stays strict.

HTB probes use the existing authorization and ownership checks.
An integrated pacing controller holding actuator authority blocks a
simultaneous HTB controller. The action engine leaves traffic filters,
socket pacing, application backpressure and MPTCP endpoints to their
respective owners. The effect model describes the local target,
reservation and ceiling deltas, expected direction, and number of
class writes. Numeric latency, goodput, fairness and recovery predictions
remain unknown until there is calibrated evidence.

[Decision trace](DECISION_TRACE.md) · [Validation](ACTION_VALIDATION_RESULTS.md)
