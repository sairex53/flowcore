# Real application benchmark methodology

## Reproduction

The runner requires root because it creates two disposable network namespaces, veth links, qdiscs, and (when requested) a Podman database container joined to the receiver namespace. It refuses to run when another FLOWCORE daemon or research runner owns the lab.

```sh
cmake -S . -B build -DFLOWCORE_BPF=OFF
cmake --build build -j2
sudo python3 scripts/real_app_lab.py --postgres \
  --modes linux-default,fq_codel,cake,static,hybrid,predictive \
  --repetitions 10 --seed 47031 --capacity-kbit 1500 \
  --rate 60 --db-rate 20 --duration 15 --concurrency 8 --db-concurrency 4
```

Use --postgres to add PostgreSQL and --grpc to add gRPC to the same namespace pair. With both flags, the runner starts the API, database, gRPC service, background upload, FLOWCORE, OTLP receiver, and clients together. The runner randomizes mode order while pairing mode runs by repetition seed. Each run starts a fresh daemon, new namespace pair, application server, OTLP receiver, and (with `--postgres`) uniquely named PostgreSQL container. It saves full per-request JSON, environment/configuration, controller history, network snapshots, an aggregate CSV, and HTML summary below `results/real-applications-*/`. Privileged setups that fail readback are marked invalid rather than counted.

`linux-default` removes the egress shaper entirely. It is an unshaped control and does not have the configured 1.5 Mbit/s bottleneck; interpret it separately. `fq_codel` is one HTB capacity class with fq_codel, not a pure root fq_codel link shaper. CAKE shapes at the requested rate. `static` uses fixed HTB class shares. `hybrid` and `predictive` run the existing FLOWCORE active controller strategies on the same class model. CAKE and single-class fq_codel do not provide per-service class guarantees.

## Recorded outcomes

The HTTP cohort records attempted/completed requests, errors, timeouts, completion ratio, deadline misses, P50/P95/P99 of successful requests, dispatch lag, and offered request rate. The artifact transfer records bytes, completion time, and goodput. With PostgreSQL, the database cohort additionally records query completion/error/deadline metrics and query throughput. The runner records daemon CPU and HTTP service CPU deltas; database container CPU is not currently included. The background backup is an upload from sender to receiver, so it traverses the same shaped egress as critical API requests. A separate HTTP artifact download endpoint is also provided, but download traffic follows the reverse direction and is not used as the lab egress contender.

The application client exports one bounded OTLP/HTTP JSON batch after its workload has completed, avoiding periodic exporter traffic during the measured interval. The benchmark report stores whether this export succeeded. The collector exposes Prometheus output and bounded series state.

## Statistical limits

Requests within a run are not independent experiments. For serious comparisons, use at least ten paired repetitions; the initial matrix below has only three. Each run preserves raw samples, and the HTML table summarizes observed runs. This report does not calculate confidence intervals or a formal causal effect. Increase repetitions and vary request rates, capacities, impairment profiles, and seeds before making general performance claims.


## Measured multi-service run

A first real-service matrix completed on 2026-11-07. The captured binary was built from commit 55859fc786f23d814278ad392d7d465f5e551c4e (clean tree). Environment: Fedora 45 Server, aarch64, kernel 7.2.11-300.fc45.aarch64, 4-vCPU QEMU VM. The workload used two isolated network namespaces linked by veth. For shaped modes, sender egress was verified at 1.5 Mbit/s. Receiver-side netem and sender-side qdisc state were recorded. One common CLOCK_MONOTONIC start time synchronized the three client workloads.

The matrix used three paired repetitions per mode, randomized mode order, and seeds 47032–47034. Each repetition ran for 8 seconds with six concurrent HTTP clients at 30 requests/s and one 1 MiB background upload; two PostgreSQL clients issued indexed reads at 10 queries/s with a 100 ms statement timeout; two gRPC clients issued unary calls at 10 requests/s with a 30 ms deadline. HTTP and gRPC had 30 ms deadlines. Each run offered 240 HTTP requests, 80 PostgreSQL queries, and 80 gRPC requests. The HTTP and gRPC handlers each included 2 ms of application work. The database queries ran against PostgreSQL in a Podman container. The database client is a separate workload in this topology; the HTTP API endpoint does not make a database call.

The backup upload shares sender egress with API, gRPC, and database traffic. The runner verified the configured qdiscs and recorded class byte/packet/drop counters. In HTB modes, traffic was classified by destination port into service classes; this demonstrates that the lab's packet filters and application telemetry work together, but does not demonstrate automatic mapping from a service's SDK identity to its packets. HTTP/2 multiplexing was preserved: gRPC deadlines and request measurements are per RPC, while network classification remains connection/packet class level.

### Application outcomes

Values are median across the three runs, with observed minimum and maximum in brackets. P99 is calculated over successful requests only; errors, timeouts, and deadline misses are shown separately. These are descriptive run summaries, not confidence intervals.

HTTP latency (milliseconds; median run P50/P95/P99 with observed min–max across three repetitions):

| Mode | P50 | P95 | P99 | Errors / 720 | Deadline misses / 720 |
|---|---:|---:|---:|---:|---:|
| Linux default | 3.900 [3.808, 4.014] | 7.051 [5.247, 7.443] | 12.723 [6.646, 20.204] | 0 | 2 |
| fq_codel | 21.997 [20.219, 23.284] | 35.628 [35.567, 42.219] | 47.015 [37.674, 53.165] | 27 | 151 |
| CAKE | 15.821 [15.695, 15.900] | 21.536 [20.909, 22.151] | 30.805 [29.285, 32.187] | 0 | 7 |
| Static HTB | 7.760 [7.755, 7.941] | 10.725 [9.201, 11.010] | 14.830 [14.327, 20.138] | 0 | 1 |
| FLOWCORE hybrid | 7.730 [7.430, 7.918] | 10.375 [9.508, 11.803] | 17.276 [16.880, 24.314] | 3 | 3 |
| FLOWCORE predictive | 7.798 [7.779, 7.805] | 9.554 [9.332, 10.886] | 17.663 [13.723, 25.562] | 0 | 2 |

PostgreSQL latency and completion outcomes:

| Mode | P50 ms | P95 ms | P99 ms | Completed / 240 | Errors / deadline misses |
|---|---:|---:|---:|---:|---:|
| Linux default | 1.618 [1.611, 1.657] | 4.138 [2.775, 4.476] | 22.111 [9.219, 27.279] | 240 | 0 / 0 |
| fq_codel | 10.248 [10.112, 10.471] | 24.308 [20.392, 31.307] | 35.327 [34.883, 51.578] | 240 | 0 / 0 |
| CAKE | 8.151 [8.010, 8.562] | 12.533 [12.504, 20.562] | 21.513 [17.518, 37.937] | 240 | 0 / 0 |
| Static HTB | 3.841 [3.698, 3.978] | 6.959 [5.672, 9.588] | 20.952 [14.316, 29.173] | 240 | 0 / 0 |
| FLOWCORE hybrid | 3.706 [3.340, 3.915] | 7.677 [6.626, 9.128] | 19.849 [17.264, 21.087] | 240 | 0 / 0 |
| FLOWCORE predictive | 3.891 [3.394, 3.911] | 6.857 [6.692, 8.467] | 18.183 [16.988, 24.715] | 240 | 0 / 0 |

PostgreSQL completed 10 queries/s in every mode, matching the offered rate. gRPC unary latency and errors:

| Mode | P50 ms | P95 ms | P99 ms | Errors/timeouts / 240 | Deadline misses / 240 |
|---|---:|---:|---:|---:|---:|
| Linux default | 4.950 [4.639, 4.976] | 8.530 [6.210, 10.585] | 23.738 [20.687, 28.916] | 2 | 2 |
| fq_codel | 12.835 [12.767, 13.696] | 23.535 [22.945, 23.994] | 29.410 [24.688, 30.607] | 6 | 7 |
| CAKE | 11.419 [10.600, 11.512] | 16.590 [15.645, 22.405] | 24.595 [21.577, 32.487] | 3 | 4 |
| Static HTB | 6.984 [6.684, 7.276] | 10.147 [8.078, 12.396] | 27.806 [18.154, 29.843] | 1 | 1 |
| FLOWCORE hybrid | 6.895 [5.852, 6.908] | 11.558 [10.120, 12.406] | 16.585 [14.978, 30.004] | 1 | 2 |
| FLOWCORE predictive | 6.721 [6.483, 6.746] | 8.821 [7.909, 10.873] | 19.163 [11.947, 24.485] | 1 | 1 |

The database client completed all 240 queries in every shaped mode; median successful query throughput was 10 queries/s (the offered rate). The gRPC completion rate was 9.75–10 requests/s across modes. These results show that the database workload ran and was measured, but they do not show that network policy controls SQL execution time independently of database work.

### Background transfer, controller actions, and runtime cost

| Mode | 1 MiB upload completion seconds, median [range] | Observed goodput Mbit/s, median [range] | Daemon CPU seconds, median [range] |
|---|---:|---:|---:|
| Linux default | 0.026 [0.020, 0.055] | 327.718 [153.000, 411.390] | 1.918 [1.840, 2.080] |
| fq_codel | 6.360 [6.343, 6.364] | 1.319 [1.318, 1.323] | 2.247 [2.151, 2.523] |
| CAKE | 6.377 [6.343, 6.497] | 1.315 [1.291, 1.323] | 2.252 [2.185, 2.401] |
| Static HTB | 6.344 [6.331, 6.356] | 1.322 [1.320, 1.325] | 2.256 [2.007, 2.281] |
| FLOWCORE hybrid | 6.337 [6.335, 6.338] | 1.324 [1.324, 1.324] | 2.197 [1.825, 2.350] |
| FLOWCORE predictive | 6.335 [6.334, 6.336] | 1.324 [1.324, 1.324] | 2.208 [2.198, 2.214] |

The Linux-default mode has no 1.5 Mbit/s shaper, so its much shorter transfer and apparent goodput are not comparable with shaped modes. The CPU values are absolute CPU seconds over the runner's measured interval, including setup/startup; they are not normalized overhead estimates. PostgreSQL container CPU and process memory were not collected.

The static HTB allocation had lower median HTTP P99 than both active FLOWCORE strategies in this workload: 14.830 ms versus 17.276 ms for hybrid and 17.663 ms for predictive. Paired per-seed HTTP P99 differences versus static were +2.446, +2.554, and +4.176 ms for hybrid, and -1.107, +3.336, and +5.423 ms for predictive. Predictive therefore had mixed paired outcomes and a +3.336 ms median paired difference. fq_codel and CAKE also had higher HTTP P99 than static in all three paired runs. This is a small, VM-only result, not evidence of general superiority of static QoS.

Neither active strategy changed the verified policy in these runs: the controller recorded zero applied actions for all six hybrid/predictive repetitions. Its decisions were holds; queue-backlog attribution was not eligible, including decisions with SLO misses. The observed latency differences therefore compare the active-controller modes' configured starting policy and run conditions, not a demonstrated adaptive improvement. This experiment provides no evidence that predictive control improves on reactive control or tuned static HTB.

All 54 application OTLP exports returned HTTP 200. SDK clients connected in the measured app runs; there were rare non-blocking SDK event drops (one per mode in some client streams), so delivery was not lossless. In the representative class counters, static HTB recorded approximately 82 kB in critical, 19 kB in interactive, 24 kB in standard, and 1.097 MB in background classes. This confirms the port filters matched the intended classes. CAKE and the single-class fq_codel baseline did not provide the same per-service class accounting.

The controller reported a Jain index over active class rates for HTB-shaped runs: static median 0.574 [0.434, 0.697], hybrid 0.412 [0.327, 0.535], and predictive 0.467 [0.447, 0.663]. fq_codel reported 1.0 because the experiment exposed one class; CAKE and unshaped Linux default had no comparable value. This controller-side class index is not per-flow fairness and is not comparable across different class counts. There is no statistically supported fairness comparison from these three repetitions.

### Interpretation and limitations

This is an initial integration and measurement result, not a release performance claim. Three repetitions are too few for reliable uncertainty estimates; the report provides ranges but no confidence intervals or hypothesis tests. Results are from one 4-vCPU virtual machine, one 1.5 Mbit/s capacity, one request mix, and one short duration. No physical host or x86_64 system was tested. HTTP and gRPC service handlers are runnable Python examples, not production service frameworks. The benchmark does not establish general PostgreSQL application latency benefits, service-identity-to-packet classification, production fairness, or successful adaptive/predictive actuation.

The raw per-run request data, qdisc snapshots, controller history, environment metadata, and logs are preserved locally under results/real-apps-validation-20261107/ (ignored by Git). Aggregate JSON, CSV, and HTML are in that directory as well. Reproduce the captured matrix with:

    sudo python3 -B scripts/real_app_lab.py --modes linux-default,fq_codel,cake,static,hybrid,predictive --repetitions 3 --seed 47031 --capacity-kbit 1500 --rate 30 --duration 8 --concurrency 6 --deadline-ms 30 --background-bytes 1048576 --postgres --db-rate 10 --db-concurrency 2 --db-deadline-ms 100 --grpc --grpc-rate 10 --grpc-concurrency 2 --grpc-deadline-ms 30 --output results/real-apps-validation-20261107-reproduction
