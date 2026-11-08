# Congestion feedback and early action

The lab correlates a verified background-class fq_codel leaf with sender-owned background TCP sockets. Receiver measurements and application request outcomes are retained separately. The pacing control compares paired socket cookies, uses the existing HTB/pacing lock, and logs the result of kernel readback for every applied adjustment.

| Signal | Provenance and interpretation |
|---|---|
| ECN mode | TCP_INFO negotiated flags and optional extended mode on each socket |
| ECT and CE on the wire | Packet socket on the receiver-side veth, after forward shaping |
| AQM marks | fq_codel ecn_mark and ce_mark, tracked separately, or CAKE ECN marks summed over tins |
| TCP CE feedback | Delta of delivered_ce for matching negotiated sender sockets, relative to their delivered segments |
| Loss and retransmits | Leaf qdisc drop delta and TCP_INFO cumulative retransmit delta. The observation alone cannot assign the retransmit to a particular qdisc drop |
| Queue pressure | Leaf backlog divided by the configured parent rate. This is a drain-time proxy with a shared link, and is separate from packet sojourn time |
| RTT and delivery | TCP_INFO RTT and matched-socket trend, plus ACK-byte throughput in the laboratory. Neither is application latency |
| SLO | Critical-request duration and outcomes from the existing application workload |

The estimator preserves missing, stale, reset and incoherent data as unknown. It skips unmatched or unnegotiated sockets for CE-feedback deltas. A mixed workload retains the modes it observed. Drop evidence vetoes early response even if CE is present. For Classic ECN, a sender feedback segment ratio need not match the observed fraction of CE packets. The wire and TCP counters use different accounting and time domains.

Early evidence requires a fresh, bounded interval, at least 32 delivered segments, at least two CE-reported segments, an increase in AQM marks, a positive queue-pressure proxy of at least 2 ms, and no observed queue drops or paired-sender retransmits. Feedback at or above 30% enters the saturated class and cannot request an early step. A marker by itself produces mark-only evidence. These are experimental policy thresholds.

In the ECN-aware strategy, an eligible early step can reduce a background socket cap by at most 12.5%. The existing demand and ownership checks, cohort-change guard, cooldown, cap bounds, action budget, compensation and readback apply. When the early gate is closed, the strategy uses the same queue/SLO/RTT path as the matched pacing control. Missing controller telemetry can return to the static socket cap. The existing HTB queue-aware strategy is a separate comparator and owns its class-level writes. An active class controller or a class transition precludes socket adaptation. A claim about earlier actions is based on timestamps of verified writes, not on proposals.

The optional use of extra kernel counters is length-checked. Forward wire capture is IPv4 in this fixture. Transport snapshots cover IPv4 and IPv6 where INET_DIAG and TCP_INFO return the relevant fields. The fusion consumes background socket lifetimes observed at adjacent samples, so it can miss short-lived flows and cannot attribute encrypted requests to a mark. [Socket pacing safeguards](TCP_PACING_ENGINE.md) · [Experiments](ECN_BENCHMARKS.md)
