# ECN benchmark evidence

The experiments ran on a Fedora 45 ARM64 QEMU guest with Linux 7.2.11-300.fc45.aarch64. They use disposable veth pairs with offloads disabled. No production interfaces or physical NICs were used.

## Wire and transport checks

The IPv4 probe used a 4 Mbit/s sender bottleneck with either 4 or 22 ms of reverse-path delay. The capture saw the handshake and both directions, with zero packet-socket capture drops. Each flow delivered 384 KiB. These are semantic checks and use their own AQM settings.

| Probe | Negotiated | Forward ECT(0) / CE / Not-ECT packets | Sender TCP_INFO delivered CE |
|---|---|---:|---:|
| CUBIC with ECN disabled | Disabled | 0 / 0 / 272 | 0 |
| CUBIC with Classic ECN | Classic | 173 / 99 / 0 | 103 |
| Mixed, CUBIC flow | Classic | 218 / 54 / 0 | 75 |
| Mixed, Reno flow with peer ECN disabled | Disabled | 0 / 0 / 272 | 0 |
| CUBIC with Accurate ECN | Accurate | 180 / 94 / 0 | 94 |
| DCTCP, two competing flows | Classic on both | 15 / 257 / 0 and 4 / 268 / 0 | 257 and 268 |
| CUBIC over CAKE | Classic | 271 / 1 / 0 | 13 |

The asymmetric Reno connection offered ECN in the SYN and received a plain SYN/ACK. The Classic CUBIC, DCTCP and CAKE cases showed the reverse ECE bit. Accurate ECN showed different ACE handshake patterns and both endpoints reported the accurate mode. The ECE bit in an Accurate ECN ACK is part of its encoded feedback. These traces illustrate why the observed wire CE count and Classic TCP's delivered CE counter are tracked separately.

## Paired Classic ECN matrix

The manifest runs seven modes in random order within each of six seeded blocks. Each run offers 880 requests in six seconds with a 40-ms deadline, a 12 Mbit/s shaped sender and 6 ms of reverse-path netem delay. It cycles through one, six and two background connections. The classified modes use the same HTB class guarantees. The DCTCP mode uses a separate shallow 1-ms threshold, so its result includes that AQM choice. CAKE uses its native best-effort shaper and no HTB class isolation.

All 42 runs were valid and all offered requests completed. P99, background and total goodput, fairness, qdisc backlog, drops and daemon CPU are medians across the six runs. Deadline misses are sums of measured application outcomes.

| Mode | P99 ms | Misses / 5,280 | Background / total Mbit/s | Jain | Peak backlog bytes | Qdisc drops | Daemon CPU s |
|---|---:|---:|---:|---:|---:|---:|---:|
| ECN off, HTB + fq_codel noecn | 37.13 | 62 | 6.27 / 11.07 | 0.9993 | 31,459 | 235 | 0.637 |
| Classic ECN, static HTB + fq_codel | 35.88 | 24 | 6.25 / 11.05 | 0.9993 | 33,883 | 0 | 0.619 |
| DCTCP, HTB + shallow marking | 35.35 | 74 | 6.14 / 10.95 | 0.9996 | 13,535 | 0 | 0.643 |
| Classic ECN, FLOWCORE queue-aware HTB | 36.48 | 145 | 6.28 / 11.08 | 0.9995 | 26,561 | 0 | 0.653 |
| Classic ECN, ECN-aware socket pacing | 38.14 | 48 | 4.79 / 9.59 | 0.9999 | 30,412 | 0 | 0.655 |
| Classic ECN, matched reactive pacing | 39.75 | 55 | 4.78 / 9.58 | 0.9999 | 24,137 | 0 | 0.636 |
| Classic ECN, CAKE best effort | 51.14 | 3,140 | 6.25 / 11.05 | 0.9993 | 29,230 | 0 | 0.619 |

The loss control's background queue recorded 1,148 drop increments across sampled windows and zero mark increments. The static ECN background leaf recorded 6,498 CoDel mark increments, while DCTCP recorded 3,221 CoDel and 10,001 threshold mark increments. These are windowed qdisc observations. They need not equal receiver packet counts or sender ACK counters.

The median paired P99 difference of Classic ECN static minus loss-based static was −0.71 ms with an individual median interval of [−13.13, +3.28] ms (96.875% attainable coverage for a nominal 95% interval). DCTCP had a lower sampled backlog, but its paired P99 interval against static ECN was [−1.78, +109.57] ms. A noisy DCTCP run remains included. At six pairs, the existing ten-pair decision gate reported INCONCLUSIVE.

There were no HTB controller adjustments. The ECN-aware and reactive pacing variants each made five verified group updates across six runs. All ECN-aware changes in this matrix used the reactive path because the ECN evidence was missing or saturated. Against static ECN, the ECN-aware mode lost a median paired 1.46 Mbit/s of background goodput. The matched controller comparison isolates the early-policy variant; its P99 interval [−21.51, +5.97] ms includes zero. The median client-workload CPU was 1.17 s for ECN-aware versus 1.20 s for reactive pacing. Its sampler used 16.03 versus 12.42 ms per run. These measurements include the research process and VM scheduling noise.

## Accurate ECN early-action ablation

Four new paired blocks use the same workload and HTB hierarchy, with Accurate ECN and a 12-ms CoDel target and 150-ms interval. Both controllers see the same evidence. Only the ECN-aware mode can use the smaller early step. All sampled background sockets reported Accurate ECN. All eight runs were valid and all requests completed.

| Controller | Median P99 ms | Misses / 3,520 | Background Mbit/s | Median peak backlog bytes | Verified changes |
|---|---:|---:|---:|---:|---:|
| Matched reactive | 44.07 | 77 | 4.71 | 37,088 | 4 |
| ECN-aware | 40.77 | 40 | 4.78 | 36,308 | 8 |

The ECN-aware mode reduced each applicable cap from 400,000 to 350,000 bytes/s at about three seconds, then to 306,250 bytes/s at about four seconds. All eight changes followed corroborated CE evidence without observed queue drops or paired retransmits. The first change was approximately one second earlier in two blocks and at the same sampling tick in two. Pairwise P99 changes were +4.38, −7.70, −6.49 and −3.56 ms. The four pairs support functional validation of the early branch. They do not establish a reproducible tail improvement. There were no verified rollback actions in this dataset.

## Reproduction and limitations

Use the commands in [ECN_ENGINE.md](ECN_ENGINE.md). The original pilot exposed a restriction on setting DCTCP as the non-initial namespace default. The completed experiments used explicit per-socket selection and kept the global allowlist and default unchanged. A separate pilot with a low Classic marking threshold was excluded. The final profile uses ordinary CoDel marks for Classic ECN.

The raw artifacts remain under results/ in the VM.

- Main six-block matrix and HTML report in results/research-1794109235733837294/
- Accurate ECN ablation in results/ecn-lab-1794122002593174584/
- Bidirectional wire evidence in results/ecn-probe-1794122070507347714/
- Final Classic configuration smoke test in results/ecn-lab-1794122193829116249/
- Final Accurate source-freshness smoke test in results/ecn-lab-1794122350339998024/
- Versioned bridge smoke replay of that seed in results/ecn-lab-1794122611588199161/

After the tests, the VM retained one reference to tcp_dctcp and normal module unload returned “in use.” The global ECN setting and CUBIC default matched their original values. The residual reference is consistent with the v7.2.11 implementation, which takes a module reference before refusing the restricted algorithm as a non-initial namespace default. Current upstream checks the namespace first. The shipped lab avoids that sysctl path for DCTCP. The tested VM retains the registered module. [Linux v7.2.11 source](https://github.com/gregkh/linux/blob/v7.2.11/net/ipv4/tcp_cong.c#L263-L288) · [Current upstream source](https://github.com/torvalds/linux/blob/master/net/ipv4/tcp_cong.c)

The refined estimator also checks source age and explicitly distinguishes CAKE root counters from background-only socket data. The held-out tables above use the original measurement artifacts. The final Accurate smoke pair was valid and showed higher P99 in ECN-aware mode, 59.22 versus 39.88 ms. A replay of that smoke seed through the versioned bridge verified both early actions and again had higher P99, 55.78 versus 35.50 ms. The two smoke runs are outside the four held-out pairs. The results are scoped to one software bottleneck, short trials and the tested algorithms. [Estimator and fallback semantics](CONGESTION_FEEDBACK.md)
