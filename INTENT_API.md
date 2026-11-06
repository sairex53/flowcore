# Network Intent API V2

Status: implemented opt-in process-bound application telemetry and intent admission.
This extends the V1 API; it does not replace the privileged network helper.
No latency, delivery, isolation or per-request scheduling guarantee is implied.

## Identity and authorization

The control socket accepts root and the daemon's effective UID, subject to existing
monitor-only authorization. A regular user can run an ordinary daemon without
capabilities and submit telemetry. The packaged read-only monitor rejects mutations.
V2 registration is bound to SO_PEERCRED UID/PID and Linux process start ticks.
SO_PASSCRED/SCM_CREDENTIALS also verify each received message against the connected
peer, preventing an inherited/delegated connected socket from impersonating its
original process. Leases expire on process death, birth-time mismatch, TTL, registry
removal, close or operator revocation. PID must still be alive during authorization.

Registration grants no helper authorization, packet classifier or host policy change.
Names are labels, not proof of business identity. The cgroup snapshot is diagnostic,
not an enforcement boundary; later cgroup moves are not automatically classified.
Priority permissions come from operator configuration:
```json
"intent_api": {"priorities": ["standard"]}
```
Only standard is allowed by default. Explicitly granting interactive/critical applies
to every admitted client UID of this daemon. Deploy a separate daemon per trust
domain; this is not a multi-tenant application identity service.
Conflicting intents for the same service/owner are rejected by the existing registry.
The legacy active controller's shared-class restrictions remain in force.

## Version and framing

The default SDK worker uses a persistent AF_UNIX SOCK_SEQPACKET connection to
intent.sock next to control.sock. Each packet is one bounded UTF-8 JSON object;
SDK packets/replies include a trailing newline. Only op=intent_v2 is allowed on
this endpoint. Every packet revalidates SO_PEERCRED against SCM_CREDENTIALS and
the process birth identity; a delegated connected FD cannot inherit authorization.

The original control.sock keeps one newline-terminated JSON object per
SOCK_STREAM connection, including V1 and V2 compatibility. FD association and
dissociation use this separate setup/teardown path. If the sidecar is absent
(ENOENT), or a custom control basename is used, the worker uses legacy V2 RPC.
An existing but unavailable/untrusted endpoint does not silently bypass checks.
No V1 protocol downgrade occurs. Both paths retain 64 KiB request and strict
bounded JSON parsing. The legacy path retains its one-second receive deadline.
Successful replies are {"ok": true, "result": {...}}; errors contain ok=false,
code (forbidden, invalid_request, operation_failed) and error. Clients must not
depend on error prose. Existing V1 operations retain their schema.
V2 requires exactly version=2; no silent downgrade. Current native client expects
the daemon's canonical JSON serialization. Native request construction formats
microsecond-derived milliseconds with integer arithmetic, independent of the
application's LC_NUMERIC, so decimal-comma locales cannot corrupt JSON.

Registration:
```json
{"op":"intent_v2","version":2,"action":"register","isolation":"shared",
 "intent":{"name":"payments","priority":"standard","target_p99_ms":30,
           "deadline_ms":25,"desired_kbit":0,"min_kbit":0,"max_kbit":0,
           "ttl_seconds":300,"persistent":false,"reliability":"balanced"}}
```
Optional intent fields use existing registry defaults; send priority explicitly
(the historical V1 default is critical). Unknown intent fields are rejected.
Registration returns a secret 32-character hexadecimal lease, never included in
audit records, and renewable=true on servers supporting explicit renewal.
An additive features array advertises lease_renewal, udp_socket_association,
socket_dissociation and notification_quality. Older V2 servers can omit these;
transport compatibility does not imply support for every new operation. Subsequent messages must come from the same live process.
```json
{"op":"intent_v2","version":2,"action":"batch","lease":"<lease>","events":[
 {"id":1,"phase":"start","start_ns":1000000000,"end_ns":0,"deadline_us":25000,"cookie":null},
 {"id":1,"phase":"ok","start_ns":1000000000,"end_ns":1001000000,"deadline_us":25000,"cookie":null}
]}
```
Example timestamps are illustrative; real events use this boot's CLOCK_MONOTONIC.
Clients and daemon must share the same time namespace; this version does not
translate time-namespace clock offsets. It verifies namespace identity through
/proc/PID/ns/time and rejects unverified or different namespaces. Cross-time-namespace telemetry
is unsupported.
Event keys are exact. IDs are positive signed-63-bit integers, unique within a
lease (native IDs increase). Terminal phases: ok, timeout, failure, cancelled.
Metadata must match start; a terminal event is counted once within the bounded
active/recent ledger. Durations must be at most 600 seconds and completions must
satisfy the observer's existing two-second freshness limit. Deadline is an explicit
budget from start: absolute deadline = start_ns + deadline_us * 1000. Zero uses the
service deadline, if configured. No encrypted-stream deadline inference occurs.
A deadline miss is recorded independently of successful completion. Cancellation
is separately counted by V2 and included in legacy failure accounting.
Expired/missing start events do not create fabricated latency samples. Dropped
terminal telemetry can leave inflight records until the 600-second expiry; monitor
drop/reject/expired-request counts and treat coverage as incomplete.

Other actions have exact fields op,version,action,lease:
- status: inflight, outcomes, socket count, process identity and monotonic expiry.
- close: revoke this lease and its service instance.
- renew: extend this process-bound lease by its originally admitted ttl_seconds.
  The token, outstanding requests and socket associations stay unchanged.
  A revoked/expired/dead-process lease cannot be renewed.
- associate: additionally requires exactly one SCM_RIGHTS IPv4/IPv6 TCP or UDP FD.
  SO_PROTOCOL and socket type are checked. Returns SO_COOKIE as a decimal string,
  protocol=6/17 and classification=none. Re-attesting an existing cookie is idempotent.
- dissociate: additionally requires cookie (positive unsigned-64-bit integer).
  Withdraws the binding and returns abandoned, the number of unfinished telemetry
  records removed. It never cancels I/O or fabricates failed-request latency.
  Explicitly complete/flush requests before teardown; late events using the removed
  cookie are rejected. Status exposes abandoned_requests separately from expiry.

Operator command: `flowcore intent revoke payments`. Revocation prevents V2
re-registration of that name until daemon restart. It is not persistent across boots.
Use config ACLs for persistent authorization policy. Audit logs record service,
UID, PID/process birth for registration/renewal/close/expiry, operator revocation
even without active instances, and explicit dissociation. Lease values are omitted.
The in-memory audit history is bounded to 128 entries; external log retention is
a deployment concern.

## Constraints versus preferences

| Field | Semantics |
| --- | --- |
| priority | Authorized service class objective; alone installs no classifier |
| target_p99_ms | Best-effort application SLO; never a hard deadline guarantee |
| deadline_ms / deadline_us | Explicit application observation budget, not packet expiry |
| desired_kbit | Best-effort throughput objective |
| min_kbit / max_kbit | Only admitted through existing owned HTB laboratory class contracts |
| reliability | balanced / prefer_reliable metadata preference; no TCP rewrite |
| isolation | shared / dedicated_preference metadata; no isolation created |
| ttl_seconds | Renewable ephemeral lease, 1–3600 seconds per admission/renewal |
| socket cookie | Exact identity of a received socket object; no request packet scheduling |

Nonzero hard rates require an owned HTB lab and existing class admission checks.
HTB guarantees and ceilings apply to classes, not independently to every request
or multiplexed stream. Registration alone does not map a socket to that class.
Without independently verified classification, no end-to-end throughput guarantee
is advertised. Kernel updates remain with the existing authorized actuator.

## Bounded operation and delivery

Daemon: 64 V2 leases, 16 registry services including configured services, 256
inflight requests and 256 recently completed IDs per lease, 64 socket cookies.
Requests abandoned for 600 seconds leave the inflight ledger with an expiry count.
Token bucket: burst 4096 events, refill 2000 events/second per lease, batch <=32.
Over-quota events are rejected, not silently admitted. Socket slots are reusable
through explicit dissociate, rather than accumulating closed sockets indefinitely.

The sidecar allows 16 active connections and a bounded listen backlog. Connections
expire after 30 idle seconds. Each client has a burst/refill limit of 2048 frames
per second, separately from event quotas. At most eight ready clients are served
per poll, in rotating order. Frames, replies and ancillary FDs are bounded; truncated,
oversized, delegated or non-reading connections are rejected/closed. Output is
nonblocking; receive is atomic, with no unbounded partial-frame buffers.

Native SDK: one worker per shared Service channel, fixed 512-event ring,
try-lock enqueue, 32-event batches with up to 1 ms worker-side coalescing. It performs no socket I/O or JSON allocation
inside native begin/complete. Contention/full queues/unavailable registration drop
telemetry with counters. Async means ordinary-thread nonblocking submission, not
POSIX signal safety, lock freedom or hard realtime.
C ABI/Python/Go bridge allocates one request handle per scope.
All SDKs are cooperative telemetry: clients can lie about latency and outcomes.

RPC has a total 250 ms deadline. On ambiguous failure, that batch is dropped rather
than replayed; existing leases are preserved on transport errors. An explicit
unknown-lease response carries retry_registration=true; only that condition clears
admission on the persistent path. Other session identity/authentication failures
reconnect without blindly leaking another lease. Restarted daemons reject stale
tokens and cause fresh registration and a new generation. Old-generation events/cookies must not be reused. No disk spool.
Counters: queued enqueue successes; accepted/rejected server acknowledgements;
dropped includes local and ambiguous transport loss; transport_errors RPC failures.
These are event counters, not a lossless partition of application requests.
flush waits for current queue/worker drain; it is not a durability/delivery promise.
ready is last-known admission, not proof the daemon is currently reachable.
registration_generation() exposes token changes. last_error() distinguishes none,
transport unavailable, authorization denied, invalid request, rejected events
and protocol errors. A successful exchange can clear the last error; cumulative
event/transport counters remain. transport_statistics() reports peer-verified
session connections, sent session frames and legacy worker RPC attempts. It does
not count separate blocking descriptor setup/teardown connections.

Initial admission starts immediately in the worker. While Service/Request keeps
its channel alive, the SDK explicitly renews at approximately half the admitted TTL.
Default idle polling is about 500 ms (200 ms for a one-second TTL). Renewal is a
lease refresh, not an absolute forever guarantee: loss, expiry, process death,
revocation or daemon restart still invalidates it. Older servers without
renewable=true retain expiry/re-registration behavior. Independent instances
extend the shared registry lifetime to cover active leases rather than expiring
a newer instance at the first instance's original TTL.

The latest coalesced notification bitmask is delivered asynchronously in batch
replies (including idle polling approximately every 500 ms):
0 SLO risk (tail latency, deadline misses, failures/timeouts), 1 observed queue backlog, 2 safe fallback, 3 unavailable inventory
interface, 4 controller reports infeasible capacity. This is a bounded snapshot,
not a lossless event subscription. Missing/stale evidence does not prove health;
congestion is the owned observed network, not necessarily this particular socket.
notifications_time_ns() reports the server snapshot generation time; it is not
the age of every underlying metric. Raw batch replies also expose application_samples
and network_observed. No samples/flags is not evidence of healthy networking.
No automatic switch to privileged networking occurs.

## Correlation limits and lifecycle

SCM_RIGHTS + SO_COOKIE attests the exact socket received, including IPv4/IPv6 TCP and UDP.
It proves descriptor access, not exclusive ownership or original creator.
Cookie event correlation is checked against that lease's bounded attestation set.
Cookies identify connections, not HTTP/2 requests, QUIC streams or TLS payloads.
A QUIC UDP socket can be attested; QUIC streams cannot be identified from it.
No cgroup BPF hook is installed by the SDK.
No SO_PRIORITY mutation occurs. Distinct request intents on a shared connection are
tracked at the application layer only; TCP bytes cannot be assigned to multiplexed
requests by this API. Cgroup/ports and configured policy associations elsewhere in
FLOWCORE retain their own limitations.

Close drains only if the caller explicitly flushes; final channel destruction
drops pending events, joins the worker and attempts bounded best-effort lease close.
An outstanding Request keeps its channel alive. Destroy all requests and service
handles for shutdown. Keep Service alive through normal request processing:
if a Request is the final channel owner, completing/destroying it can perform the
bounded shutdown/join. Descriptor association/dissociation, wait/flush and shutdown
belong outside latency-sensitive scopes. Do not use inherited SDK instances after fork; exec or create
a new client in a fresh process. Service operations support concurrent callers,
but one Request must have exactly one completing owner; do not close a C handle
concurrently with access through that same handle. No automatic application
cancellation or retry is performed.


Process identity must be visible in the daemon's procfs, including time namespace
verification. Hardened nondumpable/procfs arrangements can make this unavailable;
registration then fails explicitly. Do not grant CAP_SYS_PTRACE to the daemon to
silently bypass that boundary. Same-UID clients remain one trust domain.


The new session handlers are nonblocking, but legacy administrative actions still
share the daemon loop and can delay telemetry delivery. SDK deadlines/backpressure
bound the effect on submission, not the latency of all daemon operations. This is
another reason not to treat delivery as a hard SLO guarantee.
