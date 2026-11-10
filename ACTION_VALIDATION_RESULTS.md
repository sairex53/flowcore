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
