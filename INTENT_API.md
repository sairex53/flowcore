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

One newline-terminated UTF-8 JSON object per AF_UNIX SOCK_STREAM connection.
The existing daemon's 64 KiB request bound, bounded JSON decoder, one-second receive
deadline and socket ownership checks apply. No persistent physical IPC session is
implemented: the SDK amortizes connections with batches of up to 32 events.
Successful replies are {"ok": true, "result": {...}}; errors contain ok=false,
code (forbidden, invalid_request, operation_failed) and error. Clients must not
depend on error prose. Existing V1 operations retain their schema.
V2 requires exactly version=2; no silent downgrade. Current native client expects
the daemon's canonical JSON serialization.

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
audit records. Subsequent messages must come from the same live process.
```json
{"op":"intent_v2","version":2,"action":"batch","lease":"<lease>","events":[
 {"id":1,"phase":"start","start_ns":1000000000,"end_ns":0,"deadline_us":25000,"cookie":null},
 {"id":1,"phase":"ok","start_ns":1000000000,"end_ns":1001000000,"deadline_us":25000,"cookie":null}
]}
```
Example timestamps are illustrative; real events use this boot's CLOCK_MONOTONIC.
Clients and daemon must share the same time namespace; this version does not
translate or verify time-namespace clock offsets. Cross-time-namespace telemetry
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
- associate: additionally requires exactly one SCM_RIGHTS Internet stream socket FD.
  Returns the received socket's SO_COOKIE as a decimal string and classification=none.

Operator command: `flowcore intent revoke payments`. Revocation prevents V2
re-registration of that name until daemon restart. It is not persistent across boots.
Use config ACLs for persistent authorization policy. Audit logs record service,
UID and register/close/expiry/revoke changes without lease values.

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
| ttl_seconds | Enforced ephemeral lease, 1–3600 seconds |
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
Over-quota events are rejected, not silently admitted.

Native SDK: one worker per shared Service channel, fixed 512-event ring,
try-lock enqueue, 32-event batches. It performs no socket I/O or JSON allocation
inside native begin/complete. Contention/full queues/unavailable registration drop
telemetry with counters. Async means ordinary-thread nonblocking submission, not
POSIX signal safety, lock freedom or hard realtime.
C ABI/Python/Go bridge allocates one request handle per scope.
All SDKs are cooperative telemetry: clients can lie about latency and outcomes.

RPC has a total 250 ms deadline. On ambiguous failure, that batch is dropped rather
than replayed; existing leases are preserved on transport errors. Explicit forbidden
responses invalidate admission; restarted daemons cause fresh registration and a
new generation. Old-generation events/cookies must not be reused. No disk spool.
Counters: queued enqueue successes; accepted/rejected server acknowledgements;
dropped includes local and ambiguous transport loss; transport_errors RPC failures.
These are event counters, not a lossless partition of application requests.
flush waits for current queue/worker drain; it is not a durability/delivery promise.
ready is last-known admission, not proof the daemon is currently reachable.

The latest coalesced notification bitmask is delivered asynchronously in batch
replies (including idle polling approximately every 500 ms):
0 SLO risk, 1 observed queue backlog, 2 safe fallback, 3 unavailable inventory
interface, 4 controller reports infeasible capacity. This is a bounded snapshot,
not a lossless event subscription. Missing/stale evidence does not prove health;
congestion is the owned observed network, not necessarily this particular socket.
No automatic switch to privileged networking occurs.

## Correlation limits and lifecycle

SCM_RIGHTS + SO_COOKIE attests the exact socket received, including IPv4/IPv6 TCP.
It proves descriptor access, not exclusive ownership or original creator.
Cookie event correlation is checked against that lease's bounded attestation set.
Cookies identify connections, not HTTP/2 requests, QUIC streams or TLS payloads.
QUIC/datagram association is currently unsupported. No cgroup BPF hook is installed.
No SO_PRIORITY mutation occurs. Distinct request intents on a shared connection are
tracked at the application layer only; TCP bytes cannot be assigned to multiplexed
requests by this API. Cgroup/ports and configured policy associations elsewhere in
FLOWCORE retain their own limitations.

Close drains only if the caller explicitly flushes; final channel destruction
drops pending events, joins the worker and attempts bounded best-effort lease close.
An outstanding Request keeps its channel alive. Destroy all requests and service
handles for shutdown. Do not use inherited SDK instances after fork; exec or create
a new client in a fresh process. Service operations support concurrent callers,
but one Request must have exactly one completing owner; do not close a C handle
concurrently with access through that same handle. No automatic application
cancellation or retry is performed.
