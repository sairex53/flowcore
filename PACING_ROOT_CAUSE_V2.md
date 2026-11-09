# TCP pacing root cause investigation

## Packet and policy path

The Research Lab sends application request bodies and bulk transfers from the sender namespace. TCP queues each application's writes. Socket pacing may limit how quickly TCP offers packets to egress. Flower maps destination ports to HTB classes. The sender HTB parent enforces the bottleneck rate, and each child leaf schedules a queue. Receiver netem shapes the reverse path. The receiver bulk counter records bytes actually read by the workload. Request latency runs from scheduled dispatch through receipt of the response.

FQ uses departure timestamps and can defer a flow until its packets are eligible. FQ-CoDel uses flow queues with CoDel sojourn-time control. TCP can use an internal pacing timer without FQ. A verified socket cap therefore does not prove where the waiting occurred or whether that cap was effective. [Linux FQ source](https://github.com/torvalds/linux/blob/master/net/sched/sch_fq.c) · [Linux FQ-CoDel source](https://github.com/torvalds/linux/blob/master/net/sched/sch_fq_codel.c) · [Linux TCP output source](https://github.com/torvalds/linux/blob/master/net/ipv4/tcp_output.c)

## Confirmed causes and scope

| Finding | Evidence | Consequence and correction |
|---|---|---|
| Startup cap suppresses demand | In the fresh historical replay the one-flow phase delivered 3.195 Mbit/s with legacy pacing and 9.988 with static HTB. The inherited experiment cap of 400,000 B/s permits at most 3.2 Mbit/s for that one sender before overhead. | A substantial part of the measured loss happened before a controller cut. Guarded mode starts at a cap at least as high as the admitted link rate. |
| Apparent adaptive action can be redundant | The old controller lowered eight sockets to 300,000 B/s each. Their aggregate 19.2 Mbit/s cap was above the 12 Mbit/s parent and any single class ceiling. Per-flow effects remain possible. With fewer sockets, the very same cap can bind. | Require live class readback, a verified cohort and a target below the class ceiling. Restore on phase or cohort change. |
| Demand proxy uses the controlled output | Old demand is the larger of ACK progress and current cap × socket count when any unsent bytes exist, bounded by capacity. On a capped flow this can imply large demand even when observed delivery is low. | Guarded demand requires unsent data plus observed ACK progress and independently timed receiver delivery. The reference rate comes from the receiver rather than the target cap. |
| ACK intervals can straddle the write and cohort changes | Old controller assesses the next tick. Receiver and TCP records show overlapping phases and unmatched cookies around 1→8 and 8→2 changes. A configured ceiling and TCP_INFO delivery rate are not measured receiver goodput. | Only distinct receiver windows and application samples beginning after verified action qualify for outcome checks. Reset the cohort history at phase changes. |
| Previous class-transition lock missed membership callbacks | Adaptive ticks held the shared flock. Socket attach, detach and cohort compensation from the workload were outside that gate. | Route membership changes through the same lock. A waiting membership blocks new proposals, and the event loop yields while waiting for the class side. |
| Single sample of queue or RTT cannot attribute service delay | The original planner combined root backlog, RTT and P99, while the HTB child already separated request traffic. Bulk RTT can reflect the reverse path. A high P99 may include dispatch lag. | Guarded probes require backlog in the critical leaf as well as root pressure and a complete application sample. |

## Where the old throughput loss accumulated

The six complete steady-state pairs, including the separately recorded replacement block, show the following receiver goodput medians. The invalid historical block remains in the raw files.

| Active bulk sockets | Static HTB Mbit/s | Legacy hybrid pacing Mbit/s |
|---:|---:|---:|
| 1 | 10.022 | 3.204 |
| 8 | 6.170 | 6.189 |
| 2 | 9.294 | 5.981 |

The eight-flow phase was effectively unchanged. The loss was concentrated where the fixed cap multiplied by the smaller cohort was below what HTB could deliver. Median observed peak TCP unsent bytes were 76,744 with static and 78,192 with legacy hybrid. Median asyncio drain waits over 1 ms were 188.5 and 119 respectively. Wait counts depend on offered and completed bytes, so the lower count cannot establish less application blocking. TCP_INFO's instantaneous delivery-rate field was logged but unused by the old controller. The observed control weakness concerns ACK deltas and the cap-derived demand proxy.

## Capacity replay and receiver accounting

The historical locked replay reported 83.74 ms P99 with pacing and 51.65 ms with static. The cap update was confirmed 2.94 s after the class resize. Only about one second remained before measurement end. The earlier unlocked pair showed the opposite latency ordering and allowed a stale proposal near the fault. The fresh replay of the same historical seed produced 75.64 ms and 78.14 ms respectively, with 138 deadline misses in each arm and no request failures. It did reproduce the background goodput loss, 3.844 versus 5.515 Mbit/s.

Receiver interpolation in the fresh replay gave phase goodput of **3.20 / 3.83 / 4.51 Mbit/s** with legacy pacing and **9.99 / 3.79 / 4.49 Mbit/s** with static. The eight-flow middle phase includes the 12→7 Mbit/s fault. These phases show where the difference accumulated. The changes in P99 across individual repeats leave transition-specific latency effects unresolved.

TCP_INFO exposes unsent data and a sampled delivery estimate. Asyncio drain also reflects the transport write buffer, so a shorter kernel unsent queue does not establish lower application waiting. The historical LOWAT experiment demonstrated readiness and lower sampled unsent bytes, with inconclusive tail latency. The new study measures socket syscall phases and receiver progress. It does not infer exact TCP timer or qdisc residence from those timings.

Original raw evidence is indexed in [TCP pacing validation](TCP_PACING_RESULTS.md). Fresh historical replay data is in `results/research-1794207553515394166`. Recompute phase rates with `python3 -B scripts/pacing_analysis.py RESULTS/summary.json`. See [remediation](PACING_REMEDIATION.md) and [new results](PACING_REGRESSION_RESULTS.md).
