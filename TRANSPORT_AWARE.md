# Application-cooperative transport policies

## Model and provenance

Transport declarations are an additive extension of intent IPC version 2, advertised as transport_sessions_v1. Existing register/batch clients and C ABI options retain their layouts and wire format. The C++ SDK is source-compatible but must be rebuilt: the native Request object grew logical-stream metadata, so binary C++ ABI compatibility is not claimed. The C ABI retains opaque handles and adds functions.

Kinds: tcp, mptcp, udp, quic, logical. Declarations carry a bounded application session ID, multiplexing flag, path-mobility preference and optional associated socket cookie. Up to 16 sessions exist per authenticated process-bound lease. SO_PEERCRED and process lifetime validation apply before every operation. Registration does not confer network privileges.

Capabilities have explicit provenance:
- SO_PROTOCOL, socket type/family and SO_COOKIE come from a received descriptor.
- TCP_IS_MPTCP reports negotiation when supported; unknown stays unknown. UDP does not prove QUIC.
- QUIC negotiation, logical identities, stream IDs, stream priorities, errors and degradation are application reports.
- kernel_packet_stream_priority and mobility_enforced are false. There is no implicit protocol conversion.

A QUIC declaration associated with an FD requires an attested UDP socket. An associated MPTCP declaration requires positive TCP_IS_MPTCP. Without an FD, transport kind remains a claim. Socket associations are snapshots, not retained socket ownership; applications must dissociate when closing a socket. Closing or revoking a lease clears its reports.

## SDK

The existing Intent expresses target latency, service priority, desired/minimum/maximum throughput preferences, deadlines and reliability/isolation preferences. TransportHints adds session identity, TransportKind, multiplexing and mobility preference:
```cpp
flowcore::v2::TransportHints hints;
hints.session = "api_connection";
hints.kind = flowcore::v2::TransportKind::Quic;
hints.multiplexed = true;
hints.mobility_preference = true;
hints.cookie = service.associate_socket(udp_fd);
service.declare_transport(hints); // blocking setup
auto request = service.begin_stream_request(stream_id, 4, 50000, hints.cookie);
// application performs its own I/O
request.complete(flowcore::v2::Outcome::Ok);
```

begin_stream_request carries an application stream ID and relative priority 0..7 in the existing bounded asynchronous request queue. It does not instruct QUIC or HTTP libraries to schedule bytes. Completion, timeout, failure, cancellation and deadline accounting retain monotonic timestamps. Terminal metadata must match the start record. Recent stream reports are bounded to 64 per lease, independent of the 256-request ledger.

declare_transport and report_transport_state are blocking setup/diagnostic calls, not request hot-path operations. Diagnostic errors use a strict enum and state updates are rate-limited to one per 10 ms per session. SDK queue drops remain observable and do not prove absence of application failures. Request telemetry remains best effort.

The C ABI adds fc_v2_declare_transport, fc_v2_transport_state and fc_v2_begin_stream. The small Python bridge calls these native functions; it does not implement another lease protocol.

## Valid controls and boundaries

Service::set_socket_priority(fd, priority) explicitly sets an application-owned socket's SO_PRIORITY, restricted to ordinary values 0..6. Kernel errors propagate. This is socket-wide skb metadata; it does not imply an HTB class, guaranteed bandwidth or per-stream priority. Existing owned-namespace cgroup classifiers can classify UDP as well as TCP, with nftables and HTB counters verified in CONTAINER_RESULTS.md. No host policy or privileged-helper permissions were expanded.

HTTP/2 requests share TCP connections; HTTP/3 streams share encrypted QUIC/UDP connections. Applications may report different stream priorities/deadlines, but socket-level policy affects the connection. Differentiated stream transmission requires cooperation from the application's actual HTTP/QUIC implementation. Encrypted payloads are not parsed.

Path mobility is a preference only. The adapter does not migrate connections or change MPTCP endpoint policy. Unsupported actions produce errors. Old daemons reject the new actions explicitly; TCP/MPTCP/QUIC fallback never silently satisfies a different transport declaration.

Read sessions and recent stream reports through the authenticated intent_v2 status action for the owning lease. Generic UDP socket metrics and application-perceived SLOs remain separate from kernel proof of QUIC behavior.

## Limits

No HTTP/2 or HTTP/3 client/server stack was implemented. The experimental test is QUIC stream echo, not HTTP/3. There is no request-level kernel scheduling, automatic mobility controller, QUIC datagram extension, transparent conversion or universal UDP classification. See QUIC_INTEGRATION.md and TRANSPORT_RESULTS.md.
