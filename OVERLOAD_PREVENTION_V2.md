# Deadline-Aware Overload Prevention 2.0

## Status

The C++ estimator, cooperative admission API, controller state machine, workload adapter, accounting, and paired experiment manifest are implemented. Native tests pass. The first privileged paired runs were invalid because the new workload arm did not receive usable evidence and a malformed adapter input interrupted request accounting. The adapter now fails safe to `UNKNOWN`, and the evidence collector reports why evidence is unavailable. A complete privileged rerun is still required; no performance improvement is claimed.

## Audit and design

FLOWCORE already had `deadline::Work`, `deadline::estimate`, IPC admission queries, `DeadlineRequest`, application lifecycle reporting, and an older offered-load experiment. Those pieces remain compatible. The new `flowcore::v2::overload` engine adds response-size estimates, explicit processing costs, class-scoped rates and queues, active service concurrency, fairness status, hard lower bounds, a cooperative concurrency controller, and application-local request ranking.

The estimator is advisory. `Service::assess` is local, performs no IPC, and has no kernel write path. An application keeps the final admission decision. The existing `DeadlineRequest` API registers a deadline, tracks egress progress, and reports completion. The 2.0 overload API can query a cost snapshot and notify an application callback with the resulting assessment. `BackpressureController::set_callback` reports state or budget changes outside its internal lock.

```cpp
using namespace std::chrono_literals;
flowcore::v2::Service service(socket_path, intent);
flowcore::v2::overload::RequestCost cost{
    .deadline_ns = flowcore::v2::deadline::monotonic_ns() + 80'000'000,
    .request_bytes = remaining_upload_bytes,
    .response_bytes = estimated_response_bytes,
    .processing_p50_us = processing_p50,
    .processing_p95_us = processing_p95,
};
struct NotifyApplication {
    decltype(application_admission_callback)& callback;
    void operator()(const flowcore::v2::overload::Assessment& advice) const {
        callback(advice.recommendation, advice.reason);
    }
};
NotifyApplication on_admission{application_admission_callback};
auto assessment = service.assess(cost, evidence, on_admission);
// The application decides whether to accept, defer, or reject.
```

Missing response size or unverified path data produces `UNKNOWN` or an at-risk recommendation. `REJECT_RECOMMENDED` is reserved for an expired deadline or a proven lower bound that exceeds the remaining time. A typical estimated P95 miss is not treated as proof that a request is impossible.

## Cost model

Inputs are a request and response byte estimate, remaining absolute deadline, app processing P50/P95 and optional hard minimum, observed RTT P50/P95, upload/download service rates and queue bytes, active concurrency, queue depth, and optional operator-verified rate limits. Processing time remains a separate term; network observations do not stand in for server work.

For each direction, serialization time is `ceil(bytes * 1,000,000 / bytes_per_second)` microseconds. Network time is the greater of the measured RTT and the sum of request/response serialization plus their observed queue service time. App queue wait is estimated in concurrency waves only when active concurrency, limit, and queue depth are all present. Processing P50/P95 is then added separately. The returned uncertainty is the gap between the P95 and P50 estimates; it is an estimate spread, not a confidence interval.

The normal estimate is guarded by 10%, or 25% for a caller that prefers reliability. A result is `FEASIBLE` only when the guarded P95 fits and the required class, concurrency, response-path, and operator fairness evidence is verified. A P95 estimate beyond the budget is `AT_RISK`; it is not a proof of impossibility. A hard infeasibility bound needs verified maximum transfer rates and, when used, verified minimum RTT and application processing time.

## Controller behavior

The backpressure controller accepts only fresh, monotonic, owned-scope samples. Duplicate samples cannot advance its hysteresis; stale, incomplete, or inconsistent samples return `INSUFFICIENT_DATA` and hold the current recommendation.

- Capacity collapse: measured capacity falls at least 15% from the preceding valid sample. After the one-second action cooldown, concurrency and admission budget each fall by 25%, bounded by operator minima.
- Overload: at least 90% capacity use with backlog, a saturated service queue, or complete-window failure/deadline-miss thresholds. Pressure must persist for two valid samples before reducing the limits.
- Recovery: utilization below 75%, no queue/backlog, failures below 2%, and deadline misses below 5%. Three healthy samples are required; concurrency rises by one and the admission rate by at most 10% of its configured ceiling per cooldown.
- Fairness: the controller reports whether a configured background floor is observed, at risk, unknown, or physically impossible because capacity is below the floor. It does not claim to enforce a floor or guarantee throughput during collapse.

The Python workload adapter consumes the returned admission budget with a bounded-burst token bucket. This is an experiment policy, not an autonomous daemon action. The `deadline-overload-v2` mode does not change qdiscs, socket marks, or host networking.

## Priority and scheduling safety

Service priority remains operator-authorized by the existing service registry. The new scheduler accepts a bounded enum and ranks only work already admitted to an application-owned queue using deadline slack, transfer size, priority, and fairness debt. It cannot set a kernel priority or grant another process a critical class. Applications must populate this ranking from their authorized service policy, not an untrusted per-request priority field.

## Reproduction

Build with the repository's standard CMake configuration, then run:

```sh
ctest --test-dir build --output-on-failure
sudo FLOWCORE_TC=/usr/bin/tc ./build/flowcore bench replay scenarios/overload-prevention-v2.json
```

The benchmark is preregistered for ten paired repetitions and runs only in FLOWCORE-owned disposable namespaces. It compares no deadline admission, existing deadline admission, a static concurrency limit, the new cooperative overload engine, and the static QoS baseline under burst load and a 12-to-8 Mbit/s capacity change. See `ADMISSION_RESULTS_V2.md` for the current validation status.

## Limitations

This implementation cannot guarantee arbitrary application deadlines. The service's processing quantiles must come from the application. Current lab response-direction capacity is marked unverified; it must not be interpreted as measured reverse-path capacity. No controller action is made when required evidence is missing. Physical-interface, multi-architecture, long-duration, and production daemon validation remain separate work.
