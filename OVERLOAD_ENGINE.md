# Overload-safe network control

FLOWCORE reuses the namespace Research Lab, HTB ownership checks and
optimizer. The experimental optimizer-overload arm installs one fixed
IPv4 and one fixed IPv6 UDP destination-port rule for the lab's bulk
generator. Both select the existing background class. The original
optimizer arm is kept for controlled comparison. The read-only daemon
default and host networking are unaffected.

The candidate's TCP rules, HTB rates, ceilings, class priorities and
fq_codel leaves are unchanged relative to the optimizer arm. The
30% configured background reservation is shared by background TCP
and UDP in the candidate. The remaining classes retain their existing
minimums. This protects a class allocation in the managed qdisc.
Application throughput and per-flow fairness require separate checks.
The two UDP rules are installed before workload release, after
ownership and layout checks. Readback verifies both families. An
unverified change aborts the disposable research lab.

## Read-only diagnostics

~~~sh
build/flowcore bench overload results/EXPERIMENT/r0-optimizer-overload \
  --against results/EXPERIMENT/r0-optimizer
~~~

This command reads a V3 trial. It compares the recorded queue tree,
filter match keys, priorities, rates, ceilings, bursts and qdisc
options stage by stage. Instance-specific qdisc handles and counters
are excluded from configuration equality. HTB rates from tc JSON
are bytes per second. The separate evidence section exposes recorded
socket pacing, policy updates, capacity rebases, drops and sampled
retransmissions. Missing evidence is represented explicitly.

## State and action boundary

The state engine accepts monotonic observations of a single scope
with a verified local limit, observed utilization and queue occupancy.
A reduction of 15% or more relative to the preceding comparable
observation enters CAPACITY_COLLAPSE. Two distinct windows with
at least 90% utilization and nonempty backlog enter OVERLOADED
through APPROACHING_CAPACITY. Two healthy windows, each below
80% utilization with no backlog, clear collapse or overload through
RECOVERING. Missing, invalid, out-of-order or stale evidence produces
INSUFFICIENT_DATA. Re-reading one sample cannot advance the state.

The engine recommends NO_INTERVENTION or application-side backpressure.
The optional UDP rule is a separate, preregistered laboratory
intervention. Adaptive kernel updates use the pre-existing controller
and its verified compensation path. This engine has no new authority
to change pacing caps, classes or host networking. A paired outcome
that violates the experiment guardrails provides evidence to keep or
restore a validated baseline through the existing policy workflow.
No active strategy switch is made by the advisory.

## Application cooperation

Python applications can call the installed module on a worker that owns
the pressure state and a time-bounded local daemon client.

~~~python
from overload import Pressure, cooperative_poll

pressure = Pressure()

# query(op) returns the result of an authorized daemon IPC request.
# The application chooses when to poll and whether to adopt the advice.
recommendation = cooperative_poll(query, pressure, concurrency=12)
~~~

The adapter queries capacity_estimate on the existing authenticated
local socket. It accepts owned egress evidence in the same monotonic
clock domain and tracks the namespace identity. The recommendation
is 75% of the caller's current concurrency, rounded down with a floor
of one. Apply it once per transition if appropriate for the workload.
On unavailable IPC, stale telemetry or unmanaged capacity it returns
the original limit. The application remains responsible for queueing,
admission, deadlines and shutdown. Polling is synchronous on the
caller's worker and adds no polling or per-request work to the daemon.

[Request failure evidence](REQUEST_FAILURE_DIAGNOSTICS.md) ·
[Paired experiment](OVERLOAD_REMEDIATION_RESULTS.md)
