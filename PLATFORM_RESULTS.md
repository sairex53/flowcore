# FLOWCORE 0.2 validation report

Target: Fedora 45 ARM64, Linux 7.2.11-300.fc45.aarch64. This checkout does not
contain Git metadata. Research reports therefore record a null commit and
source hashes rather than a fabricated revision.

## Feature completion matrix

| Milestone | Implemented and verified | Remaining boundary |
|---|---|---|
| A1–3, multi-interface networking | Native rtnetlink inventory, link and route transitions, per-interface byte and drop deltas, available qdisc state, explicit device overlays. Two active veth devices and loss/recovery tested. | Host inventory is read-only. Wi-Fi hardware and routed RTT attribution were not tested. Unbound socket RTT and end-to-end loss stay unknown. |
| B4–7, MPTCP | Socket/sysctl/endpoint capability probe, compatible endpoints, two veth paths, kernel path management, negotiated subflow and TCP_INFO observations, impairment, interruption, restoration and disabled namespace probe. | Own experimental sockets only. No arbitrary-session migration or daemon-wide external MPTCP socket inventory. |
| C8–12, containers | Podman state and cgroup v2 identity, owned-namespace nft socket matching, HTB ceiling readback, SDK SLOs and per-cgroup packet/byte counters. Classifier removal on exit tested. | One attached cgroup per dedicated lab. No automatic enrollment, forwarded-traffic attribution or rootless NAT enforcement. |
| D13–18, policy | Global/device/interface/service/container precedence, validation, revisioned atomic store, bounded audit and existing verified HTB actuator. Profile recovery and no stale actuation after SIGKILL tested. | Persisted profiles require explicit lab admission. No host-wide atomic network transaction or power-loss durability guarantee. |
| E19–29, security | No-capability monitor role, private Unix socket, peer credentials, command authorization, bounded parser, deterministic input mutation tests, argv tools, pinned lab namespace mutations. | Privileged laboratory remains trusted root code. Independent actuator privilege separation and external security review remain open. |
| F30–39, reliability | Health, structured errors and logs, watchdog notification, isolated state, bounded history, idempotence, restart recovery and rollback. | Unit resource directives verified statically and watchdog tested over a real Unix datagram. Unit activation through the system manager was not performed. |
| G41–48, monitoring | Prometheus text, Grafana example, interface availability, class and service metrics, decision and revision exports. Research reports verified convergence when eligible. | Dashboard import was not tested against a running Grafana. Runtime probe state is not a convergence guarantee. |
| H49–55, developer experience | C++23 SDK export and downstream build, real request example, API/database/backup/container overlays, MPTCP quickstart, CLI and Mermaid architecture docs. | SDK operations remain same-UID for reporting and bounded synchronous IPC. |
| I56–64, packaging | Relocatable staged CMake install, versioning, local Fedora AArch64 RPM/SRPM, deterministic source archive, package digests and service definition. | Unsigned local package and unspecified repository-wide license. x86_64 is a CI target, not a verified build on this ARM64 host. Binary reproducibility is not asserted. |
| J65–75, validation | CTest, live interface and policy recovery, controller safety, Podman, MPTCP, benchmark suite, monitor overhead, staged installation/removal and unprivileged denial tests. | No host system package install, service enablement or physical-interface changes. |

## Live networking results

- MPTCP verified **4,489,216 payload bytes** on the echo connection with two
  reported subflows and no TCP fallback. There were **33 completed exchanges**
  while the primary link was down and **55 after link-up**. The primary address
  pair was observed again after link-up. This does not measure how many bytes
  were scheduled on each subflow. Disabling MPTCP in the receiver namespace
  prevented creating a new MPTCP socket there.
  Evidence: results/mptcp-1793840561893154338/.
- The Podman workload completed and reported **40 of 40 requests**. The
  installed ceiling was **7,000 kbit/s**. The critical class counted
  **186,336 bytes** and the cgroup matcher counted **254 packets / 177,632
  bytes**. The observer recorded **23.171 ms** as the application P99 histogram
  upper bound. The classifier was removed after exit. This was a classification
  and readback test, not a saturation test of the ceiling.
  Evidence: results/container-1793840185900269091/.
- The multi-interface test generated traffic on the second path, observed
  route and link changes, applied an inherited 10 Mbit/s HTB ceiling, then
  killed the daemon. On restart, the profile revision survived and the
  controller remained off without recreating the lab.
  Evidence: results/platform-live.log.

## Benchmark and overhead

The final quick suite produced **10 valid runs**. The finite-transfer
scenario completed all **630 requests** across three modes. P99 was
**21.66 ms** static, **25.23 ms** SLO and **36.86 ms** fq_codel.
Background goodput was about **7.46 Mbit/s** in each. This single run per
mode is exploratory. Evidence: results/research-1793840350630127821/report.html.

The seven-mode fault scenario offered **6,440 requests**, with **347
failures**. Static, threshold and SLO success-only P99 were **1,058.53**,
**1,069.25** and **1,086.96 ms**, respectively. Fault runs cannot establish
an adaptive advantage. Timeout recovery and censorship dominate the tails.
Daemon plus helper CPU was about **1.08–1.20 s** per measured fault run,
and sampled daemon RSS about **25.3–25.7 MiB**.
Evidence: results/research-1793840381688050461/report.html.

The monitor from the extracted RPM observed two interfaces and consumed
**0.068 CPU seconds over 10 wall seconds**, with about **25 MiB RSS**.
This is an idle, short observation, not a service capacity benchmark.
Evidence: results/platform-rpm-verification.log.

## Tests and deliverables

Six CTest targets passed, including the 2,000-case seeded parser test.
Privileged adaptive and service regression suites passed. The previous
Research Lab integration verified live replay, reduction and scoped stop.
The staged install verified zero effective capabilities, denied writes,
watchdog messages, Prometheus output, systemd unit syntax, downstream
CMake SDK consumption and manifest-based removal. Fedora RPM and SRPM
were generated and their digests and extracted runtime verified.
The initial rpm-build invocation placed a tool-managed scratch script in
/var/tmp. It was removed, and the successful builds set the RPM temp
directory inside the project.

Logs: results/platform-ctest.log, results/platform-install.log,
results/platform-adaptive-regression.log, results/platform-service-regression.log,
results/platform-rpm-final.log, results/platform-release-sha.log,
results/research-integration.log.

Release paths: build/rpm/RPMS/aarch64/flowcore-0.2.0-1.fc45.aarch64.rpm,
build/rpm/SRPMS/flowcore-0.2.0-1.fc45.src.rpm, build/release/RPM-SHA256SUMS.

## Changed files

- Network, policy and IPC: scripts/network_state.py, scripts/platform_network.py,
  scripts/platform_policy.py, scripts/ipc_security.py, scripts/daemon.py,
  scripts/cli.py, scripts/platform_cli.py, scripts/telemetry_export.py.
- Experimental paths: scripts/multipath.py, scripts/containers.py,
  examples/container_request.cpp and config/platform-examples.json.
- Runtime and packaging: scripts/paths.py, scripts/flowcored-installed.in,
  scripts/benchmark.py, scripts/adaptive_experiment.py, scripts/research_runner.py,
  scripts/observer_perf.py, scripts/service_lab.py, scripts/integration_test.py,
  scripts/app_metrics.py, scripts/bpf_source.py, src/main.cpp, CMakeLists.txt,
  packaging/flowcored.service, packaging/flowcore.spec, packaging/source-archive.sh,
  packaging/verify-rpm.py and monitoring/flowcore-grafana.json.
- Tests and docs: tests/platform_test.py, tests/platform_integration.py,
  tests/install_test.py, README.md, ARCHITECTURE.md, PLATFORM.md, SECURITY.md,
  CLI.md, RELEASE.md, RESEARCH.md and this report.

## Priorities

1. Separate the privileged actuator into a narrow helper before allowing
   operator-owned host interfaces.
2. Measure phase-matched adaptive actions against tuned static policies on
   physical links, with enough repetitions and explicit failure accounting.
3. Verify the installed service under systemd on a disposable Fedora image
   and run the network suite on x86_64.
4. Extend MPTCP observation and container enrollment only after their
   attribution, namespace and authorization boundaries are demonstrated.
5. Establish the project license, package signing and a clean-build release
   process before public distribution.
