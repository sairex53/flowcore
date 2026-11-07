# Release readiness

Decision: **experimental evaluation snapshot; public open-source release blocked**.

No project-wide license has been selected by the owner. No LICENSE was invented. The internal version remains 0.2.0 for existing SDK/package compatibility; no stable release tag is created by this review. A proposed v0.2.0-rc.1 requires the gates below.

## Gates

| Gate | Scope and requirement | Current decision |
|---|---|---|
| Functional | Fresh builds, unit/integration evidence, bounded cleanup and truthful capability reporting | PARTIAL: working research/monitoring scope; SDK and installed-active gaps |
| Security | Peer/privilege/input/cleanup checks and installed package review | PARTIAL: source helper tests pass; no external audit or complete installed active campaign |
| Performance | Held-out evaluation, strong shaped baselines, failures, uncertainty and negative results | Evidence recorded; adaptive superiority remains unproven |
| Reproducibility | Frozen seeds/selection, source/build identities, complete raw preservation, public evidence bundle | PARTIAL: local raw evidence preserved; publication/review of full bundle pending |
| Portability | Actual ARM64 and x86_64 execution within advertised scope | PARTIAL: physical Linux and broad privileged portability unavailable/unverified |
| Documentation | Current quickstart, feature matrix, API references, contribution/security guidance | Local link/schema checks recorded after completion |
| Packaging | Dependency-resolved installed upgrade/erase, signed provenance and security-profile validation | PARTIAL: local unsigned RPM; isolated transaction/security-policy blocker remains |
| Licensing | Explicit owner selection and provenance/contribution policy | BLOCKED |

Machine-readable gates are in config/release-gates.json. packaging/release-gate.py fails closed for publication. The manual candidate workflow builds/tests/checks a source candidate and uploads artifacts only if these gates pass. It has read-only repository permissions and never creates a stable release or tag.

## Reasons to evaluate FLOWCORE

Use the project as an isolated Linux networking laboratory, a read-only inventory/metrics daemon, and an opt-in application-intent research SDK. The fresh review demonstrates reproducible static-versus-adaptive comparisons and explicitly preserves cases where configured adaptive modes perform worse. It does not establish a production traffic-management advantage.

See FEATURE_MATRIX.md, BENCHMARK_REPORT.md and REVIEW_FINDINGS.md for evidence and limits. SECURITY.md describes private reporting. OSS_ROADMAP.md prioritizes unresolved engineering work.

## Actual review evidence

Functional, sanitizer, performance, scale, portability and package evidence is appended after each command completes. Failed runs remain identified; no noisy or inconvenient evaluation run is deleted.
