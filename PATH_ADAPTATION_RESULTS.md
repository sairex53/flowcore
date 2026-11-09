# Dynamic bottleneck and path adaptation results

The VM experiments use the [owned routed and direct topology](PATH_ADAPTATION.md).
P99 describes successful verified echo requests. Deadline attainment and
failure ratio include every issued request. The workload is closed loop,
so a stalled request suppresses later offers. Application goodput divides
verified response payload bytes by the client's elapsed time, including
setup and drain. Bulk goodput counts receiver-observed unique datagrams
over the offer window. Jain fairness describes the two received UDP flows.

Subflow acknowledged bytes and retransmitted segments are matched deltas
from MPTCP_FULL_INFO on the client side. They exclude first snapshots
and unmatched intervals. Reinjecting data can make these counts differ
from application goodput. Recovery latency starts at confirmed link-up
and ends at the first subsequently issued response that meets its
deadline. It does not measure time to restore the original subflow.

See [DYNAMIC_NETWORK_ENGINE.md](DYNAMIC_NETWORK_ENGINE.md) for the scope of
capacity estimates and actuation.
## Dynamic profile on Fedora 45 ARM64

Fedora 45 VM, four vCPUs, Linux 7.2.11-300.fc45.aarch64, iproute2 7.0.0,
Release build with BPF disabled. Ten independent blocks used master seed
44139, randomized mode order and identical within-block stimuli. All
40 runs were measured. The table gives medians across runs, except
request and connection counts.

| Policy | P50 / P95 / P99 (ms) | 100 ms deadline attainment | Failed / issued | Echo goodput (Mbit/s) | Bulk goodput (Mbit/s) | Connections surviving |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Fixed endpoint preference | 51.82 / 502.08 / 1404.62 | 93.98% | 0 / 648 | 1.725 | 2.902 | 10 / 10 |
| Default Linux MPTCP with configured endpoints | 59.86 / 248.85 / 800.32 | 94.24% | 0 / 690 | 1.803 | 2.936 | 10 / 10 |
| Existing startup-probe policy | 56.42 / 74.88 / 1002.44 | 95.77% | 0 / 709 | 1.870 | 2.905 | 10 / 10 |
| Path-aware | 57.09 / 65.82 / 603.64 | 97.50% | 0 / 808 | 2.082 | 2.915 | 10 / 10 |

Path-aware issued 808 requests with 21 misses, compared with 39 misses
for fixed, 42 for Linux default and 32 for the existing policy. Median
UDP Jain fairness rounded to 1.000 in each mode. The median paired
bulk-goodput change was +0.003 Mbit/s versus fixed preference
(interval −0.014 to +0.042) and −0.017 versus Linux default
(interval −0.053 to +0.011).

| Reference | Median paired P99 change (ms) | 97.85% interval (ms) | Median paired deadline-attainment change |
| --- | ---: | ---: | ---: |
| Fixed endpoint preference | −794.31 | [−1356.17, −542.30] | +3.59 pp |
| Default Linux MPTCP | −190.17 | [−793.05, +12.87] | +3.21 pp |
| Existing startup-probe policy | −403.92 | [−1486.11, −135.76] | +1.77 pp |

Intervals use order statistics on the ten within-block differences.
They are marginal intervals with no family-wise adjustment. P99 evidence
is inconclusive against Linux default. With about 65 to 81 requests per
run on average, P99 is strongly influenced by the slowest request. The
path-aware median P50 was higher than both the fixed and existing
policies. Closed-loop offered load differed because stalls delayed
future requests.

Median recovery time was 78.62 ms for path-aware, 97.93 ms for Linux
default, 127.17 ms for the existing policy and 623.16 ms for fixed
preference. One fixed run had no qualifying post-up response. Its
recovery observation is missing and remains visible in the raw data.

The observer detected the sudden rate reduction in all ten path-aware
runs, 75–148 ms after kernel confirmation. It identified gradual
degradation, carrier failure, carrier recovery and capacity recovery
in all ten, and competing demand in eight. One endpoint change per run
verified 723–769 ms after collapse and 602–680 ms before carrier loss.
The observer recorded no move back to the original path in this run
window. The initial low-rate interval still caused deadline misses.

Median matched client-side acknowledged-byte share on the bypass was
43.2% for path-aware, 37.8% for Linux default, 35.6% for the existing
policy and 29.8% for fixed preference. This is a subflow utilization
observation. Linux retained control over packet placement. Median
path-aware sample and decision CPU totaled 287 ms in the parent
and 443 ms in waited commands over a ten-second trial. Linux default
with the same measurement observer used 277 ms and 416 ms respectively.

The host link and qdisc fingerprint and host MPTCP settings matched at
entry and exit. All 40 trials verified endpoint cleanup and holder exit.
Raw evidence is in results/dynamic-path-1794241783503928360/ on the test
VM. The summary.json SHA-256 is
ae61eac7c850b4ec411d1e2fe0d3caa933a0ea435f5289b155d183d5ca8832f9.
The manifest records the base Git commit, dirty-tree state and hashes
of the sources loaded for this matrix. Subsequent checks hardened
classifier readback, capacity-estimate reset and cleanup validation.

## Stable profile and final validation

Three stable blocks used seed 66279. All 12 runs completed, every request
met its 100 ms deadline, and the path-aware controller made zero
changes. Median per-run P99 was 43.20 ms for path-aware, 31.96 ms for
fixed preference, 37.48 ms for the existing policy, and 59.39 ms for
Linux default. Three pairs cannot establish an effect with the declared
interval method. The observed stable profile does not support a claim
that path-aware improves on fixed preference. Median bulk goodput was
3.98–4.00 Mbit/s across modes. Each run verified the flower classifier
and recorded at least 3127 packets in the critical class and 3982 in
the default class. The host fingerprint and cleanup checks passed.

Stable raw evidence is in results/dynamic-path-1794242256286979989/.
The summary.json SHA-256 is
0f8096490f2b05cfb5b7cbc3356b02b42499194df295ecb9c2f9d16f5c104846.

The single dynamic intent-file run with seed 97061 exercised the
reliability preference with the final classifier readback and cleanup
checks. It verified one endpoint change, recorded P99 of 600.41 ms
and two deadline misses among 79 issued requests, and preserved the
connection. Both router class counters advanced (1900 and 3645 packets).
The host fingerprint matched. Evidence is in
results/dynamic-path-1794242384382266128/. The summary.json SHA-256 is
a3d59e9013225b19f285c4912bc119a48f881868ba1958a115075e3127007d45.

The original three-path multiple-failure integration passed, including
foreign endpoint ID reuse protection. Its evidence is in
results/multipath-1794242373117958658/. These runs exercise a known
owned bottleneck and a direct bypass in a single VM. Physical links,
uncontrolled Internet paths, and cross-host service coordination were
outside this experiment.
