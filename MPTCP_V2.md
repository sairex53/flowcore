# MPTCP Path-Aware Control V2

Status: experimental namespace research. This is a bounded endpoint path-preference controller, not a replacement MPTCP scheduler and not a daemon-managed host actuator.

## Installed-kernel audit

The target host reported Linux 7.2.11-300.fc45.aarch64, MPTCP enabled, MPTCP socket creation available, the in-kernel path manager selected, and only the default packet scheduler listed. Endpoint and limit netlink dumps were available. The MPTCP event monitor was denied by the current privilege boundary. A userspace path manager was listed by the kernel, but an mptcpd process was not installed.

A pre-existing endpoint was visible in the current host namespace. Its address and interface are intentionally omitted here. No endpoint, sysctl, route, interface, qdisc, or host MPTCP setting was changed during this audit. The isolated lab could not run because this session has no effective network-administration or namespace capabilities. A capability report is not evidence of two negotiated subflows.

The Linux MPTCP documentation distinguishes path management, which creates/removes subflows and announces addresses, from the packet scheduler, which selects subflows for packets. FLOWCORE changes only owned endpoint backup preferences through the kernel path-manager API in disposable namespaces. It does not select packet placement, implement packet scheduling, or migrate arbitrary TCP connections.
Reference: https://docs.kernel.org/networking/mptcp.html

## Path evidence and policy updates

The existing path controller requires fresh routes, available interfaces and positively observed MPTCP subflows. It estimates serialization delay from the configured/read-back rate and the application-declared payload size; it does not call configured capacity measured throughput. Probe success and RTT remain probe evidence, not packet-loss or end-to-end application facts.

The updated evidence window now records probe success ratio, RTT spread, and a sampled recovery interval. Recovery is measured from the first sampled-down frame to the first later successful probe after the path is available; it is a sampling-bounded observation, not exact kernel failure-detection time. A route identity change clears this recovery history. Path rankings use failure ratio, stability and RTT spread as deterministic tie-breaks after the selected latency, reliability or throughput objective. Measured recovery is a tie-break only; unknown recovery remains marked as unknown.

Returning to a recovered preferred path now requires three distinct evidence windows. Initial failure intervention still uses the existing bounded policy, dwell and ownership checks. Every endpoint update is preflighted, read back, and compensated on partial failure. If compensation is ambiguous, the experiment aborts and reports the state rather than continuing.

No endpoint update is made by this policy outside the owned namespace lab. All-active Linux default leaves packet selection to the kernel scheduler. Backup preference experiments are not equivalent to running a custom MPTCP scheduler.

## Existing measured evidence

MULTIPATH_RESULTS.md and PATH_ADAPTATION_RESULTS.md preserve earlier Fedora 45 ARM64 VM measurements. Those historical trials verified negotiated multipath, subflow counts, failures, recovery and endpoint cleanup in their controlled topologies. They also found large tail latency under outages and no demonstrated general performance advantage. The path-aware median P99 comparison against Linux default was inconclusive; stable three-block results were also insufficient to establish superiority. This revision has not rerun those paired experiments.

## Validation plan

Run each fault independently before combining them: primary link failure, capacity reduction, RTT increase, loss increase, restoration, and asymmetric path conditions. Capture policy generation, endpoint state before/after, MPTCP_INFO and TCP_IS_MPTCP fallback state, per-subflow TCP_INFO, application completions/deadlines/failures, and recovery-window evidence. Compare default Linux, static endpoint preference, prior path policy and the updated controller with identical offered work and randomized paired order. A trial without a verified MPTCP socket and at least two observed subflows is invalid for a multipath claim.

The current three-path lab supports isolated link/capacity impairments and endpoint rollback. Loss and RTT are controlled at both path directions. The present host cannot execute the privileged lab. No change to the host endpoint configuration is permitted as part of this validation. Use a disposable Fedora environment or explicitly authorized lab runner.

## Limitations

The kernel exposes only the default scheduler on this host, so FLOWCORE cannot compare alternate kernel packet schedulers here. The event monitor is unavailable to the current unprivileged process. The current path controller samples probe data intermittently; it cannot infer arbitrary Internet path bottlenecks or guarantee seamless recovery. Per-path byte attribution, exact failure-detection time, application processing decomposition and hard deadlines remain outside the supported evidence.
