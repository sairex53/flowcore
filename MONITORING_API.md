# Monitoring API v1

The dashboard exposes `GET /api/v1/…` with an `Authorization: Bearer <token>` header. Its static shell and assets are public and contain no operational data. All API routes, including health, require a token. The listener accepts only an exact configured Host header on IPv4 loopback. It sends no CORS grants. It rejects write methods and has no generic proxy to the daemon.

## Response envelope

All API responses use the following JSON fields:

```json
{"schema":1,"status":"ok","data":{},"meta":{"status":"ok"},"error":null}
```

A live response can be `ok`, `stale`, or `unavailable`. A stale response carries the last observed value and its sample timestamp. No data is a JSON null or empty list as specified by the field. Zero is a measured value. Unavailable health and archived data use their own response status. HTTP 401 means the token was rejected, 421 means the Host was rejected, 400 means an invalid query, 404 means an unknown route or record, 405 means a forbidden method, 422 means an unreadable or unsupported artifact, and 503 means the requested live source is unavailable. Oversized headers are rejected with HTTP 431 before routing.

| GET suffix | Data |
|---|---|
| `health` | Daemon state, coverage and collector errors, last sample age, poll interval and history write errors. |
| `overview` | A single coherent snapshot of interfaces, traffic classes, services, controller and recent link events. |
| `interfaces` | Host and owned lab interfaces with scope, state, MTU and directional observations. |
| `services` | Application P50/P95/P99, target, attempts, successes, errors, deadline reports and misses, sample count and reports/s. |
| `traffic-classes` | Class throughput, backlog, packet drops, guaranteed rate and queue-accounted demand when observed. |
| `controller` | State, mode, strategy, confirmed allocation status, pending flag and counters. |
| `policy-history` | Retained decisions with phase, previous and proposed values, rejection evidence and verification timing when recorded. |
| `bottlenecks` | Directional attribution, confidence, reason, recommended action and supporting evidence. Optional `interface=<id>`. |
| `incidents` | Retained incident IDs and event counts, plus daemon retention and eviction counts. |
| `incident` | Required `id`, optional `kind`, paged events, bundle-relative clock origin and diagnostic report. |
| `history` | Required `kind` and `id`, optional `seconds` and `points`. Supported kinds are `interface`, `service` and `traffic_class`. |
| `multipath` | Current dashboard namespace MPTCP socket and endpoint inventory, when supported. |
| `benchmarks` | IDs of explicitly configured files. |
| `benchmark` | Required `id`, paged run summaries, format, provenance and metric descriptions. |
| `benchmark-run` | Required `id`, optional zero-based `run`, bounded details for one recorded run. |

List routes accept `offset` and `limit` where applicable and return `items`, `total`, `offset` and `limit`. Limits are 1–64, or 1–32 for policy history. Incident details support named event kinds and a maximum of 64 events per page. The upstream incident bundle is itself bounded by flowcored. Query parameters are strict and duplicates are rejected. History requests accept 1–300 points within configured retention, up to seven days. The result selects the newest retained sample from each equal-width time bucket. It does not average percentiles. Missing samples produce null values. History timestamps are local Unix milliseconds. Incident `time_ns` and controller monotonic timing fields are source-local, and cannot be compared across boots or hosts. Controller wall time is carried separately where recorded.

Service latency is based on successful application samples, not transport RTT. Error rate divides failures and timeouts by reported attempts. Deadline-miss rate uses deadline-bearing requests. Request throughput is the count of explicit reports divided by window duration. Backlog and drops preserve the daemon's source interval and accounting domain. Throughput and capacities use decimal kbit/s. Capacity provenance distinguishes configured local shaper readback, reported link speed, and unknown capacity.

Requests are limited to an 8 KiB header, a three-second header deadline, eight query fields, and 16 concurrent connections. JSON responses are bounded at 1 MiB. Artifact paths are selected at startup and opened as bounded regular files without following the final symlink. A dataset can have at most 512 runs and occupy at most 16 MiB before projection. Operational artifacts should be provisioned by a trusted operator.
