# Measurement Integrity V3: observed results

## Scope and environment

The main paired TCP matrix was executed on Fedora 45 ARM64 in a QEMU virtual machine: kernel 7.2.11-300.fc45.aarch64, four visible vCPUs, 1 ns nominal CLOCK_MONOTONIC resolution, and FLOWCORE 0.2.0. This is VM validation, not physical ARM64 or x86_64 validation.

The run used a configured 12 Mbit/s sender bottleneck, 6 ms base RTT impairment, 8 KiB request payloads, 42 new TCP requests/s, four bulk TCP streams, a 75 ms request deadline, one-second warmup and cooldown, and 12 counterbalanced paired blocks per mode. There were 252 offered requests per arm per block. The mode order was randomized/counterbalanced across blocks. The baseline was the configured static HTB allocation, compared with CAKE, HTB plus fq_codel, and HTB plus FIFO. Each run retained the actual tc hierarchy and interface counters.

The full raw experiment is retained locally at results/research-1794212606825782656 and is ignored by Git. It contains 48 checksummed run directories. Its original Git metadata reports commit 1c411bf9ddb7627e7a4ba72190daac7fc1832697 and a dirty worktree. Source hashes in the experiment metadata identify the source files read at run start. Later analysis-source hashes are recorded in report.json. The manifest is stored with the summary; this original capture predates the separate experiment-manifest hash field. Do not treat this artifact as a clean-commit release benchmark.

## Measured per-run medians

Values below are medians over 12 run-level measurements. Request percentiles are successful-request P50/P95/P99 for each run, then summarized across runs. Failure and deadline rates use the full offered request cohort.

| Policy | P50 ms | P95 ms | P99 ms | Deadline attainment | Request failure ratio | Total goodput Mbit/s | Background goodput Mbit/s | FLOWCORE/child CPU seconds |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Static HTB | 20.12 | 24.08 | 28.36 | 100% | 0% | 11.306 | 8.559 | 0.430 |
| CAKE | 40.00 | 43.83 | 47.90 | 100% | 0% | 11.371 | 8.629 | 0.453 |
| HTB + fq_codel | 36.79 | 45.01 | 54.16 | 99.6% | 0% | 11.251 | 8.509 | 0.435 |
| HTB + FIFO | 75.70 | 85.71 | 91.87 | 35.9% | 0% | 11.202 | 8.482 | 0.447 |

Run-level CPU seconds are the runner's existing daemon/child accounting. Generator CPU, context switches and RSS are separately present in each client.json and measurement summary. These values are not total-host CPU percentages and should not be interpreted as FLOWCORE-only cost.

## Paired regression analysis

Each six-second block offered 252 requests. A per-run nearest-rank P99 therefore depends on only a few observations near the upper tail; the run-level confidence intervals do not remove that within-run tail uncertainty. The P99 evidence is exploratory and should be repeated with longer cohorts before release or generalization.

The paired bootstrap interval is Bonferroni-adjusted for 21 planned comparisons (three candidates by seven gates), giving 99.762% family-wise percentile intervals over 1,200 bootstrap samples of the 12 run-level paired effects. Positive effects mean candidate regression. No request is treated as an independent experimental run.

| Candidate vs static HTB | Median P99 regression | Adjusted interval | P99 gate | Deadline attainment regression | Deadline gate |
|---|---:|---:|---|---:|---|
| CAKE | +68.3% | +38.9% to +90.1% | Regression | 0.0 pp | Pass |
| HTB + fq_codel | +90.3% | +72.1% to +134.8% | Regression | +0.20 pp | Pass |
| HTB + FIFO | +225.6% | +160.0% to +301.1% | Regression | +64.1 pp | Regression |

The total and background goodput gates passed for these comparisons. The CPU gate was inconclusive for fq_codel because its adjusted interval crosses the predeclared 20% margin. At 12 pairs, the sign-test power estimate is 6.9% under the configured assumption that a true effect exceeds the gate in the declared direction on 80% of blocks; approximately 36 pairs would be needed to reach 80% power at the family-adjusted alpha under that assumption. The large P99 effects were nevertheless separated by the observed bootstrap intervals in this particular workload. This does not establish general scheduler superiority.

Observed release-lag P99 medians ranged from about 3.9 to 6.6 ms. The dispatch-delay fixture was repeated: the first run recorded 160.0 ms release-lag P99 and 205.7 ms maximum event-loop lag; the final run recorded 156.4 ms and 201.5 ms. Both exceeded the predeclared 25 ms and 110 ms limits and were classified INVALID_INFRASTRUCTURE. All 48 paired TCP blocks passed the configured integrity gates and reported zero failed requests. The FIFO runs' lower deadline attainment is not hidden behind the successful-only P99 statistic.

## Closed-loop and UDP checks

The closed-loop fixture ran two valid static-HTB trials at concurrency four. It completed 677 and 673 requests over six seconds, reporting achieved rates of 112.8 and 112.2 completions/s. The corrected active-request counter observed a peak concurrency of four in each trial, matching the configured four lanes. Their successful-request P99 values were 56.36 and 58.27 ms. Two runs do not support a QoS comparison; the result is INCONCLUSIVE for comparative claims.

The UDP fixture ran two valid six-second static-HTB trials with one paced UDP stream, four background TCP streams, and a bounded capacity step. Both recorded 120 successful TCP requests, zero UDP send errors, and 0.245 and 0.247 Mbit/s receiver-observed UDP goodput. The run medians are useful as an instrumentation check only; the single-policy two-run fixture is not a comparative UDP study and reports INCONCLUSIVE overall.

A deliberate client crash was preserved as FAILED_WORKLOAD. In the final run, its partial request-timing ledger recorded 42 flushed successful attempts as a lower bound and labels total offered work unknown. The dispatch-delay and crash self-tests are not included in policy performance estimates.

## Interpretation and limits

This result establishes a reproducible measurement pipeline and a negative comparison for the tested VM, configuration and workload. It does not show that CAKE or fq_codel are generally worse. The experiment used a short open-loop workload with fresh TCP connections, one VM, one kernel, one configured veth bottleneck, one payload size, and a small run count. There was no x86_64 or physical-host validation. The request P99 is application-observed scheduled-release-to-completion time; it is not TCP RTT and its exchange sub-interval includes peer processing and scheduler delays.

The experiment does not include a Linux-default unshaped arm, a CAKE DiffServ classification arm, multiple traffic payload sizes, or an FLOWCORE adaptive strategy. Those require separate eligible matrices. A future clean-commit run with at least 36 paired blocks, additional held-out rates and payloads, and independent host validation is needed before making broader comparative claims.
