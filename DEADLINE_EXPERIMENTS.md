# Deadline experiments

## Reproduce

~~~sh
cmake --build build -j
ctest --test-dir build --output-on-failure
sudo python3 -B tests/deadline_lab_test.py
sudo ./build/flowcore bench replay scenarios/deadline-paired.json
sudo ./build/flowcore bench replay scenarios/deadline-pressure.json
~~~

Run from the repository. The benchmark uses the existing owned two-namespace veth topology. Client requests and bulk data share sender egress. The receiver applies reverse-path netem with a 6 ms configured delay. The primary HTB bottleneck starts at 12 Mbit/s and is reduced to 8 Mbit/s at second four. Each eight-second measured trial offers 800 requests, 4 KiB uploads and 64-byte replies over new TCP connections. Eight TCP bulk streams compete during the four-second burst. The two critical services share one class and have distinct labels in the raw ledger. One request in ten has a 1 ms budget, while the rest have 40 ms. The fixture declares a 6 ms connection reserve, which exceeds the tight budget. The experiment deliberately models a mix of feasible and infeasible application intents. It does not claim that every connection establishment takes 6 ms.

Six independent paired blocks use counterbalanced order and shared seeds within each block. All modes log advisory estimates to keep instrumentation common. The deadline-unaware baseline uses a single shaped fq_codel leaf. The other five use the same initial HTB+fq_codel split, including a 40% critical reservation and 20% background reservation. The existing controller enables hybrid class adaptation. Paced static, admission only and admission plus adaptation start with the same 650,000 B/s per-socket bulk cap. The last two honor rejection and bounded deferral. Comparing paced static with admission isolates cooperative shedding at that cap. Comparing admission with adaptation isolates the incremental socket controller.

## Measured paired results

Fedora 45 ARM64 VM, Linux 7.2.11-300.fc45.aarch64, four vCPUs. All 36 trials passed topology, classifier, capacity and workload validation. BPF was off. Rows are medians of six independent trial summaries. Latencies include dispatch, connection and request completion for successful requests. Attainment and completion are percentages of all 800 offered requests per trial. CPU is process CPU time per trial, in seconds. Total goodput combines delivered bulk bytes and completed application request payload within the measurement window.

| Mode | P50 ms | P95 ms | P99 ms | Attainment % | Completion % | Background Mbit/s | Total Mbit/s | Daemon CPU s | Client CPU s |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Deadline unaware | 49.54 | 100.77 | 113.71 | 32.69 | 100 | 5.95 | 9.22 | 0.779 | 1.261 |
| Static QoS | 15.46 | 29.89 | 38.34 | 89.12 | 100 | 6.01 | 9.29 | 0.676 | 1.220 |
| Existing hybrid QoS | 15.59 | 31.49 | 40.82 | 88.69 | 100 | 5.99 | 9.26 | 0.823 | 1.221 |
| Paced static | 15.67 | 30.53 | 38.65 | 89.31 | 100 | 4.81 | 8.09 | 0.738 | 1.232 |
| Admission only | 15.59 | 29.22 | 37.77 | 88.19 | 88.75 | 5.19 | 8.08 | 0.738 | 1.220 |
| Admission + adaptation | 15.46 | 28.23 | 37.96 | 89.44 | 90.00 | 5.13 | 8.08 | 0.634 | 1.048 |

Each mode offered 4,800 requests over the series. Admission only rejected 510 (10.63%) and deferred 129 (2.69%) after retry. Admission plus adaptation rejected 503 (10.48%) and deferred 109 (2.27%). There were 480 deliberately infeasible short budgets per mode. The remaining rejections occurred among ordinary-budget requests after dispatch or the bounded wait exhausted the declared reserves. Neither of these outcomes is counted as a transport failure. Every sent request completed in all six modes. Median Jain bulk-flow fairness was 0.9970–0.9989. Estimator CPU, including the Python adapter and amortized feed reads, had per-trial medians of 122–141 ms (rounded). Every arm collected this metric, so the table cannot isolate daemon or controller overhead from ordinary run variation.

The predeclared outcome is **INCONCLUSIVE**, with six valid pairs and a gate requiring at least ten. The 36 trials were retained, including runs flagged for noise. The unadjusted paired median P99 difference of adaptation minus static QoS was **+0.41 ms**, with observed differences from −17.15 to +77.41 ms. Attainment was +0.06 percentage points paired median, with an observed range of −14.25 to +1.75 points. The paired median completion and background goodput changes were −10 percentage points and −0.88 Mbit/s. The median P99 difference for adaptation versus admission alone was +0.17 ms (range −87.96 to +88.83 ms). These ranges show the six observed paired differences and do not serve as simultaneous confidence intervals. Differences of the column medians and medians of paired differences answer different questions.

In the 40 ms cohort, static QoS completed 4,274 of 4,320 on time, admission only completed 4,002 and adaptation completed 4,124. That cohort exposes losses masked by reporting P99 over only completed requests. Across the primary series the adaptive pacing mode verified nine adjustments via the ordinary queue/SLO branch. The deadline-pressure flag was present in one held observation with duplicate feedback and did not contribute a verified early action. Existing hybrid QoS made zero class adjustments during these short windows. The primary series therefore measures the tested policies as executed, with no isolated evidence of a deadline-specific adaptation benefit. Roughly half of the adaptive mode's initial estimates lacked sufficient scoped evidence. Capacity transitions, short-lived flows, cold rate windows and the distinction between throughput and available capacity limit inference.

## Deadline-pressure stress case

The separate two-block stress scenario used 8 Mbit/s, a 20 ms normal budget and 220 requests/s during an eight-second burst with six bulk flows. All four runs were valid. Admission only had median successful P99 of 2,756 ms, 13.20% offered deadline attainment, 75.26% completion and 2.92 Mbit/s background goodput. Admission plus adaptation had 3,559 ms, 13.92%, 78.02% and 2.63 Mbit/s, respectively. The two paired P99 differences were +65 ms and +1,540 ms. The two paired attainment differences were −0.98 and +2.42 percentage points. Admission only recorded 539 rejected and 334 deferred out of 3,880 offered, with one sent-request failure. Adaptation recorded 377 rejected and 321 deferred, with four sent-request failures. This stress case incurred severe deadline misses under both policies.

One run produced a verified early action with 75,056 bytes of root backlog and a per-socket pacing cap change from 650,000 to 568,750 B/s. Four additional adjustments came from the ordinary branch. The pressure experiment shows that the early branch executes in a real namespace, while the two pairs remain inconclusive about effects attributable to that specific action. The deadline-aware policy yielded worse successful-request tails in both stress blocks. The full stress record is in results/research-1794124132442040854.

## Raw evidence and scope

The Linux checkout preserves the initial pilot in results/research-1794123556305882061 and the paired experiment in results/research-1794123743129730816. Each contains the manifest, seed schedule, reproduction command, run summaries, per-request CSV, class and qdisc readbacks, receiver counters, controller and pacing logs, host pressure samples and HTML report. The recorded Git base is ca90583 with a dirty tree containing the implementation under test. Raw artifacts remain local to the Linux checkout.

The benchmark models an owned local egress bottleneck. It does not establish effects on remote networks, different transports, HTTP/2 stream scheduling, physical NICs or production services. The SDK regression tests cover parallel logical streams on a shared connection, cooperative cancellation, reconnect and reattestation, missing evidence and exact deadline accounting. They are functional tests, separate from the comparative measurements.
