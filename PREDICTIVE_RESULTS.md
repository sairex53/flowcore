# Predictive engine: measured results

**No measurable predictive advantage is established. Keep this feature experimental
and disabled by default.** The chosen hybrid model produced no verified proactive
kernel intervention in the evaluated active arms. Risk remains an uncalibrated
heuristic index, not a probability. The repeated comparison gates are INCONCLUSIVE.

## Executed protocol

Fedora 45 ARM64 VM, Linux 7.2.11-300.fc45.aarch64. Two fresh owned network namespaces
and a veth pair per run; sender HTB with fq_codel leaves, receiver reverse-path
netem; no new BPF program. Optional TCP event tracing was off for every arm.
Source revision: `1a56a22` with source hashes in each experiment manifest.

Primary suite: `results/predictive-suite-1793968511080519845/`:

- shadow-1793968511080779257/: one four-second-horizon shadow validation, no writes.
- buildup-1793968533415123541/: ten paired randomized blocks, five modes, 50 valid runs.
- stable-1793969650971766135/: ten paired randomized blocks, four modes, 40 valid runs.
- bursts-1793970224867721236/: one unseen stress block, five modes, five valid runs.
- index.json, static-selection.json and concise-results.json index the evidence.

A separate six-run static-only pilot was reused from
`results/predictive-suite-1793968220301002552/pilot-1793968242608571675/`.
Its SHA256 is in index.json. No predictive model ran in this pilot. It used two
independent blocks, seed 660034, before evaluation seeds 771034 / 881034 / 991034.
Selection was frozen before evaluation: zero failures, active-duty-adjusted 20%
background floor (1.5 Mbit/s averaged over the buildup run), highest median deadline
attainment, then lowest median successful P99. Candidates were 20/30/40% critical
reservation. The selected 40% reserve is the best of these pilot candidates, not
proof of a globally optimal static policy.

| Static pilot | Median P99 ms | Median 25 ms deadline attainment | Background Mbit/s |
|---|---:|---:|---:|
| 20% | 634.70 | 61.81% | 3.870 |
| 30% | 39.07 | 91.84% | 3.824 |
| 40% | 27.22 | 97.67% | 3.846 |

All controllers started with 30% critical / 30% background; the selected static
started with 40% / 20%. Interactive and standard each had 20%. All ceilings remained
100%, so unused capacity could be borrowed. Thus the static comparison evaluates a
separately tuned operator policy; reactive/queue/predictive comparisons share the
same initial allocation. All use the same 10 Mbit/s parent and four-millisecond
configured base RTT, payload 4096 bytes, class weights 80/15/5, max inflight 256,
one critical service, new TCP connections and a 25 ms application deadline/SLO.
Control windows were two seconds, enter_windows=2, telemetry nominally one second.
The forecast horizon was four seconds. The generator uses open-loop scheduled
requests and preserves dispatch delay; actual completion times/failures remain raw.

Buildup: four five-second phases at 50/85/125/170 requests/s and 0/2/4/4 bulk streams.
Stable: twelve seconds at 85 requests/s with no bulk streams. Warmup was one second.
Within a block, all arms use the same workload seed; order is randomized. No
coefficients, risk thresholds or model selection were fitted to these evaluation
runs. Stable and burst scenarios were authored before evaluation and not used for
coefficient tuning. An initial shadow check is diagnostic, not another independent
paired block or training dataset.

## Repeated application comparisons

Medians of independent run summaries, not pooled-request inference:

| Buildup strategy | Successful P99 ms | Deadline attainment | Background Mbit/s | Failed / offered |
|---|---:|---:|---:|---:|
| Tuned static40 | 29.21 | 96.60% | 3.818 | 0 / 21500 |
| Reactive SLO-aware | 39.55 | 90.37% | 3.823 | 0 / 21500 |
| Queue-aware | 38.53 | 91.88% | 3.822 | 0 / 21500 |
| Predictive + reactive fallback | 37.63 | 90.93% | 3.808 | 0 / 21500 |
| Predictive shadow, static30 allocation | 40.44 | 90.40% | 3.833 | 0 / 21500 |

Paired predictive successful-P99 effects, positive = worse:

| Baseline | Median paired change | Distribution-free median interval |
|---|---:|---|
| Tuned static | +26.96% | [-0.34%, +32.85%] |
| Reactive SLO-aware | -0.42% | [-12.77%, +29.52%] |
| Queue-aware | +0.01% | [-6.41%, +7.68%] |

These are exploratory marginal intervals, coverage 97.8515625% with ten pairs;
they are not multiplicity-adjusted. The predeclared V2 performance gates separately
use their simultaneous family intervals and 10-pair requirement. All suite outcomes
were INCONCLUSIVE. Ratios of displayed medians differ from median paired effects.
Predictive deadline attainment versus tuned static changed by -5.60% relatively,
interval [-6.40%, -3.22%]; the displayed attainment levels differ by 5.67 percentage
points. No evidence supports replacing the tuned static reference.

Active predictive made three **reactive-fallback** probes; one had an inconclusive
verdict/rollback inside measurement. Two probes had no in-window verdict. The shadow
arms logged three hypothetical proposals, one tagged proactive. **No active
proactive probe, retained proactive benefit or avoided SLO violation was measured.**
Pending actions/forecast labels are not silently counted as successful evaluations.
The owned lab is removed on teardown. Existing verified actuator tests and synthetic
planner tests show the path can execute bounded proposals; they do not establish a
real predictive latency benefit.

| Stable strategy | Successful P99 ms | Deadline attainment | Failed / offered | Policy changes |
|---|---:|---:|---:|---:|
| Tuned static40 | 19.38 | 99.80% | 0 / 10200 | 0 |
| Reactive SLO-aware | 19.91 | 99.56% | 0 / 10200 | 0 |
| Queue-aware | 18.34 | 99.80% | 0 / 10200 | 0 |
| Predictive | 22.59 | 99.26% | 0 / 10200 | 0 |

The selected model emitted zero rising-edge alerts in stable runs. Absence of
policy changes does not prove zero runtime impact: predictive-versus-static P99
paired change was +23.44%, interval [-14.41%, +130.58%]. Three predictive stable
runs triggered dispatch-noise warnings. Its higher median latency cannot be
attributed solely to predictor cost, nor hidden as harmless interference.

## Forecast ablations and false warnings

All five methods were evaluated causally on each predictive shadow trace with the
same timestamps, horizons, target event and fixed warning rules. Ten buildup shadow
runs give these **fully resolved forecast-tick** counts (overlapping, dependent):

| Model | TP | FP | FN | TN | Precision | Recall |
|---|---:|---:|---:|---:|---:|---:|
| EMA persistence | 0 | 0 | 1 | 347 | undefined | 0% |
| Linear trend | 0 | 4 | 1 | 343 | 0% | 0% |
| Queue growth | 0 | 8 | 1 | 339 | 0% | 0% |
| Rate change | 1 | 207 | 0 | 140 | 0.48% | 100% |
| Hybrid | 0 | 7 | 1 | 340 | 0% | 0% |

Recall of 100% for rate change rests on one resolved positive forecast tick and
207 false positives. It is not useful prediction evidence. Hybrid had zero true
positives among resolved windows and seven false positives. Its seven resolved
early warnings had no completed positive outcome. Separately, three observed queue
onsets occurred; one had an early hybrid warning. Onset counting includes known
onsets whose full forecast window is still pending, so its denominator differs
from fully resolved forecast precision/recall. Do not conflate these metrics.

There were 600 pending forecast labels at the ends of the ten shadow runs across
all five models/classes. They are reported as censored tails, not negatives or
successful predictions. Active runs additionally censored intervention-contaminated
labels. Empirical quintile reliability-bin counts, alert frequency and sampled
lead-time sums/counts are in predictions.json and raw controller states. With this
few observed positive events, reliable probability calibration and a useful hybrid
lead-time distribution are **not established**. There is no claimed Brier/probability
score for an uncalibrated index. No advanced learned model was introduced.

One-second sampling frequently saw no target-leaf event despite application misses.
This limits what the model can anticipate; it does not establish that no transient
queue existed between samples. Drops, RTT and application tails remain independent
context, not evidence that throughput acceleration necessarily implies queue growth.
The rate-change ablation's false warnings demonstrate that distinction here.

## Unseen burst/capacity stress

Five four-second phases: request rates 55/170/55/170/85, bulk streams 0/4/0/4/2.
At nine seconds the parent changes 10→7 Mbit/s; at sixteen it returns to 10.
Every arm receives the same scheduled operator rebase only when it happens, not
advance information. The predictor resets on each rebase. One block is descriptive:

| Strategy | Successful P99 ms | Failed / offered | Deadline attainment |
|---|---:|---:|---:|
| Static40 | 1641.67 | 0 / 2140 | 68.27% |
| Reactive SLO-aware | 4047.91 | 55 / 2140 | 59.49% |
| Queue-aware | 3963.06 | 96 / 2140 | 61.54% |
| Predictive | 4222.84 | 62 / 2140 | 59.58% |
| Predictive shadow | 4177.52 | 87 / 2140 | 59.77% |

There were no speculative policy adjustments in these stress arms; configured
capacity rebases are separate operations. Severe latency/failures remain in the
ledger. Prediction did not rescue this overload; no general statistical ranking
is justified from one block. An unobserved abrupt capacity reduction is not
predictable from this local history alone.

## Cost, noise and verified limits

In ten active buildup runs, the median of per-run median prediction-collection
costs was 1.055 ms/poll; the largest sampled poll was 4.731 ms. This includes Python
history/window preparation and all five native models, not just a bare C++ call.
Median daemon/child CPU was 1.931 s/run versus static 1.783; paired CPU change
+6.89%, marginal interval [+2.36%, +11.66%]. Stable predictive-versus-static CPU
change was +11.82%, interval [+6.42%, +21.58%]. These are short VM runs, not isolated
per-program cycle attribution. Median peak daemon RSS in buildup was 29,802,496
bytes for predictive and 27,963,392 for static. No zero-overhead or long-soak claim.

All outliers were kept. Buildup r6-static had P99 822.57 ms, dispatch P99 113.50 ms
and sampled peak root backlog 279506 bytes; r7-queue-aware P99 was 347.19 ms with
dispatch P99 27.66 ms. These simultaneous observations cannot identify a unique
VM/host/network cause. Primary comparisons include them and all noise warnings;
no favourable-run subset replaced the prespecified analysis. No heavy local
build/test suite ran during the primary measurement windows. Lightweight progress
reads occurred; host scheduling and other VM-host activity remain uncontrolled.

## Corrections and validation

- A two-second prototype could not fit the unchanged two-window confirmation
  cadence. A synthetic timing contract justified four seconds before final
  evaluation. Configurations whose confirmation + sampling delay do not fit
  automatically disable the proactive branch and expose reactive fallback.
- Reboot exposed an empty adaptive journal preventing daemon startup. Invalid audit
  history now logs unavailable history while remaining disabled; journal memory/
  serialization are byte bounded. This is not durable fsync recovery.
- Benchmark history formerly accepted prior-boot monotonic records, producing
  phantom action counts. Controller session UUID filtering now prevents this;
  reproduction/regression tests include a future-looking old monotonic timestamp.
- Final boundary tests found missing root bytes could raise KeyError, and subnormal
  growth could export infinite lead time. Unknown root counters now invalidate
  prediction; lead is finite and only reported inside the horizon. Native fields
  suffixed `_bps` mean bytes/s. A local-shaper headroom proxy is explicitly labeled.

The repeated performance dataset predates the final boundary guards and context-only
RTT-variation/headroom fields; neither context field drives the forecast models. Its revision
and hashes are preserved, not relabeled as a rerun of later code. All 617 saved
predictive comparison frames had integer root counters. Leads beyond the horizon
were already ineligible for actuation; marking them unknown changes explanations,
not the selection rule. A separate post-guard live shadow smoke validates the new
build without claiming a repeated post-guard speedup. Exact build-library SHA256s
are in results/predictive-native-binary*.sha256.

Validation logs: results/predictive-final-validation.log,
results/predictive-nobpf-final-validation.log, results/predictive-installed-final.log,
results/predictive-installed-nobpf-final.log,
results/predictive-adaptive-integration-reboot-fixed.log and
results/predictive-post-guards-smoke.log. Both CTest configurations contain 17 targets;
11 predictive unit cases cover deterministic/synthetic bounds, causal labels,
unknown telemetry, native failure, memory, timing, fallback, constraints and journals.

Exploratory/aborted artifacts were retained: the initial shadow traces, a static
functional pilot with overlapping validation, and the interrupted two-second
prototype suite. They were not pooled into the frozen four-second evaluation.

## Reproduction and next decisions

```sh
cmake -S . -B build -G Ninja -DFLOWCORE_BPF=ON
cmake --build build
ctest --test-dir build --output-on-failure
sudo python3 -B scripts/predictive_suite.py --repetitions 10
python3 -B scripts/predictive_report.py results/<suite>/buildup-<timestamp>
```

The suite generates a fresh separate pilot; --pilot may reuse a validated local
static-only pilot with disjoint evaluation seeds. Each experiment preserves the
configuration, source hashes, monotonic queue/controller timeline, raw requests,
failures, transition/readback evidence, host noise, aggregated CSV, JSON and HTML.
Raw artifacts remain local and Git-ignored under the repository workflow.

Highest priorities: measure whether denser queue observations are affordable and
informative; obtain more independent congestion onsets before calibration; extend
follow-up beyond the forecast/action horizon; separate sampling overhead from
policy effects; repeat on controlled physical hardware. Do not relax safeguards
or fit a more complex model merely to manufacture positive results. The supported
conclusion is an executable experimental predictor with safe fallback and no
established advantage over reactive control or tuned static QoS.
