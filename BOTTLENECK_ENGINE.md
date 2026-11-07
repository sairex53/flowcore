# Bottleneck Intelligence Engine

The daemon now exposes directional pressure, capacity provenance and the evidence behind an attribution. The result separates a suspected cause from whether FLOWCORE owns a local actuator. The engine runs with the existing userspace controller and consumes the existing TC, INET_DIAG, application and route observations. [Experiments](BOTTLENECK_RESULTS.md) · [Directional QoS](DIRECTIONAL_QOS.md)

## Evidence and attribution

| Classification | Evidence | Action after confirmation |
|---|---|---|
| LOCAL_EGRESS_CONGESTION | Persistent backlog of at least 1,500 bytes at the observed local egress queue. An unmanaged queue also requires observed saturation or drops. | Review allocation for an owned HTB tree, review shaping for an owned CAKE queue, or advise for an unmanaged interface. |
| LOCAL_INGRESS_PRESSURE | Persistent IFB queue backlog, or a local interface RX-drop increment when no owned ingress shaper is available. | Review an already admitted IFB policy or advise. |
| REMOTE_PATH_SUSPECTED | Sampled local egress is quiet, no ingress pressure is observed, and an active matched TCP socket shows RTT inflation on a sufficient packet sample or retransmissions rise. | Advise and collect path evidence. Existing MPTCP tooling can probe authorized paths separately. |
| RECEIVER_LIMITED_SUSPECTED | An active matched TCP socket spends at least 65% of its sampled busy time limited by the peer receive window and has queued or unacknowledged data. Local pressure takes precedence. | Advise on receiver flow control. |
| APPLICATION_LIMITED_SUSPECTED | A service has at least 32 reported latency samples, P95 is at least 120% of its target, and an active TCP cohort for its configured service port has stable sampled RTT. Local queues and transport pressure do not explain the observed delay. | Advise on application timing and request instrumentation. |
| INSUFFICIENT_EVIDENCE | Counter baseline missing, evidence stale, two directions under pressure, conflicting TCP indications, or no specific cause observed. | Observe. Per-direction signals remain visible when available. |

The TCP baseline uses matched socket cookie, namespace and endpoint tuple. RTT inflation needs at least 20 data segments, a twofold increase over the socket's minimum RTT and an increase of at least 2 ms. Retransmission evidence needs at least 20 data segments and at least two retransmissions at a 2% or higher ratio. A retransmission or RTT sample may reflect reverse-path behavior, delayed ACKs, host scheduling or another queue. The remote label is a suspicion about the path outside the monitored queues.

The lab associates sockets through its single admitted veth, local addresses and any explicit interface binding. Host mode uses the explicit INET_DIAG bound interface only. Missing or truncated socket samples suppress TCP attribution. Link, address and route events invalidate observations. The daemon revalidates the owned lab shape before admitting further egress transfers. Path diagnostics shows links, associated routes and a bounded recent event list. Host mode reports only sampled inventory and makes no interface changes.

Application metrics come from the existing reporting API. The engine pairs service P95 with backlog in that service's egress class. It can report a Pearson correlation after at least four varying paired observations, using at most eight recent observations. The P95 windows can overlap and multiple services can share a class. The correlation is descriptive and leaves causality open.

## Capacity and state

Each direction records its own queue, byte accounting, offered-demand estimate and capacity source. HTB parent rate and ceiling or CAKE bandwidth provide a configured local limit. Host Ethernet speed, where reported, is a nominal interface value. Observed throughput and the recent peak are reported separately. An unshaped path without a speed observation has unknown capacity. A utilization ratio at or above 85% sets the sampled saturation indicator. Headroom is relative to a known cap and requires an observed empty queue.

Per-class demand is estimated from served bytes plus the change in leaf backlog per interval. It requires complete counters and zero observed leaf drops. Dropped bytes are unavailable, so the estimator withholds demand in such intervals. Counter regression, topology change, capacity change and long sampling gaps establish a new baseline. Readback reductions report the old and new values and invalidate the prior admission.

The bounded state engine uses `UNKNOWN`, `NORMAL`, `BUILDING`, `CONGESTED` and `RECOVERING`. Pressure needs two consecutive compatible intervals spanning at least 0.5 s. Recovery needs three clear intervals spanning at least 0.75 s. Sampling cadence can make both longer. Freshness is checked at query and at control admission. `NORMAL` with `INSUFFICIENT_EVIDENCE` means no specific bottleneck was observed in the available local sample.

The existing controller only proposes a new congestion-driven reservation transfer when the engine admits a fresh controllable HTB egress bottleneck. Its class demand, sample, fairness, rollback and budget checks still apply. Operator-specified class ceilings, background floor restoration and rollback retain their existing semantics. The engine offers ingress and remote advisories without modifying a live path.

## Commands

```sh
flowcore bottleneck inspect --json
flowcore bottleneck watch --watch 1 --count 10
flowcore capacity estimate --json
flowcore qos direction --json
flowcore path diagnostics --json
```

Use `--interface NAME` to select a monitored interface. With an active lab the default is its sender. Otherwise the daemon selects a non-loopback interface when present. Watch emits JSON lines from sampled daemon state. Before the first complete interval or when a sample expires, measurement fields can be absent. Read commands use the existing daemon socket authorization. The data model is schema 1.
