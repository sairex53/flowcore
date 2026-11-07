# Network policy optimization results

## Paired Linux performance experiment

Run on a disposable host with the same ownership and traffic checks used by the research harness.

```sh
cmake -S . -B build -G Ninja
cmake --build build
sudo ./build/flowcore bench run scenarios/policy-optimizer-paired.json
```

The frozen manifest used a 10 Mbit/s HTB parent, 4 ms base RTT, 8,192-byte requests, a 25 ms deadline and three workload phases over 18 seconds. Offered request rates were 80, 120 and 55 requests per second with four, four and two concurrent bulk streams. New connections were used per request. The same workload seed was used across the five modes in each block, and mode order was shuffled. Static40 uses the 40% critical reservation selected in the earlier independent static pilot. The other modes use the 30% default critical reservation, so comparisons against the default HTB mode also control for the initial allocation. [Prior pilot](BENCHMARK_REPORT.md)

Execution used Fedora 45 ARM64 on QEMU with four vCPUs, kernel `7.2.11-300.fc45.aarch64`, tc 7.0.0 and BPF telemetry off. All 15 runs were valid with 5,220 requests offered per mode. The clean source commit was `26f5872d6b9659764e555d07ebeab878354e1e96`. Block seeds were 2916702701, 1335735479 and 169985179.

| Mode | Median successful P99 | Failures / offered | Median deadline attainment | Median background goodput | Median background Jain index |
| --- | ---: | ---: | ---: | ---: | ---: |
| Default HTB + fq_codel | 4,547 ms | 458 / 5,220 | 19.71% | 3.317 Mbit/s | 0.99975 |
| Static40 | 2,796 ms | 3 / 5,220 | 26.90% | 2.966 Mbit/s | 0.99939 |
| Existing SLO heuristic | 4,461 ms | 336 / 5,220 | 21.21% | 3.258 Mbit/s | 0.99984 |
| Queue-aware | 4,468 ms | 328 / 5,220 | 19.37% | 3.223 Mbit/s | 0.99974 |
| Adaptive optimizer | 4,581 ms | 451 / 5,220 | 20.17% | 3.307 Mbit/s | 0.99976 |

P99 values summarize successful requests. Deadline attainment uses all offered requests. The Jain index describes background flows and should be read alongside their aggregate goodput. Absolute tail latency was high under this workload.

The optimizer's paired P99 difference against static40 was +58.2%, +67.6% and +63.9%. Against default HTB + fq_codel it was −0.4%, +0.2% and +1.7%. Static40 had the best observed tail latency and failure count in all three blocks. The harness marked the comparison **INCONCLUSIVE** against its predeclared ten-pair gate. Three blocks give descriptive paired differences and leave uncertainty about generalization.

The optimizer made zero policy writes during the comparison. Twenty-four of its 30 hold records came from truncated socket snapshots with background counters present. Five held for lack of eligible service pressure. One otherwise feasible transfer was blocked by the directional egress gate. The SLO heuristic and queue-aware controller each applied three verified probes and reverted each as inconclusive. The relative optimizer results characterize holding the default allocation under these conditions. Measuring the network effect of optimizer actions requires trials with sufficient observation coverage.

## Measured decision cost

The 30 optimizer records timed all four native solves and the Python bridge together. The median was **344 µs**, with a range of **120–2,736 µs**. The live timeline's original policy-stage medians were 40.65 ms for the optimizer, 28.80 ms for queue-aware and 0.154 ms for the SLO heuristic, each over 27 distinct decisions. That stage included the synchronous journal write for recorded holds. The SLO heuristic did not record every hold. Later runtime instrumentation exposes planner and journal time separately. These timing samples describe the test VM and include scheduler effects. The native bridge timing excludes telemetry collection, journaling and kernel changes.

## Verified actuation under controlled pressure

The privileged integration test ran competing TCP transfers in an owned veth pair and fed a fixed authorized SLO trace. It verified a change from 6,000 to 7,000 kbit/s for critical and from 6,000 to 5,000 kbit/s for background at a 20,000 kbit/s parent. The critical 9,000 kbit/s ceiling and the 4,000 kbit/s background floor remained in force. Both traffic classes carried actual traffic. Preview and shadow left rates untouched. Active mode restored the original reservations with the ceiling intact and refused to overwrite a subsequent external parent rate change. The test checks control behavior and kernel accounting. Application effects come from the separate paired experiment.

## Evidence

- Raw results and reproduction snapshot are in `results/research-1794037001657966217/`.
- Summary SHA-256 is `b6aa32991d76edce9a96860aa3458ec800d1077638947f1fde387f7b266f1035`.
- The directory contains request ledgers, namespace and TC readbacks, decisions, CPU samples, CSV and the HTML report. [Solver and transaction tests](OPTIMIZER_TESTS.md)
