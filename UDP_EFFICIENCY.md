# UDP Efficiency Audit

Status: experimental, with historical isolated-lab evidence and current unprivileged kernel metadata checks. This document does not claim a new performance win.

## Findings

The per-message sender carries urgency in per-datagram ancillary data. A single UDP socket can send standard, urgent, and background packets without permanently changing that socket’s DSCP or SO_PRIORITY defaults. The previous controlled namespace matrix verified this with actual IPv4 and IPv6 loopback receiver markings, plus owned qdisc counter readback. Its full latency and scheduling results are recorded in MESSAGE_SCHEDULING_RESULTS.md.

The C++ sender now has a reusable send_batch_into interface. It lets callers reuse result storage and continues to submit at most 64 messages per sendmmsg call. The C ABI batch path validates the complete input before transmission, then converts fixed 64-message blocks on bounded stack storage. This avoids temporary per-call metadata and result vectors while preserving a no-partial-send guarantee for malformed input. Kernel short-send behavior still returns per-message status.

Automatic timestamp IDs use a relaxed atomic counter, avoiding the former per-message ID mutex. Caller-provided IDs remain supported. The urgency token bucket remains synchronized because all sender wrappers under one service lease share its rate and burst budget.

Each registration lease receives an operator-authorized grant with a priority ceiling, UID and cgroup identity, SO_MARK requirement, rate, burst, maximum deadline horizon, and expiry. The SDK token bucket and rejection counter are shared by sender wrappers under that one lease. They are not currently aggregated across separate processes that register the same service name. Exceeding a lease’s cooperative SDK token budget either rejects the urgent message or demotes it according to the message’s expiration policy. The rejection counter is an application-side signal; it is not a kernel-enforced abuse metric. A marked socket can be used outside the SDK by its owning process, so a matching aggregate qdisc policer is required when the operator needs cross-process enforcement. The repository has validated such a policer in its owned namespace lab, but the daemon does not install one on host interfaces. Background and standard messages do not consume urgent tokens and keep their own class metadata.

## Kernel mechanisms and correctness

The SDK applies message-specific IPv4 IP_TOS or IPv6 IPV6_TCLASS plus SO_PRIORITY ancillary data. Traffic class values are advisory metadata. Actual queue selection and scheduling require a matching tc filter/qdisc configuration. The existing lab uses owned HTB/fq_codel rules and readback. The historical matrix also compared CAKE DiffServ4 with EF marking and equivalent bulk load; its intervals are workload-specific and do not establish general superiority.

The new unprivileged udp_ancillary_kernel test sends mixed standard, EF and background-class datagrams with sendmmsg on IPv4 and IPv6 loopback. It checks receiver-observed TOS/TCLASS for each datagram and verifies the sender socket defaults remain unchanged. It proves ancillary metadata and batching semantics on this kernel. It does not prove class scheduling, fairness under congestion, physical egress markings, or a per-message latency benefit.

UDP_CORK and UDP_SEGMENT are rejected for an authorized message sender. Message boundaries and per-message metadata are not considered validated with UDP GSO. Timestamping is opt-in; software transmit timestamps use the realtime clock and are not wire-time evidence. Preserve one timestamp owner per socket because the sender consumes its error queue.

## Zero-copy assessment

No zero-copy path was added. The established workload uses 256-byte probes and 1200-byte routine and background datagrams. Linux’s MSG_ZEROCOPY documentation says page pinning and completion-notification overhead generally make the mechanism useful only for writes above roughly 10 KB. It also requires the application to retain and not modify buffers until completion, and a completion can report a deferred copy rather than actual copy avoidance. That lifetime and error-queue machinery is not justified for the validated small-message workload without a large-datagram benchmark showing a benefit. A future experiment may compare ordinary sendmmsg and MSG_ZEROCOPY for sustained messages above 10 KB, recording copied-completion rates and retained-buffer pressure. No hard real-time or delivery semantics follow from zero-copy completion.
Reference: https://docs.kernel.org/networking/msg_zerocopy.html

## Performance evidence and remaining work

The historical Fedora ARM64 VM matrix reports median successful-request P99 of 0.75 ms for the C++ SDK and 0.72 ms for the raw per-message prototype on IPv4, and 0.60 ms versus 0.49 ms on IPv6. Its paired P99 effects were inconclusive against the raw prototype. CAKE DiffServ was statistically indistinguishable on IPv4 and lower in that particular IPv6 workload. Failures, deadlines and background goodput are reported in MESSAGE_SCHEDULING_RESULTS.md.

The same historical experiment measured greater worker CPU and a higher submission P50 for the SDK than the raw prototype. The current revision removes a message-ID lock, offers caller-reused result storage and removes C ABI batch vectors, but no paired remeasurement of this revision was possible: the installed host has no effective capabilities, and the live SDK fixture requires an operator-authorized SO_MARK. Therefore CPU reduction and preservation of the prior latency distribution remain unproven for this revision.

To reproduce the privileged comparison, use the owned namespace lab documented in MESSAGE_SCHEDULING_RESULTS.md. Run the same IPv4 and IPv6 matrix with at least ten paired blocks, identical qdisc readback and marks, and capture daemon, SDK worker and receiver CPU separately. Keep all invalid and failed runs. A physical NIC and GSO-specific comparison remain unverified.
