# Demand Engine Results

## Current outcome

The native estimator now separates application demand from accepted socket
writes, peer reads, qdisc delivery, class service, configured capacity, and TCP
socket limits. The observer passes TCP evidence to a class only for the sockets
registered on that service's port. Class headroom is calculated from that
class's byte-counter delta. The socket pacing ceiling is decoded from
`TCP_INFO`.

The demand gate remains advisory: experiments do not perform policy writes.
Three-repeat live trials correctly identified receiver, pacing, class
restriction, idle demand, and recovery. Local-capacity accuracy varied by
workload; remote bottleneck evidence was usually too weak, so the estimator
returned `UNKNOWN`. These are exploratory veth namespace results, not a QoS
performance claim.

## Build and complete test suite

Reproduce the host build and test run with:

```sh
cmake -S . -B build -DFLOWCORE_BPF=OFF -DCMAKE_BUILD_TYPE=RelWithDebInfo
cmake --build build -j4
ctest --test-dir build --output-on-failure -j1
```

The final complete run was on x86_64 Omarchy, GCC 16.2, kernel
`7.2.15-1-omarchy`, with `FLOWCORE_BPF=OFF`: **68 tests passed, one test was
skipped, and none failed**. `udp_intent_live` was skipped by its environment
gate. The in-tree build directory is required by tests that load local shared
libraries and create temporary files under `build/`.

The earlier Fedora 45 aarch64 build ran under QEMU TCG, not native ARM hardware.
It used GCC 16.2.1 and kernel `7.2.11-300.fc45.aarch64`, with BPF disabled.
That earlier source snapshot passed 64 tests, skipped `udp_intent_live`, and
had three unrelated SDK/session test failures. The current source has not been
rebuilt on ARM64. There is no physical NIC result.

The final x86_64 CTest log is retained locally at
`results/demand-intelligence/x86_64-ctest-final.log` (ignored experiment
output).

## Deterministic native estimator benchmark

Command:

```sh
FLOWCORE_BUILD_DIR=build python3 scripts/demand_experiment.py --iterations 50000
```

The latest run is
`results/demand-intelligence/run-1794578704810518730.json`. It used 13
deterministic evidence fixtures, including local capacity, HTB class, pacing,
receiver, external bottleneck, missing evidence, and failure accounting.

| Measurement | Result |
| --- | ---: |
| Expected fixture labels | 13/13 |
| False-positive actions in fixture gate | 0 |
| Demand-aware fixture decisions | 4 |
| Existing queue-gate fixture decisions | 3 |
| Native assessments | 50,000 |
| Process CPU time | 0.445 s |
| CPU per assessment | 8.90 µs |
| Median assessment | 9.07 µs |

The 23-sample forecasting holdout still favors the no-prediction baseline:
last-value MAE was 310,870 bit/s, EMA MAE 407,617 bit/s, and trend MAE
373,991 bit/s. Forecasts remain shadow-only. Fixture decisions do not measure
network performance or real controller actions.

## Live multi-profile namespace experiment

The harness profile definitions and expected evidence are in
[`scenarios/demand-network-v2.json`](scenarios/demand-network-v2.json). Each
profile ran three times in disposable network namespaces, using 250 ms
measurement windows. The final evidence is
`results/bottleneck-1794578779833099005/summary.json` and each run's
`observations.json` (ignored local outputs). It contains 33 completed runs and
462 load windows; `host_identity_unchanged` is true. No controller policy write
was attempted.

Accuracy below compares the estimator with the declared controlled condition
after excluding the first second of warmup. Class-specific cases score the
`background` class. `UNKNOWN` is counted as a mismatch for accuracy and is also
reported as an abstention in the raw summary.

| Profile | Target | Correct / scored | Estimator labels across all windows | Gate-eligible windows |
| --- | --- | ---: | --- | ---: |
| `demand_idle` | Overall demand-limited | 33/33 | 42 demand-limited | 0 |
| `demand_background` | Overall local capacity | 27/33 | 33 local, 9 unknown | 33 |
| `demand_bursty` | Window-dependent | Not scored as one label | 21 local, 18 unknown, 3 demand-limited | 21 |
| `demand_receiver` | Overall receiver-limited suspected | 33/33 | 42 receiver-limited suspected | 0 |
| `demand_pacing` | Background class pacing-limited | 33/33 | 39 pacing-limited, 3 unknown | 0 |
| `demand_class` | Background class restricted | 33/33 | 42 class-restricted | 42 |
| `demand_remote` | Overall remote-limited suspected | 0/33 | 3 remote-suspected, 39 unknown | 0 |
| `demand_capacity_drop` | Overall local capacity | 22/30 | 27 local, 12 unknown | 27 |
| `demand_recovery` | Local before; demand-limited after | 8/9 before; 18/18 after | 14 local, 21 demand-limited, 4 unknown | 14 |
| `demand_mixed_udp` | Overall local capacity | 29/33 | 35 local, 7 unknown | 35 |
| `demand_load` | Overall local capacity | 19/33 | 24 local, 18 unknown | 24 |

The held-out-looking recovery condition is still exploratory rather than a
pre-registered blind test. After HTB rose from 2 to 12 Mbit/s, every scored
window was demand-limited; before the change, one scored window remained
unknown. In the remote case, the current TCP path-pressure evidence was present
in only a small fraction of windows. The engine abstained in 39/42 windows and
did not open the gate. This exposes an evidence gap; it does not prove the
remote profile was locally uncongested.

The existing directional bottleneck classifier independently missed the
expected local egress label in 16/30 settled windows for `demand_background`
and 3 settled windows for `demand_capacity_drop`. It sometimes attributed a
local shaped case to `REMOTE_PATH_SUSPECTED`. Those misses are retained in the
per-run summaries; they are not counted as demand-estimator accuracy.

### Byte accounting

The current synthetic application admits every scheduled payload into its
workload. `bytes_transmitted` is the number of bytes accepted by `send()` or
`sendto()`; a short TCP write's unsent tail is not retried by this specific
generator. Peer-read bytes and qdisc delivery are separate measurements. The
experiment does not claim request-level attempt, rejection, completion, or
deadline denominators.

| Profile (sum of three runs) | Offered / admitted | Accepted by socket | Read by peer |
| --- | ---: | ---: | ---: |
| `demand_load` | 21,000,000 / 21,000,000 B | 5,347,120 B | 5,021,320 B |
| `demand_background` | 10,500,000 / 10,500,000 B | 5,151,984 B | 4,956,504 B |
| `demand_class` | 10,500,000 / 10,500,000 B | 1,459,976 B | 1,264,496 B |
| `demand_pacing` | 10,500,000 / 10,500,000 B | 1,563,840 B | 1,316,232 B |
| `demand_receiver` | 10,500,000 / 10,500,000 B | 525,624 B | 394,032 B |
| `demand_remote` | 10,500,000 / 10,500,000 B | 5,138,952 B | 4,829,080 B |
| `demand_capacity_drop` | 19,800,000 / 19,800,000 B | 3,979,208 B | 3,399,832 B |
| `demand_recovery` | 9,900,000 / 9,900,000 B | 4,802,184 B | 4,671,864 B |
| `demand_mixed_udp` | 21,032,400 / 21,032,400 B | 5,742,432 B | 5,212,152 B |
| `demand_bursty` | 4,725,000 / 4,725,000 B | 4,725,000 B | 4,545,480 B |

Across the same three `demand_mixed_udp` runs, application telemetry recorded
8,777 attempted UDP messages, 3,251 fully accepted by `sendto()`, and 5,526
send failures. Of those, 879 were urgent and 7,898 background messages. These
are generator-level send results; peer reads and qdisc bytes remain independent
measurements.

An earlier ten-run `demand_load` artifact used the previous counter contract,
where `bytes_admitted` meant socket-accepted bytes. Its totals are intentionally
kept in the older raw summaries and are not combined with the table above.

Reproduce the full profile set with:

```sh
sudo env FLOWCORE_TC=/usr/bin/tc FLOWCORE_BUILD_DIR=build \
  python3 scripts/bottleneck_experiment.py --repetitions 3 \
  --cases demand_idle demand_background demand_bursty demand_receiver \
    demand_pacing demand_class demand_remote demand_capacity_drop \
    demand_recovery demand_mixed_udp demand_load
```

The harness changes qdiscs only inside owned lab namespaces. It did not touch
host qdiscs or interfaces. The host lacked a preloaded IFB driver, so ingress
was not part of these profiles.

## Limits and next validation

- Request scheduled/admitted/rejected/completed/failure denominators and
  deadline misses remain unavailable in the live byte workload.
- The nonblocking TCP generator intentionally does not retry short-write tails;
  this makes socket acceptance visible but prevents request-level conclusions.
- Remote bottleneck attribution needs stronger corroborating evidence; unknown
  must remain the result when TCP path-pressure signals are absent.
- Run at least ten paired static QoS, existing-controller, and demand-aware
  comparisons before claiming latency, goodput, fairness, or policy benefit.
- Repeat on native Fedora 45 ARM64 and x86_64, enable BPF where supported, and
  separately validate physical NIC behavior.
- Forecasting is shadow-only, and no estimator-driven policy change was run.
