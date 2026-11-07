# Causal SLO Diagnostics

FLOWCORE joins application SLO reports, Linux queue and TCP measurements, verified policy changes and explicit laboratory faults on a local monotonic clock. The report presents competing explanations and points to the recorded evidence. Its strength labels describe evidence quality. Estimating causal effects requires controlled comparisons.

## Read the report

```sh
flowcore diagnose
flowcore diagnose --service demo
flowcore incident list
flowcore incident inspect INCIDENT_ID
flowcore incident export INCIDENT_ID > incident.json
flowcore incident replay incident.json
```

A default report contains the observed network scope and each registered service. A service report gives the current and adjacent comparison windows, sample counts, failures (including timeouts), deadline-bearing requests, misses, successful-request P99, and ranked candidates. Class and interface throughput is available only with suitable counters. It describes the class or interface rather than individual service goodput. Inspect includes a time-ordered timeline, safe investigative steps, and evidence IDs that resolve to events in the exported bundle.

| Reported change | Trigger |
| --- | --- |
| Successful-request P99 regression | At least 100 successes in each window and a rise of at least 30% and 1 ms |
| SLO P99 exceeded | At least 100 successes and P99 above the service target |
| Failure or deadline-miss increase | At least 10 eligible requests in both windows, at least three affected requests and a rise of at least 10 percentage points |
| Throughput reduction | Baseline of at least 100 kbit/s and current rate below 70% of baseline |
| Drop burst | At least five observed drops and at least triple the previous interval count |
| Retransmission increase | At least 20 data segments in each interval, at least three current retransmits and a five-percentage-point rise |
| Possible controller regression | Application regression with a nearby verified write and a comparable controller-declared degraded evaluation |

The daemon uses adjacent four-second windows and waits two seconds for delayed reports. Histogram percentiles are approximate, with limited tail resolution at 100 successes. Thresholds are engineering heuristics. The reports give denominators and leave absent metrics null.

## Associations and alternatives

- Intent V2 uses its existing peer credentials, lease and process-start validation. A request is namespaced to its reporting process and lease and exported under an opaque reference. Legacy samples have service and class but lack request and socket identity.
- A TCP association can join a passed connected descriptor to a unique INET_DIAG cookie and tuple sampled during the request. A captured cgroup ID and network namespace inode are kernel metadata when available. Missing, stale, closed or ambiguous sockets remain explicit. UDP and MPTCP descriptors can retain transport metadata even when TCP correlation is unavailable.
- An explicit bound interface is stronger than an owned-veth address match. A configured destination-port class is a candidate for the socket, while measured per-class counters describe packets across the class. Request multiplexing prevents a general request-to-packet mapping.
- The graph connects workload demand, queue pressure, transport, request latency and SLO. Edges distinguish a queue-accounted estimate, simultaneous observations, a same-socket sample, a measured threshold, and missing links. A scoped queue may be genuine and unrelated to the request. TCP RTT is sampled transport behavior and cannot decompose request latency.
- Interface retransmits and inflated RTT support path degradation as a candidate. Peer receive-window pressure may come from another connection, receiver scheduling or socket settings. Multiple equally supported candidates remain ambiguous. Missing or partial telemetry reduces the strength of attribution.

## Instrumented request stages

An authenticated Intent V2 client may include an optional `timings_us` object on a terminal request event. Allowed fields are `dns`, `connect`, `tls`, `processing`, `network_wait`, `queueing` and `response_transfer`. Supply only measured, non-overlapping durations in microseconds within the existing monotonic request interval. FLOWCORE rejects unknown fields, negative values and sums exceeding total request duration. This is an additive IPC extension. Existing SDK methods keep their original wire format.

FLOWCORE summarizes the retained stage samples. The caller determines timing boundaries and must account for server instrumentation, stream multiplexing and overlapping spans. The kernel collector supplies no automatic DNS, TLS, processing, network-wait or queueing decomposition. Request deadlines come from the caller or lease policy.

See the [incident protocol](INCIDENT_ENGINE.md) and [validation](DIAGNOSTIC_VALIDATION.md). [Linux TCP UAPI](https://github.com/torvalds/linux/blob/master/include/uapi/linux/tcp.h)
