# FLOWCORE Public API Contract

## Versioning

- The native SDK requires C++23 and exports the flowcore::sdk CMake target.
- The intent wire protocol is version 2. The current daemon advertises protocol range 2–2; the client accepts only a range containing version 2.
- A v2 server predating optional range/capability fields is treated as v2 with only features it actually returns. Missing optional features are not inferred as supported.
- The stable C ABI is declared in include/flowcore/intent_c.h. Existing v2 options/statistics structures and exported function semantics remain intact. Additions are new symbols and versioned output structures.
- fc_v2_options.size is a minimum-size prefix check; extra future bytes are ignored. Do not append fields to the existing structure and assume old applications initialize them.
- There is no v3 negotiation. A response outside v2 or an inconsistent advertised range is rejected.

## Ownership and lifecycle

- Service owns a shared process-bound SDK channel and lease. Copies share that channel; last-owner destruction stops its worker and best-effort closes the lease.
- Request is move-only. Destruction reports Cancelled if still active. cancel() reports telemetry completion only and does not interrupt application I/O.
- DeadlineRequest tracks a local monotonic deadline and optional progress. should_cancel() is a cooperative recommendation for the application.
- A service lease is bound server-side to peer UID, PID, process-start identity and a bounded lifetime. Socket association passes the socket FD and is checked against the caller's lease.
- The SDK cannot grant a priority, bandwidth guarantee, or host network authority. Operator configuration and privileged networking boundaries remain authoritative.

## Non-blocking and blocking calls

begin_request, begin_stream_request, request completion, local capability reads, statistics and notification snapshots do not perform IPC. Periodic degradation polling is opt-in via subscribe_notifications; request batches still carry coalesced notification state. Telemetry enqueue is bounded and may drop under overload; inspect Statistics.

try_open is non-throwing for local validation/construction errors and does not wait for a daemon. The compatibility constructor still throws on invalid local input. wait_ready and flush have caller-specified timeouts.

associate_socket, effective_policy, admit, declare_transport, report_transport_state, dissociate_socket, and construction of an authorized UDP message sender are setup/control operations. Current local IPC exchanges have a 250 ms deadline. Call them outside latency-sensitive hot paths.

## Capability and policy snapshots

Capability bits mean the daemon advertised support for that protocol operation. udp_message_authorized is a lease-specific grant, not merely platform availability.

The policy snapshot contains the server-accepted intent, service-registry generation, whether the controller reports a verified kernel class allocation, and that class's rate and ceiling when verified. kernel_verified=false means the SDK has no evidence of a verified per-class allocation. It must not be interpreted as a failed request or proof that no other system component configured QoS. A registered service intent is not itself enforcement.

## Errors and fallback

- C++ setup code can use std::expected<Service, OpenError> from Service::try_open.
- Synchronous policy inspection returns std::expected<EffectivePolicy, OpenError>.
- Existing throwing APIs remain supported.
- The C ABI uses null/zero return values and fc_v2_error() for the calling thread. Its string is valid until the next error on that thread.
- Unsupported features are detectable through capability bits. Existing protocol-v2 methods remain available when an optional feature is absent; the daemon may deny a call.
- The additive C UDP sender requires a successful operator grant, a valid UDP descriptor and the required socket mark. It never changes the caller's ordinary socket into an authorized one; the SDK duplicates the FD for scoped ownership. A failed optional sender setup leaves ordinary application UDP available.
- Daemon outage, protocol mismatch, authorization denial, or queue pressure must not gate application network operations. Each application chooses whether missing intent telemetry is acceptable to its business logic.

## Data semantics and limits

- Request timings use the sender process monotonic clock and are accepted only when the server verifies a shared time namespace.
- Application stream IDs and priorities are reports, not packet-level stream controls.
- A socket cookie is exact only after FD-based association and only for the observed socket lifetime. It is not a stable business request identity.
- The SDK does not inspect TLS, HTTP/2, HTTP/3, or QUIC payloads.
- UDP software TX timestamps use the documented clock domain; they are not wire timestamps.
