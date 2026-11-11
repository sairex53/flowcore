# SDK Performance

## Existing evidence

The latest committed per-message UDP experiment is summarized in MESSAGE_SCHEDULING_RESULTS.md. For its two-second workload, median worker CPU was 0.211 s vs 0.241 s (IPv4) and 0.213 s vs 0.244 s (IPv6) for the SDK path vs raw prototype. Paired effects were +0.028 s [0.008, 0.051] and +0.031 s [0.015, 0.064]. Submission P50 also increased from 7.8 to 16.9 µs and from 8.3 to 17.6 µs.

Those measurements include the test harness and receiver in worker CPU; the separate daemon CPU was excluded. They show overhead, but do not isolate its source. The prior report does not attribute the increase specifically to the SDK telemetry worker.

## Changes in this revision

- The UDP sender took a mutex for every message-ID selection, even when the caller supplied an ID. Caller-supplied IDs now bypass that lock. Automatically generated 32-bit timestamp IDs use a relaxed atomic counter.
- send_batch_into() lets a caller reuse result storage rather than allocate a vector for every send_batch() call. The existing allocating convenience method remains unchanged. The C ABI exposes a bounded UDP batch operation. It validates the entire batch first, then converts and submits fixed 64-message blocks using bounded stack storage; it does not allocate per message or allocate a temporary vector per call.
- Per-message send still builds bounded ancillary metadata and calls sendmmsg; urgency token accounting remains synchronized across concurrent senders. Those costs remain.

These are source-confirmed reductions in locking/allocation work. This revision has not yet been measured against the old implementation in an otherwise identical paired experiment, so no CPU or latency improvement is claimed.

## Runtime behavior

Request reporting uses a fixed 512-entry queue, a 32-event batch, one persistent worker thread and at most a one-millisecond coalescing wait. It does not open one IPC connection per request on the session-socket path. Queue saturation or daemon outage increments drops instead of blocking application completion.

The v2 UDP sender supports up to 4,096 input messages per batch and submits groups of at most 64 messages. The reusable-output API validates that the result span exactly matches the input span.

## Reproduction and next measurement

Build the SDK and focused tests with CMake, then run the intent_v2, intent_session, intent_parser, sdk_lifetime and udp_intent tests through CTest.

For a defensible CPU comparison, rerun scripts/udp_intent_lab.py on a host with the required namespace, qdisc and socket-mark capabilities. Use the previous experiment as context, not as a paired control: the stored run design does not contain this new revision. Capture worker and daemon CPU separately, warm up both arms, use identical datagrams and seeds, and preserve invalid runs. The unprivileged udp_intent_live CTest may skip when those kernel privileges are unavailable.

Do not infer that the C++ mutex/atomic fast path accounts for the full prior CPU delta without that measurement.
