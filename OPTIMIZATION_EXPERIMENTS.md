# Optimization experiment method

## Candidate matrix

The first implemented search dimension is FLOWCORE's owned-lab HTB critical minimum reservation, integer percentages 20–40 inclusive. This range leaves 20–40% for the critical class and 40–20% of the parent rate for the background class, with the configured 20% interactive and 20% standard reservations. HTB ceilings, priorities, fq_codel parameters, controller settings and telemetry intervals are captured as capabilities but remain fixed. Search does not silently substitute qdisc or scheduler behavior unsupported by the selected runner.

## Run design

The manually configured allocation is included in every batch. Candidate mode order is shuffled independently inside each repetition. All modes in a repetition use the same offered workload seed, preserving a paired run-level comparison. Each independent block is run by the existing Research Lab, which records namespace and interface ownership, class/qdisc readbacks, offered/completed/failed requests, application latency, deadlines, throughput, fairness, daemon CPU/RSS and controller activity.

Grid and random searches compare a predeclared fixed set. Coordinate and local searches evaluate a bounded neighboring batch, choose a provisional training winner, and refine around it. Search decisions use only training runs. Each next batch receives fresh seeds. An independent holdout phase tests the provisional winner against the manual reservation on new randomized blocks. Holdout manifests must differ in workload or network configuration and have distinct seeds; they cannot be used to change the selected candidate.

## Scoring and uncertainty

Paired improvements are calculated per repetition. P99 contributes only if neither paired run failed a request, because successful-only tails are censored when requests fail. Failure ratio and deadline attainment always include all offered requests. Weighted components use bounded symmetric relative changes (denominator is max of both magnitudes or one) for P99, goodput, CPU, memory and adjustment rate; failure ratio, deadline attainment and Jain fairness use absolute changes in their unit interval. Missing components are omitted and weights renormalized for that pair.

Holdout confidence intervals use a deterministic 10,000-resample paired run-block bootstrap of the median score difference. The independent unit is the complete workload experiment. At least ten repetitions are required. A holdout passes only if its lower 95% bootstrap endpoint exceeds the minimum objective gain and all configured hard constraints pass. A missed guardrail or a wholly negative interval is a regression; an interval overlapping the minimum gain is inconclusive. Multiple holdouts are all required to pass for an overall PASS. These intervals are conditional on tested scenarios and do not prove population-wide performance.

## Reproduction and artifacts

A plan snapshots its validated training and holdout manifests and their source digests, algorithm, deterministic search seed, objective weights, constraints, capability description and budgets. Each run directory contains the Research Lab experiment manifest, environment metadata, raw per-run directories, class/qdisc snapshots, timeline, request ledger, summary and reproduction command. The job state points to every batch and preserves interrupted or invalid runs. No raw measurements are edited out by the optimizer.

Use flowcore optimize compare SUMMARY.json SUMMARY.json --baseline-mode static30 --candidate-mode static35 for two modes recorded in the same randomized Research Lab summary and existing paired comparison gates. The optimizer's objective score is additional to the original run metrics, not a replacement for them.

## Limits

This initial engine does not tune custom HTB ceilings, class priorities, queue target/interval/limit, adaptive controller thresholds, telemetry frequency or fairness/SLO weights because the current lab integration does not expose these as candidate controls with complete validation/readback. It also does not provide Bayesian search, contextual bandits, online reinforcement learning or a live workload canary. Those require separate capability-specific experiments and ownership/rollback work.
