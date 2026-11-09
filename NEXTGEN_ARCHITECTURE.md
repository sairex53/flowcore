# Next-generation architecture

FLOWCORE has three operational boundaries. The regular daemon owns configuration, service authorization and observation. Privileged lab and helper code own kernel configuration within their admitted scope. Applications own their sockets, request budgets and cancellation decisions. The integrated experiment uses those boundaries and the existing Research Lab.

~~~text
Application request ledger and deadline telemetry       Owned bulk TCP sockets
                    |                                          |
           Daemon observation window             TCP_INFO, ECN and cap readback
                    |                                          |
            Same Research Lab sample -> scoped feedback -> native planner
                    |                                          |
            Verified HTB policy                   bounded socket transaction
                    |                                          |
            scheduled class rebase                 compensation and audit
~~~

## Inventory and authority

| Area | Code and existing capability | Completion and next-generation disposition |
|---|---|---|
| Daemon, CLI, deployment | `daemon.py`, `operations.py`, `ipc_security.py`, service units and RPM | Existing monitor and daemon ownership checks. Added read-only capability and report commands. Installed privileged operations retain existing gates. |
| TC, shaping, ingress, classification | `adaptive_tc.py`, `service_lab.py`, `directional_qos.py`, native netlink and helper | Verified class and flower tree at setup, exact HTB rate/ceil readback per admitted pacing frame. Scheduled capacity changes reuse owned HTB rebase. Dynamic filter/IFB changes remain separate. |
| Native and Python controllers | `adaptive.py`, `intelligence.cpp`, `predictive.cpp`, `optimizer.cpp`, `strategy_engine.py` | Python owns class lifecycle, native code scores and constrains actions. Threshold/SLO legacy paths overlap with queue/SLO/predictive variants. They remain selectable baselines and do not share adaptive authority with the integrated socket mode. |
| Socket pacing and ECN | `socket_pacing.cpp`, `pacing_controller.cpp`, `pacing_workload.py`, `ecn_feedback.py` | Integrated native planner admits actions from verified class policy, scoped ECN and application budget pressure. Socket handles enforce ownership/readback and compensate a failed group update. |
| SDK and deadlines | `intent_v2.cpp`, C ABI, `intent_v2.py`, `deadline.cpp`, `cooperative_slo.cpp` | Service registration, lease and request contract remain canonical. Research reports actual declared budgets. Integrated control changes bulk caps. Admission and backpressure remain application choices. |
| Telemetry, bottlenecks, diagnostics | `observability.py`, `tcp_diag.py`, `native_telemetry.py`, `bottleneck.py`, incident/dashboard modules | Reuse the sample RPC and rate/queue data. The lab verifies a known local bottleneck. Route, incident and receiver limitation inference remain advisory. |
| eBPF flow observation and classification | `bpf/`, `flow_engine.cpp`, `flow_engine.py`, `observe_bpf.cpp` | Opt-in TCX/BPF. A built object signals availability, and attachment and packet evidence establish verification. Integrated flow control uses no BPF dependency. |
| MPTCP, QUIC and FLOWID | `multipath.py`, `multipath_engine.py`, transport adapters, `flowid.cpp` | Separate transport labs. MPTCP endpoint updates require their own ownership proof and verified transport. QUIC and FLOWID offer cooperative session telemetry. |
| Workloads and containers | `platform_policy.py`, `workloads.py`, `containers.py` | Existing namespace and cgroup policy ownership. Rootless and shared namespace restrictions still apply. No integrated mode on a host interface. |
| Distributed control | `fabric_*.py`, `fleet_*.py`, cooperative RPC | Existing authenticated peer and staged rollout. Capability command can verify a configured coordinator status RPC. A Fabric lease grants no authority over this local socket cohort. |
| Experiment and release tools | `research_*.py`, `real_app_lab.py`, multi-host, autotuning, safety tests, release gate | Reused randomized blocks, raw ledgers, readback and paired analysis. Local research only. Historical claims retain the scope of their tests. |

## One intent contract

Use `flowcore::v2::Intent` for service priority, latency target, desired/min/max bandwidth, lifetime and reliability or isolation preferences. A registered service is scoped by the IPC credential, lease and authorized class mapping. `TransportHints` carries transport preference and multiplexing context. `deadline::Work` carries the monotonic deadline and request work. The same declared deadline travels in application reports and request ledgers. SLOs, desired throughput, transport preferences and deadlines are objectives. Installed class rates, ceilings, authorized ownership and lifetime are hard limits. The integrated lab uses registered class priorities and a workload deadline instead of a separate intent wire format.

## Conflict and cost inventory

- Independent class and pacing loops had distinct control intervals. A class-active flag stopped new socket decisions while an earlier cap could still bind. The integrated path excludes concurrent adaptive class authority and restores its initial cap when evidence or a generation changes.
- Daemon application reports previously omitted a supplied workload deadline unless the admission adapter was active. Reports now carry that budget even in advice-free trials. The same requested deadline is used across comparison arms.
- The daemon sample and pacing feed already exist. The integrated path adds a narrow scope record to that feed, without per-request admission IPC or an additional kernel read on the workload path. Native decision, application socket inspection, sample RPC and kernel update durations are measured separately.
- Traffic control changes are not atomic across operations. Existing rebase failure removes the disposable lab. Existing socket handles read their values back and restore only their own installed values. Access to the source research checkout and private output directory is part of the trust model. A privileged out-of-band administrator can invalidate the scope.
- Capability configuration, build artifacts, kernel readback, negotiated transport and application samples are distinct evidence. Socket TCP_INFO belongs to the sampled flow and cannot stand in for a service latency or a network-only deadline decomposition. Controller and observer costs depend on churn and sample coverage.

See [Integrated Engine](INTEGRATED_ENGINE.md) for the scoped action model and [Next-generation benchmarks](NEXTGEN_BENCHMARKS.md) for current evidence.
