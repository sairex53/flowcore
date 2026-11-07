# FLOWCORE roadmap

## Public-release prerequisites

1. Publish a reviewed, sanitized raw-evidence bundle alongside reproducible manifests and source/build hashes.
2. Resolve installed RPM upgrade/erase validation under the target security policy; sign artifacts and verify dependency-resolved installation.
3. Add genuine reboot/power-loss and installed-active-helper interruption tests on dedicated infrastructure.
4. Complete a separately reviewed threat model and vulnerability reporting path.

## Highest-priority engineering gaps

- SDK lifecycle loss: dropped end events can leave inflight entries until dissociation/expiry. Stress long-lived sessions and improve loss/recovery accounting before claiming complete application telemetry.
- Distinguish initial static allocation from dynamic-action effects. Add controlled same-allocation ablations and sufficiently long observation/evaluation periods.
- Validate controller actions and outcomes under held-out workloads; retain experimental status if tuned static QoS wins.
- Extend predictive cold-start/false-positive/lead-time evaluation without future leakage.
- Test namespace destruction, concurrent container restart and rootless limitations in installed mode.
- Add multi-hour churn/soak tests with FD/RSS/BPF-map resource accounting.
- Evaluate verified native netlink decoders only after profiling subprocess cost.

## Portability and scope

Current environments: Fedora ARM64 VM and GitHub-hosted x86_64 CI, including native helper and held-connection namespace fixtures. The full HTB service lab is unsupported with the runner’s tc 6.1 text class output; validate a compatible JSON-class toolchain next. Obtain a physical Linux system before hardware claims. A 10k-held-connection fixture does not validate 10k SDK clients, managed workloads or classified BPF flows.

Kubernetes integration, arbitrary container support, transparent TCP-to-QUIC migration, encrypted stream inspection and replacement transport stacks are outside implemented scope. ROADMAP.md links here to keep one authoritative plan.
