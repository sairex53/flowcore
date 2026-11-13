# Capacity and Bottleneck Estimation

## Capacity sources

FLOWCORE reports capacity with provenance:

- HTB root rate/ceil or CAKE bandwidth read back from the owned qdisc is a
  configured local shaping limit.
- Interface speed is nominal link capacity. It can be absent, stale, or unrelated
  to the end-to-end bottleneck, so estimates derived from it carry
  `CAPACITY_UNCERTAIN` and cannot pass the controller gate.
- Observed service is calculated from byte-counter deltas over monotonic elapsed
  time. HTB/qdisc class counters and RTM_GETLINK interface counters are kept in
  separate counter domains.
- Unused capacity is `max(0, configured-or-observed capacity - throughput)`.
  It is called reclaimable only when the observed local queue is empty and the
  available drop evidence is clean.

## HTB classes and pacing

For each class, the observer records rate, ceil, served bytes, queue backlog,
and drops. Class service and headroom are reported independently of root
throughput. Suspected double throttling requires a pressured class, a class
ceiling below the root limit, a higher ceiling than guaranteed rate, a lower
socket pacing cap, and observed service near that pacing cap. This is diagnostic
evidence; it is not permission to remove pacing.

## TCP

TCP_INFO delivery-rate and maximum-pacing fields are kernel-reported bytes per
second and are converted to bits per second. FLOWCORE also samples `bytes_acked`,
busy time, receive-window-limited time, send-buffer-limited time, notsent bytes,
send queue, send buffer, retransmits, RTT, and minimum RTT.

The model labels receiver-limited behavior only when socket pressure and a
receive-window-limited fraction of at least 0.65 agree. It does not classify
sender-buffer pressure as network congestion. App-limited state is useful
negative evidence; it is not a direct count of writes the application did not
make. A suspected remote bottleneck requires local queues to be quiet and
independent TCP path-pressure evidence.

## UDP and application demand

UDP attempted, transmitted, failed, urgent, and background message counts come
only from application reports associated with a UDP socket. Link counters do
not reveal application messages that were never attempted. A UDP report is
complete only when all message counters are supplied for every UDP byte report
in the window.

## Demand formulas

For a complete application report over interval `T`:

```text
offered_bps    = offered_bytes * 8 / T
admitted_bps   = admitted_bytes * 8 / T
transmitted_bps= transmitted_bytes * 8 / T
received_bps   = received_bytes * 8 / T
```

Delivered throughput is calculated separately from qdisc/interface counters.
When the queue and drop evidence are complete and valid, an observed lower bound
is:

```text
queue_accounted_demand_bps = (departed_bytes + backlog_after - backlog_before) * 8 / T
```

If the backlog fell, counters reset, or drops occurred, this lower bound is not
reported. Capacity uncertainty is never hidden by a point estimate.

## State interpretation

| Classification | Required evidence |
| --- | --- |
| `DEMAND_LIMITED` | Complete offered demand below measured capacity, quiet local queues and no socket backlog; TCP app-limited evidence may support a lower-confidence classification. |
| `LOCAL_CAPACITY_LIMITED` | Local root queue/drop pressure and observed throughput near known capacity. |
| `CLASS_RESTRICTED` | Class backlog, class service near its binding cap, root headroom, and complete application demand above class ceil. |
| `SOCKET_PACING_RESTRICTED` | Socket queue pressure and pacing cap below known local capacity, with service near the cap. |
| `RECEIVER_LIMITED_SUSPECTED` | Socket pressure plus a measured receive-window-limited fraction. |
| `REMOTE_BOTTLENECK_SUSPECTED` | Local queues quiet and TCP path-pressure evidence present. |
| `UNKNOWN` | Conflicting, missing, stale, incomplete, or weakly attributable measurements. |

Thresholds are intentionally explicit in `src/demand.cpp`. A future calibration
must use paired experiments and hold-out workloads before changing them.

## Reproducibility

The native demand model is in `src/demand.cpp`; TCP and qdisc adapters are in
`scripts/bottleneck.py`; app reports are accumulated in
`scripts/observability.py`. The deterministic scenarios are declared in
`scenarios/demand-intelligence-v1.json`. Commands and measured limits are in
`DEMAND_ENGINE_RESULTS.md`.
