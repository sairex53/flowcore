# FLOWCORE Autonomous Optimization Engine

## Scope and safety boundary

The engine is a bounded search over the Research Lab's owned veth namespaces. Version 1 searches the critical HTB minimum reservation from 20% through 40% of the scenario's configured parent rate. The background minimum is coupled to it as 60% minus the critical share, preserving at least a 20% background reservation. Scenario capacity is held fixed per experiment.

The capability snapshot in every plan states what the runner varies and what it holds fixed. HTB class ceilings and priorities come from the validated lab configuration, leaf queues are fq_codel with their defaults, and controller or telemetry knobs do not change for static-policy candidates. The engine rejects values outside its supported range. It does not claim that a held-fixed parameter was optimized.

The engine never applies a recommendation to a host interface or live service. Run requires root because it uses the Research Lab's isolated namespaces; the existing exclusive research lock prevents concurrent labs. Search and holdout runs manipulate only resources owned by the lab. Results are recommendations; there is no automatic apply path. A holdout is an isolated scenario comparison, not a canary deployment of an application.

## Workflow

Prepare a version 2 or 3 Research Lab manifest for training and separate manifests for holdout scenarios. A holdout must change workload or network conditions and use a different seed.

    flowcore optimize plan scenarios/policy-optimizer-paired.json \
      --algorithm grid --candidate-budget 5 --step 5 --repetitions 10 \
      --holdout scenarios/your-unseen-capacity.json \
      --budget-seconds 3600 --output results/plan.json
    sudo flowcore optimize run results/plan.json
    flowcore optimize status results/autotune-JOB_ID
    flowcore optimize results results/autotune-JOB_ID
    flowcore optimize recommend results/autotune-JOB_ID

Plan validates capability limits, candidate count, workloads, independent seeds, objective weights, constraints and projected cost without creating a network lab. Run stores a complete plan copy, shuffled run order, raw Research Lab evidence, per-stage summaries and state under FLOWCORE_RESULTS. Interrupted runs retain partial evidence and are not recommendations. Status, results and recommend are offline reads.

Algorithms are deterministic for a fixed plan seed:

- Manual measures the manifest's configured baseline.
- Grid evaluates a bounded percentage grid.
- Random samples unique percentages and always includes the manual baseline.
- Coordinate tests around the baseline and narrows the step.
- Local moves toward the best observed neighboring allocation and reduces the step when the local search stops improving.

Every batch includes the manual baseline. The Research Lab randomizes candidate order inside each independent repetition and assigns a shared workload seed to that repetition, so the comparison is paired. Later local-search batches receive new block seeds. The selected policy is provisional until independent holdout comparisons finish.

## Objectives and constraints

Objective weights can be provided in a JSON object with --weights FILE. Supported metrics are p99_ms, deadline_attainment, failure_ratio, background_mbps, jain_index, cpu_seconds, peak_daemon_rss_bytes, and adjustments_per_minute. Positive weights are normalized to sum to one. For paired runs, P99, goodput, CPU, memory and adjustments use a symmetric relative change divided by the greater of the pair magnitudes or one. Failure ratio, deadline attainment and Jain fairness use absolute change in their unit interval. If either paired run has any failed requests, its successful-only P99 is omitted from that pair's score; request failures and deadlines remain in the comparison. Missing metrics are omitted and remaining weights are renormalized.

A hard-constraint JSON file may specify max_failure_ratio, min_deadline_attainment, min_background_mbps, min_jain_index, and min_objective_improvement. Defaults impose no application-performance exclusion beyond valid allocations; operators should set workload-specific constraints explicitly. A holdout is PASS only when constraints pass and the lower endpoint of a 95% paired run-block bootstrap interval exceeds the configured minimum objective improvement. A negative interval or violated hard constraints is a regression; overlapping uncertainty is inconclusive.

The independent units are experiment blocks, never individual requests. Ten repetitions are the minimum. The bootstrap resamples paired block scores (10,000 resamples), not requests. A large request count in one run cannot substitute for more independent runs. P99 is always reported beside failure ratio and deadline attainment. Its sampling error and censoring limitations remain.

## Budgets and interruption

Plans bound distinct candidate allocations, independent repetitions, test duration, wall time, measured FLOWCORE daemon CPU, peak daemon RSS and artifact storage. The runner checks projected test and wall time before each batch, then checks measured daemon CPU/RSS and artifact size before starting another batch. A single experiment run is bounded by the manifest limits (up to 180 seconds, 512 in-flight requests, and the Research Lab's finite workloads). CPU is daemon CPU, not total host CPU; RSS is daemon RSS, not a whole-host memory cap. These are observed-and-stop budgets between bounded trials, not cgroup-enforced ceilings on the complete host.

Do not run long searches on a shared production machine. Use a disposable Linux VM with enough free storage. Ctrl-C stops the research runner and keeps completed/partial raw artifacts. Re-running the same plan creates a new job and new search-batch seeds.

## Recommendation format

A recommendation includes the selected critical reservation, its paired training evidence, each holdout outcome, objective weights and capability limits, potential regressions, confidence limitations, and a safe rollback to the original manual reservation. The recommendation is always shadow-only. PASS means it passed the configured experiments and gates; it is not a general performance guarantee.
