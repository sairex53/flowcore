# FLOWID logical sessions

FLOWID v1 is an experimental, opt-in message protocol for cooperating applications. A DNS-shaped logical service name selects a discovery record and authenticates the server certificate independently of the IP and port. The client can establish a new authenticated transport association for an existing logical session after a disconnect. Applications must adopt FLOWID framing and explicitly preserve the state needed to resume.

## C++23 SDK

Link to the CMake target flowcore::flowid and include <flowid/session.hpp>. OpenSSL 3 or newer is required to build the native target.

```cpp
flowid::Options options;
options.resolve = flowid::file_directory("endpoints");
options.ca = "ca.pem";
options.certificate = "client.pem";
options.key = "client.key";
auto session = flowid::connect("payments.test", std::move(options));
session.on_reconnect([] (const flowid::ReconnectEvent& event) {
    log_association(event.endpoint.address, event.association);
});
const auto result = session.send("example payment request");
if (!result.acknowledged()) {
    // Retain this sequence. Use flush() later to retry the original message.
    record_pending(result.sequence, result.progress.status);
}
```

The native session has a single owner and uses synchronous I/O. A user callback runs on the caller's thread after authenticated resumption and before draining outstanding messages. It should return promptly and should avoid reentering the session. Application callbacks and custom discovery can extend the time spent inside SDK calls. The socket operation budget uses a monotonic clock and defaults to 2 s, with a 500 ms per-attempt budget. Each retry uses bounded backoff. Callers can use a dedicated worker when an application thread must remain responsive.

| API | Behavior |
|---|---|
| enqueue | Assigns the next sequence number and reserves bounded local outbox space |
| send | Enqueues, then attempts to flush the FIFO outbox in order |
| flush | Retries the existing outbox, preserving message identities and bytes |
| recover | Re-resolves endpoints and resumes the same session, including when the current association is healthy |
| state | Reports the current association, acknowledged cursor and queued work. An authenticated channel may have lost reachability since its last exchange |
| close | Requires an empty outbox. An interrupted END can be retried with the same session before expiration |

Each session uses explicitly selected TCP or MPTCP for its lifetime. MPTCP must negotiate on both endpoints. The internal framed protocol consumes an authenticated ordered stream with bounded read/write calls. A future QUIC implementation could provide such an adapter over an explicit reliable stream and define its identity binding. FLOWID v1 has no QUIC adapter.

## Discovery

The built-in adapter uses an atomically replaced, operator-owned file. The first line identifies the service and its positive generation. Each subsequent row contains a transport, numeric IPv4 or IPv6 address, port and Unix-second expiration.

```text
FLOWID-DIRECTORY/1 payments.test 2
tcp 198.18.20.2 9443 1893456000
tcp 198.18.21.3 9443 1893456000
```

A resolver accepts up to 16 endpoints and reloads on association establishment. It skips expired endpoints and endpoints outside the chosen transport. Existing authenticated associations may continue past a discovery record's expiry until disconnected or explicitly recovered. The file adapter rejects a lower generation or changed content at the same generation during its lifetime. A caller can supply another discovery adapter. The TLS certificate and logical service handshake still authenticate the selected endpoint.

## Wire format

TLS 1.3 or newer with ALPN flowid/1. All integers use unsigned network byte order. Frames begin with a u32 length for the remaining bytes, followed by u8 version (1), u8 opcode, u16 flags (zero), and the body. The frame length range is 4–16,396. Sequence and association values fit in 63 bits. The client offers a u16 capability mask and the server returns the negotiated mask. V1 requires bits 0–2 for resumption, ordered cumulative ACK and durable demo commits. The service field is a u8 length followed by up to 127 ASCII bytes.

| Code | Message | Body |
|---|---|---|
| 1 | OPEN | Capability mask and service field |
| 2 | OPENED | Capability mask, 32-byte random token, u64 committed cursor (0), u64 association (1), u32 TTL seconds |
| 3 | RESUME | Capability mask, service field, token, u64 client ACK cursor |
| 4 | RESUMED | Capability mask, u64 committed cursor, u64 new association, u32 remaining TTL seconds |
| 5 | DATA | u64 sequence followed by 1–16,384 payload bytes |
| 6 | ACK | u64 durable cumulative cursor |
| 7 | END | u64 final acknowledged cursor |
| 8 | ENDED | u64 final committed cursor |
| 9 | ERROR | u8 code for protocol (1), authorization (2), expiration or missing state (3), conflict (4), quota (5), closed (6), fenced (7) or temporary store failure (8) |

A new logical session starts at sequence 1. The server only commits the next expected sequence. An identical replay within the most recent 64 sequences returns the committed cursor. A gap, changed payload or replay outside that window fails. RESUME advances the association generation in the same database transaction that reads the committed cursor. Any subsequent DATA or END on an older association fails as fenced. The client discards only entries covered by a valid ACK or RESUMED cursor. It rejects a cursor behind an acknowledged message or ahead of a sequence it attempted to send. END leaves a tombstone until expiration, so a lost ENDED can be resolved by a retry that confirms the session is closed.

## Delivery contract

- The tuple of session and sequence identifies a message for ordering and deduplication. Sending the same payload again via send() assigns a new identity.
- Retries can put the same message on the wire more than once. Delivery progress depends on eventual reachability, valid credentials and unexpired state. An unacknowledged message might already have committed when its transport failed.
- The reference application updates its counters and replay ledger in one SQLite transaction. This gives at-most-once durable demo state changes per sequence. External effects need their own idempotency keys or an application transaction that includes deduplication.
- The client keeps its token and outbox in process memory. If that state is lost, the application needs its own reconciliation. A missing or expired session is terminal and the SDK never opens a replacement behind the application's back.
