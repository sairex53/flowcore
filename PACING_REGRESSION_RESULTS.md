# Pacing regression results

Fedora 45 ARM64, Linux 7.2.11-300.fc45.aarch64, disposable veth namespaces. The comparative matrices retain request attempts and receiver goodput even when P99 is unfavorable. A small exploratory run cannot satisfy the ten-pair statistical gate.

## Reproduce

~~~sh
cmake -S . -B build
cmake --build build -j3
ctest --test-dir build --output-on-failure
sudo ./build/flowcore bench replay scenarios/tcp-pacing-transition.json
sudo ./build/flowcore bench replay scenarios/tcp-pacing-guarded-paired.json
sudo ./build/flowcore bench replay scenarios/tcp-pacing-guarded-ablation.json
sudo ./build/flowcore bench replay scenarios/tcp-pacing-guarded-pressure.json
sudo python3 -B tests/safety_integration.py
python3 -B scripts/pacing_analysis.py results/<experiment>/summary.json
~~~

The paired matrix uses 12 s trials, independent block seeds, randomized order, verified 12→7→12 Mbit/s capacity and identical offered traffic in each block. Static, legacy hybrid pacing and guarded pacing use the same HTB allocations and fq_codel leaves. The two-block steady ablation also tests FQ as the HTB leaf, a fixed cap and queue/SLO controllers under the same classified hierarchy. A separate high-demand fixture exercises the guarded admission boundary. The experimental mode remains opt-in.


## Paired capacity transition

All 30 runs were valid, with 4,500 offered requests and zero failed requests per arm. The table reports medians of per-run statistics and total deadline misses. Daemon CPU includes children. Socket sampler CPU is measured in the client.

| Policy | P50 / P95 / P99 ms | Deadline misses | Background / total Mbit/s | Bulk Jain | Daemon CPU s | Socket sampler CPU ms | Verified adaptive group updates |
|---|---:|---:|---:|---:|---:|---:|---:|
| Static HTB | 15.05 / 20.80 / 25.84 | 1 | 8.053 / 9.282 | 0.99984 | 1.037 | 21.01 | 0 |
| Legacy hybrid pacing | 15.15 / 21.80 / 25.90 | 2 | 6.543 / 7.771 | 0.99990 | 1.002 | 22.37 | 22 |
| Guarded pacing | 14.92 / 20.67 / 25.30 | 2 | 8.045 / 9.274 | 0.99978 | 1.029 | 26.10 | 0 |

Against static, the guarded paired median P99 change was −1.10%, with simultaneous interval [−10.44%, +18.74%]. P99 is **INCONCLUSIVE**. The total-goodput noninferiority gate passed, with median reduction 0.40% and interval [−0.63%, +0.70%]. The individual paired background difference was −0.038 Mbit/s with interval [−0.048, +0.021] at 97.85% attainable coverage. Failure and deadline gates passed. Guarded made zero adaptive writes. Of 66 valid sampled scopes, one showed a nonzero critical leaf backlog. This does not exclude short queue events between observations. The series measures the cost and behavior of refusing unjustified cuts.

Legacy hybrid's total goodput decreased by 16.25%, simultaneous interval [15.60%, 17.42%], exceeding the 10% margin. Its median paired background change was −1.512 Mbit/s with interval [−1.591, −1.480]. Its P99 remained inconclusive. The exploratory direct comparison found a median guarded-minus-legacy background difference of +1.493 Mbit/s, individual interval [+1.468, +1.573]. The overall matrix is **REGRESSION** because the legacy arm failed the total-goodput gate.

The receiver medians by workload phase were 10.34 / 7.31 / 10.31 Mbit/s for static, 3.23 / 7.30 / 6.41 for legacy, and 10.29 / 7.30 / 10.21 for guarded. The capacity cut and recovery occurred within the middle phase. The low-cohort loss appears again without a P99 advantage. The observed background goodput preserved by guarded comes from leaving the cap nonbinding in these trials.

The trace recorded 176 adaptive socket writes in the legacy arm and zero intersecting the held class-lock intervals. Median traced pin, precheck, setsockopt and readback times were 2.79, 0.54, 0.58 and 0.54 μs per attempted write. Median HTB resize including readback was 20.35 ms in the guarded arm. Median guarded native decision call was 36.7 μs and queue sample age at feedback read was 491 ms. This indicates that the sampled control interval dominates syscall latency here. It does not show wire-level pacing delay. The trace observes adaptive writes, while the deterministic lock fixture covers waiting membership callbacks. No assertion of cross-process atomic socket option CAS is made.

Raw paired evidence is under `results/research-1794207896933052837`. The preliminary corrected smoke is under `results/research-1794207836316498308`.


## Steady same-allocation ablations

All 14 runs were valid with zero failed requests. Values are medians over two runs per arm, except deadline misses and updates, which are totals. The FQ arm replaces the HTB child qdiscs and retains the same rates and flower classification. FQ and FQ-CoDel differ in AQM behavior as well as pacing support. Two blocks are insufficient for the predeclared gate.

| Policy | P50 / P95 / P99 ms | Deadline misses | Background / total Mbit/s | Daemon CPU s | Adaptive updates |
|---|---:|---:|---:|---:|---:|
| Static HTB + fq_codel | 14.87 / 21.38 / 30.38 | 2 | 9.954 / 11.183 | 1.072 | 0 |
| Static HTB + FQ | 14.80 / 19.12 / 26.64 | 2 | 10.054 / 11.282 | 0.957 | 0 |
| HTB + fixed 180 kB/s per-socket cap | 14.92 / 20.88 / 26.79 | 1 | 8.129 / 9.358 | 1.063 | 0 |
| HTB + queue-aware pacing | 14.97 / 21.03 / 26.48 | 3 | 8.424 / 9.652 | 0.927 | 6 |
| HTB + SLO-aware pacing | 14.80 / 21.34 / 36.85 | 5 | 8.544 / 9.772 | 0.963 | 1 |
| HTB + legacy hybrid pacing | 14.85 / 20.77 / 24.73 | 0 | 8.358 / 9.587 | 0.998 | 7 |
| HTB + guarded pacing | 14.77 / 19.89 / 27.01 | 1 | 10.011 / 11.240 | 0.976 | 0 |

Bulk Jain medians rounded to 1.000 in each arm. The static cap was installed at socket attachment and is outside the adaptive update count. These results show substantial goodput cost with several caps and no confirmed P99 ranking. Raw ablation evidence is under `results/research-1794208331652036145`.


## Overload and reliability

The 40 KiB pressure fixture offered 560 requests to each arm. Static and guarded failed 273 and 292 respectively. Both missed all 560 deadlines, and successful-only P99 was 4.971 s and 4.940 s. Guarded made zero adaptive writes and background goodput was 2.305 Mbit/s versus 2.304 for static. The failed requests remain counted. This single overloaded pair cannot establish a latency or reliability advantage. The planner lacked a sustained healthy sample for intervention. Raw evidence is under `results/research-1794208538418508932`.

The deterministic native cases exercise distinct receiver intervals, exclusion of write-straddling observations, two-window goodput regression including starvation, probe expiry on a stale record, 12→7→12 capacity changes, redundant targets, invalid rates and fd/cookie drift. The live TCP loopback case holds the class lock and verifies that both socket attachment and planner activity wait. Simulated group write rejection and failed compensation exercise the fault paths. The privileged backend test passed concurrent CAS, interrupted partial HTB install, SIGKILL recovery, veth loss while sampling and stale ownership rejection.

The Fedora ARM64 C++23 build, all 48 CTest targets and staged installation capability probe passed.

Static HTB remains the reference and default. Guarded preserves background goodput in the measured paired workload by declining to impose a cut. The live experiments did not establish an effective guarded intervention or superior P99. The legacy and guarded pacing modes remain experimental.
