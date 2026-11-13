# Adaptive Demand Intelligence

## Purpose and safety contract

The demand estimator separates application offered demand from bytes accepted by
the local socket, bytes reported as transmitted or received by the application,
and throughput observed at the qdisc or interface. These are different
measurements and are never substituted for one another.

The native implementation is `flowcore_demand_assess_v1` in
`include/flowcore/demand.hpp` and `src/demand.cpp`. The Python adapter is
`scripts/demand_intelligence.py`; `scripts/bottleneck.py` combines it with
qdisc, TCP_INFO, and application telemetry. Missing measurements are represented
as unknown. A classification that depends on incomplete evidence cannot enable
the controller gate.

All rates crossing the native interface are decimal bits per second. Byte
counters remain bytes. The adapter converts unavailable native sentinels to
JSON `null`.

## Application reporting

Applications may attach a `TrafficAccounting` value to C++ SDK request
completion. It records offered, admitted, transmitted, and received bytes, plus
UDP attempted, transmitted, failed, urgent, and background message counts. UDP
message reports require the request to be associated with a UDP socket. The
existing `flowcore report sample|complete` CLI also accepts the corresponding
byte and UDP fields.

Request lifecycle tracking marks an application window incomplete while SDK
requests remain in flight or when the start and terminal records do not fit in
the same complete window. The observer reports terminal outcomes and deadline
misses, but the current request protocol does not observe scheduled requests,
application admission rejections, or requests omitted before reporting. It
therefore does not claim a complete failure denominator or synthesize offered
request counts from terminal outcomes.

## Evidence and classification

The model reports:

- Offered, admitted, transmitted, received, and delivered rates separately.
- Configured shaping capacity, observed utilization, unused capacity, and
  reclaimable capacity when the relevant counters are available.
- Per-class rate, ceil, service, queue, and drop evidence.
- TCP delivery rate, send queue, notsent bytes, pacing cap, app-limited state,
  receiver-window-limited fraction, and sender-buffer-limited fraction.
- UDP message accounting and explicit request counters only when supplied with
  complete denominators.
- Evidence bits, confidence, uncertainty, and flags for incomplete reports,
  nominal-only capacity, queue lower bounds, and suspected double throttling.

The output classification is one of `DEMAND_LIMITED`,
`LOCAL_CAPACITY_LIMITED`, `CLASS_RESTRICTED`,
`SOCKET_PACING_RESTRICTED`, `RECEIVER_LIMITED_SUSPECTED`,
`REMOTE_BOTTLENECK_SUSPECTED`, or `UNKNOWN`.

Queue-accounted demand is a lower bound, not an exact demand measurement. It is
computed from observed departures plus queue growth only when the interval has
valid, drop-free counters and a non-decreasing backlog. A qdisc queue can show
pressure, but cannot reveal requests never released by the application.

## Controller gate

The demand-aware egress gate requires either:

1. A high-confidence local root-capacity classification with measured capacity
   and no application-report or capacity uncertainty; or
2. A high-confidence class restriction with complete offered-demand reporting,
   where reported offered demand exceeds the class ceil and the class ceiling is
   materially below root capacity.

The existing bottleneck review state and owned-lab authorization remain
required. Nominal link speed alone, a receiver or remote suspicion, missing
application reports, partial windows, or a forecast cannot authorize a policy
write. No forecast is used by the controller.

## Forecasting

The bounded native forecast combines exponential smoothing, a short linear
trend, and a burst/uncertainty estimate. It requires four spaced, complete
application windows and verified qdisc capacity. It is emitted as `SHADOW` with
`controller_use=false` and is reset after a capacity change.

The first holdout evaluation found that both EMA and trend forecasts had higher
mean absolute error than the last-value baseline. Forecasting therefore remains
diagnostic only.

## Diagnostics

Use `flowcore bottleneck inspect --json` for directional attribution and the
embedded `demand` object. `flowcore capacity estimate --json` returns the
capacity estimate and provenance. Applications can report a sample with, for
example:

```sh
flowcore report sample payments --transport tcp --outcome ok --duration-us 8000 \
  --bytes-offered 4096 --bytes-admitted 4096 --bytes-transmitted 4096 --bytes-received 4096
```

The report is cooperative evidence. It does not change the application request
path or claim that local socket acceptance guarantees remote delivery.

## Limits

Application reports are optional, and current telemetry does not provide a
complete scheduled/admitted/rejected request denominator. Remote service
saturation cannot be proven from local counters. TCP receiver and remote
classifications are explicitly suspected. Hardware-specific and MPTCP-specific
demand models remain unvalidated. See `CAPACITY_ESTIMATION.md` and
`DEMAND_ENGINE_RESULTS.md`.
