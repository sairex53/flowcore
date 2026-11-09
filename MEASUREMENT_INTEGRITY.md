# Measurement Integrity Engine 3.0

## Scope

Measurement Integrity is an opt-in profile for the isolated Research Lab runner. It adds request-level monotonic timing, generator-health checks, resource-pressure samples, explicit validity states, paired run analysis, and per-run raw-artifact hashes. Existing v1/v2 workloads remain available and are not retroactively described as fully instrumented.

The implementation is in scripts/measurement.py, scripts/workload.py, scripts/research_runner.py, scripts/research_stats.py, and scripts/research_report.py. The profile and fixtures are validated by scripts/research_manifest.py.

## Timestamp model

All local elapsed-time measurements use CLOCK_MONOTONIC through Python's monotonic clock API. The per-request file request-timing.csv records nanoseconds for:

- scheduled_ns: intended open-loop release. Closed-loop records leave this blank.
- released_ns: when the workload generator releases/adopts the request.
- dispatch_ns: when the asyncio request coroutine first executes.
- connect_ns and connected_ns: userspace timestamps immediately before and after the connect await.
- submitted_ns: immediately before the client writes request bytes to its stream.
- drained_ns: after the stream drain await completes.
- received_ns: after the expected response bytes are read.
- completed_ns: request completion in the client.

The distinct intervals are reported separately. Connect duration includes the application's connect call and the observed handshake path. Write-drain and response-exchange intervals include userspace, kernel scheduling, peer processing, and network time as applicable. They are not pure wire latency. Linux packet enqueue/transmit timestamps are not collected by this profile, so kernel scheduling is recorded as unavailable rather than inferred.

The legacy requests.csv and the nanosecond sidecar are cross-checked by request ID, outcome, latency, completion time, and the offered open-loop schedule. Successful-only P50/P95/P99 remain conditional summaries; failure ratio, completion ratio, deadline attainment, timeouts, and the full offered cohort are reported beside them. Missing or duplicate request records invalidate the ledger.

CLOCK_MONOTONIC avoids wall-clock adjustments for local durations. The recorded clock resolution is a platform-reported resolution, not a guarantee of timestamp accuracy. Cross-host wall-clock correlation is outside this local namespace experiment. Python documents monotonic clocks as non-adjustable elapsed-time clocks; the kernel scheduling and packet-level limits above remain.

## Load models

Open-loop profiles use the same scheduled cohort in every paired arm. Reports include offered rate, actual release rate, release lag, coroutine dispatch lag, event-loop probe lag, completions, and achieved goodput. The schedule fingerprint is derived from scheduled offsets and the assigned traffic class and service. A late release is measured and can invalidate a block against a threshold declared in its manifest.

Closed-loop profiles require a zero scheduled rate and a bounded concurrency setting no greater than max_inflight. The generator starts a new request after the previous request in that lane finishes. It reports configured concurrency and counts active request coroutines to record actual peak concurrency, along with achieved release and completion rates. Those rates are workload outcomes, not the offered rate of an open-loop test.

The request ceiling is fixed at 90,000 and a hit invalidates the run. Client CPU time, system CPU time, voluntary and involuntary context switches, and peak RSS are collected with getrusage. The runner process is measured separately. Host CPU, memory, and I/O pressure are sampled from PSI where available, along with aggregate CPU ticks, load average, runnable-task count, and guest steal time. Missing PSI is reported as unavailable. Guest steal and PSI cannot prove hypervisor-side oversubscription.

## Run states

The integrity profile declares thresholds before execution. The runner retains every run directory and raw record.

- VALID: the timing ledger, generator probes, host counters, and forward interface counters pass integrity limits; request failures remain reported.
- INVALID_INFRASTRUCTURE: a predeclared noise threshold is exceeded, required evidence is missing or inconsistent, or an artifact checksum fails.
- INCONCLUSIVE: evidence is present, but the run has too few successful requests or the comparison has too few independent pairs.
- FAILED_WORKLOAD: the workload's predeclared failure-fraction limit is exceeded or a client exits before final accounting. On client crash, flushed timing rows are reported as a lower bound; total offered work is explicitly unknown.
- COMPLETED_WITH_REGRESSION: the complete comparison passed measurement checks and a preregistered regression gate was exceeded.

For the supplied profiles, infrastructure invalidation checks include open-loop release P99, maximum event-loop lag, host CPU PSI, guest CPU steal, missing event-loop probes, missing interface counters, a payload-to-wire counter inconsistency, a configured request-ceiling hit, and timing-ledger integrity. The profile's failure-ratio threshold classifies workload failure; it does not erase requests or turn them into infrastructure noise. These thresholds are stored in the run manifest. There is no post-hoc outlier deletion. Existing investigation flags can identify unusual run-level values, but every included block remains in the analysis.

## Statistics

Requests within a trial are samples used to calculate that trial's percentiles. They are not treated as independent experimental repetitions. The independent unit for comparisons is the run-level block. A paired block uses the same workload seed and scheduled workload fingerprint across network modes. Mode order can be randomized or Williams-counterbalanced. A one-second cooldown and manifest-declared warmup are applied.

The v3 comparison reports paired run-level effects for critical P99, deadline attainment, request failure ratio, total goodput, background goodput, Jain fairness, and CPU seconds. It reports the median effect, variance, paired values, a deterministic paired bootstrap percentile interval, minimum pair requirement, and a one-sided sign-test power estimate under a declared direction probability. Bonferroni family-wise adjustment covers all declared metrics and candidate arms. Bootstrap intervals are run-level intervals; they do not turn requests into experimental units. The resampling method follows the original nonparametric bootstrap formulation.

A run with failures is not silently omitted from P99 comparisons: successful-only tails are marked censored and that gate is inconclusive. A missing or invalid block makes the affected comparison invalid rather than reducing its denominator. The power estimate is conditional on its assumed direction probability and is not a guarantee of study power under arbitrary effect distributions.

## Artifact integrity and replay

Each run directory contains checksums.json with SHA-256 digests for bounded regular raw files, plus a summary-row digest for the checksum manifest. Symlinks, path traversal, oversized artifacts, duplicate IDs, and changed results are rejected by verification. The root experiment manifest is stored separately; newer captures include its canonical SHA-256 in environment metadata. Older integrity captures without that field are checked for equality with the summary manifest but report that the separate hash was unavailable. Hashes detect accidental or unauthorized changes after capture; they are not digital signatures and do not establish who produced the data.

Source hashes, Git commit and dirty state, binary hash, build configuration, kernel, CPU topology, VM fingerprint, clock resolution, tc version, selected host sysctls, random block seeds, manifest, actual qdisc readback, policy transitions, raw requests, receiver counters, controller history, and host-pressure data are recorded where available.

Use:

    sudo ./build/flowcore bench replay scenarios/measurement-v3-paired.json
    ./build/flowcore bench verify results/research-<run-id>/summary.json
    ./build/flowcore bench report results/research-<run-id>/summary.json
    ./build/flowcore bench compare results/research-<run-id>/summary.json results/research-<run-id>/summary.json --baseline-mode static --candidate-mode cake

The runner requires the owned network-namespace laboratory. Verification and report generation read saved artifacts and do not require changing networking state.

Official references: [Linux Pressure Stall Information](https://docs.kernel.org/accounting/psi.html), [Python monotonic clock API](https://docs.python.org/3/library/time.html#time.monotonic_ns), [Efron's bootstrap paper](https://projecteuclid.org/journals/annals-of-statistics/volume-7/issue-1/Bootstrap-Methods-Another-Look-at-the-Jackknife/10.1214/aos/1176344552.full).
