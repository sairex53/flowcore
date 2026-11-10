# Adaptive action validation results

## Audit of earlier no-action campaigns

The mixed-collapse and overload confirmation evidence already
recorded no optimizer actuation. The original mixed matrix retained
43 hold windows across optimizer runs. Of 129 service-window records,
117 had fewer than 100 successful samples and 113 lacked the class
pressure predicate. The overload confirmation retained 41 hold
windows. Of 123 service-window records, 113 had fewer than 100
successes and 111 lacked the class pressure predicate. The categories
overlap. These observations identify concrete admission barriers
in the recorded windows. They do not assign the measured failures
to a controller action.

The audit also found that the native solver could consider a
reservation boost for an otherwise eligible class with a met
application objective. The Python planner now excludes that
candidate. Separately, the old probe could retain an allocation
from a latency improvement without an observed target-class
effect. The retention gate now requires corroborating class evidence.

## Reproduction

~~~sh
cmake --build build
ctest --test-dir build --output-on-failure
sudo build/flowcore bench run scenarios/adaptive-action-paired.json
sudo build/flowcore bench run scenarios/adaptive-action-holdout.json
sudo build/flowcore bench run scenarios/adaptive-action-transport-ablation.json
sudo build/flowcore bench run scenarios/adaptive-action-local-holdout.json
sudo python3 -B tests/optimizer_integration.py
~~~

The paired matrix compares static HTB, the existing threshold
strategy, queue-aware, SLO-aware and the instrumented optimizer.
The same offered requests and class configuration are replayed
within each seed block. Queue-aware versus SLO-aware probes the
effect of the objective signal within the same allocator family.
The distinct holdout changes capacity, request sizes, RTT, jitter
and background load. The transport ablation changes only whether
a bounded diagnostic dump is required for an otherwise justified
owned HTB action. The matrices are exploratory and below the
prespecified ten-pair release gate.

[Engine](ADAPTIVE_ACTION_ENGINE.md) · [Trace schema](DECISION_TRACE.md)

## Five strategies and held-out workload

The paired evidence at results/research-1794307045998214853 contains
two independent seed blocks and ten infrastructure-valid trials.
All arms completed all 4,400 offered requests per arm. None issued
an adaptive action.

| Mode | Conditional P99 (ms) | Deadline attainment | TCP background (Mbit/s) | Daemon and child CPU (s) | Adaptive kernel updates |
| --- | ---: | ---: | ---: | ---: | ---: |
| Static HTB | 48.81 | 99.02% | 5.60 | 2.253 | 0 |
| Threshold | 43.55 | 99.57% | 5.67 | 2.361 | 0 |
| Queue-aware | 45.81 | 99.32% | 5.65 | 2.287 | 0 |
| SLO-aware | 46.22 | 99.36% | 5.63 | 2.234 | 0 |
| Optimizer | 46.43 | 99.32% | 5.68 | 2.296 | 0 |

Values are run medians. TCP background Jain indices were 0.9999
or higher. Median per-window planning time was 0.23 to 0.30 ms
for the non-optimizer controllers and 1.37 ms for the optimizer,
including the native solver bridge. Trace records show eight
truncated-transport holds out of ten windows for each of the three
non-optimizer strategies, with the remaining two lacking an
eligible target-class bottleneck. The optimizer had seven holds
with incomplete inputs, two without class pressure at the
reservation and one without root pressure.

The holdout at results/research-1794307283477695110 had five
infrastructure-valid trials. All arms completed all 2,020 offered
requests per arm, with zero adaptive actions. P99 was 48.77 ms
static, 44.35 threshold, 49.56 queue-aware, 61.19 SLO-aware and
73.22 optimizer. Background goodput ranged from 4.51 to
4.56 Mbit/s. These results retain an unfavorable optimizer outcome.
With identical kernel allocations, the observed P99 differences
cannot establish an adaptation effect. Neither series established
convergence at the configured 18 ms objective.

## Scoped live actuator verification

The Fedora 45 ARM64 fixture used a dedicated 20 Mbit/s veth
bottleneck. One authorized ceiling change reduced the observed
critical wire rate from 19.13 to 8.88 Mbit/s with readback of the
9 Mbit/s ceiling. This tested enforcement of a declared limit.

Four simultaneously backlogged TCP classes then exercised a
separate reservation transfer. The application objective input was
a fixed synthetic 80 ms trace. The traffic counters and writes
were real. HTB changed critical from 6 to 7 Mbit/s and background
from 6 to 5 Mbit/s. Critical wire rate increased from 5.97 to
6.97 Mbit/s, while background fell from 5.97 to 4.98 Mbit/s,
crossing the 15% budget. The recorded class Jain index fell
from 0.9622 to 0.9436. The application fixture had unchanged
histogram P99 and the verdict was harmful. Verified compensation
restored the earlier rates and preserved the authorized ceiling.
The reservation transaction took 33.52 ms across preflight, write
and verification, and the verdict was evaluated 4.17 s after
kernel confirmation. The fixture also verified external capacity
drift refusal and namespace cleanup. Raw evidence is in
results/action-validation/actuator.log.

## Transport coverage ablation

The first two-block ablation at results/research-1794307428444881377
retained one invalid baseline trial. Generator release P99 was
25.79 ms versus the fixed 25 ms infrastructure limit. The remaining
three trials had no failed requests. The strict optimizer issued
no action. The two optimizer-local trials each verified one
600 kbit/s transfer and one also verified restoration.

In the first local probe, class backlog P95 fell from 7,513 to
3,159 bytes while the application histogram P99 remained 42.50 ms.
The verdict was inconclusive and the controller restored the prior
allocation. The second probe recorded backlog P95 falling from
23,171 to 3,159 bytes and application histogram P99 falling from
131.07 to 71.47 ms in its fixed windows. The original controller
retained it as useful under its then-current rules. These are local
pre/post measurements. The invalid matrix and two repetitions
provide insufficient evidence for comparative benefit.

Those probes also exposed a fairness measurement edge case.
Incidental traffic made the list of classes with any bytes change,
so the fairness cohorts were incomparable. The revised guard
requires matching material classes at a 1% share threshold and
uses the explicit deadline-reporting denominator.

The revised two-block retest at results/research-1794307576124270798
also retained one invalid trial. This time r1-optimizer-local
exceeded the release threshold at 39.31 ms. Its raw result,
including the action and compensation, remains in the bundle.
In the infrastructure-valid pair both arms completed all 2,200
requests. The strict arm recorded P99 91.19 ms, 95.09% deadline
attainment and 5.54 Mbit/s background goodput. The local arm
recorded 79.96 ms, 97.77% and 5.63 Mbit/s. These are descriptive
results from one pair in an invalid campaign.

The valid local trial verified one reservation probe and one
rollback. In the fixed action windows, class backlog P95 fell
from 7,513 to 5,312 bytes, while application histogram P99 rose
from 35.73 to 46.34 ms. The controller classified the probe as
harmful under the 10% P99 guard and restored the prior allocation.
The verdict used a comparable material-class cohort and was
evaluated 6.11 s after kernel confirmation. The within-trial
guard and the whole-run paired comparison use different intervals
and answer different questions.

The final isolated actuator repeat, recorded in
results/action-validation/actuator-final.log, independently
verified the ceiling and the harmful transfer. Critical wire
rate rose from 6.04 to 6.98 Mbit/s during the transfer, while
background fell from 6.03 to 4.99 Mbit/s. The allocation change
took 19.09 ms through kernel verification. The verdict took
3.79 s and rollback verified generation 3.

## Final local-action holdout and test status

The independent two-arm holdout at results/research-1794307734249579866
used 9.4 Mbit/s, changed workload phases and fresh seed 1943629949.
Both infrastructure-valid arms completed all 2,020 requests.
Strict optimizer P99 was 55.52 ms with 98.42% deadline attainment.
Optimizer-local P99 was 44.76 ms with 99.75% attainment. Background
goodput was 4.49 and 4.48 Mbit/s respectively. Background Jain was
approximately 0.99995 for both. Daemon and child CPU was 2.225 and
2.276 s. This is one pair.

The local optimizer verified one transfer late in the trial and
the strict optimizer issued zero actions. The local verdict fell
outside the recording window. The offline summary marks it as
inconclusive and right-censored. Neither strategy reached the
configured convergence criterion. The whole-trial P99 difference
does not establish a causal effect or support enabling the local
option by default.

The Fedora ARM64 C++23 build, CTest, staged installation, optimizer
actuator and scoped safety fixtures passed. CTest recorded 54
passes and one privileged UDP skip. Fault evidence covers stale
generations, unavailable telemetry, external capacity drift,
failed updates, ambiguous acknowledgements, compensation, crash
recovery and namespace cleanup. The two invalid ablation matrices
remain available. All five campaign evidence digests verified.
The action engine was exercised with owned HTB. No comparative
socket pacing or MPTCP actuation result is claimed.
