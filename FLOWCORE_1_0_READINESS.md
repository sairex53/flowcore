# FLOWCORE 1.0 readiness

**Decision: retain the experimental 0.2.0 scope.** The code is an
opt-in Linux networking laboratory, read-only monitor and application
intent SDK. The supported unit of privileged experimentation is a
disposable namespace with explicit ownership. Project licensing is
Apache-2.0. The optional eBPF sources keep their declared dual-license
terms. [License](LICENSE) · [BPF license](LICENSE.GPL-2.0)

| Gate | Mandatory exit criterion | Current scope |
| --- | --- | --- |
| Correctness | Rebuild and exercise each advertised path on its supported environment. Refuse unsupported or conflicting capabilities. | Source monitor, isolated helper and scoped namespace features have execution evidence. Broader integrations remain partial. |
| Security | Verify the installed and active privilege boundaries, package provenance and dependency checks. Close all unaccepted high-severity findings. | Local and isolated source controls tested. Installed active operation and wider trust boundaries still need coverage. |
| Reliability | Test interruption, reconciliation and cleanup, then complete a representative long soak and genuine restart campaign. | Scoped crash and rollback passed. Genuine power loss and prolonged production use are unverified. |
| Performance evidence | Publish all held-out paired outcomes with explicit failure denominators and uncertainty. Claim improvement only inside the demonstrated envelope. | See the fresh comparison and the retained negative evidence. |
| API stability | Freeze supported CLI, C ABI and wire surfaces, test upgrade and rollback with previous clients, and publish a compatibility window. | Current SDK consumers were built and run. A 1.0 C++ ABI and cross-version service contract have not been frozen. |
| Reproducibility | Archive the exact source and tools, and publish a privacy-reviewed raw evidence bundle with independent rerun instructions. | Frozen plans, raw outputs and build identities are retained locally. |
| Portability | Run the advertised workload and failure scope on each architecture and toolchain. Test physical NICs before making wire-performance claims. | Fedora ARM64 QEMU and Ubuntu x86_64 VM evidence is scoped separately. |
| Packaging | Verify installation and removal, dependency-resolved upgrade, preserved config/state, and signed provenance. | Local artifacts and bounded installation evidence. Full signing and package transaction gates remain separate. |
| Documentation | Match README, quickstart, install/API/security and benchmark references to the actual supported surface. | Current documentation uses the maturity matrix and explicit limits. |

The machine-readable [publication gate](config/release-gates.json)
remains fail-closed. The workflow builds and checks a source candidate
but cannot tag or publish a stable release. Local artifact checks do
not substitute for signed distribution.

Read the [feature matrix](FEATURE_MATRIX.md), [execution and audit
evidence](INDEPENDENT_VALIDATION.md), [performance evidence](PERFORMANCE_EVIDENCE.md),
[known limitations](KNOWN_LIMITATIONS.md) and [research roadmap](NEXT_RESEARCH_ROADMAP.md).

## Candidate disposition

The final campaigns retain 173 measured trials. Several exceeded
prespecified failure thresholds, and earlier invalid or interrupted
campaigns remain in the evidence ledger. Shaped fq_codel led the
optimizer on conditional P99 and failures in the mixed collapse
profile. The held-out Hybrid V3 and application comparisons observed
zero policy actions. The scoped UDP latency result carried a CPU
cost, and the MPTCP comparison against Linux defaults remained
inconclusive. These outcomes support experimental evaluation.

The publication check returns eight blocking gates. Source builds,
namespace tests and the staged monitor have execution evidence on
VMs. The unsigned RPM passed extracted-runtime tests and isolated
file transactions. The remaining release work includes dependency-
resolved signed distribution, installed active security testing,
restart and soak coverage, an API compatibility contract, portable
hardware evidence and publication of a privacy-reviewed raw bundle.
