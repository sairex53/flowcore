# Known limitations and retained negative evidence

## Performance

The latest campaign is documented in [performance evidence](PERFORMANCE_EVIDENCE.md).
The historical comparisons below remain useful as negative or
inconclusive findings within their original experimental scopes.

| Scenario | Evidence to retain |
| --- | --- |
| Hybrid V3 capacity transition | The ten-block study reported a 43.54% median paired total-goodput loss against static HTB. [Hybrid V3 results](HYBRID_V3_RESULTS.md) |
| Legacy socket pacing with small cohorts | Total goodput fell by 16.25%. Guarded pacing avoided most of that loss in its held-out comparison, with a nonbinding cap in those trials. [Pacing regression results](PACING_REGRESSION_RESULTS.md) |
| Earlier ECN-aware socket controller | Goodput fell by 15.35% in the comparison with static QoS. The integrated arm's P99 remained inconclusive. [Integrated benchmarks](NEXTGEN_BENCHMARKS.md) |
| Multi-objective and predictive control | Short initial studies show tradeoffs or insufficient evidence. Static can win, and an unchanged kernel policy cannot establish an adaptation effect. [Optimizer results](SLO_OPTIMIZATION_RESULTS.md) · [Predictive results](PREDICTIVE_RESULTS.md) |
| UDP per-message priority | Classification and starvation protection are scoped to the tested workloads and marked socket. CAKE DiffServ remains a strong comparator. [UDP results](MESSAGE_SCHEDULING_RESULTS.md) |
| Stable MPTCP path | Earlier stable trials recorded a higher median P99 for path-aware than fixed preference with zero controller changes. During impairment, P99 benefit against the default MPTCP scheduler remained inconclusive. [Path results](PATH_ADAPTATION_RESULTS.md) |
| Real application stack | Application service, database processing, queueing and transport consume different parts of request latency. Early application observations do not establish a workload-independent advantage. [Application results](APPLICATION_PERFORMANCE_RESULTS.md) |

## Correctness, security and deployment

- The read-only monitor is the packaged default. Active shaping,
  optional pacing, per-message classification, BPF/TCX and MPTCP
  experiments require explicit opt-in, scoped resources and
  capability checks.
- HTB, BPF and socket writes have different atomicity boundaries.
  Kernel readback is point-in-time evidence. Competing privileged
  namespace writers can race it. Reboot and power-loss recovery and
  long-duration production soak remain open.
- The research runner trusts the invoking user and local tree. Some
  result directories are writable by the invoking UID while the
  privileged runner produces artifacts. The live lab is unsuitable
  for delegation to an untrusted user.
- Requests and flow telemetry have bounded state and can drop or
  truncate. P99 over successes excludes unsuccessful requests.
  Socket metadata and scheduler timestamps cannot establish wire
  latency or per-stream packet enforcement for encrypted protocols.
- Full physical-NIC behavior, general host QoS, native Kubernetes
  integration and production multi-tenant isolation are unsupported
  in the candidate scope. The experimental SDK does not promise a
  stable C++ ABI.
- Dependency advisory checks are scoped to identified versions.
  Fedora advisory/backport review, container SBOM and signed package
  provenance remain release gates. The installed helper is not a
  general-purpose host traffic-control actuator.

[Runtime safety](RUNTIME_SAFETY.md) · [Threat model](SECURITY.md)
· [1.0 readiness](FLOWCORE_1_0_READINESS.md)
