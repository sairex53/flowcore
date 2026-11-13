# Demand Engine Results

## Summary

The native estimator, SDK reporting path, observer aggregation, CLI reporting,
and conservative controller gate are implemented. On synthetic fixtures, all
13 declared classifications matched their expected labels. In live namespace
experiments, the estimator withheld controller eligibility in all six tested
scenarios because those existing workloads did not report complete application
demand. This is the intended safe result, but it means end-to-end demand-aware
control benefit has not yet been demonstrated.

## Build and tests

Build command:

```sh
cmake -S . -B /path/to/flowcore-demand-build -DFLOWCORE_BPF=OFF -DCMAKE_BUILD_TYPE=RelWithDebInfo
cmake --build /path/to/flowcore-demand-build -j8
FLOWCORE_BUILD_DIR=/path/to/flowcore-demand-build \
  ctest --test-dir /path/to/flowcore-demand-build --output-on-failure -j1
```

The x86_64 integration build was rebased onto `origin/main` at `87f83ff`,
using GCC 16.2 on Omarchy Linux kernel 7.2.15-1-omarchy, with
`FLOWCORE_BPF=OFF`. The full serial suite passed 66 tests, skipped
`udp_intent_live`, and failed `deployment_unit` in its source-configuration
ownership test. All demand-native, demand-model, observer, bottleneck, and live
intent SDK tests passed; `flowid_session` also passed in this refreshed-main
run. The suite expects an in-tree `build/`, so that path was symlinked to the
external build directory during validation. The full x86_64 log is retained at
`results/demand-intelligence/x86_64-ctest-87f83ff.log` (ignored local output).

Before the remote main refresh, the demand implementation also built
successfully on Fedora 45 aarch64 with GCC 16.2.1, kernel
7.2.11-300.fc45.aarch64, and `FLOWCORE_BPF=OFF`. This guest ran under
QEMU TCG emulation on an x86_64 host, so its CPU timings are not native ARM64
performance measurements. Full serial CTest ran as the ordinary `elliot` user:
64 passed, `udp_intent_live` was skipped, and three failed. The failures were
`intent_protocol_compat` (legacy-v2 probe exit 7), `intent_v2_live` (concurrent
SDK clients reconnected and observed a transport error), and `flowid_session`
(21 of 64 flush acknowledgements). An earlier pre-refresh x86_64 checkout
also showed a `flowid_session` flush acknowledgement failure (33 of 64), but
that test passed on the refreshed-main run above. A separate ARM focused run
passed 10 of 11 demand, ownership, security,
observer, and SDK tests; only `intent_v2_live` failed. Its short IPC deadline
and TCG scheduling are a plausible interaction, but the exact cause has not
been isolated and needs confirmation on native ARM64 hardware.

The ARM64 build and CTest run validated the demand patch on the earlier local
source snapshot. The patch later applied cleanly to refreshed `origin/main`
and was rebuilt and tested on x86_64; the refreshed source has not yet been
rebuilt on ARM64.

The ARM64 run log is retained at
`results/demand-intelligence/arm64-ctest-2026-11-13.log` (ignored local test
output, not part of a source commit).

## Deterministic estimator benchmark

Reproduce from the repository root after building:

```sh
FLOWCORE_BUILD_DIR=/path/to/flowcore-demand-build python3 scripts/demand_experiment.py --iterations 50000
```

The refreshed-main run at commit `87f83ff` is
`results/demand-intelligence/run-1794573670086227876.json`. It used 13
deterministic fixtures covering idle and heavy demand, bursts,
capacity collapse/recovery, receiver limitation, socket pacing, HTB class
restriction, remote pressure, mixed TCP/UDP accounting, missing evidence, and
sender-buffer pressure.

| Measurement | Result |
| --- | ---: |
| Expected fixture classifications | 13/13 |
| False-positive actions in demand-aware fixture gate | 0 |
| Demand-aware fixture actions | 4 |
| Existing queue-gate fixture actions | 3 |
| Native assessments | 50,000 |
| Process CPU time | 0.463 s |
| CPU per assessment | 9.26 µs |
| Median single assessment | 9.12 µs |

These are synthetic fixture results. The gate comparison is not a measured
network policy comparison and records no latency, goodput, fairness, or real
policy changes.

The forecasting holdout contained 23 one-step predictions:

| Estimator | MAE |
| --- | ---: |
| Last-value baseline | 310,870 bit/s |
| Exponential smoothing | 407,617 bit/s |
| Trend forecast | 373,991 bit/s |

Both estimators lost to the no-prediction baseline. Forecast output remains
shadow-only and is not used to control networking.

The same 50,000-assessment fixture benchmark ran in the Fedora 45 aarch64 TCG
guest. Its machine-readable output is
`results/demand-intelligence/run-1794572100012738022.json`.

| ARM64 guest measurement | Result |
| --- | ---: |
| Expected fixture classifications | 13/13 |
| Process CPU time | 20.668 s |
| CPU per assessment | 413.36 µs |
| Median single assessment | 381.50 µs |

This timing includes TCG emulation overhead and must not be compared with the
x86_64 host timing as an architecture comparison. The synthetic classification
and forecast results match the x86_64 fixture run; they do not establish
real-traffic accuracy or policy benefit on ARM64.

## Live namespace experiment

Command (run as root; every topology is created inside owned network
namespaces):

```sh
FLOWCORE_TC=/usr/bin/tc FLOWCORE_BUILD_DIR=/path/to/flowcore-demand-build \
  python3 scripts/bottleneck_experiment.py --repetitions 1 \
    --cases egress remote receiver application capacity_drop idle
```

The captured evidence is
`results/bottleneck-1794570680999958862/summary.json` and its per-case
`observations.json` files. The experiment ran on x86_64 Omarchy, not Fedora or a
physical NIC. `host_identity_unchanged` was true.

| Scenario | Existing bottleneck result | Detection delay | Demand gate |
| --- | --- | ---: | --- |
| Egress saturation | Local egress congestion | 0.509 s | Held |
| Capacity reduction | Local egress congestion | 0.509 s | Held |
| Receiver-limited TCP | Receiver-limited suspected | 0.509 s | Held |
| Application-limited request load | Application-limited suspected | 1.259 s | Held |
| Emulated remote bottleneck | Remote path suspected | 0.507 s | Held |
| Idle | No congestion | None | Held |

There were zero false-positive and zero false-negative bottleneck windows in
this single repetition. These figures describe the existing bottleneck
classifier only; they are not a statistically powered demand-estimation result.
The actual demand estimates remained `UNKNOWN` except the remote suspicion, and
`controller_eligible` was false in all six cases. In particular, the local
queue lower bound did not become exact application demand. No policy action was
taken.

The first attempt also tried ingress and stopped because the host did not have
an IFB driver preloaded. FLOWCORE refused to create a host IFB device. The
selected six-case run excludes ingress. The aborted attempt did not modify host
network identity.

## Unresolved validation

- Add complete scheduled, released, admitted, rejected, transmitted, and
  completed demand reporting to instrumented network workloads.
- Run at least ten independent paired comparisons for static QoS, the existing
  controller, and demand-aware control under identical offered load.
- Measure P99, failure denominators, deadline attainment, background goodput,
  fairness, unused capacity, policy churn, detection delay, and estimator CPU.
- Repeat on native Fedora 45 ARM64 hardware and a native x86_64 Fedora host.
  The ARM64 build and suite were run under TCG emulation, not on ARM hardware;
  BPF was disabled on both build environments.
- Run ingress scenarios only in an environment where IFB is already available
  or can be safely enabled within the disposable VM.
- Validate physical NIC behavior separately; namespace veth results are not
  hardware validation.

Until those measurements exist, application-dependent actions remain gated on
complete cooperative reports and the forecast remains shadow-only.
