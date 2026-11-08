# Cooperative admission control

The application owns every admission decision. Call Service::admit(work, cookie) for authenticated advisory IPC, or flowcore::v2::deadline::estimate(work, evidence, now) for a nonblocking local calculation using trusted cached observations. The IPC round trip uses the existing bounded Intent v2 transport and can consume much of a short budget. The SDK reevaluates local expiry and reserved budget after the round trip. The returned reason and quality form a synchronous risk notification. Existing asynchronous service notification bits expose aggregate SLO risk and congestion, including observed deadline misses.

| Advice | Interpretation |
| --- | --- |
| ACCEPT | The buffered forecast fits with both application reservations and a supplied transfer-history estimate. |
| ACCEPT_WITH_RISK | The forecast is marginal, its inputs are incomplete, or its timing margin is too small. |
| DEFER | The typical forecast overruns the remaining budget and class queue pressure is present. Defer or shed cooperatively if this matches service semantics. |
| REJECT_INFEASIBLE | The local deadline expired, the caller's declared reservations consume the remaining budget, or a trusted upper service envelope makes the transfer impossible within the remaining time. |
| INSUFFICIENT_DATA | The required evidence is missing, stale, outside the supported path, or the daemon is unavailable. The SDK exposes the reason. |

## Forecast inputs

The owned sender's HTB parent rate and ceiling are read back in bytes/s. Class goodput is an observed byte-counter rate, capped by the parent when used as a forecast. It is not residual link capacity. Class queue bytes divided by that rate approximate queue wait. Sender-side sampled TCP RTT histograms represent the namespace and can include other flows. They do not resolve an individual request's transit time. The network component adds payload serialization to the greater of queue wait and RTT to avoid automatically charging both. A supplied transfer-history P95 can raise the high estimate. The lab supplies that estimate using the last 64 successful write-to-reply intervals per class, requiring at least eight recent samples. Those intervals include peer and protocol time, so combining them with separate declared reservations can overestimate. This history is filtered by a 2.5 s horizon, and estimated P95 is a heuristic rather than a calibrated probability. The balanced and reliability-preferred options apply 10% and 25% margins respectively.

Live IPC uses a process-bound lease, a shared clock namespace and a socket cookie attested via SCM_RIGHTS. The lab path requires a connected TCP socket matching the owned sender namespace and the configured class port on the veth peer. The observation is associated at class scope. The daemon can return a limited risk forecast when transfer history is unavailable. It returns insufficient data for a different interface, an ambiguous path, a missing cookie, stale counters or absent RTT. Expiry and exhausted declared reserves can be evaluated without any route association. The SDK exposes an offline status for unavailable or older daemons. A service can keep serving in this case. Priorities remain set by operator policy. Each lease has a bounded burst of 64 queries and refills at 64 queries per second.

Use the exact upper service envelope input only with a verified capacity bound and maximum initial burst. The live HTB source intentionally leaves it unset because HTB maintains rate and ceiling token buckets. An average rate alone cannot prove a short transfer impossible. [Linux HTB implementation](https://github.com/torvalds/linux/blob/master/net/sched/sch_htb.c)

## Acting on advice

For a fixed deadline, deferral spends part of the same budget. The lab retries advice once after at most 5 ms of configured wait and records persistent deferral separately from an infeasible rejection. Risk and insufficient data let the request proceed. A live application can instead use its own queue, cancellation token, overload response or retry policy. Set explicit reservations only when the application wants them included in the feasibility decision. The daemon cannot terminate a request or apply a class change through this API.

A rejected or deferred request remains in the offered-load denominator. The benchmark also reports transport/request failures separately from admission decisions. Successful-request P99 after shedding describes the completed population, so compare it alongside offered-load attainment, completion ratio and background progress.
