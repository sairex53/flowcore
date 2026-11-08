# Cooperative end-to-end SLOs

FLOWCORE's experimental cooperative mode combines explicit RPC budgets, application-reported dependency timings and existing Fabric egress allocation. Standalone FLOWCORE and Fabric without the extension remain supported. The experimental application runs a gateway, backend, database-like service and bulk transfer over real Linux TCP. The gateway fans out to the backend and database, then joins both replies. The backend first does local work and then calls the database. The database fixture models a bounded processing interval.

## Components

| Component | Responsibility |
| --- | --- |
| C++23 SLO model | Computes conservative received budgets and a bounded critical-path forecast over a topologically ordered dependency graph. |
| Cooperative RPC application | Propagates request context, emits local spans, cooperatively expires or defers requests and cancels descendant work on disconnect. |
| Fabric agent | Publishes a bounded per-service aggregate and optionally writes a fresh local advisory with the verified egress cap and observed rate. |
| Fabric coordinator | Applies operator policy to aggregate pressure on a modeled bottleneck. Administrators explicitly reconcile any network change. |
| Existing Fabric actuator | Checks resource ownership and local bounds, applies and verifies the cap, and restores the configured floor when its lease expires. |

The application associates a hop with its own one-request TLS/TCP connection and records optional Linux TCP_INFO smoothed RTT from that socket. It records local monotonic durations and separately timed local processing intervals. The result includes per-hop caller elapsed time, the matching remote-reported duration when available and the residual. That residual can contain TCP and TLS setup, transport delay, queuing and event-loop scheduling. It cannot separate these components. The database interval also includes wake-up delay. TCP RTT stays separate from RPC latency. The lab captures root qdisc bytes, drops and backlog at run boundaries. Queue residence remains unknown for individual requests.

## Forecast and feedback

The native forecast computes the longest dependency path. Sequential durations add, parallel branches contribute their maximum and joins wait on every required predecessor. A missing stage remains explicit even if other stages have observations. An already-expired budget and a known-stage subtotal that exceeds the budget have separate verdicts. The running application uses recent successful per-peer RPC P95 observations with a minimum of eight observations in a 2.5 s window. Each nested RPC already includes its own descendants. The gateway does not add the database timings inside the backend's observed duration again. Percentiles combined this way are heuristics rather than a calibrated probability or a guaranteed high percentile.

When available, a fresh verified cap and observed egress utilization can raise the transfer-time hint for an already-observed branch. This uses the application's payload estimate and the configured local cap. Egress utilization does not reveal spare end-to-end capacity. A stale advisory is discarded. Actual deadlines and failure outcomes remain in the offered-request ledger even when no estimate is available. Unknown remote processing is never filled in from TCP_INFO.

Fabric enables the opt-in capability **cooperative_slo_v1** on the existing mutually authenticated channel. Each reporting host has one configured service summary path. The coordinator verifies the agent's certificate pin, session and heartbeat sequence, then checks the service against enrollment. An optional version-1 cooperative policy selects enrolled critical or interactive services attached to a declared resource. Fresh aggregate risk or misses, sufficient offered samples and observed egress near the current verified cap can multiply an already authorized weight by at most two. They cannot raise the host ceiling, reduce protected floors, select an interface, or bypass the shared-capacity allocation. The existing lower-before-increase barrier and readback checks still apply. Fabric snapshots expose whether a cooperative signal influenced the plan.

A lost agent blocks a new rollout through the existing verified-state gate. Lease expiry restores the local floor. During coordinator loss, applications continue using their own deadlines, local history and fallback concurrency rules. Partitions and restarts can interrupt an in-flight request. Each subsequent invocation uses a new authenticated stream. Business-operation retry and idempotency belong to the application.

See [request coordination](REQUEST_COORDINATION.md) and [measured experiments](COOPERATIVE_RESULTS.md).
