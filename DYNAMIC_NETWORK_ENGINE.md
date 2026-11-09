# Dynamic Bottleneck and Path Adaptation Engine

FLOWCORE separates the location of pressure from the mechanisms available
to influence it. The existing daemon's `flowcore path diagnostics` output
combines link and route inventory, ingress and egress counters, matched
TCP evidence and application SLO windows. It adds an advisory action for
verified owned local pressure. The routed lab adds a controlled
intermediate location and a path controller with separate authority.

| Evidence | Location | Eligible response |
| --- | --- | --- |
| Owned egress queue pressure | Local egress | Review existing HTB allocation or retain static shaping |
| Verified IFB pressure | Managed ingress | Retain the IFB policy and review admission |
| Owned forwarding shaper pressure | Managed intermediate | Test an alternate established MPTCP path |
| Inflated TCP RTT or retransmits with quiet local queues | Suspected remote path | Observe or advise |
| Receive-window pressure on a matched socket | Suspected receiver | Observe or advise |
| Service delay with stable sampled TCP evidence | Suspected application | Observe or advise |
| Conflicting, absent or stale signals | Unknown | Hold policy |

Pacing advice requires a verified pacing qdisc and an authorized competing
socket. Per-message advice requires cooperating datagrams and a class
eligible for their socket mark. The daemon diagnostics cannot prove those
authorizations from generic inventory, so it does not select either for
arbitrary live sockets. The existing adaptive controller still owns HTB
changes in its own lab. The routed lab's active actuator is restricted to
MPTCP endpoint backup flags. The router's rate and link transitions are
scheduled experimental stimuli.

## Evidence and capacity

The path observer records the pinned namespace and interface identities,
route resolution, carrier, egress qdisc bytes, backlog and drops,
interface counters in both directions, and a source-bound TCP echo probe.
It samples the managed shaper rate from kernel readback. Five probe slots
retain success and missing outcomes. Probe failures are reliability
evidence and do not measure packet loss directly. Recent SLO evidence
comes from verified application responses and failures on the workload
connection. It expires after two seconds.

A rate readback describes a configured local limit. Departure rate is a
counter delta divided by the actual elapsed time. The observer reports a
separate saturated departure estimate after three consecutive qualifying
windows with backlog and utilization between 70% and 125% of the
configured rate. Idle or inconsistent windows clear that estimate.
Unknown capacity stays unknown. The engine does not estimate Internet
bottleneck bandwidth from an idle socket or from a small echo.

A rate reduction of more than 15% is a sudden reduction. Consecutive
smaller downward steps indicate gradual degradation. Counter resets,
route identity changes and gaps over two seconds clear trend evidence.
Increasing competing-class departures with queue growth can identify
a local demand burst. Decreases in delivery alone are ambiguous. Carrier
loss and kernel-confirmed restoration are recorded separately from
observed service recovery.

## Safety and reporting

The lab uses three pinned disposable namespaces. It checks link indices,
route fingerprints and the exact owned endpoint flag map before writing.
The controller moves only an observed established subflow into the
preferred set and promotes it before demoting another. A failed update
attempts compensation and verifies the prior map. After a rejected or
uncertain update it freezes further decisions. Uncertain compensation
invalidates the trial and triggers owned lab teardown.

The policy does not change host routes, arbitrate production sockets
or implement MPTCP packet scheduling. See the [path policy and topology](PATH_ADAPTATION.md)
and [measured results](PATH_ADAPTATION_RESULTS.md).
