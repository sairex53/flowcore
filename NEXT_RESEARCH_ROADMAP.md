# Next research and release work

| Priority | Question or blocker | Exit evidence |
| --- | --- | --- |
| P0 | Can the privileged runtime work from an installed package without trusting user-writable output paths? | Run the helper and active policies from the packaged units on a disposable Fedora image, with SELinux enforcing. Test ownership races, cancellation and result publication under a separate unprivileged UID. |
| P0 | Can the package be upgraded and rolled back with preserved configuration and no implicit host activation? | Verify dependency-resolved install, upgrade and erase with scriptlets and systemd. Publish package SBOM, verified source provenance and signed artifacts after review. |
| P0 | Can another maintainer reproduce the conclusions? | Publish a reviewed raw bundle with exact commit, OS/tool versions, seeds, input manifests and hashes. Independently replay the quality and paired gates. |
| P1 | Why do class and pacing strategies lose under capacity collapse? | Instrument offered versus admitted demand and queue occupancy, compare static and adaptive using the same classification and reservations, and show a measurable action effect across disjoint held-out scenarios. |
| P1 | Does UDP urgency help where stock DSCP and CAKE are already suitable? | Compare matched markings, held-out workload shapes and configured minimum service shares on both address families. Count late and missing responses and background goodput together. |
| P1 | When is an MPTCP path preference beneficial? | Separate capacity, link, delay and loss interventions. Demonstrate repeatable effects against Linux defaults on independent links. |
| P1 | What portable public API can be supported? | Freeze documented CLI, wire and C ABI surfaces and run old-client/new-daemon and new-client/old-daemon checks. Define C++ rebuild policy. |
| P2 | How does the lab model translate to devices and longer workloads? | Run a genuine reboot and a long soak on Fedora ARM64 and a separate Linux x86_64 environment, then measure on physical NICs with explicit offload and driver settings. |

Use existing Linux traffic control, pacing, packet metadata and MPTCP
for these investigations. A kernel extension needs a reproducible
limitation that those mechanisms cannot address. The novel work under
test is the coordination of application evidence, scoped actuators and
outcome guardrails. [Innovation assessment](INNOVATION_REPORT.md)
