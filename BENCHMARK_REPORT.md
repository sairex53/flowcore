# Independent benchmark report

Maintainer-run reproduction on Fedora 45 ARM64/QEMU. No external reviewer or physical system participated.
The performance suite was frozen before pilot/evaluation. Static policy selection used disjoint pilot seeds.
The primary gate endpoint is overall successful-request P99 across traffic classes; critical-class metrics are secondary exploratory endpoints.
Every reported latency is offered/scheduled-to-completion application latency, including dispatch lag, TCP establishment, a request payload and 64-byte response. Failures and deadlines are reported separately.

## Static pilot

Selected static40: 40% critical guarantee.
Selection: lower median failure ratio, then lower overall P99, then higher background goodput; two pilot repetitions.

## primary

Evidence: /home/elliot/projects/flowcore/results/review-primary-1794013173664498860

| Mode | Valid/invalid | Overall P50/P95/P99 ms | Critical P99 ms | Failed/offered | Deadline attainment | Total app Mbps | Background Mbps | Background Jain | CPU s | Peak RSS MiB | Changes |
|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| static | 10/0 | 15.032/21.010/26.806 | 26.910 | 0/17600 | 1.000 | 11.184 | 7.580 | 1.000 | 1.479 | 32.660 | 0 |
| hybrid | 10/0 | 15.559/25.244/30.993 | 32.062 | 0/17600 | 1.000 | 11.181 | 7.576 | 1.000 | 1.525 | 34.057 | 0 |
| queue-aware | 10/0 | 15.468/25.014/30.269 | 31.064 | 0/17600 | 1.000 | 11.185 | 7.581 | 1.000 | 1.535 | 34.693 | 0 |
| predictive | 10/0 | 15.474/25.339/30.327 | 31.225 | 0/17600 | 1.000 | 11.195 | 7.591 | 1.000 | 1.575 | 34.316 | 0 |
| predictive-shadow | 10/0 | 15.484/25.421/31.012 | 31.875 | 0/17600 | 0.999 | 11.174 | 7.569 | 1.000 | 1.543 | 34.494 | 0 |

### Run-level variability and uncertainty

| Mode | Overall P99 min–max ms | 95% distribution-free median interval ms |
|---|---:|---|
| static | 25.740–28.088 | 25.808–27.851 (coverage 0.9785) |
| hybrid | 27.855–36.346 | 29.014–32.975 (coverage 0.9785) |
| queue-aware | 28.501–33.030 | 28.768–32.774 (coverage 0.9785) |
| predictive | 28.216–33.429 | 28.905–32.214 (coverage 0.9785) |
| predictive-shadow | 28.297–110.076 | 28.299–61.475 (coverage 0.9785) |

### Paired effects

- hybrid: overall P99 median relative regression 16.037%; gate INCONCLUSIVE; simultaneous interval {'low': 0.03360955223800179, 'high': 0.3408810723832358, 'coverage': 0.998046875}.
  Critical P99 median paired effect 19.352%; exploratory interval {'low': 7.5698670156146175, 'high': 24.92978463620057, 'coverage': 0.978515625}.
- queue-aware: overall P99 median relative regression 15.301%; gate INCONCLUSIVE; simultaneous interval {'low': 0.01469432854843248, 'high': 0.2699175914052256, 'coverage': 0.998046875}.
  Critical P99 median paired effect 16.757%; exploratory interval {'low': 7.033380921150423, 'high': 21.218897740929886, 'coverage': 0.978515625}.
- predictive: overall P99 median relative regression 13.861%; gate INCONCLUSIVE; simultaneous interval {'low': 0.069716180179356, 'high': 0.23299609814256148, 'coverage': 0.998046875}.
  Critical P99 median paired effect 15.011%; exploratory interval {'low': 10.3197156922481, 'high': 23.27908575428168, 'coverage': 0.978515625}.
- predictive-shadow: overall P99 median relative regression 15.373%; gate INCONCLUSIVE; simultaneous interval {'low': 0.060742396285317406, 'high': 3.213206520466899, 'coverage': 0.998046875}.
  Critical P99 median paired effect 17.290%; exploratory interval {'low': 7.21730096289912, 'high': 116.70445098915665, 'coverage': 0.978515625}.
- predictive_vs_shadow: {"overall_p99_percent_effect": {"n": 10, "median": -1.2055847161001387, "minimum": -70.73497128249191, "maximum": 6.62500245049189, "median_ci_95": {"low": -52.09830657647758, "high": 6.288319461738889, "coverage": 0.978515625}}, "qualification": "zero-action series cannot identify the benefit of dynamic actuation"}

## baselines

Evidence: /home/elliot/projects/flowcore/results/review-baselines-1794014094472115202

| Mode | Valid/invalid | Overall P50/P95/P99 ms | Critical P99 ms | Failed/offered | Deadline attainment | Total app Mbps | Background Mbps | Background Jain | CPU s | Peak RSS MiB | Changes |
|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| fifo | 3/0 | 282.842/310.559/312.710 | 311.544 | 0/1080 | 0.000 | 4.647 | 2.812 | 0.999 | 0.416 | 32.531 | 0 |
| fq_codel | 3/0 | 76.948/109.207/120.497 | 122.829 | 0/1080 | 0.353 | 4.619 | 2.686 | 0.998 | 0.433 | 32.258 | 0 |
| cake | 3/0 | 85.886/95.457/98.050 | 98.222 | 0/1080 | 0.333 | 4.615 | 2.682 | 1.000 | 0.451 | 31.684 | 0 |
| static | 3/0 | 31.180/45.305/52.545 | 51.003 | 0/1080 | 0.986 | 4.616 | 2.667 | 0.985 | 0.458 | 32.523 | 0 |

### Run-level variability and uncertainty

| Mode | Overall P99 min–max ms | 95% distribution-free median interval ms |
|---|---:|---|
| fifo | 311.312–315.347 | Insufficient independent runs |
| fq_codel | 115.663–122.829 | Insufficient independent runs |
| cake | 96.748–98.249 | Insufficient independent runs |
| static | 49.573–54.262 | Insufficient independent runs |

### Paired effects

- fifo: overall P99 median relative regression 500.152%; gate INCONCLUSIVE; simultaneous interval None.
  Critical P99 median paired effect 518.291%; exploratory interval None.
- fq_codel: overall P99 median relative regression 126.361%; gate INCONCLUSIVE; simultaneous interval None.
  Critical P99 median paired effect 128.177%; exploratory interval None.
- cake: overall P99 median relative regression 84.126%; gate INCONCLUSIVE; simultaneous interval None.
  Critical P99 median paired effect 90.994%; exploratory interval None.

## low

Evidence: /home/elliot/projects/flowcore/results/review-low-1794014196307277881

| Mode | Valid/invalid | Overall P50/P95/P99 ms | Critical P99 ms | Failed/offered | Deadline attainment | Total app Mbps | Background Mbps | Background Jain | CPU s | Peak RSS MiB | Changes |
|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| static | 3/0 | 16.822/22.445/24.442 | 27.292 | 0/360 | 1.000 | 0.328 | 0.000 | N/A | 0.339 | 31.902 | 0 |
| hybrid | 3/0 | 16.347/22.215/23.930 | 27.420 | 0/360 | 1.000 | 0.328 | 0.000 | N/A | 0.354 | 33.523 | 0 |
| queue-aware | 3/0 | 15.774/22.404/23.301 | 23.606 | 0/360 | 1.000 | 0.328 | 0.000 | N/A | 0.349 | 32.555 | 0 |
| predictive | 3/0 | 16.637/22.776/23.997 | 23.997 | 0/360 | 1.000 | 0.328 | 0.000 | N/A | 0.361 | 33.215 | 0 |

### Run-level variability and uncertainty

| Mode | Overall P99 min–max ms | 95% distribution-free median interval ms |
|---|---:|---|
| static | 23.301–25.349 | Insufficient independent runs |
| hybrid | 23.521–26.086 | Insufficient independent runs |
| queue-aware | 23.138–24.741 | Insufficient independent runs |
| predictive | 23.165–30.448 | Insufficient independent runs |

### Paired effects

- hybrid: overall P99 median relative regression -2.095%; gate INCONCLUSIVE; simultaneous interval None.
  Critical P99 median paired effect 0.468%; exploratory interval None.
- queue-aware: overall P99 median relative regression -4.667%; gate INCONCLUSIVE; simultaneous interval None.
  Critical P99 median paired effect -13.507%; exploratory interval None.
- predictive: overall P99 median relative regression 2.983%; gate INCONCLUSIVE; simultaneous interval None.
  Critical P99 median paired effect -14.963%; exploratory interval None.

## linux_default

Evidence: /home/elliot/projects/flowcore/results/review-linux_default-1794014296991932472

| Mode | Valid/invalid | Overall P50/P95/P99 ms | Critical P99 ms | Failed/offered | Deadline attainment | Total app Mbps | Background Mbps | Background Jain | CPU s | Peak RSS MiB | Changes |
|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| linux-default | 3/0 | 6.190/12.569/14.679 | 14.679 | 0/360 | 1.000 | 0.328 | 0.000 | N/A | 0.331 | 31.633 | 0 |

### Run-level variability and uncertainty

| Mode | Overall P99 min–max ms | 95% distribution-free median interval ms |
|---|---:|---|
| linux-default | 12.761–15.024 | Insufficient independent runs |


## Interpretation and limits

The primary held-out series has 50 valid runs and 88,000 offered requests, with zero request failures. Static QoS has lower median P99 than every adaptive mode here. All four paired overall-P99 effect intervals lie above zero under the run-level model, supporting the direction of worsening in this scenario. However, each interval crosses the predeclared 10% regression margin: the configured noninferiority/regression gate is INCONCLUSIVE, not PASS. This does not establish acceptable regression or adaptive superiority.

There were **zero policy adjustments in every primary run**. Decision records show critical-class borrowing and no observed critical backlog despite link saturation. Holding the policy is consistent with requiring network evidence before acting on application latency; it is not evidence that proactive allocation works. Static used a pilot-selected 40% critical guarantee; other modes started at 30%. Initial allocations and controller/reporting overhead confound a pure dynamic-action comparison. Predictive versus shadow also has zero actions, so their comparison cannot identify an actuation benefit. Do not tune against these evaluation seeds.

Low-load hybrid and queue-aware point effects are favorable (−2.095% and −4.667% paired overall P99), while predictive is unfavorable (+2.983%). Three repetitions cannot supply the required uncertainty; all are INCONCLUSIVE. Zero actions also prevent attributing those favorable points to control. FIFO/fq_codel/CAKE/static rows use the same shaped 5 Mbps scenario; they are exploratory three-run evidence, separate from the 12 Mbps primary. Linux-default is explicitly unshaped/unimpaired with no background traffic: its P99 is context, not a capacity-matched congestion superiority comparison.

### Configured versus observed network

Primary configuration: sender egress HTB 12 Mbps; receiver-side reverse-path netem gives configured base RTT 6 ms. Offloads are disabled/read back on owned veths. Request payload 4,096 bytes, response 64 bytes; fresh TCP connections. Phases (seconds, requests/s, background streams): (2,40,1), (3,100,4), (8,150,8), (3,60,2). One-second warmup; 80/15/5 class weights, two critical logical service labels, 40 ms deadline, 30 ms controller target. A guarantee is not a maximum: HTB leaves may borrow up to their ceilings.

All 50 primary counter windows support saturation. Observed sender egress spans 11.858–11.980 Mbps, or 98.819–99.833% of configured rate. Sender/receiver forward byte discrepancy is at most 0.0191%, packet discrepancy at most 0.0176%, and summed-class/root-qdisc byte discrepancy at most 0.0515%; snapshots are sequential. These are observed traffic/counter checks, not an assertion of perfectly atomic measurement. Primary sender/receiver topology, class hierarchy and counters are retained per trial.

Primary service labels share class-aggregated control. This does not validate conflicting, independently enforced per-service SLO optimization. Jain values measure duration-weighted fairness among background streams within phases; they do not measure fairness between services/classes.

### Failure accounting, statistics and host noise

Tables show medians of independent run summaries; failed/offered are totals. Deadline attainment counts all offers, including failures. Completed requests outside the scheduling window remain in the ledger; they are not silently dropped from latency/failure comparisons. Successful-request latency alone is not the outcome. Total app Mbps counts verified request payload plus background goodput, excluding transport overhead; observed link rate counts link bytes.

Order is seeded/randomized within paired repetition blocks; block workload seeds match between modes. Pilot and evaluation partitions are disjoint. The plan and its pre-data sample-window amendment are in Git. No outlier was deleted: predictive-shadow's 110.076 ms P99 run remains in the primary summary. Individual-run median intervals use exact order-statistic coverage; primary gate intervals also adjust for the tested endpoint family. Three-run intervals are explicitly unavailable, not bootstrapped from thousands of requests. Statistical fixture tests in tests/research_test.py cover effects, failure regressions, invalid experiments and insufficient samples.

No primary run triggered the configured noise warning. One static baseline run and every low-load/default run triggered dispatch-P99 >10 ms. All remain included. Primary median host busy fraction is about 12–12.4% across four CPUs; low-load medians are about 4.2–4.5%, yet dispatch lag is about 11.4–11.7 ms. Low host utilization does not prove absence of VM scheduling interference. Host CPU/PSI/load/kernel/build/commit snapshots are preserved. CPU seconds in the main table cover daemon/collected child cost during the sampled interval; RSS is sampled daemon peak. Neither is whole-system or universal overhead.

No recovery time is estimable from the primary series: it has no injected link fault or policy action. Recovery results below belong to a different MPTCP fixture.

## Scoped ablations and additional comparisons

These supplemental runs were independently executed but are exploratory; their short duration/repetition counts do not satisfy the main inferential gate.

### Per-flow classification

Three two-second repetitions per mode, seed 513791:

| Classifier | Median app P99 ms | Median total Mbps | Host busy CPU s | Offered (all repetitions) | Failed |
|---|---:|---:|---:|---:|---:|
| none | 11.649 | 17.569 | 0.530 | 2696 | 0 |
| TC flower | 8.856 | 18.174 | 0.590 | 3005 | 0 |
| BPF | 9.241 | 18.032 | 0.610 | 3002 | 0 |

Paired BPF P99 effect is −20.666% versus none and +4.352% versus flower. Three-pair intervals are unavailable: both comparisons INCONCLUSIVE. The workload is closed-loop, so offered load differs; these are not fixed-offered-load causal effects. BPF userspace CPU rounded to zero scheduler ticks in a short sample; kernel execution is not included in that counter and the engine is not zero-cost. Retained-flow classification accuracy is not a full-traffic coverage guarantee. Separately, the 1,200-wire-flow export test passed; malformed kernel fixtures are excluded from that wire count.

### Observer/controller overhead

One four-second workload per mode, 125 requests/s, seed 871331; zero failed requests in all six modes, no background congestion. CPU windows include setup/warmup (about 7.1 seconds), separate from four-second application latency. “off” retains the daemon and laboratory.

| Mode | P99 ms | Daemon CPU s | Child CPU s | Collector CPU s | Daemon RSS MiB |
|---|---:|---:|---:|---:|---:|
| off | 16.410 | 0.054 | 0.102 | N/A | 32.086 |
| basic | 17.191 | 0.117 | 0.165 | N/A | 30.379 |
| diag | 19.795 | 0.124 | 0.149 | N/A | 31.555 |
| bpf | 19.603 | 0.544 | 0.146 | 0.020 | 34.141 |
| shadow | 15.484 | 0.313 | 0.119 | N/A | 34.242 |
| active | 26.247 | 0.364 | 0.168 | N/A | 34.129 |

The BPF sample processed 5,002 events with reported reserve/transport/decode/parse drops all zero and about 5.09 MiB collector RSS. This proves only observed loss in that fixture. One run cannot establish a general overhead effect; no daemon-absent ablation or decision to replace Python follows from these numbers.

### MPTCP primary-path failure

One eight-second repetition per strategy, seed 619007, fixed order. All connections survived and all requests completed, but every request missed the fixture's 25 ms target (deadline attainment 0). MPTCP negotiation and two subflows were verified for MPTCP rows; plain TCP was not reported as multipath.

| Strategy | Offered/completed | P99 ms | Outage completed | Outage Mbps | First completion after restoration ms |
|---|---:|---:|---:|---:|---:|
| single-path-tcp | 46/46 | 3492.440 | 0 | 0.000 | 1304.501 |
| linux-default | 56/56 | 2330.233 | 0 | 0.000 | 142.283 |
| static-endpoints | 63/63 | 766.428 | 7 | 0.823 | 41.438 |
| latency-preferred | 63/63 | 652.410 | 12 | 1.430 | 615.964 |
| reliability-preferred | 58/58 | 897.777 | 10 | 1.167 | 270.827 |
| throughput-preferred | 67/67 | 539.336 | 12 | 1.408 | 287.822 |
| slo-aware | 70/70 | 520.896 | 13 | 1.525 | 157.153 |

Lower one-shot P99 for several path policies is a candidate benefit, not a demonstrated general win. Offers differ in this closed-loop fixture. A reobserved primary address is not proof of restored path traffic; endpoint backup flags do not control packet scheduling. “First completion” is an application event, not a bound on subflow failure detection. Host MPTCP configuration before/after matched, including pre-existing non-FLOWCORE endpoints.

### Cooperative transports

One repetition per impairment: 40 requests per TCP/MPTCP/QUIC mode, including individual and mixed phases, five scenarios. All 600 measured echoes completed. Separate intentional cancellation is not silently counted as a successful normal request. SDK telemetry is best-effort and dropped events were observed even with successful wire requests; daemon telemetry must not substitute for the complete workload ledger.

| Scenario | TCP P99 ms | MPTCP P99 ms | QUIC P99 ms | Deadline attainment TCP/MPTCP/QUIC |
|---|---:|---:|---:|---|
| stable | 0.443 | 0.417 | 1.923 | 1.000/1.000/1.000 |
| loss | 0.410 | 0.389 | 2.033 | 1.000/1.000/1.000 |
| reorder | 15.029 | 15.183 | 43.143 | 1.000/1.000/1.000 |
| bandwidth | 46.639 | 84.263 | 109.612 | 1.000/0.500/0.500 |
| interface | 671.738 | 671.537 | 512.034 | 0.975/0.975/0.975 |

All QUIC handshakes were confirmed with aioquic. MPTCP negotiation was confirmed here, but this single-path transport fixture does not prove multipath; the separate failover test does. Real configured impairment does not guarantee a short sample observed every random loss event. Closed-loop transport differences, protocol implementations and differing service intervals preclude causal protocol superiority claims. This is echo/SDK integration, not HTTP/2 or HTTP/3.

## Ablations still missing

There is no independently reproduced same-allocation application-intent on/off comparison, sufficiently powered queue-input ablation, prediction method/lead-time ablation, or active-action benefit comparison. Existing fixture tests and decision logs are not substitute measurements. Complete absence of FLOWCORE versus installed host-monitor overhead, long-duration memory stability and physical-hardware measurements remain unverified.

## Reproduction and evidence retention

Use a disposable Fedora 45 ARM64 VM with supported iproute2/tc, build both optional-BPF configurations and run the unprivileged tests first. From the project root:

```sh
cmake -S . -B build -DCMAKE_BUILD_TYPE=Debug
cmake --build build -j2
ctest --test-dir build --output-on-failure
sudo python3 -B scripts/release_validation.py
python3 -B packaging/review-results.py results/release-review-<id>/index.json build/review-analysis.json --markdown build/review-generated-tables.md
```

The evaluation data source is commit 7e801a6de4f1e4438e243d8f22e8b2aae0f26c8b, clean at start. Later review fixes concern admission/readback, tests, packaging and release documentation; no new evaluation fitting was performed. config/release-evidence.json records hashes of retained summary/index/plan artifacts, not a substitute for full raw data. The local primary index is results/release-review-1794013063206910777/index.json. It covers 77 evaluation runs plus six pilot runs. Raw request CSV, per-run JSON, topology/kernel readbacks, decision histories, host snapshots and generated CSV/HTML remain under the indexed result directories. Supplemental paths are recorded in the evidence index and release readiness.

Full raw artifacts are preserved locally and excluded from Git per repository policy. A reviewed public evidence bundle is still pending; current hashes and reproduction commands are useful but do not make the existing raw data publicly accessible. Performance/reproducibility release gates remain PARTIAL.
