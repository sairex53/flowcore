# Bottleneck experiment results

The isolated Fedora 45 ARM64 VM series completed 21 runs, three repetitions of seven scenarios. The engine distinguished the owned outbound shaper and the owned receive-side IFB queue. It reported suspected remote, receiver and application causes with advisory actions. All three capacity reductions were detected. [Engine](BOTTLENECK_ENGINE.md) · [Directional setup](DIRECTIONAL_QOS.md)

## Method

The runner uses fresh veth namespaces, stock kernel `7.2.11-300.fc45.aarch64` and iproute2 7.0. The remote-like fixture adds a third namespace and configures its outgoing netem queue at 35 ms and 4 Mbit/s after the TCP connection is established. The engine sees the monitored endpoint only. The router counters provide independent ground truth. The other fixtures use one or two long-lived TCP connections. One restricts the receiver buffer and reads in 1 KiB chunks every 25 ms. Another adds 25 ms of processing before each response and requests a TCP ACK before the sleep.

Each repetition randomizes the order of the seven cases using seed `20261109`. The runner takes three baseline samples and schedules 14 load samples 250 ms apart. It also samples recovery after the egress and ingress loads end. The normal daemon cadence is configurable and defaults to 2 s, so the measured delays apply to this runner.

Detection delay is elapsed time from load or fault stimulus to the first `CONGESTED` sample with the expected label. A false positive is a confirmed cause in an idle or baseline sample, or a confirmed wrong cause. A false negative is a missing or wrong confirmed cause in a positive-case sample after the 1.25 s grace period. For capacity changes the interval from 1.50 to 2.25 s is excluded from the steady-window count to allow the counter baseline to reset. The counts are per sample window with shared runs. They do not estimate field error rates.

| Ground truth fixture | Detected runs | Median detection, s | False positives | False negatives / scored positive windows | Egress eligible load windows |
|---|---:|---:|---:|---:|---:|
| Owned 4 Mbit/s egress, competing TCP flows, loss-free queue buildup | 3/3 | 0.524 | 0 | 0/30 | 39/42 |
| Owned 2 Mbit/s receive IFB, reverse TCP bulk and separate 12 Mbit/s egress | 3/3 | 0.539 | 0 | 0/30 | 0/42 |
| Intermediate router queue | 3/3 | 0.522 | 0 | 0/30 | 0/42 |
| Receiver window limitation | 3/3 | 0.524 | 0 | 0/30 | 0/42 |
| Application processing delay | 3/3 | 1.525 | 0 | 2/30 | 0/42 |
| Owned egress cap change from 4 to 2 Mbit/s | 3/3 | 0.528 at load onset | 0 | 2/21 | 31/42 |
| Idle control | 0/3 positive detections | — | 0 | — | 0/42 |

There were 105 negative control windows from idle load and the baselines, with zero false positives. All four missed positive windows carried the expected cause but were still `BUILDING`. Two were early application samples and two followed the rate change. The first development run classified the slow application as suspected remote pressure when delayed ACKs inflated RTT. That run is retained. The classifier now requires more TCP segments for RTT-only suspicion, and the application fixture separates TCP acknowledgment from its processing delay. The final suite used a new seed.

## Kernel and control evidence

- Both competing egress classes carried traffic. The loss-free case saw peak sender backlog of 72,672–77,054 bytes across the three repetitions, with zero sampled sender qdisc drops. Each egress run returned through `RECOVERING` to `NORMAL`.
- The inbound fixture verified both IFB classes with 178 critical packets and 416–421 background packets at the last load sample. IFB throughput medians were 1.988–1.999 Mbit/s while the egress medians were 59–70 kbit/s. Both receiving sockets delivered data. The ingress action was review of the IFB policy in 39/42 load windows. During drain, all three runs returned to `NORMAL`. Two first passed through `UNKNOWN` while queue evidence was mixed.
- The isolated router readback showed a 46,986-byte backlog in each remote-like run. Those observations were withheld from the classifier. The local action stayed advisory after confirmation.
- The configured egress reduction was read back in every run. Confirmed egress attribution returned 0.744–0.988 s after the last capacity update command. Median throughput before the reduction ranged from 3.832 to 3.907 Mbit/s and after it from 2.176 to 2.217 Mbit/s. These short counter intervals include queue drain, packet accounting and token-bucket effects.
- Egress transfer eligibility remained closed in all 210 load windows outside the two egress scenarios. The engine lab measures admission consequences. It leaves the existing controller's service and actuator checks in place and does not measure latency improvement from applying a new transfer.

The privileged CLI test also passed IPv6 request and reply classification in the corresponding egress and ingress critical counters. It covered monitor-mode denial, refusal to replace an existing ingress hook, invalid rate rejection, distinct rate readback, JSONL watch, route changes and namespace cleanup. The existing service and adaptive integration fixtures passed. They cover classification, explicit class ceilings, shadow and dry-run behavior, capacity drift, replacement rollback and cleanup.

## Reproduce and inspect

```sh
cmake -S . -B build
cmake --build build -j2
ctest --test-dir build --output-on-failure
sudo python3 -B tests/bottleneck_integration.py
sudo python3 -B scripts/bottleneck_experiment.py --repetitions 3 --seed 20261109
```

The final run started from clean commit `b694f505caf0ac29c906a2acdd7bfdb0eb1c1158`. Raw samples, command logs and TC readbacks are in `results/bottleneck-1794034013598228395/`. Its `summary.json` SHA-256 is `b27cfe387ba7819960b3e294ce4754d11005c1be841e859a989b9487ed8dac33`. Pilot data remain in `results/bottleneck-1794028643973925042/` and `results/bottleneck-1794028776621918083/`. Host link and qdisc fingerprints matched before and after the final run.

This evidence is scoped to controlled VM topologies. Performance under concurrent real bottlenecks, unbound host sockets, external route changes and physical links remains uncertain. The remote and receiver labels remain suspicions even when the fixture's configured cause is known.
