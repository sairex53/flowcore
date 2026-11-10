# Release candidate feature maturity

**Stable** requires a published supported contract and passed release gates.
**Experimental** has working scoped execution evidence.
**Partial** has working pieces with material integration or contract gaps.
**Unverified** has insufficient evidence for the stated environment.
**Unsupported** is outside the implemented scope. No feature is designated
stable for 1.0 in this candidate.

| Capability | Maturity | Code and supporting evidence | Boundary |
| --- | --- | --- | --- |
| Read-only daemon and CLI | Experimental | scripts/daemon.py, network_state.py, service and staged install tests | Local inventory and metrics, capability-dependent fields |
| Private privileged helper | Experimental | scripts/network_helper.py, helper_netlink.py, live Fedora and x86 peer/ACL/crash tests | One isolated namespace, fixed typed operations |
| HTB and runtime safety | Experimental | scripts/adaptive_tc.py and runtime_safety.py, ownership/failure tests on both VMs | One owned writer and point-in-time readback |
| Adaptive and Hybrid V3 | Experimental | scripts/adaptive.py, hybrid_runtime.py, native pacing and mixed workload evidence | Lab-only active policy, general advantage unproven |
| Multi-objective optimizer and autotune | Experimental | src/intelligence.cpp, policy_optimizer.py, live class transfer and solver tests | Bounded objectives and research-only action selection |
| Predictive and online strategies | Partial | scripts/predictive.py and strategy_engine.py, forecast, shadow and rollback tests | Missing general prediction and live strategy benefit evidence |
| Application intents and C ABI | Partial | src/intent_v2.cpp, intent_c.cpp, scripts/intent_session.py, installed SDK consumer and lifecycle tests | Best-effort events, bounded lease and application cooperation |
| Deadline and distributed SLO | Experimental | Native feasibility and coordinated request tests, isolated deadline workloads | Advisory admission only, no arbitrary load shedding |
| TCP socket pacing and ECN feedback | Experimental | Native socket readback and capability/guardrail tests | Owned TCP sockets, FQ and signal availability vary |
| UDP per-message intent | Experimental | src/udp_intent.cpp, authorized SDK and class counter experiments | Cooperating marked sockets, bounded priority, no arbitrary datagram deadline guarantee |
| BPF TCP observer and TCX flow intelligence | Partial | Native libbpf paths, wire-flow/churn fixtures and detach checks | Bounded map and export coverage, optional source build |
| Bottleneck and directional QoS | Experimental | bottleneck diagnostics and owned IFB fixtures | Local observation and advisory where causality/authority is absent |
| MPTCP path adaptation | Experimental | scripts/multipath.py and path_adaptation.py, negotiated subflows and failover | Backup preference via Linux PM, no transparent migration |
| Workload and Podman policy | Partial | Workload identity and cgroup tests, scoped Podman experiments | Rootless and multi-tenant policy coverage incomplete |
| HTTP, PostgreSQL, gRPC and HTTP/3 labs | Experimental | examples/real_apps, real request ledgers and dependency-scoped tests | Test services and local trust model, no transport-independent performance claim |
| FLOWID logical sessions | Experimental | src/flowid.cpp and protocol/recovery fixtures | Cooperating applications, explicit transport and failure semantics |
| Incident recorder and causal diagnosis | Experimental | scripts/incident_runtime.py and diagnostic/replay fixtures | Local evidence and competing hypotheses, no causal proof from correlation |
| Dashboard, Fabric and fleet rollout | Partial | Monitor API, mTLS and staged policy modules, functional fixtures | Separate opt-in prototypes, sustained production scale and fleet security unverified |
| Research Lab and measurement integrity | Experimental | scripts/research_runner.py, measurement.py, raw manifests and replay tests | Deterministic inputs rather than deterministic packet scheduling |
| External multi-host coordination | Partial | multi_host_agent.py and documented lab configurations | No fresh cross-host physical performance series |
| RPM, staged CMake and source distribution | Partial | packaging/ and install tests, digest and source-archive checks | Signing, active installed deployment and dependency-resolved lifecycle gates open |
| Physical hardware behavior and production soak | Unverified | ARM64 and x86_64 VM results only | NIC, driver/offload and long-duration behavior need separate execution |
| Host-wide automatic policy, arbitrary TCP migration, encrypted stream packet priority and Kubernetes integration | Unsupported | No supported actuation path or release contract | Lab and explicitly cooperating applications only |

See [fresh validation](INDEPENDENT_VALIDATION.md), [performance evidence](PERFORMANCE_EVIDENCE.md),
and [known limitations](KNOWN_LIMITATIONS.md). The historical release
review and earlier raw-evidence references remain in [release readiness](RELEASE_READINESS.md).
