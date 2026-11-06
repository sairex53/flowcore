# Experimental local congestion prediction

FLOWCORE has an opt-in C++23 forecast kernel, bounded causal telemetry history,
online shadow scoring, and an experimental predictive strategy in its existing
owned-namespace HTB controller. No new privilege, eBPF attachment, TCP replacement,
network-wide prediction, automatic host activation, or learned model is introduced.
The default controller remains off. The intended first predictive use is shadow:

```sh
./build/flowcore controller shadow --strategy predictive
./build/flowcore controller status
```

A privileged isolated lab and admitted service intents are required by the existing
controller. `controller start --strategy predictive` explicitly enables experimental
active operation there; no configuration, SDK registration or warning enables it.
Existing authorization rules still gate activation. Stopping restores the baseline.

## Signals and provenance

Every regular telemetry poll (nominally one second), the daemon supplies observed
HTB class departure-byte deltas, leaf fq_codel backlog/drop counters and root
qdisc throughput. It also snapshots complete application windows: successful tail
latency, failed requests, deadlines, throughput and SLO state, plus independently
sampled TCP RTT. These application/RTT signals are context, not packet deadlines
or an assumption that every slow request is caused by this queue. Reports retain
current allocations, action journals and configured shaping capacity.

All temporal calculations use kernel monotonic nanoseconds. Per-class histories
hold 64 samples; native estimation uses at most the last eight. There are at most
128 pending forecast labels and 128 recent outcomes, with fixed-size aggregate
counters and five reliability bins per model. Actuator-triggered refreshes within
750 ms are skipped. Gaps over 2.5 seconds and capacity changes invalidate history.
Counter resets or missing backlog/throughput invalidate that class's samples.

An arrival **proxy** is departures plus smoothed backlog derivative. It excludes
unknown dropped-byte volume, GSO discrepancies and arrivals between samples that
leave no backlog. It is not a measured offered load. Root capacity is the configured
local shaper bound, not measured remote available bandwidth. No prediction of a
previously unobservable capacity step or remote failure is claimed.

## Deterministic native models

`src/predictive.cpp` exposes the internal `flowcore_predict_v1` C ABI. It allocates
no memory, performs no system calls and rejects non-finite, excessive, out-of-order,
discontinuous or inconsistent-capacity input. Python owns bounded histories and
orchestration; the C++23 core computes the forecasts.

All models use a fixed two-second horizon and the same histories:

- EMA: queue persistence, time-aware alpha = 1 - exp(-dt / 2 s).
- Trend: least-squares queue slope, extrapolated from the latest backlog.
- Queue growth: smoothed per-interval backlog derivative.
- Rate change: positive departure-rate acceleration as a deliberately simple ablation.
- Hybrid: equally weighted trend and smoothed queue growth; the selected strategy.

Slopes are bounded by parent bytes/s; queue forecasts are bounded to four seconds
of parent capacity. Negative forecasts clamp to zero. No coefficients or thresholds
are fitted to evaluation traces. The hybrid choice is frozen before pilot results;
poor ablations are reported, rather than removed or silently retuned.

## Risk, uncertainty and calibration checks

The observed target event is a class backlog at least max(1514 bytes, 20 ms times
its guaranteed byte rate) **and** root utilization at least 85%. The serialization
threshold is an operator-style queue-pressure proxy; borrowing, fq_codel and new
arrivals prevent interpreting it as measured packet sojourn or application latency.

Risk index = min(1, forecast / threshold) * min(1, utilization / .85).
History quality = min(1, samples / 8) / (1 + trend RMS residual / threshold).
Four samples are required. A warning needs index >= .8 and quality >= .45.
Residual is a model-fit diagnostic, not a statistical confidence interval.
Missing history is explicitly `insufficient_data`, not zero congestion risk.
The index is **not calibrated as a probability**. Frozen quintile bins report
subsequent observed-event frequency, so poor calibration is visible. Without
independent calibration evidence, a value .8 must never be called 80% likelihood.

The explanation includes source observations, root utilization, application P99 /
SLO ratio, deadline misses, forecast bytes, slope, residual, quality, horizon and
threshold-crossing lead estimate. Application degradation alone cannot arm a probe.

## Causal shadow evaluation and false positives

Each forecast is labeled only by later samples strictly after issue and at/before
its horizon. The result is withheld until horizon expiry. No future values enter
the predictor. No subsequent usable observation, excessive reporting gap, or an
intervening allocation change produces a censored/inconclusive label. Forecasts
within the final horizon of a run remain pending and are reported separately.
Capacity rebasing creates a distinct predictor instance; reports keep segments apart.

TP/FP/FN/TN refer to forecast ticks for an event observed within the horizon;
precision and recall are undefined when denominators are zero. A separate onset
counter measures false-to-true queue events and whether an early warning preceded
them. Warnings during already-observed congestion are not counted as anticipatory
successes. Early precision, false warnings, missed onsets and sampled lead times
are exposed. Warning samples and rising-edge alert episodes are distinct counts.
Overlapping horizons/classes are dependent: these counts are descriptive, not
thousands of independent validation runs.

## Intervention and fallback

A proactive candidate must have a fresh hybrid warning, no current target event,
positive predicted crossing time within two seconds, and at least 100 ms of remaining
lead after sample age. Existing evidence must independently show local network
pressure, two target-leaf backlog samples, class departures >= 90% of its guarantee,
at least 100 successful application samples, and no reported failures.

The original allocator and verified incremental HTB actuator remain authoritative:
5-point reservation steps, total capacity, ceilings, per-service admission bounds,
background floor, cumulative boost bounds, two eligible control windows, cooldown,
action budget and reversal guard. Rates are guarantees, not maxima; unchanged
ceilings still permit borrowing. No full qdisc-tree replacement is introduced.

Every proposal runs through the existing fixed post-change evaluation: retain only
with comparable demand, sufficient samples, measured latency/throughput benefit,
no increased errors/deadline harm, no peer harm and background protection. Otherwise
restore the prior allocation, including ambiguous/unnecessary probes. Prediction
and action metadata accompany the verdict. These observational probes are not
randomized causal proof; benchmark arms provide the comparisons.

Absent/stale/unusable forecasts automatically use the established hybrid reactive
score; the same reactive score also handles immediate violations. A failed native
load or prediction clears predictive history and leaves reactive planning available.
Unavailable base telemetry invokes the existing controller stop/baseline restoration
path instead. Ownership/readback conflicts still stop actuation, not bypass checks.

The adaptive journal is session-tagged: benchmark extraction cannot mix monotonic
stamps from old boots. A corrupt audit-history file no longer blocks disabled/read-only
startup or grants activation; it logs unavailable history. In-memory/serialized
history is additionally byte bounded. This is not a durable fsync transaction log.

## Verification and experimental protocol

```sh
cmake -S . -B build -G Ninja -DFLOWCORE_BPF=ON
cmake --build build
ctest --test-dir build --output-on-failure
sudo python3 -B tests/adaptive_integration.py
sudo ./build/flowcore bench run scenarios/predictive-shadow.json
sudo ./build/flowcore bench run scenarios/predictive-buildup.json
sudo ./build/flowcore bench run scenarios/predictive-stable.json
sudo ./build/flowcore bench run scenarios/predictive-bursts.json
python3 -B scripts/predictive_report.py results/<run-directory>
```

The benchmark retains two disposable namespaces/veth, actual tc readbacks, configured
versus observed rate checks, request/failure ledgers, monotonic queue timelines,
source hashes, host-noise metadata, JSON/CSV and HTML reports. Matched block seeds,
randomized mode order and run-level intervals apply to static, reactive SLO-aware,
queue-aware and predictive arms. Stable load tests unnecessary interference;
unseen burst/capacity/recovery shapes test limits. Separate-seed static pilots may
select only the static reservation, never model coefficients or evaluation thresholds.
Performance gates and pair requirements remain the existing declared V2 gates.

Tests cover stable/rising/oscillating/malformed histories, discontinuities, finite
bounds, memory limits, delayed labels, censored outcomes, misleading SLO-only
signals, proactive plans preserving floors, stale-data reactive fallback, session
isolation and corrupt journals. Existing adaptive tests cover rollback, gaps,
conflicts, minimum samples, failures, churn and verified kernel changes.

Production prediction remains disabled by default regardless of benchmark outcome.
See PREDICTIVE_RESULTS.md for executed evidence and limitations.
