# Application-intent scheduler benchmarks

Tested on the Fedora 45 ARM64 QEMU guest with four vCPUs, Linux 7.2.11-300.fc45.aarch64 and iproute2 7.0. Two experiments answer different questions. The UDP matrix tests same-socket packet intent, while the TCP matrix measures the existing FLOWCORE controller on an application request workload. Their P99 values are not interchangeable.

## Same-socket UDP scheduling

Each of ten paired blocks uses a fresh veth pair per mode, randomized mode order and the same seeded offered schedule within the block. The sender shapes egress to 12 Mbit/s. The reverse path carries small UDP echoes without artificial delay. Offloads are disabled on the owned veth pair. A single socket submits 120 short echo requests per second and 1,550 routine 1,200-byte datagrams per second. Four other sockets offer 430 1,200-byte datagrams per second each. Mid-run routine and background sends arrive in bursts. Each run offers 240 echo requests with a 12-ms deadline over two seconds. Late, missing and locally failed requests count as deadline misses.

The successful-request P99 starts at actual submission and ends when the application reads the echo. Release lag from the precomputed schedule is separate. Probe scheduling spans match socket TX_SCHED and TX_SOFTWARE timestamp pairs by explicit ID. These software timestamps cover entry into packet scheduling through driver handoff on this veth path. They are a proxy for local queue residence, with no wire-level or physical-NIC timestamp. Dropped or unmatched samples cannot contribute to the span percentile. [Kernel timestamping documentation](https://docs.kernel.org/networking/timestamping.html)

| Stock Linux configuration | Median successful P99 ms | Median failure | On time / offered | Median probe span P99 ms | Median background Mbit/s | Median qdisc drops |
|---|---:|---:|---:|---:|---:|---:|
| Single HTB class + fq_codel | 95.46 | 83.33% | 3 / 2,400 | 95.10 | 9.29 | 4,192 |
| CAKE best effort | 279.25 | 82.71% | 0 / 2,400 | 278.86 | 9.25 | 4,263 |
| CAKE DiffServ4 + EF | 2.66 | 0% | 2,398 / 2,400 | 1.27 | 9.04 | 4,104 |
| Static HTB + fq_codel, whole trusted socket admitted | 197.82 | 74.79% | 10 / 2,400 | 197.51 | 8.17 | 4,059 |
| Intent HTB + fq_codel, only trusted urgent datagrams admitted | 1.29 | 0% | 2,400 / 2,400 | 0.04 | 9.07 | 4,040 |

Drops include routine and background packets. For runs with loss, the successful-only P99 describes the surviving subset. Comparisons of P99 effect exclude pairs with failed requests. Both urgency-aware modes delivered every probe, so their tail comparison uses all ten pairs. Intent minus CAKE DiffServ had a median P99 difference of −0.74 ms and a distribution-free median interval of [−2.86, +0.04] ms (97.85% attainable coverage for a nominal 95% interval). The median paired deadline-attainment difference was zero, with an interval of [0, +0.42] percentage points. These are individual run-level intervals. The two misses establish neither general superiority nor exact equivalence. The point estimate versus DiffServ is small enough that the study does not justify a new scheduler.

Jain's index across the four background flows was at least 0.99999 in every held-out run. No background flow had an empty 250-ms receive window. Both urgency-aware modes sacrificed some background goodput relative to best effort, while intent HTB delivered about 0.90 Mbit/s more background traffic than whole-socket static HTB. That static configuration moved all of the routine traffic into the protected queue, so the result motivates per-packet classification rather than generic preference for HTB. A stock alternative already does that classification.

The combined workload and timestamping process used median CPU time of 0.251 s for intent HTB and 0.244 s for CAKE DiffServ over the 2.2-s workload and drain. The runner's process peak was 33,404 KiB. These measures include the harness, with no per-qdisc attribution or independent memory peak per mode. Timestamp-pair coverage matched all received probes in this run. Median release-lag P99 ranged from 2.38 to 3.67 ms across modes, making precise microsecond comparisons unsuitable in this VM.

### Pilot corrections and safety

With the default 4 MiB CAKE memory limit in an exploratory run, the shared UDP socket's 2 MiB send buffer hit EAGAIN before CAKE could classify 113 of 240 probes. That is a socket/qdisc budget interaction. The held-out comparison uses CAKE at 1 MiB, with no local send failures. HTB leaves each use a 1 MiB and 256-packet limit. The buffer layouts differ and are part of the compared configurations. The first pilot also caught two tooling issues. The correct ematch key is nf_mark, and Fedora tc emitted malformed JSON for the basic filter's ematch expression. The runner verifies that filter using textual readback and checks actual class counters. Pilot and interrupted runs were excluded. The held-out seed was 81403297.

A capability-free sender setting priority 6 incremented only the default HTB leaf. Every intent-mode protected leaf counted the 240 probes and no routine packets. In the separate capacity-collapse trial, three incremental class updates plus verification took 42.22 ms, halved the parent to 6 Mbit/s and retained the two fq_codel leaves. All 240 probes met their deadline from actual submission. The measured background rate across the two-second transition run was 6.84 Mbit/s, averaging time before and after the change. Release-lag P99 rose to 20.92 ms because the lab applies the changes synchronously. This does not establish a bound on intent latency from the planned release time or on production reconfiguration cost. No background flow had an empty 250-ms window.

## Existing TCP FLOWCORE controller

Five randomized paired blocks at a configured 12 Mbit/s sender bottleneck, 6-ms reverse-path netem and three workload phases. The manifest freezes 800 offered application requests per run, 4 KiB payloads, a 40-ms deadline, two named critical services and up to eight concurrent bulk streams. The static 40% critical reservation was selected in an earlier pilot. TCP requests use new connections, so this is also a short-connection churn case at the reported rate. The same per-datagram UDP prototype does not operate on this TCP workload. [Scenario](scenarios/scheduler-tcp-comparison.json) · [Earlier pilot](BENCHMARK_REPORT.md)

| Configuration | Median run P99 ms | P99 range ms | Median deadline attainment | Median background Mbit/s |
|---|---:|---:|---:|---:|
| Single-class HTB + fq_codel | 72.45 | 59.64–85.08 | 42.38% | 7.87 |
| CAKE best effort | 47.28 | 46.54–50.62 | 59.13% | 7.94 |
| Classified HTB + fq_codel | 31.27 | 30.39–167.41 | 99.88% | 7.87 |
| Tuned static HTB + fq_codel | 31.33 | 25.34–43.58 | 99.63% | 7.83 |
| FLOWCORE hybrid | 32.35 | 30.00–55.03 | 99.88% | 7.90 |

All 25 TCP runs were valid and had zero failed requests. The hybrid controller made zero policy changes. The static tail was lower by the median-run summary, and the existing regression gate returned INCONCLUSIVE with five pairs. One classified HTB run had a 167.41-ms P99. It remains in the report. These runs show no adaptive or new scheduler benefit for TCP.

## Capability and test evidence

Software TAPRIO installed on the veth with a TAI clock and a 2-ms gate cycle. The root counted two packets during the connectivity probe. ETF's separate probe delivered a datagram requested 150 ms in the future after 140.56 ms with a 10-ms configured lead. An expired send produced ENOBUFS and a transmit-time error-queue report. This was a semantic check, separate from the congestion benchmark. No TAPRIO hardware offload or BPF qdisc attach was tested.

The unmodified Linux cmsg_so_priority kselftest at revision d48723711bae9b8cdc56719c448ba42fef28cbee passed all 144 checks. Its helper was compiled with GCC. It ran in a private mount namespace with a private /run/netns view, using the distro tc binary. Focused FLOWCORE tests cover deadline bounds, deterministic offered schedules, rejection of a misconfigured hierarchy, and excluding failure-censored tails from paired inference. Host link and qdisc configuration fingerprints matched after the UDP matrix and the capability probe. [Upstream selftest](https://github.com/torvalds/linux/blob/d48723711bae9b8cdc56719c448ba42fef28cbee/tools/testing/selftests/net/cmsg_so_priority.sh)

Raw artifacts stay on the test VM. They contain per-run qdisc reads, probe timestamps, seeds, host fingerprints and summaries. The held-out UDP readbacks were also checked against the final stricter verifier, with paired inference saved separately.

- UDP runs and collapse case in results/scheduler-udp-1794105011066014787/
- TCP runs and HTML report in results/research-1794104485307771581/
- Capability readbacks in results/scheduler-capabilities-1794105246732799753/
- Upstream test output in results/scheduler-kselftest.log

[Reproduction commands](SCHEDULER_PROTOTYPE.md) · [Kernel decision](KERNEL_UPSTREAM_PLAN.md)
