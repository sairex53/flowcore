# Real application performance results

## Fixed capacity with concurrent bulk upload

The 3 Mbit/s matrix offered 240 HTTP/1.1 requests, 80 gRPC RPCs, 80
HTTP/3 streams and 80 independent PostgreSQL queries per four-second run.
A 1 MiB TCP upload competed with request traffic. The API protocols shared the
backend and database. The gRPC error rate includes three scheduled
client cancellations per run.

| Mode | Measured / attempted | HTTP P99 ms | HTTP errors | HTTP on time | HTTP completions/s | Confirmed bulk Mbit/s |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Shaped fq_codel | 9 / 10 | 64.78 | 2.50% | 97.50% | 58.50 | 2.45 |
| CAKE best effort | 10 / 10 | 64.79 | 1.04% | 98.75% | 59.38 | 2.44 |
| Tuned static HTB | 10 / 10 | 75.32 | 1.25% | 98.75% | 59.25 | 2.46 |
| FLOWCORE SLO-aware | 10 / 10 | 78.57 | 0.83% | 98.75% | 59.50 | 2.45 |
| FLOWCORE hybrid | 10 / 10 | 62.82 | 1.25% | 98.75% | 59.25 | 2.46 |

| Mode | gRPC P99 ms | gRPC errors | HTTP/3 P99 ms | HTTP/3 errors | PostgreSQL P99 ms | DB queries/s |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Shaped fq_codel | 91.97 | 5.00% | 140.84 | 1.25% | 42.81 | 20.00 |
| CAKE best effort | 62.01 | 4.38% | 124.07 | 0.00% | 28.85 | 20.00 |
| Tuned static HTB | 41.12 | 5.00% | 125.09 | 0.00% | 33.92 | 20.00 |
| FLOWCORE SLO-aware | 80.86 | 5.00% | 135.10 | 0.00% | 25.98 | 20.00 |
| FLOWCORE hybrid | 65.73 | 4.38% | 130.71 | 0.00% | 34.02 | 20.00 |

Cells are medians of run metrics. Each run offered 20 gRPC, HTTP/3 and
independent database requests per second. Static median attainment was
95.0% for gRPC, 99.38% for HTTP/3 and 100% for the independent query
client. Throughput and attainment for every run are in the artifacts.

Compared with static HTB, the paired median HTTP P99 effect was
+3.08 ms [-33.95, +24.76] for hybrid and +8.27 ms [-29.11, +34.15]
for SLO-aware. Both intervals span zero. The controllers made zero
allocation changes in these runs, so these comparisons provide no
evidence of a latency improvement from actuation. SLO-aware stopped in
three fixed-capacity trials with its background-starvation safeguard.
The safeguard is a controller observation. It does not establish
starvation of the upload. The gRPC P99
difference between shaped fq_codel and static HTB was +40.43 ms
[+16.34, +64.79] across nine complete pairs, with equal median failure
rates. The individual-run tails are noisy and this exploratory
comparison has no multiplicity adjustment.

Static HTB used a median 1.58 daemon CPU seconds in the observation
window, matching the hybrid and SLO-aware medians. Median daemon RSS
was 33.56 MiB for static, 35.40 MiB for hybrid and 35.45 MiB for
SLO-aware. The raw component snapshots include the application and
database processes. The static backend median of run median database
round trips was 2.35 ms, and the median of run mean PostgreSQL
executor times was 0.060 ms. Shaped fq_codel gave 8.25 ms and 0.059 ms
respectively. These signals support a path or wait component alongside
executor time. They do not identify a precise network-only duration.
Median static PostgreSQL executor count normalized to the offer
window was 119.75 queries/s across all clients and gateways.

One fq_codel trial is retained as INVALID_EXPERIMENT. An HTTP/3
deadline expired as aioquic removed the stream, and a cancellation
attempt raised an unhandled error. The runner kept the incomplete
trial and its process logs. The race was fixed during the last
blocks and subsequent fault runs used the corrected implementation.
All 196 protocol workload records from the 49 measured runs passed
the offered-count and outcome integrity checks. All measured
workloads exported their OTLP snapshots successfully.

## Changing network conditions

The three-mode dynamic profile measured all nine trials. At two seconds
the shaper fell from 3 to 1.5 Mbit/s and the receiver applied 16 ms
of reverse delay and 0.5% random loss. The sender interface went down
at four seconds and was restored at 5.2 seconds along with the initial
conditions. These are scheduled times. Confirmed changes lagged by
11.7 to 294.6 ms. The kernel readback, actual times and phase-specific
outcomes are preserved in the trial artifacts.

| Mode | HTTP P99 ms | HTTP errors | HTTP on time | HTTP recovery ms | HTTP/3 errors | HTTP/3 recovery ms | Bulk Mbit/s |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Tuned static HTB | 70.14 | 17.71% | 81.88% | 65.71 | 16.88% | 58.12 | 1.77 |
| FLOWCORE SLO-aware | 63.74 | 17.92% | 82.08% | 56.96 | 15.63% | 37.66 | 1.75 |
| FLOWCORE hybrid | 75.29 | 19.17% | 80.63% | 80.20 | 16.25% | 57.50 | 1.77 |

Cells are medians across three runs per mode. Each HTTP/3 client recorded
48 to 50 packet-loss events from its QUIC stack. Those events include
interface outage effects and do not independently estimate netem loss.
Each upload received a valid digest receipt. Independent PostgreSQL P99
rose to roughly 1.21 to 1.26 seconds, reflecting query completion across
the interrupted path. Median successful HTTP latency alone hides the
failure burst. The nine trials do not resolve policy effects on recovery.
One SLO-aware run ended in the background-starvation fallback. All nine
uploads still completed.

## Abundant capacity and application delay

With no bulk transfer, the unshaped Linux reference and 100 Mbit/s
static HTB both achieved 100% median HTTP deadline attainment over
three runs each. Their median P99 values were 60.24 and 64.88 ms.
The unshaped reference has a different reverse path configuration,
so this is a reference observation rather than an equal-capacity
causal comparison.

With 40 ms of configured backend work and no bulk transfer, HTTP
median run P50 was 46.34 ms with static QoS and 46.07 ms with hybrid.
Median backend database round trip was 2.40 ms in both. Hybrid recorded
zero allocation changes. Median P99 was 77.80 ms for static and
61.94 ms for hybrid, with median errors of 1.25% and 1.88% respectively.
Three trials per mode and unchanged allocation cannot establish an
adaptive benefit. Gateway and database pools start cold each run,
so startup effects remain in the application tails.

## Confirmation with isolated daemon state

Ten new paired blocks compared static HTB and hybrid with fresh daemon
state for every trial. All 20 trials were measured. Median HTTP P99 was
69.72 ms for static and 55.43 ms for hybrid, with 1.46% median errors for
both. Median deadline attainment was 98.33% and 98.54% respectively,
and confirmed bulk goodput was 2.45 Mbit/s for both. The paired
HTTP P99 effect was **-5.09 ms [-50.25, +12.81]** for hybrid minus
static. The gRPC, HTTP/3 and PostgreSQL P99 intervals also spanned zero.

Hybrid recorded zero verified class allocation changes and entered its
background-starvation fallback in two trials. The results provide no
evidence of adaptive benefit under these settings. One static run had
224.00 ms P99 and 2.50% errors. Raw request rows show three of its
slowest successful requests spent 159 to 229 ms reconnecting. The trial
remains in the analysis. The application latency includes reconnects.

The confirmation median daemon CPU was 1.58 seconds for static and
1.55 for hybrid. Median daemon RSS was 32.93 MiB and 34.54 MiB
respectively. These are VM observations in the full trial window.
A separate two-trial fault replay verified the final state isolation
path. The first on-time HTTP response after restoration was observed
after 81.07 ms with static and 36.53 ms with hybrid. The corresponding
HTTP/3 times were 62.96 and 64.43 ms. One pair is insufficient to rank
the policies, and both recorded zero verified allocation changes.

## Reproduction

See [SERVICE_STACK.md](SERVICE_STACK.md) for the version lock, build,
namespace layout and image digest. Run these commands from the repository.
All measured comparisons use Fedora 45 ARM64 in a VM. Each run starts a
fresh service stack and a fresh namespace pair.

```sh
sudo python3 -B scripts/real_app_lab.py --stack --grpc --grpc-cancel-every 32 \
  --http3-python build/quic-venv/bin/python --modes fq_codel,cake,static,slo-aware,hybrid \
  --repetitions 10 --duration 4 --rate 60 --concurrency 12 --capacity-kbit 3000 \
  --background-bytes 1048576 --deadline-ms 100 --grpc-deadline-ms 100 --http3-deadline-ms 150

sudo python3 -B scripts/real_app_lab.py --stack --profile dynamic --grpc --grpc-cancel-every 32 \
  --http3-python build/quic-venv/bin/python --modes static,slo-aware,hybrid --repetitions 3 \
  --duration 8 --rate 60 --concurrency 12 --capacity-kbit 3000 --background-bytes 2097152 \
  --deadline-ms 100 --grpc-deadline-ms 100 --http3-deadline-ms 150 --seed 8138451

sudo python3 -B scripts/real_app_lab.py --stack --profile abundant --grpc \
  --http3-python build/quic-venv/bin/python --modes linux-default,static --repetitions 3 \
  --duration 4 --rate 40 --capacity-kbit 100000 --deadline-ms 100 --grpc-deadline-ms 100 \
  --http3-deadline-ms 150 --seed 602141

sudo python3 -B scripts/real_app_lab.py --stack --profile application --grpc \
  --http3-python build/quic-venv/bin/python --modes static,hybrid --repetitions 3 \
  --duration 4 --rate 40 --capacity-kbit 100000 --deadline-ms 100 --grpc-deadline-ms 100 \
  --http3-deadline-ms 150 --seed 369057

sudo python3 -B scripts/real_app_lab.py --stack --grpc --grpc-cancel-every 32 \
  --http3-python build/quic-venv/bin/python --modes static,hybrid --repetitions 10 \
  --duration 4 --rate 60 --concurrency 12 --capacity-kbit 3000 --background-bytes 1048576 \
  --deadline-ms 100 --grpc-deadline-ms 100 --http3-deadline-ms 150 --seed 706729

sudo python3 -B scripts/real_app_lab.py --stack --profile dynamic --grpc --grpc-cancel-every 32 \
  --http3-python build/quic-venv/bin/python --modes static,hybrid --repetitions 1 \
  --duration 8 --rate 60 --concurrency 12 --capacity-kbit 3000 --background-bytes 2097152 \
  --deadline-ms 100 --grpc-deadline-ms 100 --http3-deadline-ms 150 --seed 445251
```

The first four matrices used separate processes and fresh qdisc trees.
They reused a durable decision journal across trials. The final runner
assigns each trial its own state directory and verifies that history
entries are newer than the trial. The confirmation runs use the isolated state path.

P99 describes successful responses and accompanies error and deadline
rates. The independent unit is a run, not an individual request. The
paired JSON reports candidate minus static QoS. Intervals are
distribution-free median intervals and are not adjusted for multiple
comparisons. Three-pair profiles are exploratory and cannot produce a
95% interval with this method.

## Raw evidence

The Fedora VM retains 92 measured trials, one invalid trial and 48,480
validated application request records across the six matrices. All
six preserved the host link and qdisc fingerprint. Each directory
contains the workload and seed manifest, JSON and CSV summaries, HTML
report, paired effects and per-trial raw application and kernel readback.

| Matrix | VM path under the repository |
| --- | --- |
| Fixed capacity | `results/real-applications-1794240093108738278/` |
| Changing network conditions | `results/real-applications-1794240558121706185/` |
| Abundant capacity | `results/real-applications-1794240691532261506/` |
| Application delay | `results/real-applications-1794240746926915352/` |
| Isolated state confirmation | `results/real-applications-1794240859741054473/` |
| Isolated state fault replay | `results/real-applications-1794241045269473440/` |

The first matrix's `fq_codel-rep07/` contains the failed HTTP/3
worker log. The matrix artifacts are retained on the VM outside Git.
