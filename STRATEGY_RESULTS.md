# Strategy engine results

Experiments ran on a four-vCPU Fedora 45 ARM64 VM with Linux 7.2.11-300.fc45.aarch64 and iproute2 7.0.0. The Research Lab used owned veth pairs and verified the installed topology. The environment records identify the base revision and mark the checkout dirty because the experiments exercised the candidate changes before commit.

## Independent holdout

Ten randomized paired blocks yielded 30 valid runs. Each arm started with the same HTB reservations. The experiment moved from 16 Mbit/s and 10 ms configured RTT to 11 Mbit/s and 14 ms while short requests competed with bulk transfers. P99 and throughput entries are medians across runs. Jain measures fairness across active bulk flows.

| Strategy | P99, ms | Failed / offered | Deadline misses | Total / background Mbit/s | Jain index | Daemon and child CPU s |
|---|---:|---:|---:|---:|---:|---:|
| Static HTB | 39.90 | 0 / 4640 | 6 | 14.507 / 13.423 | 0.999982 | 1.123 |
| Fixed queue-aware | 39.26 | 0 / 4640 | 1 | 14.542 / 13.458 | 0.999976 | 1.156 |
| Guarded queue-aware canary | 38.44 | 0 / 4640 | 0 | 14.548 / 13.464 | 0.999977 | 1.231 |

The guarded arm passed the preregistered noninferiority margins versus static. Its median paired relative P99 change was −2.87%, with a simultaneous interval of −31.05% to +4.90%. The interval includes zero, so a latency improvement remains unproven. Fixed queue-aware versus static was inconclusive for P99, with a median paired change of −4.49% and an interval of −28.07% to +42.01%. The complete comparison matrix was inconclusive. The selector kept static.

Every online run entered the candidate once and returned to the verified baseline once. Seven returned before the scheduled capacity change and three after a guard detected a different regime. Across ten runs there were zero performance guard regressions, 600 shadow evaluations and a median 25.4 ms of shadow planner CPU per 14 s trial. There were zero controller class adjustments in all 30 trials. Short observation windows did not provide the existing controller's 100-success per-service eligibility threshold. This series measures supervision overhead and behavior. It cannot establish a scheduling benefit.

The dedicated privileged integration test verified a real ceiling update under the canary. A shadow proposed 6 Mbit/s while readback remained at 12 Mbit/s. The authorized active canary then applied 6 Mbit/s, and rollback restored 12 Mbit/s in the same namespaces. Namespace holders exited after cleanup.

All 47 CTest targets passed. A staged installation reported the eight strategies and reproduced the paired comparison through the installed CLI.

## Calibration and socket validation

| Workload | Trials | P99, ms | Failed / offered | Deadline attainment | Verified socket updates |
|---|---:|---:|---:|---:|---:|
| Calibration, fixed static | 2 | 29.90 median | 0 | See raw run summaries | N/A |
| Calibration, fixed queue-aware | 2 | 25.91 median | 0 | See raw run summaries | N/A |
| ECN and socket shadows | 1 | 34.49 | 0 / 880 | 99.77% | 1 |
| Deadline and socket shadows | 1 | 33.45 | 80 / 800 | 89.88% | 2 |

Calibration had two independent pairs and was inconclusive under the preregistered ten-pair gate. The ECN run logged eight native shadow evaluations and one verified update by the active controller. The deadline run logged eight native shadow evaluations and two verified updates by the active controller. Its offered workload included 80 requests with a deliberately infeasible 1 ms deadline. Those requests remain in the failure and attainment denominators. Cross-profile previews explicitly reported unavailable inputs.

A higher-churn pilot produced a verified return to baseline about 1.01 seconds after activation when the telemetry guard fired. The recorded rollback reason was telemetry loss. The pilot used a higher rate of short connections and a smaller observation quota. Its artifacts do not identify which telemetry predicate failed.

| Evidence | Lab directory under results/ |
|---|---|
| Independent holdout | research-1794205089194888035 |
| Privileged ceiling and rollback | strategy-integration-1794205583897499506 |
| Calibration | research-1794125991885251802 |
| Higher-churn pilot | research-1794125885342950928 |
| ECN socket shadow | research-1794205069091808871 |
| Deadline socket shadow | research-1794205078648978797 |

The repository contains the manifests and reproduction commands in [ONLINE_EXPERIMENTATION.md](ONLINE_EXPERIMENTATION.md). Raw captures and reports remain on the experiment machine.
