# TCP pacing validation

The experiments ran on a Fedora 45 ARM64 QEMU guest with Linux 7.2.11-300.fc45.aarch64 and four vCPUs. The Research Lab uses two disposable network namespaces. The sender egress is shaped to 12 Mbit/s, with a 6-ms netem delay on the reverse path. The actual qdisc, class rates, flow classifiers and offload settings are verified and saved per run.

```sh
cmake -S . -B build -G Ninja -DFLOWCORE_BPF=OFF
cmake --build build -j2
sudo ./build/flowcore bench replay scenarios/tcp-pacing-paired.json
sudo ./build/flowcore bench replay scenarios/tcp-pacing-ablation.json
sudo ./build/flowcore bench replay scenarios/tcp-pacing-transition.json
```

The paired matrix uses the same offered request and bulk workload per block, with a randomized mode order. Each run measures 800 short TCP requests over eight seconds after warmup, with 4 KiB request payloads, a 64-byte application echo, a 40-ms deadline, two named critical services and phases with one, eight and two background connections. The active TCP congestion algorithm is the system default except in explicit algorithm trials. The uncapped default TCP control uses a shaped FIFO, keeping the bottleneck rate and direction comparable.

The capped sockets are the application-owned background TCP senders. They have a 32 KiB requested send buffer and the same asyncio write-buffer settings across modes. Request traffic uses the existing application workload and reports. The experimental hybrid mode compares against the same tuned static class reservations, with pacing as the additional actuator. The pacing-lowat ablation changes only the background send-readiness threshold from its default to 4 KiB under FQ. CUBIC and Reno are selected on the background sockets before connecting, so critical sockets retain the default algorithm.

## Accounting

Latency runs from scheduled request release to application completion. Deadline attainment counts every offered request, including failures, while P99 describes successful requests. Receiver bytes determine background goodput. Jain fairness averages per-phase background flow ratios for phases with multiple active connections. HTB adjustments and socket updates are separate counts. A group update counts once even when several owned sockets are verified. Peak backlog is a sampled qdisc value. Peak unsent bytes are sampled TCP_INFO values across background sockets.

The native sampler reports TCP_INFO and pacing readbacks plus input validity, proposal and verification status. Asyncio drain measures wait at the application's transport boundary. POLLOUT is tested separately in a controlled native fixture. CPU figures in the research summary refer to the daemon and its children. The socket_pacing section separately reports client-workload CPU and sampler CPU time. Both are process-level measurements.

## Capability observations

The unprivileged process successfully probed the requested TCP and ordinary socket options. SO_MARK read succeeded and writing returned EPERM. The VM's current algorithm was CUBIC, with CUBIC and Reno registered and available to ordinary callers. BBR was configured as a module and absent from the registered set. The benchmark did not load it or alter congestion-control sysctls.

The privileged bpftool probe found sock_ops, cgroup_sock, cgroup_sock_addr, cgroup_sockopt and cgroup_skb program types. It reported bpf_get_socket_cookie, bpf_setsockopt and bpf_getsockopt for sock_ops. These were capability probes. The pacing engine uses ordinary socket syscalls. FQ was confirmed by readback on the owned veth in its benchmark modes.

The native loopback fixture sampled 28,672 bytes in TCP's unsent queue with the receiver paused. At that point POLLOUT was ready with the inherited threshold, suppressed at TCP_NOTSENT_LOWAT=4096 and ready again after verified restoration. The threshold affects readiness independently of the application's own write-buffer queue. Under real traffic, sampled unsent bytes can exceed the threshold because larger writes can cross it.

## Measured comparison

### Paired TCP matrix

The first matrix produced 34 valid runs and two infrastructure errors in block 2. During that block the Python bridge temporarily required a diagnostic symbol from a newer build than the loaded shared library. The complete block is excluded from the paired analysis and preserved in the original artifact. One replacement block used the same manifest with repetitions set to 1 and seed 426185407. The table uses six complete independent blocks and 4,800 offered requests per mode. A valid FQ block with 164 request timeouts remains included. It also showed 5.80 s P99 dispatch lag and elevated CPU pressure, so the measurement does not isolate an FQ-specific cause.

| Configuration | Median P99 ms | Deadline misses / 4,800 | Failures | Background Mbit/s | Total Mbit/s | Jain fairness |
|---|---:|---:|---:|---:|---:|---:|
| Default TCP + shaped FIFO | 142.67 | 4,282 | 0 | 7.90 | 11.17 | 0.9868 |
| FQ, inherited socket cap | 154.31 | 1,735 | 164 | 7.83 | 11.10 | 0.9999 |
| FQ + static 180,000 B/s socket cap | 157.73 | 1,433 | 0 | 4.48 | 7.76 | 0.9999 |
| Tuned static HTB + fq_codel | 28.65 | 58 | 0 | 7.93 | 11.20 | 0.9992 |
| FLOWCORE hybrid HTB controller | 34.24 | 35 | 0 | 7.92 | 11.19 | 0.9992 |
| Static HTB + adaptive socket pacing | 26.99 | 4 | 0 | 5.39 | 8.67 | 0.9998 |

Successful-only FQ P99 includes the surviving requests from the noisy run. It is descriptive and excluded from failure-free paired latency inference. Relative to the matched static HTB baseline, the median paired P99 difference for adaptive socket pacing was −2.39 ms. The distribution-free median interval was [−220.88, +7.24] ms at 96.875% attainable coverage, widened by a noisy static run. The median paired background difference was −2.51 Mbit/s with interval [−2.59, −2.33]. These are individual intervals. The data show a consistent background goodput cost of about 32%, and the tail improvement is inconclusive.

The existing hybrid HTB controller made zero adjustments. Socket pacing made one or two verified group updates per hybrid run, with no benchmark rollbacks. Median sampled backlog was 35,529 bytes for static HTB and 37,159 bytes for static HTB plus socket pacing. Median client CPU was 0.907 s and 1.058 s respectively, while daemon CPU was 0.699 s and 0.711 s. The pacing sampler consumed a median 19.21 ms per run in the socket-adaptive mode. These process measurements include VM noise.

### Capacity reduction

One separate block lowered the verified sender capacity from 12 to 7 Mbit/s at the scheduled four-second point. The existing owned-HTB resize took 38.04 ms with static and 53.22 ms with the socket-adaptive workload, measured from the runner's operation start through kernel confirmation. The socket workload performed one rate change from 400,000 to 300,000 bytes/s before the resize started, then observed the new 875,000 bytes/s capacity. The logged socket observation preceded the recorded HTB operation. That first version used a transition flag, leaving a check-to-act race.

P99 was 55.75 ms with static HTB and 53.58 ms with socket pacing. The deadline misses were 108 versus 73 of 800. Background goodput was 5.56 versus 3.91 Mbit/s. All requests completed in both runs. After adding shared actuator serialization, the same seed was replayed. Both runs delivered all 800 requests. The P99 was 51.65 ms for static and 83.74 ms with socket pacing. Background goodput was 5.59 and 3.81 Mbit/s, with 61 and 95 deadline misses. The socket update to 306,250 B/s was verified 2.94 s after the HTB confirmation. The live ticks did not contend on the lock. A deterministic regression test held the class-side lock and verified that the socket tick skipped its observation and actuation. The multi-block steady and ablation numbers above precede the serialization change. The paired transition repeats establish coordination, and their latency results point in different directions.

## Send queue and congestion-control ablations

All 18 runs in three independent blocks completed, with zero failed requests. The effects below are descriptive because the experiment has three pairs.

| Configuration | Median P99 ms | Misses / 2,400 | Background Mbit/s | Median peak unsent bytes | Client CPU s |
|---|---:|---:|---:|---:|---:|
| FQ, system TCP default | 151.12 | 846 | 7.79 | 72,400 | 0.864 |
| FQ, explicit CUBIC on bulk sockets | 158.47 | 703 | 7.80 | 83,984 | 0.912 |
| FQ, Reno on bulk sockets | 151.79 | 907 | 7.83 | 89,776 | 1.071 |
| FQ, bulk TCP_NOTSENT_LOWAT=4096 | 161.56 | 856 | 7.69 | 47,784 | 0.987 |
| FQ, queue-aware socket pacing | 178.51 | 853 | 5.27 | 78,192 | 1.005 |
| FQ, SLO-aware socket pacing | 157.08 | 608 | 5.31 | 78,192 | 0.965 |

The low-water setting reduced the median sampled peak of unsent bytes by 34%. The median number of background asyncio drain waits above 1 ms went from 180 to 172. The latency, goodput and CPU measurements do not establish an improvement. Explicit CUBIC is a control for the system's default selection. The small experiment cannot rank the congestion algorithms. Queue and SLO pacing each made one verified rate change per run and traded background bandwidth for different deadline outcomes. One queue-aware run had a 614.71-ms P99 and remains included.

## Raw evidence and limits

- Primary matrix and invalid block retained in results/research-1794107493318685064/
- Replacement block in results/research-1794108190891984892/
- Ablations in results/research-1794107986490403501/
- Initial capacity transition in results/research-1794108255335947384/
- Serialized capacity transition in results/research-1794108438486388667/
- Privilege and BPF capability probes in results/pacing-unprivileged-capabilities.json and results/pacing-privileged-capabilities.json

The experiment ran in one VM and uses single-host namespace scheduling. It did not test BBR, physical NIC pacing, or long-run production behavior. The existing benchmark gate requires at least ten pairs for a confirmatory decision. The matching six-pair analysis is exploratory and does not establish a net improvement over tuned static HTB.

[Engine](TCP_PACING_ENGINE.md) · [Socket API and telemetry](SOCKET_INTELLIGENCE.md)
