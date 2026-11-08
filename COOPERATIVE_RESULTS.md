# Cooperative SLO experiments

## Reproduce

Run on Linux from the FLOWCORE checkout. The harness uses the existing Fabric namespace topology, creates short-lived TLS credentials and cleans up its namespace workers. The CLI accepts a new output directory inside results/.

~~~sh
cmake --build build -j
ctest --test-dir build --output-on-failure
sudo python3 -B scripts/cooperative_lab.py --output results/coop-reproduction \
  --repetitions 4 --duration 8 --rate 24 --deadline-ms 250 --payload 4096
sudo python3 -B scripts/cooperative_lab.py --output results/coop-recovery-reproduction \
  --mode cooperative --repetitions 1 --duration 5 --rate 5 --deadline-ms 500 \
  --payload 512 --extended-recovery
~~~

The measured system was a Fedora 45 ARM64 VM with Linux 7.2.11-300.fc45.aarch64 and four vCPUs. Five disposable namespaces model three sending nodes, a router and a receiver. Gateway and backend calls to the database compete with bulk uploads at the router's shared 6 Mbit/s HTB+fq_codel bottleneck. Gateway-to-backend traffic uses the same gateway's local egress and a different router output. The control network uses separate veth links. The database is a processing fixture, not a database benchmark.

Four counterbalanced blocks use paired seeds and 192 offered requests per arm. Every arm runs identical TCP/TLS RPC framing and fan-out. The no-FLOWCORE arm keeps the shared HTB+fq_codel bottleneck with no sender caps. Independent QoS adds three 2 Mbit/s sender caps. Existing Fabric uses its regular planner with observed egress rates and no cooperative application aggregates, initially allocating 2,240/2,240/1,520 kbit/s. Cooperative mode starts with the same quotas and adds application-side enforcement and authenticated SLO aggregates. The run reconciles during measurement in both Fabric modes. Differences between the two Fabric modes combine application deadline behavior and network policy changes.

## Results

All 16 comparative runs completed with valid readbacks, a complete offered ledger and measured bottleneck traffic. Values below are medians of four run summaries. Deadline attainment counts every offered request. Error rate includes expired and partial results. Deferrals are tracked separately and there were zero in this matrix. Goodput counts successful gateway responses inside the eight-second window. Background throughput is delivered receiver payload inside the window.

| Mode | Successful P99 ms | Attainment % | Error % | Goodput req/s | Bulk Mbit/s |
| --- | ---: | ---: | ---: | ---: | ---: |
| No FLOWCORE | 105.58 | 100.00 | 0.00 | 24.00 | 3.348 |
| Independent QoS | 2794.97 | 3.13 | 44.53 | 10.69 | 1.894 |
| Existing Fabric | 2729.09 | 16.41 | 5.47 | 18.00 | 1.427 |
| Cooperative | 247.09 | 61.72 | 38.28 | 14.50 | 1.307 |

The no-FLOWCORE arm met every offered deadline and delivered the most background traffic. Cooperative mode lost to it in all four blocks, with paired median P99 **+138.30 ms** and attainment **−38.28 percentage points**. Observed paired ranges were +114.53 to +145.90 ms and −48.96 to −37.50 points. These are ranges of four observations, with insufficient replication for a general performance claim.

Against existing Fabric, cooperative mode increased paired median deadline attainment by **41.15 percentage points** but delivered **4.25 fewer successful requests/s** during the window and **0.121 Mbit/s less bulk goodput**. It recorded 455 successes, 215 expired and 98 partial results across 768 offered requests. Existing Fabric recorded 720 successes, 45 transport errors and three partial results. Many successful Fabric requests completed late. The cooperative arm expired requests at the application's budget. Its successful P99 therefore describes a different completion population. These experiments do not isolate the benefit of quota changes from deadline expiration and reduced load.

The coordinator verified quota changes to **2,858/1,828/1,314 kbit/s** in all four cooperative runs, after staged decreases. Median verified rollout time was 1.67 s. Existing Fabric retained 2,240/2,240/1,520 kbit/s during measurement. The control links carried median totals of 369 kB for cooperative mode and 454 kB for existing Fabric. Median summed process CPU for agents, actuators and coordinator was 0.74 s and 0.90 s respectively. These totals cover complete trial windows of different lengths and include control traffic from the test instrumentation. They do not establish a CPU saving from the new policy.

## Failure validation

During the comparative cooperative trial, disconnecting the backend's management link restored its 800 kbit/s floor in 3.16 s and blocked a new rollout. All five probe requests completed. Verified allocation returned 2.07 s after reconnect. Existing Fabric also completed five probes, with 2.85 s fallback and 1.92 s recovery.

In the separate low-load recovery run all 25 offered requests completed. An abrupt backend stop produced five partial requests. After restart, five out of five completed. Time from restart initiation through the five-request probe was 1.27 s. An abrupt coordinator stop restored all local floors in 2.81 s. Five requests completed while the coordinator was unavailable. After restart, all agents verified the same policy epoch and generation within 2.99 s.

## Artifacts and limitations

The Linux checkout retains results/cooperative-pilot, results/cooperative-matrix and results/cooperative-recovery. Each run includes the environment and seed schedule, request spans, receiver goodput counters, qdisc readbacks, rollout decisions and control-link/process measurements. The recorded Git base is 71d0ee1 with a development tree. The recovery run used the later revision with bounded RPC shutdown. Raw results and generated TLS credentials are excluded from Git.

These tests demonstrate distributed request cooperation across isolated Linux network namespaces on one VM. Separate physical hosts, independent clock drift, sustained production traffic and encrypted multiplexed transports remain unmeasured. The candidate quota weights and sender caps need a separate ablation against the shared-only fq_codel baseline before attributing benefits to cooperative actuation.
