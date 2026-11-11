# Transport Validation Results

## Current run

Repository commit before these changes: 71d411f. Environment: Fedora 45 ARM64, Linux 7.2.11-300.fc45.aarch64. Effective process capabilities were empty.

Read-only UDP capability probes succeeded for IPv4 and IPv6 TOS/TCLASS ancillary data, SO_PRIORITY, sendmmsg, software TX timestamping and SO_TXTIME. Loaded module entries reported CAKE, ETF and TAPRIO; fq_codel was not present as a module entry. These probes do not show which qdisc is active or prove a scheduling outcome. The host MPTCP namespace reported enabled MPTCP sockets, kernel path manager and default scheduler. Netlink endpoint/limit dumps worked; event monitor access was denied. A pre-existing host endpoint was observed and left unchanged.

The new unprivileged udp_ancillary_kernel CTest passed. It sent mixed default/EF/background metadata in a single sendmmsg batch over IPv4 and IPv6 loopback, received the expected per-datagram TOS/TCLASS values, and verified the socket’s configured DSCP and SO_PRIORITY defaults stayed unchanged.

The UDP unit suite, MPTCP policy suite and path adaptation suite passed. The privileged native fixture contains a lease-shared quota-counter assertion but was skipped here. The stability/recovery evidence and three-window path-return guard were exercised by unprivileged tests. These are executable correctness tests. They are not comparative performance experiments.

The privileged udp_intent_live test was not run successfully: it requires the operator-authorized SO_MARK descriptor and lab privileges, neither of which is available. The isolated MPTCP lab also could not run without namespace/network capabilities. No host network settings were modified.

## Historical experiments, not rerun

The detailed UDP VM matrix and MPTCP/path-adaptation experiments are retained in MESSAGE_SCHEDULING_RESULTS.md, MULTIPATH_RESULTS.md and PATH_ADAPTATION_RESULTS.md. Their results came from earlier QEMU Fedora ARM64 runs and must not be represented as measurements of the current optimized revision.

The UDP evidence shows low failure counts and high deadline attainment for SDK and raw per-message modes under one shaped workload. It also shows higher SDK worker CPU and submission latency than the raw prototype. The current source has fewer locks/temporary batch allocations, but that effect was not measured in paired runs.

MPTCP evidence shows actual multi-subflow operation and safe endpoint cleanup in earlier owned namespace trials. It does not establish that the path-aware policy beats Linux default generally. Earlier P99 differences versus Linux default were inconclusive, and the stable sample count was too small to support a general effect. This revision’s additional stability/recovery ranking and failback hold-down have not been validated under a real path fault.

## Classification

- Verified now: this kernel accepts IPv4/IPv6 per-message TOS/TCLASS and SO_PRIORITY control data on loopback, and sendmmsg preserves mixed per-message markings.
- Verified by unit tests: path evidence records only sample-bounded recovery; route changes discard unrelated recovery history; returning to a recovered MPTCP path requires three distinct observations. The privileged UDP test adds a lease-shared quota counter assertion but did not run here because its marked-socket fixture is unavailable. The SDK quota remains per registration lease, not aggregate across same-name processes.
- Historically measured: the prior one-VM UDP QoS matrix and prior owned MPTCP fault labs, with exact scopes in their reports.
- Not measured in this revision: UDP CPU/P99 change, GSO behavior, zero-copy benefit, per-message qdisc scheduling on this host, MPTCP failover benefit, and updated path-controller performance.
- Not available here: physical NIC validation, unrestricted MPTCP event monitoring, and privileged namespace execution.

## Reproduction

Build and run unprivileged checks:

    cmake -S . -B build -G Ninja
    cmake --build build -j2
    ctest --test-dir build -R 'udp_ancillary_kernel|udp_intent_unit|multipath_unit|path_adaptation_unit' --output-on-failure

Run the privileged UDP or MPTCP labs only in an explicitly owned disposable Linux environment following MESSAGE_SCHEDULING_RESULTS.md and MULTIPATH_ENGINE.md. Keep static qdisc/CAKE baselines, random seeds, complete denominators, failure counts, deadline attainment, background goodput, fairness, and per-process CPU in the same result bundle.
