# Admission Results 2.0

## Verdict

**Performance result: unverified.** The preregistered ten-pair network comparison did not complete validly, so there is no defensible claim that the new engine improves useful completed work. No successful-request P99 comparison is reported as a result.

## Implemented and tested

- Native request cost and deadline feasibility estimator.
- Cooperative admission recommendations and app-local admission decision callbacks.
- Hysteretic overload/capacity-collapse and gradual recovery controller.
- Bounded admission-rate token bucket in the benchmark workload.
- Offered/admitted/rejected/completed/failed/deadline-miss lifecycle accounting.
- Class and response-path evidence diagnostics.
- Cost, slack, priority, and fairness-debt-aware local ranking.
- `scenarios/overload-prevention-v2.json`: 10 paired repetitions, five comparison arms, a burst phase, and a 12-to-8 Mbit/s capacity step.

Full CMake build succeeded on the available x86_64 Linux host. CTest completed **71 passed, 1 skipped, 0 failed**; the skipped test is `udp_intent_live`. The four targeted deadline/overload tests also passed.

## Invalid live attempts

Two privileged replay attempts were made in owned FLOWCORE lab namespaces. They are preserved locally under:

- `results/research-1794582666842615755` — interrupted after eight arms. The new arm failed before producing a complete request ledger because its adapter sample construction raised `TypeError` on duplicate `counters_complete` fields.
- `results/research-1794582789653967520` — interrupted after eight arms. The new arm saw 800 offered requests, 543 completed, 10 admission rejections, and 247 requests without terminal outcomes after a malformed observation escaped the adapter. The run therefore failed the complete-accounting check and is invalid. The recorded evidence map was empty.

The adapter now treats malformed input as insufficient evidence, preserves the current recommendation, emits evidence availability diagnostics, and continues the application workload. These changes passed unit tests, but could not be exercised in another privileged replay because the current `sudo` credential check rejected the supplied credential. The benchmark's minimum of ten pairs is intentional; partial arms above are not treated as a comparison.

The first corrected arm's ordinary completion/P99 numbers are not used as results: request accounting is incomplete, the adaptive arm is invalid, and the paired experiment did not reach ten repetitions.

## Metrics still required

The next valid replay must report, for every arm and all ten paired seeds:

- offered, released, admitted, rejected, completed, failed, and deadline-missed requests;
- deadline attainment over offered requests, not only admitted or completed requests;
- successful-request P50/P95/P99 with the denominator shown;
- useful application goodput, background throughput, and fairness;
- controller state/action counts and evidence availability;
- estimator/controller CPU overhead and recovery delay.

An invalid or interrupted arm stays in the evidence directory with its failure reason. It must not be silently removed from the denominator or described as an unfavorable performance result.

## Environment and validation boundary

Source-tree tests ran on the current x86_64 Linux host, with BPF disabled in the build. This does not validate Fedora ARM64, physical NIC behavior, reverse-path capacity, RPM-installed runtime, or long-duration daemon recovery. The response direction in the current lab evidence is intentionally marked unverified.
