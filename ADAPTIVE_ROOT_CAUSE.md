# FLOWCORE adaptive controller root-cause investigation

## Original controller

The controller runs on the daemon thread and mutates only its own HTB
lab. Each tick uses application-reported success-latency histograms,
optional desired throughput, observed class bytes, and root backlog
and utilization. INET_DIAG provides TCP context and a freshness/error
check. TCP RTT is separate from application latency. A class may have one
admitted intent. The critical, interactive, standard and background
classes use the configured HTB priorities, respectively 0, 1, 2 and 3.
The higher-priority class has the lower numerical priority value.

The original tick ran once per configured observation window, four
seconds by default. The tested manifest used two seconds. Policy
evaluation required at least 20 successful samples, a 10% SLO deadband
and two consecutive eligible windows. The threshold strategy used
pressure and minimum samples without the SLO deadband. Static strategy
enforced admitted hard ceilings and made no speculative transfers.
Root utilization of at least 75% and a nonempty root backlog enabled a
transfer. The planner moved at most five percentage points of reserved
capacity from background to the highest-priority eligible class.
It preserved the configured background floor, the admitted ceilings,
and the cumulative boost cap. Guarantees did not exceed parent rate.

The active controller verified pre-state, decreased a reservation before
increasing its peer's, then checked kernel readback. Shadow logged the
proposal without writing. Pending probes originally waited about one
window from the beginning of the write and looked for a 10% improvement.
They rolled back on inadequate improvement or background protection
failure, waited at least three windows before re-probing, and stopped
after two consecutive rejected probes. A retained boost could be
released after three healthy or idle windows. Restart, intent changes,
drift and telemetry failures had separate baseline restoration paths.

## Confirmed defects and changes

| Finding | Evidence and reproduction | Change and remaining uncertainty |
|---|---|---|
| Root backlog falsely implicated critical | In the original phased experiment three of four probes had zero critical backlog in the latest archived sample. The bulk leaf was backlogged in every sampled point of both SLO runs. Critical still borrowed 3,410–3,689 scheduler-accounted packets in those runs. `tests/adaptive_unit.py` feeds the same background-only observation to the old eligibility predicate and the new planner. | `adaptive.py:Controller.plan` now also requires two positive backlog observations for the target leaf and measured class throughput at least 90% of its current guarantee. Borrowing remains possible up to ceil. The counters demonstrate borrowing and queue occupancy, while one-second snapshots can miss brief critical queues. |
| Windows and verdict timing could mix regimes | The original `Windows.summarize` included the current partial second. The old pending check was anchored before `tc` changes and used the latest rolling window. A 20-sample P99 is effectively the maximum. In the reproduced runs original verdicts arrived about two seconds after each pending record. | `telemetry_windows.py` adds complete windows for controller decisions. `adaptive.py:Controller.tick` anchors a fixed post interval to successful kernel verification and leaves a one-second settling period after rounding up. Recent application completion timestamps keep report delivery from shifting samples across buckets. Overlapping requests, fewer than 100 successes, or a report count outside ±20% of pre-action prevent retention. Report count is a demand proxy and can miss composition changes. |
| Failed and late reports could be masked by a lower success tail | Application histograms contain successes. The original retention decision used that tail and could overlook failure growth. The observer also missed fast failed requests in its deadline-miss count. Synthetic tests reproduce failure and partial-window cases. | The planner holds latency probes during windows with failed reports. Retention checks failures and deadline attainment alongside the successful tail, and records inadequate evidence separately. Safety rollback still restores the previous allocation. This is a heuristic on short windows. |
| Recovery could undo the benefit it was retaining | The previous recovery predicate counted met SLOs under continued pressure as evidence to remove protection. The control-flow defect is reproduced with fixed healthy samples and a boosted allocation. | Sustained low pressure or a known idle class can release a boost. Merely meeting the SLO under pressure no longer releases it. The live reproduction does not isolate this branch as a cause of past P99 regressions. |

Attribution uses leaf qdisc backlog. It does not infer packet residence
time from a byte count. A peak of 11,854 bytes at a 3 Mbit/s guarantee
would take 31.6 ms to serialize at that guarantee alone. Borrowing,
arrivals, fq_codel and link-layer accounting prevent treating this as
actual drain time. Nonempty-to-empty samples only bound when an empty
queue was observed. The reports include fq_codel drops and ECN marks
where present. The two original SLO runs had zero sampled critical-leaf
drops and ECN marks, with 570–576 bulk-leaf drops. These counters do not
identify application-level loss. [HTB semantics](https://github.com/iproute2/iproute2/blob/main/man/man8/tc-htb.8) · [FQ-CoDel](https://www.rfc-editor.org/rfc/rfc8290)

## Matched workload and timing

Two randomized 18-second blocks per revision used the same seeds,
four background flows, a 10 Mbit/s sender cap and a step from 90 to
170 requests/s at eight seconds. Static and SLO started at the same
30% critical reserve. Each run offered 2,420 requests. All eight
matched runs completed with zero failures.

| Revision | Static successful P99 per block | SLO successful P99 per block | SLO transfers |
|---|---:|---:|---|
| Original | 40.51, 51.50 ms | 57.12, 37.05 ms | 4 probes, 1 retained, 3 reverted |
| Corrected | 39.60, 39.01 ms | 38.92, 38.82 ms | 1 late probe without an in-window verdict |

The original first block hit the reversal guard. In the second block
the original SLO was faster than static. Both corrected blocks were
slightly faster than their paired static runs. Static P99 also shifted
between revisions. Two blocks and a censored late probe cannot establish
that the fix improved application P99. The gate remains INCONCLUSIVE.

In the corrected matched SLO runs the median time per sampled stage
was about 27 ms for kernel counters, 7 ms for observer sampling
including INET_DIAG, 1 ms for controller-window aggregation, and 0.1 ms
for planning. Median sample age at decisions was about 0.93 s.
Benchmark control IPC round trip was 12–13 ms median. The per-class
application reporter measured 1.58–8.93 ms P95 IPC round trip in the
matched SLO runs. All reports were accepted. The late probe spent
12.4 ms in pre-read, 10.2 ms in writes, 7.2 ms in verification and
17.4 ms refreshing telemetry. Instrumentation was absent in the
original revision. Millisecond-scale work was small relative to the
seconds spent collecting observations and evaluating outcomes. The
telemetry age is sampled in the daemon and does not include unknown
client-side scheduling effects.

## Reproduction and evidence

```sh
sudo ./build/flowcore bench run scenarios/adaptive-root-paired.json
sudo ./build/flowcore bench run scenarios/adaptive-counterfactual.json
sudo ./build/flowcore bench run scenarios/adaptive-saturation.json
sudo ./build/flowcore bench suite --quick
python3 -B tests/adaptive_unit.py
python3 -B scripts/adaptive_diagnostics.py results/<experiment-directory>
```

Run the matched manifest at `df42adb` for the original controller and at
the corrected revision for the updated controller. The original experiment
used an identical copy of the manifest. Raw before and after evidence is in
`results/research-1793845440134802343/` and
`results/research-1793845680405090755/`. Both preserve request and kernel
timelines, control decisions, host-noise samples and generated reports.

The counterfactual matrix uses controller-off static, SLO shadow, bounded
one-adjustment SLO, a longer SLO window and cooldown, and a deterministic
30%→35% critical reserve step at four seconds. Static35 starts at the
same 35% critical and 25% background reservation that the forced step
reaches. The fixed observation test isolates policy logic. The live
counterfactual has one block and is descriptive.

| Counterfactual | Successful P99 | Deadline attainment | In-window action |
|---|---:|---:|---|
| Static 30% | 53.05 ms | 85.41% | None |
| Static 35% | 34.42 ms | 92.36% | None |
| Fixed step 30% to 35% | 34.69 ms | 92.36% | One verified step |
| SLO shadow | 41.35 ms | 87.19% | No eligible proposal |
| SLO, one-adjustment budget | 42.30 ms | 85.58% | None |
| SLO, longer window and cooldown | 38.34 ms | 88.60% | One late probe |
| Corrected SLO | 51.54 ms | 81.78% | None |

Every arm in this block completed 2,420 requests without failures.
SLO lost to static35 and to the forced step on both reported metrics.
The fixed step reached the static35 allocation at 4.07 s. Its kernel
pre-read, write and verification took 10.1, 6.7 and 4.8 ms. The similar
results from fixed step and static35 make this allocation worth testing
again on independently seeded blocks. The single block cannot separate
allocation effects from run-to-run variation. It also shows why the
hold policy can remain worse than a better fixed allocation.

Raw counterfactual evidence is in
`results/research-1793845761815864558/`. The
`results/adaptive-root-*-diagnostics.json` files summarize the matched
and counterfactual evidence.
