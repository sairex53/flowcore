# Autotuning results

## Isolated Fedora ARM64 search

The engine ran a seeded random search with three candidate reservations: critical HTB minimum 20%, 23% and the manual 30% baseline. The test used ten independent paired workload blocks per candidate. Candidate order was shuffled within each block; every candidate in a block received the same workload seed. All 30 runs were valid. Each mode offered 1,800 requests across its ten runs and had zero failed requests.

The training environment was Fedora 45 ARM64 in a QEMU VM with four vCPUs, kernel 7.2.11-300.fc45.aarch64, and tc/iproute2 7.0.0. The isolated veth bottleneck was configured at 10 Mbit/s with 4 ms base RTT, no configured loss or jitter, two background flows, and 30 critical requests/second. Each run lasted six seconds. The training source seed was 1101; the generated block seeds are retained in the raw summary. BPF was disabled. The source tree was dirty during the experiment at base commit 46035c5c59223178c0cdbff870e83ef10be88b1d (the recorded autotune.py SHA-256 is in the source snapshot), so this is exploratory evidence, not a clean-release benchmark.

| Critical minimum | Median P50 | Median P95 | Median successful P99 | Failures / offered | Median deadline attainment | Median background goodput | Median Jain index |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 20% | 12.66 ms | 20.20 ms | 26.56 ms | 0 / 1,800 | 98.33% | 1.659 Mbit/s | 1.000 |
| 23% | 12.55 ms | 20.36 ms | 26.59 ms | 0 / 1,800 | 98.33% | 1.647 Mbit/s | 1.000 |
| 30% manual | 12.55 ms | 18.20 ms | 26.63 ms | 0 / 1,800 | 98.89% | 1.660 Mbit/s | 1.000 |

The existing paired P99 comparison gate on the same summary was also INCONCLUSIVE: the median relative latency change was a +0.77% regression for 20% versus 30%, with a simultaneous median interval from −52.0% to +495.7%; no P99 improvement was supported.

The run persisted paired weighted objective changes of −0.0055 for 20% (95% paired run-block bootstrap interval −0.1303 to +0.0311) and −0.0237 for 23% (−0.2863 to +0.0733). After the experiment, the relative-change denominator was bounded symmetrically to prevent extreme scores near zero; re-scoring the same retained blocks with that updated formula gives −0.0031 (−0.0924 to +0.0309) for 20%, and −0.0196 (−0.1174 to +0.0685) for 23%. Both scoring versions include zero in each interval, neither reaches the minimum +0.01 objective improvement, and the selected manual baseline is unchanged. The original job record remains unmodified as-run evidence. The engine retained the 30% manual reservation. These short runs did not establish that 30% is generally better; P99 varied substantially across individual blocks.

The plan included a separate holdout with 8 Mbit/s capacity and 45 requests/second. It was not executed because the training choice was exactly the manual baseline, so the engine correctly declined to compare a policy against itself. The holdout outcome is INCONCLUSIVE and provides no generalization evidence. Overall autotuning outcome: **INCONCLUSIVE**; no new policy recommendation was validated.

## Runtime and artifacts

The search took 225.6 seconds wall time for 180 seconds of configured workload. Measured FLOWCORE daemon CPU summed to 13.17 seconds; maximum observed daemon RSS was 36.2 MB; retained artifacts occupied 7,631,942 bytes. These are daemon and artifact measurements, not whole-host CPU/memory caps.

- Plan: build/autotune-plan.json; SHA-256 d7ef46e4e9d33af08c10669f24a2d1892ec00051c04f378320d5f719268ee06c
- Training summary and raw runs: results/autotune-1794040553543846279/
- Training manifest SHA-256: 508a13a273fea1774537cfc49797edfb25390d4e4e57c3d565a9e03a7006a31e
- Holdout manifest SHA-256 (validated but not run): 026559cf9b593ce021d7afe61d61b07e0e0aa78a8dac56d7f783d260406ee06a
- Reproduce the plan: sudo ./build/flowcore optimize run build/autotune-plan.json

The raw output contains environment and source digests, randomized run order, all request ledgers, per-run snapshots and metrics, reproduction commands, candidate scores and the final job state. A later single-policy ten-block smoke run also completed and verified private owner-readable job state and CLI status access; it is not a policy comparison. No raw experiment data was added to Git.

## Interpretation

This trial verifies the end-to-end search workflow and its evidence accounting on one VM workload. It does not show an improvement over the manual baseline, does not validate the unrun holdout, and is not hardware or production evidence. An optimization win remains unproven.
