# Adaptive controller validation

Fedora 45 ARM64, Linux 7.2.11-300.fc45.aarch64. Experiments used the managed
daemon and real TCP sockets in disposable veth namespaces. Each run issued
1,520 application requests in four 4-second phases at 60, 100, 135 and
85 requests/s, with four continuous background flows. The controlled
sender egress used 10 Mbit/s. The Linux default veth control was unshaped.
All modes used the same application reporter and INET_DIAG polling. BPF
was disabled to isolate the managed control loop.

The independent static pilot chose a 40% critical reservation. Pilot P99
was 85.66 ms at 20%, 55.87 ms at 30% and 47.93 ms at 40%. All three met the
preset minimum background goodput and fairness criteria. The selected policy
was then evaluated in two separate runs alongside the other modes.
Seed 20261105 determined evaluation order.

| Mode | P99 per run (ms) | Background goodput range (Mbit/s) | Daemon + child CPU range (s) |
|---|---:|---:|---:|
| Linux default, unshaped | 455.30, 2839.39 | 298.37–341.47 | 1.31–2.38 |
| Shaped FIFO | 99.07, 100.03 | 6.09–6.11 | 1.12–2.29 |
| Shaped fq_codel | 43.25, 52.89 | 6.12–6.12 | 1.08–1.13 |
| CAKE | 36.11, 38.36 | 6.15–6.16 | 1.08–1.96 |
| Tuned static HTB | 28.41, 40.89 | 6.12–6.15 | 1.53–2.36 |
| FLOWCORE active | 61.58, 95.78 | 6.06–6.13 | 1.18–3.13 |
| FLOWCORE shadow | 28.92, 30.34 | 6.10–6.15 | 1.07–1.11 |

Every evaluation run completed without request failures. Background flow
Jain index was at least 0.9999 at four-decimal precision. The shaped
comparison is the relevant one for policy effects. Unshaped veth puts a
different load on the VM.

The active controller made three readback-verified transfers. It retained
one and rolled back two. The first verdict per run arrived in 1.93 and
2.03 seconds after application. This is time to the first feedback verdict,
not demonstrated steady-state convergence. Shadow recorded eight hypothetical
transfers and made zero kernel adjustments.

Both adaptive runs had higher overall P99 than the tuned static runs and
shadow runs. In one adaptive run the retained probe's local P99 fell from
42.50 to 23.17 ms, while the next probe had unchanged P99 and rolled back.
In the other run the post-probe window rose from 30.05 to 142.94 ms around
the high-rate phase, triggering rollback. The unchanged shadow configuration
also maintained the background floor. Short windows straddled workload
changes, and HTB could already borrow spare capacity in the initial state.
Those factors limit the inference that another reservation will help.
A larger run should compare phase-matched windows with a frozen policy,
then separate actuation effects from demand transitions.

Evidence: `results/adaptive-1793811269972994533/`. This includes raw request
samples, per-flow receive counters, plan, tuning result, source snapshot,
network readback and decision history. The full comparison preceded the
final recovery and consecutive-reversal safeguards. The final smoke is
reported separately below.

## Final controller and regression checks

The final safety revision adds an explicit two-reversal guard, recovery to the
baseline after sustained healthy or idle windows, and refusal to adopt an
operator baseline that has drifted. In a separate one-run-per-mode smoke,
P99 was 28.30 ms active, 29.15 ms static and 38.70 ms shadow. The active
probe was rolled back after 2.01 s. Shadow recorded four hypothetical
actions and made no writes. All three runs completed with zero request
failures. The small sample does not overturn the full comparison.
Raw evidence: `results/adaptive-1793811793988198652/`.

CTest passed 4/4 targets. Tests cover registry ownership, conflicts,
persistence, expiry, insufficient data, deadbands, protection floors,
partial write rollback, drift, recovery and repeated-reversal shutdown.
The privileged adaptive integration test used real TCP for critical-only,
background-only, mixed, burst and rapid-switch workloads. It verified SDK
reporting and release, dry-run, shadow, readback of a 9 Mbit/s ceiling,
removal with background traffic still running, refusal to overwrite an
external capacity drop, and cleanup after loss of a namespace holder.
The managed service and standalone controller regression suites passed.
The SDK installed into the project-local install directory.

Relevant logs: `results/adaptive-ctest-final.log`,
`results/adaptive-integration-final.log`,
`results/adaptive-service-regression.log`,
`results/adaptive-legacy-regression.log` and `results/adaptive-install.log`.

New implementation files: `scripts/adaptive.py`, `scripts/adaptive_tc.py`,
`scripts/intents.py`, `include/flowcore/client.hpp`, `src/client.cpp` and
`examples/sdk_request.cpp`. Existing daemon, CLI, configuration, workload and
CMake wiring were extended. The experiment and test entry points are
`scripts/adaptive_experiment.py`, `tests/adaptive_unit.py` and
`tests/adaptive_integration.py`. CLI help coverage was updated in
`tests/service_unit.py`.
