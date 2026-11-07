# Kernel capability experiment results

The tested Linux interfaces covered socket metadata, datagram priority and departure time, TCP pacing and local queue feedback. The shared TCP stream retained ordered delivery under deliberate packet reordering. The results support keeping FLOWCORE in userspace. [Capability review](KERNEL_RESEARCH.md) · [API decision](KERNEL_API_PROPOSAL.md)

## Environment and reproduction

The completed series ran on Fedora 45 ARM64 in the QEMU VM, kernel `7.2.11-300.fc45.aarch64`, iproute2 `7.0.0` and libbpf `1.7.0`. The probe used clean FLOWCORE commit `aba0cff1e524ccb61defd2c9e0d18fe49eb9a389`, the stock VM kernel and an IPv4 veth pair across two fresh namespaces. The harness disabled offloads and disabled IPv6 on the two test interfaces to isolate background control traffic. All TC changes were scoped to sender `fc0`. Boot ID, initial namespace identity and host link/qdisc fingerprints matched after the run. Both namespace holders exited.

From the repository root on a disposable Linux VM, build FLOWCORE in `build` first. The optional storage probe requires libbpf development headers. Use a `tc` that supports JSON class readback for the subprocess comparison. The repository setup or `FLOWCORE_TC` selects the binary.

```sh
cmake -S . -B build -DCMAKE_BUILD_TYPE=Debug
cmake --build build -j2
g++ -std=c++23 -Wall -Wextra -Wpedantic -Werror tests/kernel_socket_storage.cpp -lbpf -o build/kernel_socket_storage_probe
sudo python3 -B scripts/kernel_api_experiment.py --repetitions 12 --feedback-pairs 40 --seed 870493
```

The script uses the existing exclusive research lock. Stop a running FLOWCORE daemon before starting it. With the optional binary absent, the summary records the storage case as unrun. Required stock capabilities in this fixture include `prio`, netem, HTB, fq_codel, FQ, ETF, flower and fw. Failure exits preserve an invalid summary and the successful stages completed before the error.

Raw evidence is in `results/kernel-api-1794024667274382535/` on the VM. The directory has per-trial TCP markers, per-readback wall/CPU values, socket results, qdisc counters, command logs, offload settings and source hashes. The SHA-256 of `summary.json` is `8ac4dd606c18533f91bf1b220b7eaac383d2de54a914a7a37e56a99b00e89a4e`. The optional executable hash is recorded inside the summary. Console log is `build/kernel-research/confirmatory.log`. Raw artifacts stay local under the repository policy.

Development runs were kept separate. With ETF delta zero, the future datagram timed out in the software-dequeue fixture. A 10 ms lead time gave the watchdog a usable window. The expired-send check needed to handle the synchronous `ENOBUFS` in addition to the error queue. A TC class read originally included fq_codel flow records without HTB byte fields. Filtering both readers to HTB resolved that schema mismatch. The first longer run then failed the quiescent-counter check. The completed fixture uses UDP-only counter traffic before the TCP cases and disables IPv6 on the owned links. The earlier failure is retained in `build/kernel-research/final.log` and `results/kernel-api-1794024596567134730/`. The cause of that counter change was not isolated.

## Ordered TCP under packet reordering

The sender put a 64-byte bulk marker in a 250 ms netem band and a 64-byte urgent marker in a fast band using `SO_PRIORITY`. Each trial used fresh TCP connections and an observer in the receiving namespace. The shared case sent the markers as consecutive TCP sequence ranges. The split case used two independent connections. Both connections were established before measurement. The test randomized the order within each of 12 paired repetitions.

| Mode | Urgent marker first observed at receiver ingress, median ms | Bulk ingress, median ms | Urgent marker read by application, median ms (min–max) | Urgent arrivals within 100 ms |
|---|---:|---:|---:|---:|
| Shared TCP stream | 0.222 | 251.912 | 251.781 (250.297–260.388) | 0/12 |
| Separate TCP streams | 0.225 | 253.087 | 0.259 (0.203–0.320) | 12/12 |

Every urgent packet preceded the bulk packet at ingress. The application read bulk before urgent in all shared-stream trials and urgent before bulk in all split trials. All markers were delivered. The results isolate the receive-order dependency, since the scheduler had already given the urgent packet early egress in both conditions.

Times are userspace monotonic observations from the urgent send setup to the packet-socket or application read. They include local scheduling and polling. The packet socket can be drained after an application read, so submillisecond differences between the two observations are timing uncertainty. The roughly 250 ms separation and TCP sequence numbers make the ingress ordering clear. The 100 ms criterion belongs to this induced-delay fixture. A full request benchmark would include connection setup, protocol behavior and application work.

## Socket policy and departure-time controls

| Probe | Observed behavior |
|---|---|
| UDP `SO_PRIORITY` ancillary data | Urgent marker arrived in 0.180 ms and the bulk marker in 258.726 ms on the same UDP socket. The socket's default priority remained zero. |
| TCP `SO_PRIORITY` ancillary data | The cmsg send accepted all 64 bytes. The receiver read the marker in 256.247 ms in the slow-band fixture. Changing the TCP socket's priority itself selected the fast path in the paired stream trials. |
| UDP `SO_MARK` with TC fw | The marked packet arrived in 0.092 ms and the unmarked packet in 252.733 ms. The TC filter maps mark `0x2a` to the fast band. |
| FQ and monotonic `SCM_TXTIME` | With a target 150 ms ahead, the UDP marker arrived in 152.082 ms. The TCP cmsg send succeeded and the marker arrived in 0.075 ms on the sparse connection. |
| ETF and TAI `SCM_TXTIME` | An isolated UDP class with a 150 ms target and a 10 ms software delta delivered in 141.355 ms. A past timestamp produced `ENOBUFS` on send and `SO_EE_ORIGIN_TXTIME` with `EINVAL` and invalid-parameter code 1 on the error queue. The ETF leaf reported one packet delivered and one drop, and the expired payload was absent at the receiver. |

These were single semantic probes. The TCP results establish protocol-specific behavior on this kernel and are consistent with the reviewed output path. An application must verify effects when selecting a socket option. ETF used software mode and a matching clock. Its timestamps concern local packet departure. [ETF implementation](https://github.com/torvalds/linux/blob/d48723711bae9b8cdc56719c448ba42fef28cbee/net/sched/sch_etf.c) · [TCP send implementation](https://github.com/torvalds/linux/blob/d48723711bae9b8cdc56719c448ba42fef28cbee/net/ipv4/tcp.c#L1119)

Under FQ, five paired TCP transfers delivered 262,144 bytes each. The uncapped transfers had median duration 1.903 ms and median goodput 1,102.096 Mb/s on the local veth. With `SO_MAX_PACING_RATE=262144` bytes/s, the socket readback matched, median duration was 0.930 s and median goodput was 2.255 Mb/s. The short transfer includes startup credit. Use the result to establish pacing control, with a longer run for steady-state accuracy. The socket's median `TCP_INFO` RTT was 158 µs uncapped and 6,288 µs capped in these samples. Those RTT observations are separate from measured application delivery.

## Queue sampling and feedback overhead

The pulse fixture sent one selected UDP datagram into a 250 ms netem leaf. A snapshot before send and one at 1,002.570 ms both had zero backlog. Native `RTM_GETQDISC` at a nominal 10 ms cadence found one packet and 106 bytes in 25 samples through 243.719 ms. The samples bound what the reader saw. They do not measure the exact enqueue/dequeue times or coverage on busy queues.

For the feedback comparison we sent verified payloads into both HTB classes, then read the same quiet tree with FLOWCORE's `nsenter + tc -s -j` approach and a persistent `NETLINK_ROUTE` socket. Both readers agreed for all 40 pairs on handles, bytes, packets, queue length, backlog, drops, overlimits and requeues. The critical leaf recorded 106 bytes and one packet, the background leaf 148 bytes and two packets. Counters include control traffic and qdisc-accounted overhead. Pair order was randomized.

| Reader | Wall time p50 / p95 (ms) | CPU time p50 / p95 (ms) |
|---|---:|---:|
| tc subprocess and JSON decode | 0.873 / 1.334 | 0.887 / 1.345 |
| Persistent rtnetlink and binary decode | 0.084 / 0.098 | 0.083 / 0.096 |

The median paired tc/native ratio was 10.380 for wall time and 10.671 for CPU. The order-statistic median intervals computed with `research_stats.median_ci` were 10.221–10.802 and 10.424–11.088 respectively, with nominal coverage 0.9615 under independent sampling. Consecutive pairs share one VM and one quiet tree, so the intervals describe this run and its sampling assumptions. CPU counts the Python process and reaped subprocess time. It does not isolate kernel scheduler work. The comparison covers reading and decoding HTB counters. Production readback also validates policy fields and identity, and a loaded controller could see different contention and tail cost.

## Privilege and lifetime

The capability-free UDP process succeeded with priority 6 and monotonic transmit time. The kernel returned `EPERM` for priority 7, nonzero mark and TAI transmit time. It returned `EINVAL` for an unknown transmit-time flag. These results confirm the observed boundary without granting privileges to the SDK.

The C++23 libbpf probe created typed socket storage. A second insert through a duplicate descriptor returned `EEXIST`. The duplicate still read the value after closing the first descriptor. An unrelated socket and a new socket reusing the old descriptor number each returned `ENOENT`. Closing the descriptors and map completed with zero attached programs and zero pins. This checks observable identity and cleanup in the fixture. It does not measure memory reclamation under concurrency or verify an application trust policy.

## Relationship to FLOWCORE measurements

The previous 10-pair static30/hybrid run used a separate congestion workload and measured application P99, failure, goodput, background Jain fairness and CPU. Its median P99 was 29.950 ms for static30 and 29.519 ms for hybrid, with an interval for the paired effect spanning zero. Background goodput was 7.587 and 7.578 Mb/s, and both median Jain values rounded to 1.000. The controller made zero adjustments. The current microbenchmarks establish packet and observation semantics. Use the earlier data for the scoped adaptive comparison. No candidate kernel extension was justified, so a candidate comparison has no measurements. [Prior paired data](BENCHMARK_REPORT.md#separately-preregistered-same-allocation-check)

The C++23 probe compiled with warnings as errors and ran in the namespace fixture. The stock experiment completed, and the existing FLOWCORE suite passed 30/30. Full kernel kselftest, cross-architecture packet timing, NIC offload, loaded multi-class sampling and sustained fairness with added TCP connections remain outside these results.
