# FLOWCORE v0.2.0 engineering audit

The target was Fedora 45 AArch64, kernel 7.2.11-300.fc45.aarch64.
The starting tree had no Git metadata. Commit `7a3cba7` records the
existing sources as the baseline. The repository was created private.
Build output, local tooling, runtime state and raw evidence stay outside Git.

## Execution and ownership map

| Path | Responsibility and evidence |
|---|---|
| `src/`, `include/`, `examples/` | C++ launcher, native RTM_GETLINK, original two-class controller and SDK. The SDK sends registration and timed application requests over the daemon socket with SO_PEERCRED and a 250 ms deadline. |
| `scripts/daemon.py`, `ipc_security.py`, `service_lab.py` | Daemon config, locks and stale workspace cleanup precede readiness. Single-owner IPC admits a policy, creates and checks a candidate topology, then publishes it. The packaged monitor disables mutation. |
| `adaptive.py`, `adaptive_tc.py`, `intents.py`, `platform_policy.py` | Managed controller owns four HTB classes and explicit admission. Updates decrease reservations before increases, verify kernel readback and refuse drift. Persisted intent and profile state do not replay live actuation at startup. |
| `network_state.py`, `tcp_diag.py`, `observability.py`, `bpf/` | Interface snapshots, namespace-bound INET_DIAG and bounded observation windows. The optional native BPF collector supplies events while TCP socket snapshots provide independent sampling. Application latency comes from the SDK or benchmark workload. |
| `benchmark.py`, `workload.py`, `adaptive_experiment.py`, `research_*.py` | Distinct legacy, managed and research protocols. Sender egress owns the shaper. Receiver egress netem affects reverse responses and ACKs. Goodput uses receive counters. The research comparator excludes successful-only P99 from pairs with failed requests. |
| `multipath.py`, `containers.py` | Private MPTCP experiment and opt-in Podman cgroup-v2 classification. Both have independent lab cleanup and evidence files. |
| `CMakeLists.txt`, `packaging/`, `.github/workflows/` | C++/Python build, optional BPF, staged install, unsigned local RPM workflow and unprivileged CI. The privileged workflow uses a designated manual runner. |

The controllers overlap in feedback-driven HTB policy and command execution.
The C++ one is a two-class experimental control with different feedback,
class IDs and rollback behavior. The managed controller owns service
admission, validation, readback, shadow decisions and four classes.
Removing the C++ path would break the legacy experiment. Shared namespace
ownership was consolidated without merging experimental semantics.

## Findings and verification

[AUDIT_FIXES.md](AUDIT_FIXES.md) records corrected findings with severity,
cause, evidence and reproduction. [REMAINING_RISKS.md](REMAINING_RISKS.md)
records open findings and their validation boundaries.

Previous reports were checked against the source and the stored evidence.
`ADAPTIVE_RESULTS.md` reports worse P99 for active than tuned static in
its larger comparison. Its later smoke has insufficient repetitions to
reverse that result. `PLATFORM_RESULTS.md` and the clean/chaos research
artifacts similarly give no general latency advantage. The audit uses
short lab runs for functional verification. It makes no performance claim.

The secret check covered Git-tracked files using credential-prefix,
private-key and access-key patterns, plus inspection of password/token
assignment candidates. The matching token fields in adaptive integration
are runtime instance identifiers. No credential was found by these checks
and no Git history was rewritten. SSH and GitHub login state are outside
the repository. This was a bounded scan and code audit, not an exhaustive
proof that every branch or kernel failure is covered.
