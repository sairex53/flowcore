# Per-message scheduling results

## Setup

Fedora 45 ARM64 QEMU guest, Linux 7.2.11-300.fc45.aarch64. A disposable
veth pair connects sender and receiver namespaces. The sender's egress is
shaped at 12 Mbit/s. The reverse echo path has no matching shaper. TSO, GSO
and GRO are disabled on the test pair. The experiment used a Release build
with optional BPF disabled.

Each address family has 10 independent seed blocks with a randomized order
of six modes, for 120 completed trials. Each two-second trial offers 240
echo probes at 256 bytes, routine datagrams at 1200 bytes on the same
socket and four competing 1200 byte background flows on separate sockets.
The schedule includes a middle burst phase. Application latency starts at
the actual probe submission attempt and ends on echo receipt. The deadline
is 12 ms from that attempt. Release lag is recorded separately. Modes use
the same offered schedule within a block. The timed matrix disables TX
timestamp requests uniformly.

| Family | Mode | Median run P99 (ms) | Median failure | Median deadline attainment | Median background goodput (Mbit/s) |
| --- | --- | ---: | ---: | ---: | ---: |
| IPv4 | Single HTB leaf + fq_codel | 93.36 | 83.75% | 0.00% | 9.29 |
| IPv4 | CAKE best effort | 277.64 | 82.71% | 0.00% | 9.26 |
| IPv4 | Static HTB, whole socket admitted | 202.46 | 74.79% | 0.42% | 8.17 |
| IPv4 | CAKE DiffServ4 + EF | 1.83 | 0.00% | 100.00% | 9.04 |
| IPv4 | Raw per-message prototype | 0.72 | 0.00% | 100.00% | 9.07 |
| IPv4 | C++23 SDK | 0.75 | 0.00% | 100.00% | 9.07 |
| IPv6 | Single HTB leaf + fq_codel | 93.57 | 83.75% | 0.00% | 9.14 |
| IPv6 | CAKE best effort | 277.95 | 83.54% | 0.00% | 9.11 |
| IPv6 | Static HTB, whole socket admitted | 204.26 | 76.04% | 0.42% | 8.04 |
| IPv6 | CAKE DiffServ4 + EF | 1.86 | 0.00% | 100.00% | 8.87 |
| IPv6 | Raw per-message prototype | 0.49 | 0.00% | 100.00% | 8.91 |
| IPv6 | C++23 SDK | 0.60 | 0.00% | 100.00% | 8.91 |

P99 is conditional on successful echo receipt. Each failure rate includes
missing replies and locally failed sends. Late replies count against deadline
attainment. The SDK delivered all 2400 probes on time per family with no local
send errors. Across 2400 probes, CAKE DiffServ had no losses in either family
and two late IPv6 replies. All runs showed progress in every sampled 250 ms
background flow window. SDK median run Jain fairness exceeded 0.99999 in
both families. Median SDK leaf qdisc drops were 4040.5 for IPv4 and 4082 for
IPv6 across all offered traffic, including routine and background datagrams.

Paired differences use SDK minus comparator. Intervals are unadjusted
distribution-free median intervals over independent blocks. With ten pairs,
their discrete coverage is 97.85%. The loss-heavy baselines are ineligible
for paired P99 effects because their success-conditioned samples differ.

| Comparison | IPv4 median effect (ms) | IPv6 median effect (ms) |
| --- | ---: | ---: |
| SDK vs CAKE DiffServ | -1.21 [-2.50, +0.32] | -1.11 [-1.99, -0.78] |
| SDK vs raw prototype | -0.01 [-0.50, +0.76] | +0.08 [-0.30, +0.36] |

The SDK reproduced the raw prototype's delivery and background progress.
Its tail difference from the prototype is inconclusive in both families.
The CAKE DiffServ comparison shows lower SDK P99 in this IPv6 workload and
an inconclusive IPv4 difference. The experiment uses one VM, one bottleneck
capacity and one workload shape. The data cannot establish general
superiority over CAKE or predict performance on physical NICs.

Median worker CPU seconds per two-second trial rose from 0.211 to 0.241
for IPv4 and from 0.213 to 0.244 for IPv6 compared with the raw prototype.
Paired median differences were +0.028 s [0.008, 0.051] and +0.031 s
[0.015, 0.064], respectively. Median per-run submission P50 for routine
plus probe datagrams rose from 7.8 to 16.9 µs and from 8.3 to 17.6 µs.
This timing includes Python, the test C ABI boundary and local syscall
time, and worker CPU includes the receiver and SDK worker thread. The
separate daemon's CPU is excluded. The reused process reached a cumulative
38,060 KiB RSS high-water mark. This does not resolve per-mode memory cost.

## Correctness evidence

- Native IPv4 and IPv6 live tests observed the expected receiver DSCP for an
  urgent datagram, an over-budget demotion, explicit background and ordinary
  traffic on the same socket. A mixed sendmmsg batch retained its individual
  datagram metadata. An unmarked descriptor, expired deadline, excessive
  horizon and UDP corking flag were rejected in their respective tests.
- Four TX scheduler/software transmit timestamp pairs per family had matching
  identifiers and valid stage ordering. These were Linux software timestamps.
- Owned HTB readback showed exactly one urgent packet and no urgent increase
  for the same socket's two ordinary messages or for a separate capability-free
  sender. The latter could set EF and priority 6 but could not set SO_MARK.
- Separate IPv4 and IPv6 packet-policer fixtures warmed a one-packet bucket,
  then used two marked sockets from separate processes. One urgent packet
  reached the urgent leaf, the second reached the receiver via the default
  leaf, and the policer reported one overlimit and zero drops. An initial
  fixture before bucket warmup observed both packets in the default leaf.
- The isolated software TAPRIO readback confirmed its configured gate cycle.
  ETF accepted a future transmit time, delayed that datagram by 141.4 ms in
  the fixture and rejected an expired submission with a TXTIME error report.
  Those experiments exercise a separate mechanism from SDK deadlines.

## Reproduce

    cmake -S . -B build -DFLOWCORE_BPF=OFF -DCMAKE_BUILD_TYPE=Release
    cmake --build build -j4
    ctest --test-dir build -R 'udp_intent_unit|scheduler_research' --output-on-failure
    sudo ctest --test-dir build -R '^udp_intent_live$' -V
    sudo python3 -B scripts/udp_intent_lab.py --classification-only
    sudo python3 -B scripts/udp_intent_lab.py --repetitions 10 --duration 2 --seed 26110891
    sudo python3 -B scripts/scheduler_experiment.py --capabilities

Raw VM evidence lives under `results/udp-intent-1794238991849811093/`
for the paired matrix and `results/udp-intent-1794239491774523144/`
for the policer and classification fixtures. The earlier invalid, pre-warmup
fixture is retained in `results/udp-intent-1794239299971884932/`.
ETF and TAPRIO readback are under
`results/scheduler-capabilities-1794239372730650372/`. Raw artifacts remain on
the VM. The matrix records a development checkout based on c99e372 and used
bridge SHA-256 9d62d2f77e40610b2502e7e6ca8fc71def142af9eea320a0aebed1c548578fb6.
The final build adds capability probe and partial-batch error reporting
changes outside the matrix's data path. The namespace runs preserved the host
link and qdisc fingerprint.

Validated workload scope covers mixed sizes, bursts, multiple sockets, a
capability-free second process and asymmetric shaping. Sustained multi-service
load, UDP GSO behavior, physical hardware timestamps and deployment outside
the owned namespace lab remain unverified.
