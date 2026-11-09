# SLO optimization results

## Paired namespace experiment

Measured on Fedora 45 ARM64 in a four-vCPU QEMU VM with Linux
`7.2.11-300.fc45.aarch64`, iproute2 7.0.0 and a Debug build without BPF.
All 36 trials were valid. Each of 12 independent randomized blocks ran
static HTB with fq_codel, the existing allocation optimizer, and Hybrid V3.
The arms started with the same HTB policy and had a verified 12 Mbit/s
sender bottleneck. The workload used two critical services, 24 KiB requests,
a 70 ms deadline, and four then six bulk flows. Hybrid V3 began without a
binding socket cap.

| Policy | Critical P99 ms | Deadline misses / offered | Successes | Bulk / weakest critical service Mbit/s | Jain | Wire utilization | Daemon CPU % | Updates/min |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Static HTB | 52.80 | 27 / 4,680 | 100% | 5.849 / 2.114 | 0.99996 | 99.10% | 7.33 | 0.00 |
| Existing optimizer | 52.80 | 8 / 4,680 | 100% | 5.850 / 2.107 | 0.99993 | 99.13% | 7.75 | 0.00 |
| Hybrid V3 | 51.65 | 20 / 4,680 | 100% | 5.831 / 2.114 | 0.99995 | 98.94% | 7.17 | 8.57 |

Metrics are run medians except for the request counts. Four runs carried
host or dispatch noise warnings and were retained. Median peak daemon RSS
was 34.01, 36.70 and 34.77 MiB respectively. All arms passed the configured
per-run resource and outcome budgets. Stable workload recovery was
not applicable.

All three arms remained on the evidence front. The recommendation was
**keep static HTB**. For Hybrid V3 versus static, the paired median critical
P99 change was −0.17 ms with simultaneous interval [−148.55, +54.24] ms.
The paired background goodput change was −0.016 Mbit/s
[−0.149, +0.126]. The normalized weighted loss changed by +2.05
[−50.75, +20.99], where positive values favor static. For the existing
optimizer versus static the weighted loss change was −0.85
[−52.07, +5.25]. Both alternatives lacked a verified improvement and a
verified critical P99 guardrail. The breadth of the latency intervals
includes extreme run tails and reflects the conservative planned family.

Hybrid V3 performed 22 verified HTB changes including restoration. Their
pre-read, apply and verify stages totaled 26.76 ms at the median and
50.98 ms at the maximum. The existing optimizer logged no verified
allocation changes in this workload. This comparison measures the modes as
run. It does not establish a benefit of the existing optimizer's actuator.

The objective profile was authored during the experiment and applied to the
complete matrix. It is an illustrative operator profile and the comparison
is exploratory. The score labels describe the measured workload only.

## Capacity collapse and recovery

A separate seed set used four bulk flows, 30 requests/s, and a verified
12 → 7 → 12 Mbit/s transition. All six trials finished with zero request
failures. The 70 ms deadline miss rate ranged from 23.67% to 25.17%.
The critical P99 ranged from 1.63 to 1.95 seconds. All three arms violated
the profile's 15% deadline-miss budget. The two-block series also falls
short of the profile's independent-block minimum.

Every arm reached two healthy one-second critical request bins four
seconds after restoration in both runs. That observed recovery describes
this fault profile. The six trials do not resolve performance differences
between policies.

## Live guard and verification

A single Hybrid V3 smoke run verified a socket cap release. Receiver
goodput rose from 400,362 to 1,288,649 and 1,291,664 bytes/s. The
configured weighted-loss deltas were −11.92 and −13.56. Neither window
cleared the deliberately high 30-point gain required by the test profile.
An existing latency guard also failed. The log records both findings
and the controller froze further actions. All 524 offered requests
completed successfully. The guard is observational and the single
smoke run supplies no independent comparative estimate.

The main report spent approximately 1.4 ms of process CPU time in
selection and interval calculation after loading and validating the
artifacts. All 50 CTest entries passed, including eight SLO unit cases.
The privileged shadow test verified the active Hybrid generation,
read an authorized service intent and returned `REJECT_INFEASIBLE`
for a 1 ms deadline with a supplied 2 ms processing budget. Its kernel
allocation and host network snapshots were unchanged.

## Reproduction

Run the two manifests in an otherwise idle Linux environment with a built
FLOWCORE checkout. Both experiments use the existing disposable veth lab
and apply no policy to host interfaces.

```sh
cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Debug -DFLOWCORE_BPF=OFF
cmake --build build
ctest --test-dir build --output-on-failure
sudo python3 -B tests/slo_lab.py
sudo ./build/flowcore bench replay scenarios/slo-optimization-paired.json
sudo ./build/flowcore bench replay scenarios/slo-optimization-recovery.json
sudo ./build/flowcore bench replay scenarios/slo-optimization-live.json
./build/flowcore slo report results/EXPERIMENT/summary.json \
  --profile config/slo-optimization.json
```

Each raw trial retains its application request ledger, receiver counters,
configuration, order and seed, topology readbacks and controller decisions.
The example objective profile uses ordinary measured units and separate
normalization scales. The raw artifacts remain on the test machine.

| Evidence | Local artifact directory |
| --- | --- |
| Paired optimization | `results/research-1794211051005631209/` |
| Capacity recovery | `results/research-1794211643197275820/` |
| Live objective guard | `results/research-1794211779426664301/` |
