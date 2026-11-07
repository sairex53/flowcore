# Independently reproduced feature scope

Review target: Fedora 45 ARM64 QEMU VM. Reviewer: repository-maintainer agent; external third-party certification was not performed.

Labels: STABLE means a published supported contract with its scoped release gates passed; EXPERIMENTAL means working but evaluation-limited; PARTIAL means material advertised objectives/integration remain incomplete; UNVERIFIED means this review has no adequate execution evidence; UNSUPPORTED means absent/out of scope. No umbrella feature is classified STABLE in this snapshot.

| Feature | Status | Fresh evidence | Limits |
|---|---|---|---|
| Read-only daemon and lifecycle | EXPERIMENTAL | CTest/live IPC; installed validation repeated in release review | No genuine reboot/power-cut or production soak |
| CLI and strict configuration | EXPERIMENTAL | Clean build, parser/unit/live tests; root source-config regression fixed | Management responses are local-daemon contracts; host activation unsupported |
| Privileged helper | EXPERIMENTAL | Non-root helper, CAP_NET_ADMIN-only private namespace, seccomp, peer ACL, revision races, SIGKILL, foreign-object rejection | Installed active-helper campaign incomplete; source lab daemon still privileged |
| C++23/C ABI intent SDK | PARTIAL | Live TCP applications, process-bound leases, renewal/restart, descriptor/stream metadata; selected native sanitizer tests | Best-effort drops; orphan starts until dissociation/expiry; flush is not delivery |
| Telemetry/metrics | EXPERIMENTAL | Request ledgers, TC/link readback, INET_DIAG, histograms, loss counters, CPU/RSS | Polling misses short events; bounded samples can be truncated |
| eBPF observer | EXPERIMENTAL | Fresh live observer run and mode-overhead comparison | Short single-repetition overhead; not zero cost; installed RPM disables BPF |
| Per-flow intelligence | PARTIAL | 1,200 wire-flow cookies/exported rows; IPv4/IPv6/TCP/UDP/NAT; interrupted export refusal; 1,070 kernel fixtures separately labelled | 4,096 map bound; retained rows are not complete traffic coverage; no 10k-classification proof |
| Adaptive controller | PARTIAL | 10 held-out paired runs per strategy against pilot-selected static QoS; separate 10-pair same-allocation follow-up | Zero dynamic changes in both series; same-allocation regression-margin gate passed, superiority unproven |
| Predictive controller | EXPERIMENTAL | Held-out predictive/shadow comparisons and causal forecast histories | No demonstrated predictive/action benefit; short-window/cold-start limits |
| Constrained policy optimization | EXPERIMENTAL | Bounded native solver, protected shares, verified live HTB transfer | Active only in owned HTB labs. Static40 led the initial paired comparison. |
| Causal SLO diagnostics | EXPERIMENTAL | Bounded evidence, real fixture attribution, export and offline replay | Local observations and application reports support candidates. Receiver and controller attribution need live diagnostic accuracy studies. |
| MPTCP lab/path policies | EXPERIMENTAL | Fresh TCP/default-MPTCP/static/policy failover matrix; negotiation/subflows verified; host state preserved | One repetition; endpoint backup controls, not custom scheduling or TCP migration |
| Podman/cgroup integration | PARTIAL | Fresh rootful container classification and stale-rule cleanup; bounded owner registry | Multiple-container congestion/restart/rootless enforcement unverified |
| Workload discovery/policies | PARTIAL | Fresh two-cgroup UDP classifier/counter/conflict/cleanup fixture; PID/cgroup/namespace identity tests | No automatic host-network management or safe restart rebinding |
| Research/chaos lab | EXPERIMENTAL | Fresh chaos/replay/UDP/report/minimization/cancellation/cleanup integration; frozen evaluation | Deterministic configuration replay, not deterministic packet scheduling |
| Cooperative QUIC | EXPERIMENTAL | Fresh aioquic handshakes/echo under five impairments, C++ SDK stream reporting | No HTTP/3 implementation, transparent migration or encrypted content inference |
| Native histogram backend | EXPERIMENTAL | Differential and concurrent native tests plus ASan/UBSan | Opt-in; previous CPU gain is not independently re-established as a runtime speedup here |
| Packaging | PARTIAL | Fresh RPM/SRPM and extracted installed checks are recorded in RELEASE_READINESS.md | Unsigned; license pending; host upgrade/erase and active-helper installation remain gaps |

## Scale

Fresh fixture: 100, 1,000 and 10,000 simultaneously established TCP connections, zero failed echoes and FD count 7→7 after each case. At 10,000, process peak RSS was 131,084 KiB and P99 echo time 8.187 ms. This is a roughly 1.5-second held-connection fixture with at most 128 concurrent requests. It does not certify 10k SDK clients, services, BPF entries or long-duration sessions. Passive INET_DIAG correctly indicated truncation above its 128-entry limit.

The per-flow wire test had 1,200 exported cookies. Its 1,070 BPF_PROG_TEST_RUN cases include 1,000 malformed fixtures and zero wire packets; they must not be added to the wire-flow count.

## Environments

| Environment | Status |
|---|---|
| Fedora 45 ARM64 QEMU VM | Fresh root/network/scale/performance validation executed |
| GitHub-hosted x86_64 Linux | Fresh build/unit/sanitizer/staged-install + SDK-consumer CI; native helper and 10k held connections passed; full HTB lab UNSUPPORTED with tc 6.1 text class readback |
| Physical Linux system | UNVERIFIED: none available in this review |

Host-wide adaptive enforcement, native Kubernetes integration, arbitrary container coverage, transparent TCP→QUIC migration and per-request kernel scheduling inside encrypted multiplexed connections are UNSUPPORTED.
