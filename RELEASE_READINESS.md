# Release readiness

Decision: **experimental evaluation snapshot; public open-source release blocked**.

The owner selected Apache-2.0 after the independent review. The repository and RPM metadata record that choice. The internal version remains 0.2.0 for SDK and package compatibility. A proposed v0.2.0-rc.1 requires the gates below. [License](LICENSE)

## Gates

| Gate | Scope and requirement | Current decision |
|---|---|---|
| Functional | Fresh builds, unit/integration evidence, bounded cleanup and truthful capability reporting | PARTIAL: working research/monitoring scope; SDK and installed-active gaps |
| Security | Peer/privilege/input/cleanup checks and installed package review | PARTIAL: source helper tests pass; no external audit or complete installed active campaign |
| Performance | Held-out evaluation, strong shaped baselines, failures, uncertainty and negative results | Evidence recorded; adaptive superiority remains unproven |
| Reproducibility | Frozen seeds/selection, source/build identities, complete raw preservation, public evidence bundle | PARTIAL: local raw evidence preserved; publication/review of full bundle pending |
| Portability | Actual ARM64 and x86_64 execution within advertised scope | PARTIAL: actual x86 helper/scale passed; HTB lab unsupported with runner tc 6.1; physical Linux unavailable |
| Documentation | Current quickstart, feature matrix, API references, contribution/security guidance | PASS for current source documentation: local links, gate schema/tests and limitation review |
| Packaging | Dependency-resolved installed upgrade/erase, signed provenance and security-profile validation | PARTIAL: local unsigned RPM; isolated transaction/security-policy blocker remains |
| Licensing | Explicit owner selection and provenance/contribution policy | PASS for owner selection and published license |

Machine-readable gates are in config/release-gates.json. packaging/release-gate.py fails closed for publication. The manual candidate workflow builds/tests/checks a source candidate and uploads artifacts only if these gates pass. It has read-only repository permissions and never creates a stable release or tag.

## Reasons to evaluate FLOWCORE

Use the project as an isolated Linux networking laboratory, a read-only inventory/metrics daemon, and an opt-in application-intent research SDK. The fresh review demonstrates reproducible static-versus-adaptive comparisons and explicitly preserves cases where configured adaptive modes perform worse. It does not establish a production traffic-management advantage.

See FEATURE_MATRIX.md, BENCHMARK_REPORT.md and REVIEW_FINDINGS.md for evidence and limits. SECURITY.md describes private reporting. OSS_ROADMAP.md prioritizes unresolved engineering work.

## Actual review evidence

Failed runs remain identified; no noisy or inconvenient evaluation run was deleted. All paths below are relative to /home/elliot/projects/flowcore on the review VM. Raw/build artifacts are retained locally and excluded from commits.

| Check | Observed result | Evidence |
|---|---|---|
| ARM64 clean C++23 build, optional BPF enabled | PASS; configured compiler warnings enforced | build/review-final-build.log; build/review-final-build-2.log |
| Final unprivileged CTest | 30/30 PASS, 20.91 s, including real legacy loopback and admission regressions | build/review-final-ctest-2.log |
| ARM64 ASan/UBSan + leak detection | 3/3 PASS: telemetry_native, intent_parser, sdk_lifetime | build/review-asan-tests.log |
| C++ static analyzer | GCC -fanalyzer PASS on policy, intelligence/predictive, telemetry targets only | build/review-analyzer.log |
| Python/static/source hygiene | compileall PASS; Ruff 0.17.4 E9/F63/F7/F82 PASS; 183 source files checked for whitespace/newlines | build/review-final-static.log; build/review-final-style.log |
| Documentation/release gate | local Markdown links PASS; strict schema/license approval/symlink tests PASS; --enforce exits 1 as required | tests/release_gate_test.py; build/review-release-gate-enforced.json |
| Source archive | two archives from identical input/epoch had matching SHA-256 | build/review-archive-{1,2}.sha256; not independent reproducible binary builds |
| Tracked-file secret patterns | no candidates in bounded prefix/private-key/password-assignment scan; not exhaustive | build/review-secret-scan.json; no values printed |
| Native private helper | PASS: nonroot CAP_NET_ADMIN-only, seccomp, peer ACL, bounds, revision conflict, crash/ownership/host-state tests | build/review-helper.log |
| Service/network/lifecycle after fixes | PASS: IPv4/IPv6, four classes with traffic, link events, CAKE/netem, SIGKILL, cleanup/rollback | build/review-service-final.log; results/service-test-1794016021197991782 |
| Research lab after fixes | PASS: chaos/replay/UDP/report/minimization/cancellation/host state | build/review-research-final.log; results/research-integration-1794016070312450386 |
| Flow engine | PASS: 1,200 wire cookies exported; 1,070 kernel fixtures separately scoped | build/review-flow-v2.log; results/flow-v2-1794012859885053036 |
| Cgroup workloads | PASS: two authorized workloads, at least 100 classified packets each, conflict/stale nft cleanup | build/review-workload.log; results/workloads-1794015909711438153 |
| Podman | PASS within one rootful container scope | build/review-container.log; results/container-1794014688374116181 |
| MPTCP | PASS functional: two subflows confirmed, one failover repetition per strategy, host state unchanged; not performance superiority | build/review-mptcp.log; results/multipath-1794014619473428020 |
| QUIC | PASS functional: five scenarios, 600/600 measured transport echoes, confirmed aioquic handshake; SDK drops recorded | build/review-transport.log; results/transport-1794014693324543205 |
| Scale ARM64 | 100/1,000/10,000 held TCP connections PASS, zero failed echoes, FD 7→7 | results/release-scale-1794013003494455224; no soak/10k-client guarantee |
| Performance | 77 held-out/exploratory runs plus six disjoint pilot runs, then 20 newly preregistered same-allocation runs; failures/uncertainty preserved | BENCHMARK_REPORT.md; config/release-evidence.json |
| Staged CMake installation | PASS: zero capabilities, watchdog, metrics, parser/auth refusal, SDK consumer, staged file removal | build/review-installed.log |
| RPM/SRPM BPF-disabled build | PASS, including CTest 30/30 in RPM check | build/review-rpm-final.log |
| Latest extracted RPM runtime/lifecycle | PASS: reload/partial-config refusal, SIGKILL/stale socket, concurrent clients, rapid restart, symlink/checkpoint refusal, cleanup | build/review-rpm-runtime-final.log |
| Extracted payload in hardened systemd | PASS: DynamicUser, CapEff=0, read-only bind, invalid HUP reload, SIGKILL recovery, runtime cleanup | build/review-systemd.log; results/deployment-systemd-1794015671180026521.json |
| Isolated RPM transactions | BLOCKED at --initdb, Permission denied; no install/upgrade/erase reached | build/review-rpm-transaction.log; earlier SELinux AVC evidence in build/deploy-rpm-audit.log |

The hardened systemd test used the earlier 2352546 payload; the final ec32f2f RPM was separately rechecked with extracted lifecycle tests. No host package was installed, upgraded or removed to bypass the isolated database restriction. RPM digests/ELF dependencies/telemetry SONAME checks passed; signatures are absent. Scriptlet/dependency-resolved upgrade semantics and SELinux installation profile remain unverified.

The package is an unsigned local evaluation artifact with internal 0.2.0-2.fc45 version, not a published Fedora release. Immutable final review copies: build/review-package/ec32f2f/:

- Binary RPM SHA-256: 6b5a55b039aaba542fab7a3e07a9b457bf9314db7ba4ccb1d3ef5b91205f7291.
- Source RPM SHA-256: b7dfa2f526dcd15c3fb34181ab38dfb9f772a623db2edbc4c01cc3eba4f4a127.

The earlier extracted payload remains in build/review-package/payload. Build outputs are not public downloads or signed release provenance.

## Separate x86_64 execution

[CI at ec32f2f](https://github.com/sairex53/flowcore/actions/runs/42326082275) passed build, CTest 30/30, Python checks and selected ASan/UBSan/leak tests on Ubuntu 24.04 x86_64. This is actual execution, not cross-compilation.

[First privileged run](https://github.com/sairex53/flowcore/actions/runs/42325128325) and [diagnostic reproduction](https://github.com/sairex53/flowcore/actions/runs/42325363242) ran on kernel 7.0.0-1017-azure, Microsoft virtualization, iproute2 6.1. Both passed the native helper and held TCP scale fixtures up to 10,000; the full service test failed because tc class returned text despite -j. The failure led to diagnostic and pre-admission fixes; it was not relabelled PASS. No performance/BPF/Podman/RPM or physical x86 validation is claimed. Privileged jobs are manual, main-only, read-only GitHub permissions on disposable runners, not pull-request execution.

## Scope of independence and decision

This was a fresh maintainer-run reproduction using disjoint pilot/evaluation seeds, not an external independent audit. Prior results are historical scoped evidence; current claims are bounded by FEATURE_MATRIX.md and BENCHMARK_REPORT.md.

**Decision: retain the experimental source snapshot.** Functional, security, packaging, portability and reproducibility gates remain PARTIAL after the licensing decision.

Next priorities: recover from SDK terminal-event loss in long-lived sessions; isolate same-allocation controller action effects on new held-out scenarios; support/verify TC class readback across declared toolchains; complete installed active-helper, genuine reboot and soak campaigns; resolve dependency-aware RPM upgrade/erase under enforcing security policy; review/publish a raw-evidence bundle and sign licensed artifacts.

## Controlled same-allocation follow-up

Scenario frozen in 408e7df before execution, 10 paired static30/hybrid runs on new disjoint seeds: 20/20 valid, 35,200 requests, zero failures/actions. The 10% regression-margin gate passed; lower P99 remained unsupported (−2.073% effect, simultaneous interval −13.815% to +8.800%). This is useful bounded non-regression evidence with identical initial allocations, not an adaptive-performance release gate or an improvement guarantee. BENCHMARK_REPORT.md contains the complete table, scoped CPU cost and limits. Raw path: results/research-1794016468042146959.

## x86 installed-mode follow-up

[CI at fb2c9ff](https://github.com/sairex53/flowcore/actions/runs/42327112417) additionally passed staged installation on the x86_64 VM: ordinary-user monitor with zero effective capabilities, watchdog/metrics, authorization/parser refusal, installed SDK consumer/session reuse and staged file removal. CTest 30/30 and selected sanitizers 3/3 also passed in that run. This is CMake staged installation, not a Fedora RPM transaction or installed privileged-helper campaign. Retained log: build/review-x86-installed-ci.log.


## Next-generation integrated engine

The integrated engine is an opt-in research mode for owned veth labs. Its release scope remains experimental. The verified HTB and flower tree, managed-socket cap and compensation paths have explicit ownership checks. The integrated deployment gate remains open for installed privileged operation, longer fault campaigns and independent host or architecture runs. The 40-trial next-generation matrix leaves integrated P99 inconclusive and detects a goodput regression in the old ECN-aware arm. The performance gate requires repeated evidence across workloads and stronger baselines. No release tag or production promotion was created.

The current feature completion matrix is in [Next-generation architecture](NEXTGEN_ARCHITECTURE.md). The same-allocation study, separated scheduler baselines, raw evidence locations and local verification are in [Next-generation benchmarks](NEXTGEN_BENCHMARKS.md).
