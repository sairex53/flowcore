# FLOWID security

FLOWID v1 is an experimental application protocol. The reference server runs as a regular account and uses no privileged FLOWCORE helper, packet classification, host network changes or capabilities. Run the server with a private state directory on a local filesystem.

## Trust and authorization

- The client verifies a server certificate chained to its configured CA with a DNS SAN for the exact service name. Wildcards and subject CN fallback are disabled. The endpoint IP comes from discovery and does not determine service identity.
- The server requires a trusted client certificate and matches the SHA-256 fingerprint of its DER leaf against an explicit allowlist. The wire service name must match server configuration. A session token is usable only with the same authorized leaf identity. Reload the server with both pins for planned overlap and drain existing sessions before removing the old pin. Rotating the client certificate changes its identity for session resumption.
- TLS 1.3 and the mandatory flowid/1 ALPN protect and identify the application stream. The reference endpoint issues no TLS tickets and accepts no early data. Logical resumption uses a fresh authenticated connection.
- The server generates 256-bit random tokens using the operating system CSPRNG. It persists a SHA-256 digest and uses the plaintext token only on encrypted setup/resumption and in client memory. Demo output and fault artifacts omit tokens, payloads, pins and private keys. Restrict core dumps and access to either process's memory.
- Provide trust anchors, certificates, keys and endpoint files via trusted directories. Both implementations verify a private key through the same opened file they parse, rejecting symlinks, unsafe modes and non-regular files. Server JSON and endpoint files are bounded and require trusted file ownership. Parent directories and filesystem mounts are part of the operator trust boundary. The endpoint directory generation check lives in memory and provides no cross-process rollback protection.

The server host and its authorized certificate holders are trusted for session behavior. A compromised authorized service can lie about its own cursor or record plaintext messages. A compromised client credential plus its token can resume a session as that client. Keep endpoint records and credentials independently managed. Use a dedicated deployment CA where possible.

## Quotas and failure semantics

| Resource | Bound |
|---|---|
| Session lifetime | 1–3,600 s fixed at OPEN, with monotonic client expiry and server-side wall-clock expiry |
| Sessions | Up to 256 persisted sessions, 128 by default, with a configurable per-pin limit |
| Connections | 16 server worker slots and a bounded listen backlog |
| Transport time | 2 s handshake and write timeout, 1–30 s bounded frame read window |
| Client outbox | 64 messages and 256 KiB of payload |
| Message and replay | 16 KiB payload and 64 stored digests per session |
| Server storage | 16 MiB maximum SQLite database plus bounded rollback journal overhead |
| Discovery | 8 KiB endpoint file and 16 endpoints |

A peer with missing or invalid credentials cannot establish a FLOWID session. Missing state, expiration, a mismatched peer, a contradictory sequence and an unsupported protocol fail closed. Database failure emits a temporary error and the client retains pending messages. Expired sessions are pruned during OPEN. Expired tokens and closed sessions cannot perform DATA operations.

Use one authoritative database across the cutover. SQLite WAL shipping, distributed consensus, failover replication and stale-backup detection are outside this implementation. The migration demo uses a single local database visible from two network namespaces and stops the old server before moving traffic. Restoring a stale database can lose durable decisions, so backup restoration requires application reconciliation. SQLite's durability also depends on the storage stack honoring its sync operations. Wall-clock steps can alter TTL behavior on the server.

## Verified coverage

The automated tests exercise a wrong service certificate, a CA-signed unpinned client, replay with an altered payload, an old association writing after resumption, frame-size/version/flags rejection, expiration, outbox and session quotas, insecure discovery-file permissions, and attempted resume at a replacement server with no state. The lab exercises process termination and endpoint changes. Independent security review, high-volume denial-of-service testing and filesystem power-loss testing remain open.
