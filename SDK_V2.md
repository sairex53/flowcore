# FLOWCORE Application Intent SDK 2.0

The SDK lets a cooperating process register a bounded service intent and report application request outcomes. It is optional telemetry and policy input: applications must keep serving requests if the daemon is absent or policy enforcement is unavailable.

## C++23

    #include <flowcore/intent_v2.hpp>
    #include <chrono>
    #include <utility>

    using namespace flowcore::v2;
    Intent intent;
    intent.service = "payments";
    intent.priority = Priority::Critical;
    intent.target_latency_us = 25'000;
    intent.deadline_us = 40'000;

    auto opened = Service::try_open("/run/flowcore/flowcored.sock", intent);
    if (!opened) return; // Keep serving without FLOWCORE.
    Service service = std::move(*opened);
    if (service.wait_ready(std::chrono::milliseconds{250}) &&
        service.capabilities().supports(Capability::DeadlineAdmission)) {
        // Optional advisory feature discovered at registration.
    }
    auto request = service.begin_request(std::chrono::microseconds{40'000});
    // Perform ordinary application I/O.
    request.complete(Outcome::Ok); // Scope destruction reports Cancelled.

The integer-microsecond overloads remain for compatibility. Requests and deadline scopes are move-only. Assigning over a live scope reports cancellation of the old telemetry scope. Cancellation is not a transport cancellation; the application owns that operation.

Use subscribe_notifications() to opt in to periodic bounded notification polling, then read notification_snapshot() for the latest coalesced state. Polling is disabled by default to avoid idle IPC; request batch replies still update the snapshot when requests are being reported. The snapshot reports observation time and application sample count. The SDK does not run user callbacks on its worker thread.

effective_policy() is read-only IPC bounded by the local IPC timeout. Its snapshot separates the accepted intent from kernel_verified class values. RegisteredOnly means no kernel policy was verified for this registration. Never call it in a request hot path.

## C and language bindings

The C ABI is in include/flowcore/intent_c.h. Existing fc_v2_options and fc_v2_statistics layouts are unchanged. fc_v2_options.size accepts the known v2 prefix or a larger caller structure; unknown tail fields are ignored. New output structures have their own version and size.

Examples:

- C++: examples/intent_v2_demo.cpp
- C: examples/intent_c_demo.c
- C per-message UDP example: examples/intent_c_udp_demo.c (requires an operator-approved socket mark and UDP grant). The C API also exposes bounded send and send-batch operations through fc_v2_udp_sender_open/fc_v2_udp_send/fc_v2_udp_send_batch.
- Rust through the C ABI: examples/intent_rust_demo.rs
- Go through cgo: examples/intent_go_demo.go
- Python adapter through the same C ABI: examples/python/flowcore_intent.py
- HTTP/gRPC/database service examples: examples/real_apps/

See APPLICATION_INTEGRATION.md, REAL_APPLICATIONS_V2.md, and GRPC_INTEGRATION.md for complete workload examples.

The Go and Rust toolchains were not installed on the validation host, so those foreign-language examples were not compiled here. C and C++ consumers are built and exercised by the live SDK test.
Compatibility coverage includes the original v2 C function/layout surface exercised against the current daemon, a new SDK against a mock v2 peer that omits optional capability fields, and explicit rejection of an advertised incompatible protocol range. Missing optional capabilities remain absent rather than being inferred.

## Transport and message semantics

- Service intent is attached to a process-bound lease authenticated with Unix peer credentials and process lifetime checks. Priority remains operator-authorized.
- Socket association passes the actual socket FD. The daemon validates socket family/protocol and derives its kernel cookie; an application-supplied cookie alone is not identity.
- Request stream IDs and priorities are application-reported metadata. They do not make the kernel schedule individual HTTP/2, HTTP/3, QUIC, or persistent TCP requests separately.
- QUIC content is not inspected. One multiplexed connection may carry requests with different application urgency while kernel controls remain connection/socket/class scoped.
- UDP per-message urgency is opt-in and quota-limited. The native SDK and additive C ABI sender require the daemon grant and a marked authorized socket, use supported ancillary data, and do not promise deadline delivery. Batch C calls have a 4,096-message bound; the C++ send_batch_into path can reuse caller-owned result storage.
- Socket priority and DSCP are metadata for Linux qdisc/classification. They are not bandwidth or latency guarantees.
- Deadline admission is an estimate, not a reservation.

## Restart and failure behavior

The SDK event queue is fixed at 512 records and the IPC event batch is at most 32. Request completion uses a non-blocking bounded enqueue; records can be counted as dropped if the queue is full, the process was forked, or the daemon is offline. The worker reconnects and re-registers after a process-bound lease is lost. Events belonging to an older registration generation are discarded instead of replayed into a new lease. The SDK does not replay request or business operations.

Setup actions such as FD association, effective policy inspection, transport declaration, and socket sender construction can block for the bounded local IPC timeout. Do not place them in an application request hot path.
