# Next-generation benchmarks

The Research Lab runs a latency-sensitive TCP request/response service and bulk transfer service on distinct ports in disposable veth namespaces. Two critical service labels, interactive and standard requests, and concurrent bulk streams compete on the shaped sender egress. The receiver netem adds delay to the reverse path. Each application attempt has a 36 ms deadline. The workload records latency, failures and deadline attainment from the client ledger.

The main matrix fixes Classic ECN and identical HTB class reserves before comparing static, the existing queue-aware class controller, the existing ECN-aware socket controller and the integrated socket controller. It uses ten independently seeded paired blocks in randomized order. The configured capacity falls from 12 to 8 Mbit/s with increased reverse delay and 0.15% reverse netem loss at 10 seconds. Offered load rises from 24 to 33 requests per second and one to six background streams. This workload and seed are disjoint from the smoke used to check operation.

The scheduler matrix runs fq_codel, CAKE and two static allocations. Static40 was selected by earlier project calibration and is a reference configuration in this new workload. This matrix uses two pairs and the normal namespace ECN default. The unshaped Linux veth default runs separately with no netem. Its throughput and latency are descriptive and cannot be interpreted as a same-bottleneck comparison.

## Reproduction

~~~sh
cmake -S . -B build
cmake --build build -j3
ctest --test-dir build --output-on-failure
sudo ./build/flowcore bench replay scenarios/nextgen-smoke.json
sudo ./build/flowcore bench replay scenarios/nextgen-holdout.json
sudo ./build/flowcore bench replay scenarios/nextgen-qdisc.json
sudo ./build/flowcore bench replay scenarios/nextgen-linux-default.json
sudo ./build/flowcore bench replay scenarios/nextgen-observer.json
sudo ./build/flowcore bench replay scenarios/nextgen-collapse.json
./build/flowcore nextgen report results/<experiment>/summary.json
~~~

Benchmark runs retain the manifest, request ledger, kernel topology, host noise, per-run summaries, socket decision log and transition timeline. Use the path printed by each replay. P99 distributions use independent runs, and the paired summary adjusts confidence for all planned metrics and modes. Failure rates and deadline attainment include failed requests. When a run has failures, the successful-only P99 remains descriptive and the existing paired P99 gate omits that pair.

The control cost breakdown separates sample RPC elapsed time, observer elapsed time, socket sampler CPU, native decision CPU, verified group actuation wall time and daemon CPU/RSS. Utilization is observed sender egress relative to configured shaping in each readback window. The delay from a verified kernel rebase to a verified socket fallback measures control convergence for that observed handoff. It does not measure when queues or TCP congestion windows stabilize.


## Measured holdout on Fedora ARM64

Fedora 45, Linux 7.2.11-300.fc45.aarch64, four vCPUs and iproute2 7.0.0. All 40 trials were valid. Each arm offered 4,340 requests with zero transport failures. P50/P95/P99 and other continuous measurements below are medians of the ten run summaries. Deadline misses are summed across runs.

| Policy | P50 / P95 / P99 ms | Deadline misses | Total / background Mbit/s | Jain bulk fairness | Daemon and child CPU s | Peak daemon RSS MiB |
|---|---:|---:|---:|---:|---:|---:|
| ECN static | 30.48 / 40.44 / 46.45 | 1248 | 10.229 / 9.214 | 0.99995 | 1.168 | 33.96 |
| Existing queue-aware | 30.32 / 40.38 / 46.14 | 1214 | 10.251 / 9.238 | 0.99991 | 1.203 | 35.94 |
| Existing ECN-aware | 30.59 / 40.14 / 46.18 | 1081 | 8.638 / 7.625 | 0.99997 | 1.211 | 34.44 |
| Integrated | 30.33 / 40.47 / 44.84 | 1228 | 9.719 / 8.706 | 0.99990 | 1.150 | 34.70 |

Relative to ECN static, integrated P99 had a paired median change of −1.22% with a simultaneous interval from −51.06% to +44.15%. The P99 gate is **inconclusive**. Integrated total goodput decreased by a paired median 4.92%, interval 3.55% to 6.16%, within the predeclared 10% noninferiority margin. Deadline attainment and failure gates passed. The old ECN-aware arm exceeded the allowed goodput regression with a 15.35% decrease, interval 13.41% to 17.08%, and had fewer deadline misses than integrated. Fixed queue-aware P99 was inconclusive and it made zero adaptive class changes. The complete matrix is **REGRESSION** because the old ECN-aware arm failed its goodput gate. Static remains the reference.

Integrated made 27 verified cap adjustments, seven verified rollbacks and ten verified fallbacks. All 27 adjustments coincided with both qualified ECN feedback and deadline pressure in the recorded samples. That coincidence does not identify which input caused the outcome. Each scheduled capacity change had a verified socket compensation, with median kernel-confirmation-to-compensation time of 960 ms. The existing ECN-aware arm made 56 verified updates. Median observed utilization was 95.64% integrated, 99.15% static and 83.37% existing ECN-aware. Integrated median native decision CPU was 0.95 ms per trial, group actuation wall time 0.58 ms and socket sampler CPU 44.53 ms. Median daemon observer elapsed time was 7.60 ms per sample and sample RPC time 10.16 ms. These timings include the Python-to-native call boundary where applicable.

The holdout used the initial class-rate and background leaf guard. The stricter committed guard additionally checks every flower rule and leaf. Replaying it over 160 healthy captured holdout samples admitted all 160. The separate live smoke tests the final guard. The candidate was measured from an uncommitted checkout based on d509a2b. Raw evidence and reports are under `results/research-1794205990504538236`.


## Scheduler baselines

The separate shaped scheduler study had eight valid trials and zero failures. Values are medians across two runs per arm. These comparisons are exploratory because two pairs cannot meet the ten-pair gate.

| Shaped policy | P99 ms | Deadline attainment | Background Mbit/s |
|---|---:|---:|---:|
| fq_codel | 66.65 | 11.18% | 9.167 |
| CAKE | 64.34 | 11.64% | 9.151 |
| Static HTB, 30% critical | 41.23 | 72.93% | 9.265 |
| Static HTB, 40% critical | 46.89 | 77.65% | 9.186 |

The shaped scheduler matrix is **INCONCLUSIVE**. Raw evidence is under `results/research-1794206664295295230`. The experiment uses the same application phases and fault schedule as the main matrix. Its different seed blocks and ECN configuration limit cross-matrix comparisons.


The separate Linux-default noqueue pair was valid, with no request failures and median P99 7.59 ms. Median background goodput was 13.98 Gbit/s on the unshaped virtual link. This is an unshaped, unimpaired veth reference under bulk traffic. Raw evidence is under `results/research-1794206796773833517`.

## Safety and capability checks

The final scope guard passed a live trial with 434 offered requests, zero failures, four verified cap adjustments, two verified outcome rollbacks and one verified capacity-transition fallback. The smoke is a functional exercise. Its raw evidence is under `results/research-1794206830829454171`.


A separate 12→2 Mbit/s collapse trial confirmed one pacing update before the fault and one verified fallback after it. Every subsequent sample held the restored cap because the original protected floor exceeded the new link capacity. It recorded 177 deadline misses and zero transport failures out of 434 attempts. The controller cannot restore the lost link capacity. Raw evidence is under `results/research-1794206882201243410`.

| Validation | Outcome |
|---|---|
| Fedora ARM64 C++23 build and CTest | 48/48 passed with BPF enabled and 48/48 passed with BPF disabled. Native tests cover deadline pressure, stale scope, outcome rollback, conflicting authority and bound preservation. |
| Invalid deadline, foreign filter, partial socket write | Rejected inputs or compensated writes in deterministic tests. An unverifiable compensation was surfaced as a failure. |
| Daemon crash, partial HTB, concurrent changes, namespace deletion | Existing privileged safety harness passed concurrent CAS, SIGKILL during class install, restart, veth deletion during sampling and stale ownership rejection. This exercised the common backend. |
| MPTCP | Loopback negotiated MPTCP with an MPTCP peer and verified TCP fallback with a TCP peer. The separate privileged three-path failover and ownership fixture passed. |
| Staged installation | Installed capability and report commands passed. Active installed kernel policy and RPM transaction validation remain separate release gates. |
| Release gate | Enforcement exited 1 with existing partial gates. |


The first observer-auto trial ran with the build's original BPF option disabled. The observer explicitly reported unavailable CO-RE artifacts, the integrated trial completed with INET_DIAG, and the standalone TCX fixture refused to attach. Capability discovery now honors the build marker even when old BPF artifacts remain in the build directory. That fallback trial is retained under `results/research-1794206847478149959`.

With BPF rebuilt and enabled, the matched-seed smoke processed 4,392 tracepoint events with zero recorded reserve, transport, decode or parse losses. Collector CPU increased by 0.04 s at procfs sampling resolution and peak collector RSS was 5.10 MiB. Total daemon and child CPU was 1.770 s in the active-BPF smoke versus 1.089 s in the BPF-off final smoke. These are individual runs with different socket action counts. They cannot isolate the causal cost of BPF. Raw active-observer evidence is under `results/research-1794206937673989597`. The separate TCX dual-stack and churn test passed under `results/flow-engine-1794206953979320799`.
