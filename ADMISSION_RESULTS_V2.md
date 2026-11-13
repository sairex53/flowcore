# Admission Results 2.0

## Verdict

**The engine did not demonstrate an improvement over the static QoS baseline.** A valid 10-pair/5-arm replay completed in isolated, rootless user/network namespaces. The candidate's deadline attainment was close to static QoS, but it rejected 91 of 8,000 offered requests while static QoS completed all 8,000 and delivered 8 more on time. The preregistered comparison is `INCONCLUSIVE`; successful-request P99 is censored by the comparison because some candidate requests were rejected. Keep the v2 controller opt-in; static QoS remains the safer default.

## Implemented and tested

- Native request cost and deadline feasibility estimator.
- Cooperative admission recommendations and app-local admission decision callbacks.
- Hysteretic overload/capacity-collapse and gradual recovery controller.
- Bounded admission-rate token bucket in the benchmark workload.
- Offered/admitted/rejected/completed/failed/deadline-miss lifecycle accounting.
- Class and response-path evidence diagnostics.
- Cost, slack, priority, and fairness-debt-aware local ranking.
- `scenarios/overload-prevention-v2.json`: 10 paired repetitions, five comparison arms, a burst phase, and a 12-to-8 Mbit/s capacity step.

Full CMake build succeeded on the available x86_64 Linux host. CTest completed **71 passed, 1 skipped, 0 failed**; the skipped test is `udp_intent_live`. The targeted deadline/overload tests also passed.

## Valid paired experiment

Evidence is preserved outside the repository at `/tmp/flowcore-overload-results3/research-1794584797683771887`. The run used ten repetitions of each of five modes (50 arms total), an 8-second 40→150→60 requests/s workload, 40 ms ordinary deadlines with periodic 1 ms deadlines, a 12→8 Mbit/s capacity step, and concurrent background transfers. It ran in an isolated rootless user/network namespace on x86_64, kernel `7.2.15-1-omarchy`, 16 CPUs, BPF off. Host networking was not modified. The report records commit `6060b82` and `git_dirty=true`; the summary includes per-file hashes. This is not a Fedora 45 ARM64, physical-NIC, or production-daemon validation.

All values below use the offered request count as the denominator. “On time” means completed within that request's own deadline. P99 is the median of each run's successful-request P99 and is descriptive; the formal paired comparison does not accept it when failures make the arms incomparable.

| Mode | Offered | Admitted | Rejected/deferred | Completed | On time / offered | Median P99 (ms) | Request payload Mbps | Background Mbps | Jain fairness |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| No admission (`deadline-unaware`) | 8,000 | 8,000 | 0 | 8,000 | 5,483 / 8,000 (68.54%) | 134.50 | 3.28 | 5.68 | 0.890 |
| Static QoS (`deadline-static`) | 8,000 | 8,000 | 0 | 8,000 | 7,190 / 8,000 (89.88%) | 33.74 | 3.28 | 5.40 | 0.976 |
| Existing deadline admission | 8,000 | 7,193 | 807 | 7,193 | 7,190 / 8,000 (89.88%) | 32.11 | 2.95 | 4.87 | 0.984 |
| Static concurrency cap | 8,000 | 8,000 | 0 | 8,000 | 7,191 / 8,000 (89.89%) | 33.83 | 3.28 | 5.53 | 0.920 |
| Deadline overload v2 | 8,000 | 7,909 | 91 | 7,909 | 7,182 / 8,000 (89.78%) | 34.73 | 3.24 | 5.98 | 0.904 |

V2 improved on-time completions substantially over the uncontrolled run, but static QoS delivered 1,707 more on-time requests than uncontrolled and eight more than v2. Compared with existing deadline admission, v2 admitted/completed 716 additional requests but produced eight fewer on-time completions; the existing admission arm itself rejected or deferred 807 requests, which remain in its offered denominator.

The official `bench compare` result for static QoS vs v2 is `INCONCLUSIVE`: P99 had zero comparable pairs due to candidate rejections; total throughput passed the 10% regression gate (median candidate advantage 0.91%, simultaneous interval −8.77% to +5.25%); failure ratio increased by a median 1.125 percentage points (interval +0.625 to +1.625, crossing its 1-point margin); deadline attainment was lower by 0.125 percentage points (interval −0.375 to +0.5, within the 5-point margin). The summary-level multi-arm verdict is `REGRESSION` because uncontrolled and existing-admission arms regress against static QoS.

In the v2 arm, 91 requests received `REJECT_RECOMMENDED`, 3,788 `ACCEPT_WITH_RISK`, and 4,121 `UNKNOWN`. RTT was withheld because socket scans were truncated, while owned class-scoped qdisc evidence remained available. The controller issued one `REDUCE_CONCURRENCY` action in each repetition (64→48 concurrency; 300→225 admissions/s); there were no input errors. Median adapter estimator CPU was about 92 ms per 800 offered requests. The Jain index was 0.904 for v2 versus 0.976 for static QoS in this workload, so the extra background throughput (5.98 vs 5.40 Mbps) came with lower measured fairness. These results show observable controller activity, not a demonstrated performance win.

## Invalid live attempts

Two privileged replay attempts were made in owned FLOWCORE lab namespaces. They are preserved locally under:

- `results/research-1794582666842615755` — interrupted after eight arms. The new arm failed before producing a complete request ledger because its adapter sample construction raised `TypeError` on duplicate `counters_complete` fields.
- `results/research-1794582789653967520` — interrupted after eight arms. The new arm saw 800 offered requests, 543 completed, 10 admission rejections, and 247 requests without terminal outcomes after a malformed observation escaped the adapter. The run therefore failed the complete-accounting check and is invalid. The recorded evidence map was empty.

The adapter now treats malformed input as insufficient evidence, preserves the current recommendation, emits evidence availability diagnostics, and continues the application workload. A corrected complete replay was subsequently run without sudo inside a user/network namespace. It is reported above. The benchmark's minimum of ten pairs remains intentional; partial arms below are not treated as comparisons.

The two interrupted runs below remain invalid and are not used as results. Their artifacts and reasons are preserved.

## Remaining validation

The next valid replay must report, for every arm and all ten paired seeds:

- offered, released, admitted, rejected, completed, failed, and deadline-missed requests;
- deadline attainment over offered requests, not only admitted or completed requests;
- successful-request P50/P95/P99 with the denominator shown;
- useful application goodput, background throughput, and fairness;
- controller state/action counts and evidence availability;
- estimator/controller CPU overhead and recovery delay.

An invalid or interrupted arm stays in the evidence directory with its failure reason. It must not be silently removed from the denominator or described as an unfavorable performance result. The completed run satisfies the paired-run count for this workload, but the evidence limitations below block generalization.

## Environment and validation boundary

Source-tree tests and the rootless namespace replay ran on the current x86_64 Linux host, with BPF disabled in the build. This does not validate Fedora ARM64, physical NIC behavior, reverse-path capacity, RPM-installed runtime, or long-duration daemon recovery. The response direction in the lab evidence is intentionally marked unverified, and RTT was unavailable because the socket scan was truncated.
