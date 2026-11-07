# Diagnostic validation

## Protocol

Run in a disposable Linux VM with a current iproute2 that supports JSON class readback. The experiment creates its own namespaces and validates host link and qdisc identity before and after. It refuses to overlap an existing laboratory daemon.

```sh
cmake -S . -B build -G Ninja
cmake --build build
ctest --test-dir build --output-on-failure
sudo python3 -B tests/incident_integration.py
sudo python3 -B tests/multipath_integration.py
sudo python3 -B scripts/diagnostic_experiment.py --repetitions 2 --seed 20261117
sudo python3 -B scripts/diagnostic_experiment.py --case queue --repetitions 2 --seed 20261121
```

Each trial establishes a new veth pair and eight persistent TCP request workers. Each worker offers one 512-byte request per 67 ms slot and reads a 64-byte reply carrying an explicitly measured server processing span. The request deadline is 15 ms. Four consecutive four-second windows cover baseline, fault, intervention and reintroduction. The collector polls at a target of 250 ms and finalizes a completed window after two seconds of grace. Cases are shuffled by seed within each repetition. The two queue repetitions compare different interventions. Timing and traffic are sampled on the same VM with real Linux scheduling.

The known fixture causes are introduced at defined points. The queue fixture uses two paced bulk flows and a 4 Mbit/s HTB shaper, with FIFO test leaves on the critical and background classes. The path fixture applies 22 ms netem delay to receiver egress, after the monitored sender shaper. The application fixture introduces a 21 ms server sleep and reports the actual span. The opaque fixture runs the same application stimulus with timing and socket association withheld and collector gaps declared. Stable runs apply no stimulus. Score the first fault-window report against the intended cause. A top-ranked contextual candidate counts as a correct lead, with its strength shown separately.

## Fedora ARM64 results

The first complete evaluation used Fedora 45 QEMU ARM64, four vCPUs, kernel `7.2.11-300.fc45.aarch64`, tc 7.0.0, with BPF collection off. Source commit `984f0cc` was clean. All ten trials completed. Host link and qdisc identity matched. All ten retained bundles reproduced the live engine report offline.

| Fixture | First candidate or outcome | Correct trials | Fault-window successful P99 | Deadline misses in fault window |
| --- | --- | ---: | ---: | ---: |
| Sender HTB queue | Queue saturation, supported | 2/2 | 339.96–370.73 ms | 147/152, 155/160 |
| Reverse-path netem | Network path degradation, contextual | 2/2 | 30.05–42.50 ms | 472/477, 480/480 |
| Reported server processing | Application processing delay, supported | 2/2 | 27.56–30.05 ms | 473/474, 480/480 |
| Withheld evidence | SLO incident with insufficient evidence for attribution | 2/2 | 32.77–35.73 ms | 480/480, 472/472 |
| Stable control | No incident | 2/2 | 7.51–10.62 ms | 0/473, 1/480 |

The expected candidate ranked first in 6/6 attributable trials, with zero incorrect top candidates in these ten trials. Eight fault windows with SLO events, including the opaque fixture, reported symptoms 6.001–6.033 s after application. This reflects the window length and grace period for boundary-aligned faults. It is not a general detector latency bound. Independent repetitions are limited to two per fixture. This study provides no population accuracy estimate.

Removing netem reduced P99 to 5.79–10.62 ms with zero deadline misses. Reapplying it brought P99 to 27.56–38.97 ms. Removing the application sleep reduced P99 to 6.32–6.89 ms with zero deadline misses; reapplying it brought P99 to 30.05–32.77 ms. These within-trial reversals support the known manipulated mechanisms in those fixtures. They are not field effect sizes.

In the first queue trials, all eight request workers stopped after their transport timeout. One trial recorded eight transport timeouts during the fault window and the other eight during the intervention window. There were 216 and 315 observed skipped slots respectively, plus unoffered slots after worker exit. Those trials demonstrate attribution under severe congestion. Their intervention and restoration windows are insufficient for measuring application recovery.

### Runtime cost in the first series

Across the ten trials, the median CPU cost per call for retaining request evidence was 5.4–7.3 µs. The p99 of that per-call cost ranged from 156 to 259 µs across trials. Existing application histogram ingest measured a median 11.2–14.3 µs per call. Recording a network sample and checking the window boundary together took a median 50–78 µs per call. Their p99 ranged from 1.31 to 4.19 ms and includes periodic window aggregation. Measurements use the sampler thread CPU clock. They exclude INET_DIAG, TC queries, IPC, kernel work, host PSI polling and artifact serialization. They do not measure whole-daemon overhead.

### Retained first-series evidence

- `results/causal-1794035924737673899/`
- `summary.json` SHA-256 `8af4642eecc85cd25a15072c8325174aa1670f78f2c4f2a550f39f978c3bcc27`
- Each trial retains application request outcomes, a raw event stream, phase reports, kernel readbacks and exported bundles under `incidents/`. Raw files remain local under the repository results directory.

### Queue interventions after reconnect support

The first series exposed a probe limitation. The runner now records transport failure, closes the unusable stream and attempts a fresh connection at the next eligible slot. It retains attempts, skips and reconnect attempts. Reconnection duration is part of the next request measurement. The two targeted reruns used commit `dc0a74b` and seed `20261121`. The worktree had untracked documentation, and the experiment source was committed. Host identity matched after cleanup.

| Intervention | Deadline misses during fault | Deadline misses during intervention | Deadline misses after reintroduction | Successful P99, fault → intervention |
| --- | ---: | ---: | ---: | ---: |
| Pause both competing bulk flows at 4 Mbit/s | 173/174 | 36/424 | 135/144 | 370.73 → 370.73 ms |
| Raise verified HTB capacity from 4 to 12 Mbit/s under bulk load | 155/160 | 33/448 | 150/152 | 311.74 → 404.28 ms |

Both trials ranked queue saturation first. The request workers continued through intervention and reintroduction. Deadline misses fell and completed request volume increased in both intervention windows. Whole-window successful-request P99 stayed high in the load intervention and rose in the rate intervention. These windows include the transition and any requests already in flight. The rate run also recorded eight intervention failures. Neither result establishes a steady-state P99 improvement. There was one run of each intervention, which leaves substantial uncertainty about effect size.

The targeted rerun is retained at `results/causal-1794036238726011734/`. Its `summary.json` SHA-256 is `2d981e2b77e3f01b9e42cdad94c75915d2ed8274b8877fbf3c16eaf33b96a7d3`.

### Scope of validation

The live daemon test rejects intervention outside an owned lab and checks freeze, restoration, resource evidence when PSI is available, incident export and replay with the daemon stopped. The unit suite checks the daemon UID rule and untrusted replay inputs. The MPTCP regression verifies schema 1 path transitions against kernel-confirmed timestamps in a three-path trial. Receive-window and controller regression signals have deterministic tests or existing underlying telemetry coverage. The study does not measure their live attribution accuracy, cross-host clock alignment or operational incident false-positive rates.
