# Changelog

## Unreleased experimental 0.2.0 snapshot

The build version is retained for existing API/package compatibility. No stable public-release claim is made.

- The fresh candidate review restores daemon interface inventory and fixes measurement integrity for version 3 and in-flight rejections.
- Release archives use committed sources only. Publication gates now cover reliability and API stability explicitly.
- The trusted x86 validation builds digest-pinned tc and runs the owned namespace service and recovery checks.
- The optional gRPC example pins a patched dependency set and rejects outdated runtimes.
- Added owner-scoped workload discovery, concurrent owned-lab cgroup classification and explicit lifecycle limitations.
- Extended isolated MPTCP experiments with endpoint backup policies and fallback confirmation.
- Added opt-in native histogram summaries, versioned ABI, differential/concurrency/sanitizer tests and documented CPU/RSS/IPC tradeoffs.
- Added cooperative transport/stream hints and an aioquic integration with actual namespace tests.
- Added durable state/checkpoints, conservative reload, operational diagnostics and read-only systemd profiles.
- Improved RPM/SRPM completeness and ELF dependency verification; host package transaction validation remains incomplete.
- Independent review fixed privileged source-checkout configuration ownership compatibility while retaining strict installed/state checks.
- Independent review corrected a live SDK test that incorrectly equated flush with delivery; orphan/dropped-event limitations remain explicit.
- New held-out static-pilot/evaluation plan, scale fixture and release gates preserve negative/inconclusive evidence.

Earlier measured values remain in their historical reports and must be interpreted with each report's conditions. No universal latency improvement or production readiness is claimed.

- Reject laboratory policy admission when required TC counter readback is unusable; preserve the previous policy and identify the failed TC object kind.
- Fix legacy non-research bulk workload startup, surface unexpected asynchronous worker failures, and allow independent UDP/TCP listeners to share a numeric port.
- Add real loopback regression coverage and document the x86 tc 6.1 class-JSON limitation.
