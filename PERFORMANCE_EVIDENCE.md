# Held-out performance evidence

The [preregistered plan](config/release-rc-plan.json) froze workload
parameters, independent seeds and regression margins at 061855d.
The mixed and Hybrid V3 profiles vary offered load and capacity.
The mixed profile uses two critical services, asymmetric shaping and
TCP and UDP traffic. The application stack uses an HTTP gateway,
backend and PostgreSQL. UDP scheduling and MPTCP are evaluated in
their own comparable topologies.

The first campaign stopped after a reproducible timing-ledger defect
was identified. All 24 recorded entries, including the interrupted
entry, remain under results/research-1794270125017657985. No invalid
measurements from that campaign are promoted to evidence. The
restarted campaign retained the plan and seeds at source revision
811e70e. It then hit ENOSPC. The 40-entry mixed matrix recorded 24
measured and 16 invalid runs, and the subsequent group startup
failures are retained. Those invalid blocks prevent a release-gate
comparison. Reinstallable dashboard-browser caches and a duplicate
toolchain archive were removed. A second retry retained the original
workloads and changed only execution order and the application
output directory. Raw outputs are indexed under results/rc-811e70e
and results/rc-811e70e-retry.

## Analysis contract

Use paired within-block differences and retain all blocks, including
workload and infrastructure failures. P50/P95/P99 are for successful
attempts and require the failure and deadline denominators beside them.
A shape with many missing or rejected requests can have an attractive
conditional percentile. The primary gate requires at least ten pairs.
The manifests predeclare relative limits of 10% for P99 and goodput,
absolute limits of one percentage point for failures and five for
deadline attainment, and additional fairness and CPU guards. The
Research Lab uses run-level paired bootstrap intervals with a
multiple-comparison correction. Other lab reports use exact
order-statistic median intervals with the coverage stated, and are
exploratory when their evidence cannot satisfy a primary gate.

Linux defaults are a separate unshaped, unimpaired reference. The
controlled shaped comparisons use matching bottlenecks. Static HTB
reservations were fixed before the held-out runs. Per-message UDP
cannot be applied to an ordinary TCP echo or a PostgreSQL stream.
The MPTCP comparator uses the Linux scheduler with both endpoints
configured. CPU, memory and update timings are reported only where
the specific runner instrumented them.

Run the individual argv arrays from the preregistered plan in order
with the trusted root runner. For integrity campaigns use the
following read-only commands on the completed summary.

~~~sh
./build/flowcore bench verify results/EXPERIMENT/summary.json
./build/flowcore bench report results/EXPERIMENT/summary.json
~~~

[Measurement integrity](MEASUREMENT_INTEGRITY.md) · [Research Lab](RESEARCH.md)

## Hybrid V3 under a burst and capacity collapse

Twenty measured runs in ten paired blocks completed. Every run exceeded
the prespecified 5% workload failure limit, so the gate is
**FAILED_WORKLOAD**. Failure means an unsuccessful attempt. Deadline
attainment includes every offered attempt, including rejected ones.

| Mode | P50 / P95 / P99 (ms) | Deadline attainment | Failed / offered | Background / total goodput (Mbit/s) | Jain index |
| --- | ---: | ---: | ---: | ---: | ---: |
| Static HTB | 53.10 / 3806.78 / 4530.74 | 45.78% | 807 / 5330 | 4.20 / 8.58 | 0.9962 |
| Hybrid V3 | 56.11 / 3767.25 / 4399.21 | 45.40% | 805 / 5330 | 4.17 / 8.57 | 0.9969 |

Values are run medians except counts. The descriptive paired P99 delta
was −192.46 ms with a marginal 97.85% interval [−286.16, +9.02].
All P99 pairs contain failed requests and cannot pass the primary
latency gate. No controller action occurred in either arm. Daemon and
child CPU medians were 0.968 s and 0.959 s, with peak daemon RSS
36.54 and 35.88 MiB. Scheduled stimulus update/readback medians were
64.64 and 62.09 ms. This measures the fault injector, with no active
controller update to time. SLO recovery was right-censored. Raw
directory is results/research-1794271034359405578.

## UDP per-message classification on IPv4

Ten randomized blocks used fresh seeds on the existing saturated
12 Mbit/s workload shape. All 60 trials completed and the host network
fingerprint matched. The raw per-probe RTTs were used to derive P50 and
P95. Counts are sums across the ten runs. Other columns are medians.

| Mode | P50 / P95 / P99 (ms) | Probe deadline attainment | Failed / offered | Background Mbit/s | Worker CPU seconds |
| --- | ---: | ---: | ---: | ---: | ---: |
| Shaped fq_codel | 35.62 / 66.32 / 91.11 | 0.00% | 1988 / 2400 | 9.288 | 0.232 |
| CAKE best effort | 208.05 / 275.40 / 278.43 | 0.00% | 1983 / 2400 | 9.242 | 0.224 |
| CAKE DiffServ | 0.865 / 1.265 / 2.338 | 100% | 0 / 2400 | 9.031 | 0.234 |
| Static HTB | 132.67 / 189.09 / 200.83 | 0.42% | 1807 / 2400 | 8.170 | 0.238 |
| Direct intent prototype | 0.146 / 0.362 / 0.870 | 100% | 0 / 2400 | 9.072 | 0.239 |
| FLOWCORE SDK | 0.147 / 0.428 / 0.949 | 100% | 0 / 2400 | 9.072 | 0.283 |

The SDK's paired P99 delta versus CAKE DiffServ was −1.604 ms,
marginal 97.85% interval [−2.421, −0.472]. Paired background goodput
increased by 0.041 Mbit/s [0.029, 0.058] and worker CPU increased by
0.053 s [0.042, 0.073]. Against the direct prototype the P99 interval
crossed zero and worker CPU increased by 0.044 s [0.017, 0.068].
Jain fairness rounded to 1.000 across all modes. The process-level
peak RSS was 37,596 KiB and is a shared high-water mark, not a
per-policy memory estimate. The loss-heavy arms are ineligible for
a success-only latency gate. These observations support a scoped
latency/CPU tradeoff for the tested queue configuration. They do
not isolate the SDK from its classifier or establish an improvement
across traffic shapes or address families. Raw directory is
results/udp-intent-1794271329877029141.


## Routed MPTCP path reliability preference

Thirty runs in ten randomized blocks used the routed-collapse lab,
a reliability preference and an 85 ms request deadline. All 30
connections survived and no request failed. Closed-loop stalls change
the number of offered requests, so keep the denominators visible.

| Mode | P50 / P95 / P99 (ms) | Deadline attainment | Misses / offered | Request / bulk goodput (Mbit/s) | Recovery (ms) |
| --- | ---: | ---: | ---: | ---: | ---: |
| Fixed endpoint preference | 51.72 / 546.95 / 1560.32 | 92.98% | 41 / 558 | 1.623 / 2.930 | 1105.29 |
| Default Linux MPTCP | 60.18 / 96.14 / 766.82 | 93.80% | 39 / 628 | 1.842 / 2.956 | 137.46 |
| Path-aware | 56.91 / 69.51 / 603.64 | 97.12% | 23 / 724 | 2.102 / 2.944 | 125.94 |

The paired P99 change was −923.23 ms against fixed preference
[−1410.22, −557.92], but −121.22 ms against Linux default
[−369.64, +34.39]. These are marginal 97.85% intervals. P99 benefit
against Linux default is inconclusive. Path-aware P50 increased by
5.11 ms [4.08, 5.53] against fixed preference. Bulk Jain fairness
was about 0.9999 throughout. One preference update per path-aware run
verified in a median 20.37 ms. The median parent and command CPU
totals were 267 ms and 390 ms over a nine-second trial, versus
255 ms and 357 ms for the observed Linux default. This lab did not
instrument peak policy RSS. The host fingerprint matched. Raw
directory is results/dynamic-path-1794271473082541068.

## Unshaped Linux reference

Three VM-only reference trials had no capacity cap or netem. Median
P50/P95/P99 was 0.512/0.824/1.552 ms, and all 1,620 requests met
their deadlines. Median aggregate payload was about 14.6 Gbit/s
on virtual links. These numbers characterize that unshaped veth
workload. They are outside the shaped gate and do not measure physical
line speed. Raw directory is results/research-1794271804578041977.

## Mixed TCP/UDP collapse with two critical services

The final forty-run campaign had ten complete, verified blocks. The
5% workload failure threshold was exceeded by all ten CAKE, static
HTB and optimizer runs, yielding **FAILED_WORKLOAD** for the primary
comparison. All shaped fq_codel runs were valid. These failures remain
in the ledger and descriptive table.

| Mode | P50 / P95 / P99 (ms) | Deadline attainment | Failed / offered | Background / total goodput (Mbit/s) | Jain index |
| --- | ---: | ---: | ---: | ---: | ---: |
| Shaped fq_codel | 83.51 / 894.02 / 2137.12 | 58.02% | 0 / 6670 | 3.84 / 8.15 | 0.9639 |
| CAKE | 76.33 / 2443.02 / 2696.72 | 50.67% | 550 / 6670 | 3.49 / 7.41 | 0.9687 |
| Static HTB | 49.09 / 3603.43 / 4573.35 | 52.92% | 567 / 6670 | 3.89 / 8.01 | 0.9974 |
| Optimizer | 48.22 / 3568.73 / 4637.43 | 52.85% | 550 / 6670 | 3.88 / 8.00 | 0.9963 |

Against static, the optimizer's descriptive paired conditional P99
change was +76.56 ms, marginal 97.85% interval [−44.33, +165.75].
Against fq_codel it was +2557.92 ms [2379.25, 2631.98]. The optimizer
also had an 8.10 percentage point paired failure increase over
fq_codel [7.65, 9.15] and a 4.20 point attainment loss [2.55, 6.90].
It preserved a better median TCP background Jain index and received
more UDP payload, with median UDP goodput of 0.65 versus 0.50 Mbit/s.
No optimizer policy update occurred. The tradeoff describes the tested
configurations, with no evidence of an active optimization gain.

Median CPU for daemon and children was 1.14 s with the optimizer,
versus 1.08 s for static and fq_codel over ten seconds. Median peak
daemon RSS was 37.83 MiB, versus 36.08 MiB static and 34.23 MiB
fq_codel. Median scheduled stimulus update time ranged from 12.69 ms
with CAKE to 35.26 ms with the optimizer. Version 3 event ledgers
record recovery and capacity readback timing, while this profile has
no independent per-mode SLO convergence estimate. The primary P99
gate cannot infer non-regression after failures. Raw directory is
results/research-1794271830208968921.

## HTTP gateway and PostgreSQL

The initial application retry had twenty invalid startup records
because a relative schema path was interpreted as a named Podman
volume. The absolute-bind fix then produced five measured runs
before the campaign was stopped to contain anonymous-volume growth.
Both datasets are retained and excluded from this final comparison.

The clean comparison used source c7dba30, identical workload seeds,
a pooled gateway/backend and a PostgreSQL fixture on bounded tmpfs.
All twenty trials measured successfully. The Podman volume inventory
was unchanged across the checked interval, and the host network
fingerprint matched. This benchmark exercises the request path,
without characterizing production database disk I/O.

| Mode | HTTP P50 / P95 / P99 (ms) | HTTP deadline attainment | HTTP failed / offered | Bulk Mbit/s | PostgreSQL P99 (ms) / query rate |
| --- | ---: | ---: | ---: | ---: | ---: |
| Static HTB | 13.59 / 42.76 / 52.97 | 81.25% | 617 / 3360 | 1.763 | 1195.56 / 14.0 qps |
| Hybrid | 13.78 / 43.93 / 53.66 | 81.40% | 613 / 3360 | 1.764 | 1198.50 / 14.0 qps |

The marginal 97.85% interval for the paired HTTP P99 change was
[−12.37, +7.03] ms, with median −0.36 ms. P99 benefit is
**INCONCLUSIVE**, with many failures and zero controller actions.
Median deadline attainment misses the application target in both arms.
PostgreSQL had 0 failures in 1,120 offered queries per arm, but
171 static and 172 hybrid queries missed their budget.

Median daemon CPU was 1.78 s static and 1.81 s hybrid. Daemon RSS
was 34.70 and 36.02 MiB respectively. Bulk transfer completion
was about 14.27 s per run. Median scheduled kernel fault
update/readback took about 81 ms in each arm. Median HTTP recovery
was 62.94 and 63.08 ms respectively. Raw directory is
build/release-audit/results/rc-app-tmpfs, including paired intervals,
component CPU/memory, fault timestamps and request ledgers.

## Retained artifacts and revisions

The six final campaigns contain 173 measured trials, including
workload-failure outcomes. The mixed campaign digests verified
with the unprivileged evidence reader. The unshaped reference
retains legacy artifacts outside the V3 digest check. The Hybrid V3
digests verified with the trusted runner because its raw pacing
inputs include root-only files. UDP, MPTCP and application directories
retain their runner-native artifacts and separate analysis scope.

The core matrices used 811e70e. Reproduce the final application
matrix on c7dba30, which includes the schema and volume fixes, and
choose a fresh output path. Earlier application outputs remain at
results/rc-application-725003425-retry and
build/release-audit/results/rc-app-final. The chronological retry
index is results/rc-811e70e-retry/status.jsonl. Raw evidence currently
lives on the validation machine. Public reproduction can start from
the committed plan. Publishing a reviewed raw bundle remains a
release gate.
