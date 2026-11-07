# Fabric protocol v1

The control channel uses mutual TLS 1.3. One request and response travel on each TLS connection. Frames contain a four-byte unsigned big-endian length and a UTF-8 JSON object. Both directions carry `version: 1` and an operation name. Frames are capped at 128 KiB, with an absolute three second read deadline and bounded JSON depth and node count. Unknown fields and versions are rejected. Responses use `op: result` with `result`, or `op: error` with a stable `code`.

| Operation | Principal | Request | Response |
|---|---|---|---|
| `hello` | Enrolled agent | Cluster and host IDs, boot ID, capabilities | Random session ID, policy epoch, maximum lease |
| `heartbeat` | Agent with current session | Cluster and host, session, strictly increasing sequence, telemetry and optional acknowledgement | Current desired cap, or null |
| `status`, `agents`, `services`, `policies`, `diagnostics` | Operator | Operation only | Bounded inventory, status, policy or diagnostic view |
| `reconcile` | Operator | Optional boolean `apply` | Preview, blocked hosts, or committed rollout phase |

The v1 capabilities are `telemetry_v1` and optional `cap_v1`. A new registration replaces the host's former session. Duplicate and delayed sequences are rejected and do not refresh liveness. The agent can send a later sequence after a lost response. Every successful heartbeat pulls the current generation rather than consuming a one-shot command. After a coordinator restart, the agent re-registers and the coordinator rebuilds acknowledgements. A changed policy epoch or conflicting same-generation cap fails closed until the operator performs a drain and re-enrollment.

Telemetry contains a bounded list of interfaces and service windows, observed egress rate, local daemon health, congestion classification and controller state and action when available. Each service window identifies its duration and reports samples, failures, misses and P99. Coordinator receive time determines agent freshness. Agent-provided age comes from a local monotonic interval. Clocks across hosts are not compared. The service aggregate reports reporting coverage and worst observed host P99. It is not a merged cluster percentile. Shared egress aggregation sums only recent measured rates and identifies missing members. It reports utilization against configured capacity only when every participating member has a fresh rate. Independent sampling windows limit timing precision.

Desired cap fields are `cluster`, `host`, `epoch`, `generation`, `resource`, `interface`, `rate_kbit` and `lease_ms`. The acknowledgement reports epoch, generation, resource, current cap, verification flag and applied, fallback or error state. A received generation, a persisted accepted generation, a local apply attempt and a confirmed kernel readback are distinct. The coordinator considers only an exact-generation, exact-rate, fresh verified applied acknowledgement eligible for a rollout barrier.

Agents poll with bounded failure backoff. Normal cap refresh is a maximum three second local lease and the coordinator reports the last heartbeat stale after four seconds. These intervals are measured on each process's monotonic clock. Scheduling and kernel command delays can extend observed recovery time. A coordinator outage leaves local networking active at the configured protected cap once leases expire. The actuator stays local and continues enforcement without the Fabric agent. Missing hosts prevent new coordinated increases.
