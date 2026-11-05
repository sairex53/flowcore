# Measured FLOWCORE results

Fedora 45 ARM64, 20 Mbit/s veth egress. Three held-out runs per mode, 1,200 offered requests per run. Raw evidence: results/20261104-160357-9732/.

| Mode | Runs | P99 median (ms) | P99 range (ms) | BG median (Mbit/s) | Minimum Jain | CPU busy median (%) | Failed requests |
|---|---:|---:|---:|---:|---:|---:|---:|
| cake | 3 | 20.46 | 17.83–30.05 | 15.41 | 1.000 | 11.9 | 0 |
| fifo | 3 | 431.35 | 431.11–476.00 | 15.72 | 0.999 | 5.9 | 0 |
| flowcore | 3 | 11.84 | 10.75–13.22 | 15.56 | 1.000 | 9.8 | 0 |
| fq_codel | 3 | 12.32 | 12.02–16.38 | 15.57 | 1.000 | 9.8 | 0 |
| static10 | 3 | 4896.23 | 4824.94–4985.79 | 17.17 | 1.000 | 9.5 | 2543 |
| static20 | 3 | 14.82 | 11.70–183.47 | 15.53 | 1.000 | 10.8 | 0 |
| static40 | 3 | 26.19 | 11.44–49.19 | 15.49 | 1.000 | 9.5 | 0 |

Pilot-selected static policy: static20
FLOWCORE P99 reduction versus FIFO: 97.3%
Initial gate (>=20% reduction plus bandwidth/fairness): True
FLOWCORE P99 reduction versus fq_codel: 3.9%
FLOWCORE P99 reduction versus cake: 42.1%
FLOWCORE P99 reduction versus static20: 20.1%
Measured adaptive transitions: 3
Exploratory per-run tail estimates. See raw samples, trial errors and tuning.json.

Static10 P99 describes successful requests only. It failed 2,543 of 3,600
requests and is excluded from tuning and performance success claims.
FLOWCORE met the initial bandwidth and fairness gate in all three runs.
Its 0.48 ms median advantage over fq_codel is small relative to run-to-run
variation. The series does not establish a repeatable advantage over fq_codel
or isolate the gain attributable to adaptation from the gain due to classification.
The next comparison should include a fixed balanced policy with the same
classifier and leaf queues, longer runs, and a predeclared demand-change workload.

Verified: C++23 build and CTest; integration checks for live HTB changes,
stale feedback, hysteresis and rollback; request accounting, source hashes
and cleanup of all namespace holders in the evaluation.
