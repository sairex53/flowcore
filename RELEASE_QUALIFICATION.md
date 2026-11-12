# FLOWCORE release qualification

## State model

`scripts/release_qualification.py` reads `config/release-gates.json` and the
retained Prompt 52 reports, validates their schemas and paths, and writes
`config/release-qualification.json`. Existing `PARTIAL` declarations become
`UNVERIFIED`; missing or short soak evidence cannot become `PASS`. Reports are
hashed into the output. Symlink evidence, paths outside the repository, and
oversized evidence files are rejected.

Each project gate and Prompt 52 subgate uses one of:

- `PASS`: the specified evidence passed its explicit qualification check.
- `FAIL`: retained evidence shows an observed test failure.
- `BLOCKED`: a required environment, artifact, or execution prerequisite was
  unavailable.
- `UNVERIFIED`: evidence is incomplete or below the declared qualification
  threshold.

A valid soak report must be a complete `service` or `full` profile of at least
24 hours by default, contain at least 20 samples, pass every resource gate, and
retain its step logs. A shorter smoke run is useful for exercising the runner
but remains unverified for long-term reliability. A reboot report must preserve
the backing image and pass initial boot, clean reboot, abrupt QEMU restart,
installed-runtime startup, config persistence and service startup checks.
QEMU `SIGKILL` evidence is never used to claim actual machine power loss.

Physical NIC validation requires a Linux physical interface and a controlled
peer for actual TCP/UDP behavior; a virtual NIC or local link inventory alone
does not pass the hardware gate. Signed distribution requires an actual
operator-controlled signing key and verification of resulting artifacts. If
either environment is missing, the report records a blocker.

## Current qualification (2026-11-13)

The machine-readable report at `config/release-qualification.json` records
**Release blocked**. The x86_64 and Fedora 45 AArch64 builds each passed 13
selected CTest checks, and bounded service smokes passed on both architectures.
The separate reboot harness passed the guest OS clean reboot and QEMU-stop
recovery checks while preserving its backing image. Its overall lifecycle gate
remains blocked because FLOWCORE is not installed in that guest.

The 24-hour soak has not run. Resource drift is unverified after one cycle;
physical peer testing and production artifact signing are blocked; package
upgrade/service lifecycle and the complete fault matrix are unverified. The
existing project release gates also remain incomplete. Accordingly,
`production_ready` stays false, and no tag, package publication or release
upload was made. Sanitized run summaries are under
`results/prompt52-validation/`; raw guest reboot artifacts remain outside the
repository.

## Final decision rules

- **1.0 release candidate ready for review** requires every existing release
  gate and every Prompt 52 gate to be `PASS`. This is only readiness for review,
  not permission to publish or tag.
- **Experimental release ready** is available when the Prompt 52 validation
  suite is complete, no gate is `FAIL`/`BLOCKED`, and only older project gates
  remain `UNVERIFIED` from the existing release-gate snapshot.
- **Release blocked** is selected if any required gate fails, is blocked, or a
  required Prompt 52 result remains unverified.

The report always keeps `production_ready` false. It never creates tags,
publishes packages or uploads evidence.

## Reproduction

```sh
python3 -B scripts/release_qualification.py \
  --soak-report results/runtime-soak-EXAMPLE/report.json \
  --reboot-report results/reboot-qualification-EXAMPLE/reboot-qualification.json \
  --validation-manifest results/prompt52-validation.json
```

Omit report arguments to produce an explicit blocker/unverified inventory of
the current state. Review the JSON report alongside
[`FLOWCORE_1_0_READINESS.md`](FLOWCORE_1_0_READINESS.md) and its underlying raw
artifacts before making a release decision.
